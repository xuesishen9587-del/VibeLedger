import json
from typing import Optional, Dict, Any, List, Tuple, Set
from uuid import UUID, uuid4
from datetime import datetime, date, timezone
from decimal import Decimal

from app.domain.money import parse_decimal, quantize_money, validate_currency_code
from app.domain.reconciliation.models import (
    NormalizedStatementLine,
    CandidateProposal,
    ReconciliationResult,
    AUTO_ADJUST_THRESHOLD_CNY,
    RECONCILIATION_RESIDUAL_TOO_LARGE
)
from app.domain.reconciliation.normalizer import normalize_description
from app.domain.reconciliation.engine import run_deterministic_reconciliation
from app.domain.reconciliation.scoring import compute_match_score, validate_target_match_compatibility
from app.domain.reconciliation.residuals import evaluate_residual_and_batch_readiness
from app.services.reference_fx_service import ReferenceFxService
from app.services.snapshot_service import ledger_balance_as_of

import app.repositories.accounts as accounts_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo
import app.repositories.installments as installments_repo
import app.repositories.credit_cards as credit_cards_repo
import app.repositories.audit as audit_repo


def create_statement_reconciliation_batch(
    conn,
    household_id: UUID,
    account_id: UUID,
    lines: List[NormalizedStatementLine],
    authoritative_balance: Optional[Decimal] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    statement_balance: Optional[Decimal] = None,
    current_outstanding: Optional[Decimal] = None,
    unbilled_balance: Optional[Decimal] = None,
    user_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    default_expense_category_id: Optional[UUID] = None,
    default_income_category_id: Optional[UUID] = None,
    household_movements: Optional[List[Dict[str, Any]]] = None,
    fx_service: Optional[ReferenceFxService] = None,
    credit_card_snapshot_payload: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:

    """
    Creates and processes a Statement reconciliation batch from normalized statement lines.
    Does NOT mutate committed transactions or account_state until batch commit.
    """
    account = accounts_repo.get_account(conn, account_id)
    if not account:
        raise ValueError(f"Account {account_id} not found")
    if account["household_id"] != household_id:
        raise ValueError("Account does not belong to household")

    account_curr = account["currency"]
    auth_balance = quantize_money(authoritative_balance, account_curr) if authoritative_balance is not None else None
    is_credit = (account["account_type"] == "credit")

    # Validate default categories belong to household, are active, and have correct category_type
    valid_exp_cat_id: Optional[UUID] = None
    if default_expense_category_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, household_id, category_type, status FROM categories WHERE id = %s;",
                (default_expense_category_id,)
            )
            c_row = cur.fetchone()
            if c_row and c_row[1] == household_id and c_row[2] == "expense" and c_row[3] == "active":
                valid_exp_cat_id = default_expense_category_id

    valid_inc_cat_id: Optional[UUID] = None
    if default_income_category_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, household_id, category_type, status FROM categories WHERE id = %s;",
                (default_income_category_id,)
            )
            c_row = cur.fetchone()
            if c_row and c_row[1] == household_id and c_row[2] == "income" and c_row[3] == "active":
                valid_inc_cat_id = default_income_category_id


    # Determine baseline effective date for historical projection (order-independent max effective date)
    if lines:
        valid_dates = [l.effective_date for l in lines if l.effective_date is not None]
        as_of_date = period_end or (max(valid_dates) if valid_dates else date.today())
        min_date = min(valid_dates) if valid_dates else as_of_date
        max_date = max(valid_dates) if valid_dates else as_of_date
    else:
        as_of_date = period_end or date.today()
        min_date = period_start or as_of_date
        max_date = as_of_date

    as_of_dt = datetime.combine(as_of_date, datetime.max.time(), tzinfo=timezone.utc)

    # 1. Historical ledger balance as-of statement end date
    baseline_projected_balance = ledger_balance_as_of(conn, account_id, as_of_dt)

    # 2. Fetch existing committed transactions around period
    from datetime import timedelta
    tx_from_date = min_date - timedelta(days=10)
    tx_to_date = max_date + timedelta(days=10)

    committed_txs, _ = tx_repo.list_transactions_with_filters(
        conn=conn,
        household_id=household_id,
        account_id=account_id,
        from_date=tx_from_date,
        to_date=tx_to_date,
        limit=200
    )

    # 3. Fetch installment plans if credit account
    inst_plans = []
    inst_periods: Dict[UUID, List[Dict[str, Any]]] = {}
    if is_credit:
        all_plans = installments_repo.list_installment_plans(conn, household_id)
        inst_plans = [p for p in all_plans if p["credit_account_id"] == account_id and p["status"] in ("pending_first_bill", "active")]
        for p in inst_plans:
            inst_periods[p["id"]] = installments_repo.list_periods_for_plan(conn, p["id"])

    # 4. Fetch candidate refund expenses (180 days lookback)
    refund_from_date = min_date - timedelta(days=185)
    refund_txs, _ = tx_repo.list_transactions_with_filters(
        conn=conn,
        household_id=household_id,
        account_id=account_id,
        transaction_type="expense",
        from_date=refund_from_date,
        to_date=max_date,
        limit=200
    )
    existing_refund_totals: Dict[UUID, Decimal] = {}
    for exp in refund_txs:
        active_refunds = tx_repo.get_active_refunds_for_expense(conn, exp["id"])
        tot = sum((parse_decimal(r.get("from_amount") or r.get("to_amount") or r.get("original_amount")) for r in active_refunds), Decimal("0.00"))
        existing_refund_totals[exp["id"]] = tot

    # 5. Household accounts
    hh_accounts = accounts_repo.list_accounts(conn, household_id)
    hh_movements = household_movements if household_movements is not None else []


    # FX rate to CNY
    fx_rate_cny = Decimal("1.00")
    if account_curr != "CNY":
        if fx_service is None:
            fx_service = ReferenceFxService()
        fx_rate_cny = fx_service.get_rate(account_curr, "CNY", as_of=as_of_date)

    # 6. Execute deterministic reconciliation engine
    result = run_deterministic_reconciliation(
        lines=lines,
        transactions=committed_txs,
        selected_account_id=account_id,
        account_currency=account_curr,
        baseline_projected_balance=baseline_projected_balance,
        authoritative_balance=auth_balance,
        is_credit_account=is_credit,
        installment_plans=inst_plans,
        installment_periods=inst_periods,
        candidate_refund_expenses=refund_txs,
        existing_refund_totals=existing_refund_totals,
        household_accounts=hh_accounts,
        household_movements=hh_movements,
        default_expense_category_id=valid_exp_cat_id,
        default_income_category_id=valid_inc_cat_id,
        fx_rate_to_cny=fx_rate_cny
    )


    # 7. Persist reconciliation batch
    batch_id = uuid4()
    batch_record = reconciliation_repo.create_reconciliation_batch(
        conn=conn,
        batch_id=batch_id,
        household_id=household_id,
        account_id=account_id,
        batch_type="statement",
        status=result.batch_status,
        currency=account_curr,
        authoritative_balance=auth_balance,
        statement_balance=statement_balance,
        current_outstanding=current_outstanding,
        unbilled_balance=unbilled_balance,
        residual_amount=result.residual_amount,
        adjustment_amount=result.adjustment_amount,
        period_start=period_start,
        period_end=period_end,
        source_request_id=source_request_id,
        created_by_user_id=user_id
    )

    # 8. Persist statement lines
    line_id_map = {}
    for line in lines:
        db_line = reconciliation_repo.create_statement_line(
            conn=conn,
            line_id=line.id,
            batch_id=batch_id,
            description_raw=line.description_raw,
            amount=line.settlement_amount,
            currency=line.settlement_currency,
            direction=line.direction,
            line_type=line.line_type,
            description_normalized=line.description_normalized,
            transaction_on=line.transaction_on,
            posted_on=line.posted_on,
            match_status="unmatched",
            confidence=line.confidence,
            source_page_no=line.source_page_no,
            source_row_no=line.source_row_no,
            line_fingerprint=line.line_fingerprint
        )
        line_id_map[line.id] = db_line

    # 9. Persist reconciliation candidates
    line_by_id = {l.id: l for l in lines}
    persisted_candidates = []
    for cand in result.candidates:
        if cand.statement_line_id and cand.statement_line_id in line_by_id:
            src_line = line_by_id[cand.statement_line_id]
            if "evidence" not in cand.payload:
                cand.payload["evidence"] = {}
            if "line" not in cand.payload["evidence"]:
                cand.payload["evidence"]["line"] = {}
            cand.payload["evidence"]["line"].update({
                "original_amount": f"{parse_decimal(src_line.original_amount):.2f}" if src_line.original_amount is not None else None,
                "original_currency": src_line.original_currency,
                "merchant_hint": src_line.merchant_hint,
                "external_reference": src_line.external_reference
            })

        db_cand = reconciliation_repo.create_reconciliation_candidate(
            conn=conn,
            candidate_id=cand.id,
            batch_id=batch_id,
            statement_line_id=cand.statement_line_id,
            candidate_type=cand.candidate_type,
            status=cand.status,
            target_transaction_id=cand.target_transaction_id,
            payload=cand.payload,
            confidence=cand.confidence,
            reason_code=cand.reason_code,
            reason_detail=cand.reason_detail
        )
        persisted_candidates.append(db_cand)

        # Update statement line match_status if applicable
        if cand.statement_line_id:
            if cand.candidate_type == "match":
                m_status = "matched" if cand.status == "accepted" else "ambiguous"
                reconciliation_repo.update_statement_line_status(
                    conn=conn,
                    line_id=cand.statement_line_id,
                    match_status=m_status,
                    matched_transaction_id=cand.target_transaction_id if cand.status == "accepted" else None
                )
            elif cand.candidate_type in ("create_transaction", "create_transfer", "refund", "recognize_installment"):
                reconciliation_repo.update_statement_line_status(
                    conn=conn,
                    line_id=cand.statement_line_id,
                    match_status="new_candidate"
                )

    if credit_card_snapshot_payload:
        cc_cand_id = uuid4()
        db_cand = reconciliation_repo.create_reconciliation_candidate(
            conn=conn,
            candidate_id=cc_cand_id,
            batch_id=batch_id,
            statement_line_id=None,
            candidate_type="snapshot",
            status="accepted",
            payload=credit_card_snapshot_payload,
            confidence=Decimal("1.00"),
            reason_code=None,
            reason_detail="Authoritative credit card statement snapshot metadata"
        )
        persisted_candidates.append(db_cand)

    # 10. Update batch summary statistics
    reconciliation_repo.update_reconciliation_batch_stats(
        conn=conn,
        batch_id=batch_id,
        status=result.batch_status,
        matched_count=result.matched_count,
        created_count=result.created_count,
        pending_count=result.pending_count,
        residual_amount=result.residual_amount,
        adjustment_amount=result.adjustment_amount,
        engine_version=result.engine_version
    )

    return {
        "batch_id": str(batch_id),
        "status": result.batch_status,
        "matched_count": result.matched_count,
        "created_count": result.created_count,
        "pending_count": result.pending_count,
        "residual_amount": str(result.residual_amount) if result.residual_amount is not None else None,
        "adjustment_amount": str(result.adjustment_amount) if result.adjustment_amount else None,
        "candidates": persisted_candidates
    }


def commit_statement_batch(
    conn,
    batch_id: UUID,
    user_id: Optional[UUID] = None,
    device_id: Optional[UUID] = None,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Atomically commits an accepted/reviewed Statement reconciliation batch:
    - Locks batch FOR UPDATE
    - Replay safety: if already committed, returns idempotent committed response
    - Locks all affected accounts in sorted UUID order
    - Re-reads current committed ledger state
    - Revalidates accepted candidates against current ledger (handles concurrent shortcut transactions!)
    - Executes financial writes (matches, new transactions, transfers, refunds, installment periods, adjustments)
    - Updates account_state ledger balances
    - Updates statement_lines match_status
    - Marks candidates applied
    - Marks batch committed
    - Appends audit events
    """
    # 1. Lock batch row FOR UPDATE
    batch = reconciliation_repo.lock_reconciliation_batch(conn, batch_id)
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")

    # Replay safety: if already committed, return idempotent response
    if batch["status"] == "committed":
        candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
        applied_tx_ids = [str(c["applied_transaction_id"]) for c in candidates if c.get("applied_transaction_id")]
        return {
            "status": "committed",
            "batch_id": str(batch_id),
            "residual_amount": str(batch.get("residual_amount") or "0.00"),
            "matched_count": batch.get("matched_count", 0),
            "created_count": batch.get("created_count", 0),
            "applied_transaction_ids": applied_tx_ids,
            "message": "Batch was already committed (idempotent replay)"
        }

    # Strict committability check: only ready batches without any unresolved needs_review candidates can commit
    if batch["status"] != "ready":
        raise ValueError(f"Batch status {batch['status']} is not committable; batch must be in ready status")

    if batch["batch_type"] != "statement":
        raise ValueError(f"Expected statement batch type, got {batch['batch_type']}")

    candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
    unresolved_cands = [c for c in candidates if c["status"] in ("needs_review", "proposed")]
    if unresolved_cands:
        raise ValueError(f"Cannot commit batch {batch_id}: {len(unresolved_cands)} candidate(s) are in needs_review status; manual resolution required")

    household_id = batch["household_id"]
    primary_account_id = batch["account_id"]
    curr = batch["currency"]

    household = accounts_repo.get_household(conn, household_id)
    reporting_currency = household["reporting_currency"] if household else "CNY"

    statement_lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
    line_map = {l["id"]: l for l in statement_lines}

    # 2. Determine all affected account IDs and lock them in deterministic sorted UUID order
    affected_account_ids: Set[UUID] = {primary_account_id}
    for c in candidates:
        if c["status"] in ("accepted", "applied"):
            if c["candidate_type"] == "create_transfer":
                tx_data = c["payload"].get("transfer", {})
                if tx_data.get("from_account_id"):
                    affected_account_ids.add(UUID(tx_data["from_account_id"]))
                if tx_data.get("to_account_id"):
                    affected_account_ids.add(UUID(tx_data["to_account_id"]))

    account_states = accounts_repo.lock_account_states(conn, list(affected_account_ids))

    # 3. Reconstruct NormalizedStatementLines using candidate evidence where available
    line_evidence_map: Dict[UUID, Dict[str, Any]] = {}
    for sl in statement_lines:
        line_ev: Dict[str, Any] = {}
        if sl.get("original_amount") is not None:
            line_ev["original_amount"] = f"{parse_decimal(sl['original_amount']):.2f}"
        if sl.get("original_currency") is not None:
            line_ev["original_currency"] = sl["original_currency"]
        if sl.get("merchant_hint") is not None:
            line_ev["merchant_hint"] = sl["merchant_hint"]
        if sl.get("external_reference") is not None:
            line_ev["external_reference"] = sl["external_reference"]
        if line_ev:
            line_evidence_map[sl["id"]] = line_ev

    for c in candidates:
        sl_id = c.get("statement_line_id")
        if sl_id:
            ev_line = c.get("payload", {}).get("evidence", {}).get("line", {})
            if ev_line:
                if sl_id not in line_evidence_map:
                    line_evidence_map[sl_id] = {}
                ev_copy = dict(ev_line)
                if ev_copy.get("original_amount") is not None:
                    ev_copy["original_amount"] = f"{parse_decimal(ev_copy['original_amount']):.2f}"
                line_evidence_map[sl_id].update(ev_copy)

    def merge_canonical_line_evidence(
        payload: Optional[Dict[str, Any]],
        statement_line_id: Optional[UUID]
    ) -> Dict[str, Any]:
        merged_payload = dict(payload) if payload else {}
        if not statement_line_id or statement_line_id not in line_evidence_map:
            return merged_payload
        canonical_line_ev = line_evidence_map[statement_line_id]
        if not canonical_line_ev:
            return merged_payload
        if "evidence" not in merged_payload:
            merged_payload["evidence"] = {}
        else:
            merged_payload["evidence"] = dict(merged_payload["evidence"])
        if "line" not in merged_payload["evidence"]:
            merged_payload["evidence"]["line"] = {}
        else:
            merged_payload["evidence"]["line"] = dict(merged_payload["evidence"]["line"])
        for k, v in canonical_line_ev.items():
            if v is not None:
                if k not in merged_payload["evidence"]["line"] or merged_payload["evidence"]["line"][k] is None:
                    merged_payload["evidence"]["line"][k] = v
                elif k in ("original_amount", "original_currency", "merchant_hint", "external_reference"):
                    if not merged_payload["evidence"]["line"][k]:
                        merged_payload["evidence"]["line"][k] = v
        return merged_payload

    norm_lines: List[NormalizedStatementLine] = []
    for sl in statement_lines:
        line_no = sl.get("source_row_no") or 0
        amt = sl.get("amount") or Decimal("0.00")
        line_curr = sl.get("currency") or batch["currency"]
        ev = line_evidence_map.get(sl["id"], {})
        orig_amt_val = ev.get("original_amount") or sl.get("original_amount")
        orig_curr_val = ev.get("original_currency") or sl.get("original_currency")
        merchant_hint_val = ev.get("merchant_hint") or sl.get("merchant_hint")
        ext_ref_val = ev.get("external_reference") or sl.get("external_reference")

        norm_lines.append(NormalizedStatementLine(
            id=sl["id"],
            description_raw=sl["description_raw"],
            direction=sl["direction"],
            line_type=sl.get("line_type", "unknown"),
            settlement_amount=parse_decimal(amt),
            settlement_currency=line_curr,
            transaction_on=sl.get("transaction_on"),
            posted_on=sl.get("posted_on"),
            description_normalized=sl.get("description_normalized") or sl["description_raw"],
            original_amount=parse_decimal(orig_amt_val) if orig_amt_val is not None else None,
            original_currency=orig_curr_val,
            merchant_hint=merchant_hint_val,
            external_reference=ext_ref_val,
            source_page_no=sl.get("source_page_no"),
            source_row_no=line_no,
            line_fingerprint=sl.get("line_fingerprint")
        ))

    if norm_lines:
        valid_dates = [l.effective_date for l in norm_lines if l.effective_date is not None]
        as_of_date = batch.get("period_end") or (max(valid_dates) if valid_dates else date.today())
        min_date = min(valid_dates) if valid_dates else as_of_date
        max_date = max(valid_dates) if valid_dates else as_of_date
    else:
        as_of_date = batch.get("period_end") or date.today()
        min_date = batch.get("period_start") or as_of_date
        max_date = as_of_date

    as_of_dt = datetime.combine(as_of_date, datetime.max.time(), tzinfo=timezone.utc)
    fresh_baseline = ledger_balance_as_of(conn, primary_account_id, as_of_dt)

    from datetime import timedelta
    tx_from_date = min_date - timedelta(days=10)
    tx_to_date = max_date + timedelta(days=10)

    fresh_txs, _ = tx_repo.list_transactions_with_filters(
        conn=conn,
        household_id=household_id,
        account_id=primary_account_id,
        from_date=tx_from_date,
        to_date=tx_to_date,
        limit=200
    )

    primary_acc_info = accounts_repo.get_account(conn, primary_account_id)
    is_credit = (primary_acc_info["account_type"] == "credit") if primary_acc_info else False

    inst_plans = []
    inst_periods: Dict[UUID, List[Dict[str, Any]]] = {}
    if is_credit:
        all_plans = installments_repo.list_installment_plans(conn, household_id)
        inst_plans = [p for p in all_plans if p["credit_account_id"] == primary_account_id and p["status"] in ("pending_first_bill", "active")]
        for p in inst_plans:
            inst_periods[p["id"]] = installments_repo.list_periods_for_plan(conn, p["id"])

    refund_from_date = min_date - timedelta(days=185)
    refund_txs, _ = tx_repo.list_transactions_with_filters(
        conn=conn,
        household_id=household_id,
        account_id=primary_account_id,
        transaction_type="expense",
        from_date=refund_from_date,
        to_date=max_date,
        limit=200
    )
    existing_refund_totals: Dict[UUID, Decimal] = {}
    for exp in refund_txs:
        active_refunds = tx_repo.get_active_refunds_for_expense(conn, exp["id"])
        tot = sum((parse_decimal(r.get("from_amount") or r.get("to_amount") or r.get("original_amount")) for r in active_refunds), Decimal("0.00"))
        existing_refund_totals[exp["id"]] = tot

    hh_accounts = accounts_repo.list_accounts(conn, household_id)
    hh_movements: List[Dict[str, Any]] = []
    for c in candidates:
        if c.get("candidate_type") == "create_transfer" and c.get("status") in ("accepted", "applied"):
            t_data = c.get("payload", {}).get("transfer", {})
            from_acc_str = t_data.get("from_account_id")
            to_acc_str = t_data.get("to_account_id")
            if not from_acc_str or not to_acc_str:
                continue

            from_acc_id = UUID(from_acc_str)
            to_acc_id = UUID(to_acc_str)
            occ_on_val = date.fromisoformat(t_data["occurred_on"]) if t_data.get("occurred_on") else as_of_date

            if primary_account_id == from_acc_id:
                # Outbound transfer from selected account: counter leg is to_account (credit)
                hh_movements.append({
                    "account_id": to_acc_id,
                    "direction": "credit",
                    "amount": parse_decimal(t_data.get("to_amount")),
                    "currency": t_data.get("to_currency"),
                    "occurred_on": occ_on_val,
                    "is_counter_statement_leg": True
                })
            elif primary_account_id == to_acc_id:
                # Inbound transfer to selected account: counter leg is from_account (debit)
                hh_movements.append({
                    "account_id": from_acc_id,
                    "direction": "debit",
                    "amount": parse_decimal(t_data.get("from_amount")),
                    "currency": t_data.get("from_currency"),
                    "occurred_on": occ_on_val,
                    "is_counter_statement_leg": True
                })

    # Extract default categories from accepted candidates in preview and revalidate against DB
    batch_exp_cat_id: Optional[UUID] = None
    batch_inc_cat_id: Optional[UUID] = None
    for c in candidates:
        p = c.get("payload") or {}
        if "transaction" in p:
            cat_str = p["transaction"].get("category_id")
            if cat_str:
                if p["transaction"].get("transaction_type") in ("income", "cash_income"):
                    batch_inc_cat_id = UUID(cat_str)
                else:
                    batch_exp_cat_id = UUID(cat_str)
        elif "installment" in p:
            cat_str = p["installment"].get("category_id")
            if cat_str:
                batch_exp_cat_id = UUID(cat_str)

    # Revalidate categories against current DB
    valid_batch_exp_cat_id: Optional[UUID] = None
    if batch_exp_cat_id:
        exp_cat = accounts_repo.get_category(conn, batch_exp_cat_id)
        if exp_cat and exp_cat["household_id"] == household_id and exp_cat["category_type"] == "expense" and exp_cat["status"] == "active":
            valid_batch_exp_cat_id = batch_exp_cat_id

    valid_batch_inc_cat_id: Optional[UUID] = None
    if batch_inc_cat_id:
        inc_cat = accounts_repo.get_category(conn, batch_inc_cat_id)
        if inc_cat and inc_cat["household_id"] == household_id and inc_cat["category_type"] == "income" and inc_cat["status"] == "active":
            valid_batch_inc_cat_id = batch_inc_cat_id

    # Use injected fx_service or fallback to ReferenceFxService
    fx_srv = fx_service or ReferenceFxService()
    fx_rate_cny = Decimal("1.00")
    if curr != "CNY":
        fx_rate_cny = fx_srv.get_rate(curr, "CNY", as_of=as_of_date)

    auth_balance = quantize_money(batch["authoritative_balance"], curr) if batch.get("authoritative_balance") is not None else None

    # Re-run full deterministic reconciliation engine under lock
    fresh_result = run_deterministic_reconciliation(
        lines=norm_lines,
        transactions=fresh_txs,
        selected_account_id=primary_account_id,
        account_currency=curr,
        baseline_projected_balance=fresh_baseline,
        authoritative_balance=auth_balance,
        is_credit_account=is_credit,
        installment_plans=inst_plans,
        installment_periods=inst_periods,
        candidate_refund_expenses=refund_txs,
        existing_refund_totals=existing_refund_totals,
        household_accounts=hh_accounts,
        household_movements=hh_movements,
        default_expense_category_id=valid_batch_exp_cat_id,
        default_income_category_id=valid_batch_inc_cat_id,
        fx_rate_to_cny=fx_rate_cny
    )

    old_cand_by_line = {c["statement_line_id"]: c for c in candidates if c.get("statement_line_id")}
    old_adj_cand = next((c for c in candidates if c["candidate_type"] == "adjustment"), None)
    old_snapshot_cand = next((c for c in candidates if c["candidate_type"] == "snapshot"), None)

    if old_snapshot_cand:
        fresh_result.candidates.append(CandidateProposal(
            id=old_snapshot_cand["id"],
            candidate_type="snapshot",
            status=old_snapshot_cand["status"],
            statement_line_id=None,
            payload=old_snapshot_cand["payload"],
            confidence=Decimal("1.00")
        ))

    # Incorporate user-reviewed candidate statuses and patched payloads
    for fc in fresh_result.candidates:
        if fc.statement_line_id:
            old_c = old_cand_by_line.get(fc.statement_line_id)
            if old_c:
                if old_c.get("status") == "rejected":
                    fc.status = "rejected"
                elif old_c.get("resolved_at") is not None:
                    # Candidate was explicitly reviewed/resolved by user
                    if old_c.get("status") == "accepted":
                        if old_c.get("candidate_type") == "match" and old_c.get("target_transaction_id"):
                            # Check if explicit target transaction is still committed, active, and passes deterministic compatibility
                            t_tx = tx_repo.get_transaction(conn, old_c["target_transaction_id"])
                            st_line_obj = next((l for l in norm_lines if l.id == fc.statement_line_id), None)
                            if t_tx and st_line_obj:
                                try:
                                    validate_target_match_compatibility(st_line_obj, t_tx, primary_account_id)
                                    fc.candidate_type = "match"
                                    fc.target_transaction_id = old_c["target_transaction_id"]
                                    fc.status = "accepted"
                                except Exception as ce:
                                    fc.status = "needs_review"
                                    fc.reason_code = getattr(ce, "code", "TARGET_TRANSACTION_INCOMPATIBLE")
                                    fc.reason_detail = str(ce)
                            else:
                                fc.status = "needs_review"
                                fc.reason_code = "TARGET_TRANSACTION_INVALID"
                                fc.reason_detail = "Target transaction was modified, deleted, or is no longer active."
                        else:
                            fc.status = "accepted"
                if isinstance(old_c.get("payload"), dict):
                    for k, v in old_c["payload"].items():
                        if k not in fc.payload or fc.payload[k] is None:
                            fc.payload[k] = v
                        elif isinstance(v, dict) and isinstance(fc.payload.get(k), dict):
                            fc.payload[k].update(v)

    # Re-evaluate residual and batch status with reviewed candidate states
    active_cands = [c for c in fresh_result.candidates if c.candidate_type not in ("adjustment", "snapshot")]
    b_status, fresh_residual, fresh_adj = evaluate_residual_and_batch_readiness(
        baseline_projected_balance=fresh_baseline,
        authoritative_balance=auth_balance,
        candidates=active_cands,
        account_id=primary_account_id,
        account_currency=curr,
        fx_rate_to_cny=fx_rate_cny
    )
    fresh_result.batch_status = b_status
    fresh_result.residual_amount = fresh_residual
    fresh_result.matched_count = sum(1 for c in active_cands if c.candidate_type == "match" and c.status == "accepted")
    fresh_result.created_count = sum(1 for c in active_cands if c.candidate_type in ("create_transaction", "create_transfer", "refund", "recognize_installment") and c.status == "accepted")
    fresh_result.pending_count = sum(1 for c in active_cands if c.status == "needs_review")
    if fresh_adj and fresh_adj.status == "accepted":
        fresh_result.adjustment_amount = parse_decimal(fresh_adj.payload["adjustment_amount"])
    else:
        fresh_result.adjustment_amount = None

    # If fresh re-evaluation produces needs_review: persist workflow state and return needs_review result
    if fresh_result.batch_status == "needs_review":
        for fc in fresh_result.candidates:
            if fc.statement_line_id:
                old_c = old_cand_by_line.get(fc.statement_line_id)
                persisted_payload = merge_canonical_line_evidence(fc.payload, fc.statement_line_id)
                if old_c:
                    reconciliation_repo.update_reconciliation_candidate_full(
                        conn=conn,
                        candidate_id=old_c["id"],
                        candidate_type=fc.candidate_type,
                        status=fc.status,
                        payload=persisted_payload,
                        target_transaction_id=fc.target_transaction_id,
                        confidence=fc.confidence,
                        reason_code=fc.reason_code,
                        reason_detail=fc.reason_detail
                    )
                else:
                    reconciliation_repo.create_reconciliation_candidate(
                        conn=conn,
                        candidate_id=fc.id,
                        batch_id=batch_id,
                        statement_line_id=fc.statement_line_id,
                        candidate_type=fc.candidate_type,
                        status=fc.status,
                        target_transaction_id=fc.target_transaction_id,
                        payload=persisted_payload,
                        confidence=fc.confidence,
                        reason_code=fc.reason_code,
                        reason_detail=fc.reason_detail
                    )
                m_status = "ambiguous" if fc.status == "needs_review" else ("matched" if fc.candidate_type == "match" and fc.status == "accepted" else "new_candidate")
                reconciliation_repo.update_statement_line_status(
                    conn=conn,
                    line_id=fc.statement_line_id,
                    match_status=m_status,
                    matched_transaction_id=fc.target_transaction_id if fc.status == "accepted" else None
                )
            elif fc.candidate_type == "adjustment":
                if old_adj_cand:
                    reconciliation_repo.update_reconciliation_candidate_full(
                        conn=conn,
                        candidate_id=old_adj_cand["id"],
                        candidate_type="adjustment",
                        status=fc.status,
                        payload=fc.payload,
                        confidence=fc.confidence,
                        reason_code=fc.reason_code,
                        reason_detail=fc.reason_detail
                    )
                else:
                    reconciliation_repo.create_reconciliation_candidate(
                        conn=conn,
                        candidate_id=fc.id,
                        batch_id=batch_id,
                        statement_line_id=None,
                        candidate_type="adjustment",
                        status=fc.status,
                        payload=fc.payload,
                        confidence=fc.confidence,
                        reason_code=fc.reason_code,
                        reason_detail=fc.reason_detail
                    )

        reconciliation_repo.update_reconciliation_batch_stats(
            conn=conn,
            batch_id=batch_id,
            status="needs_review",
            matched_count=fresh_result.matched_count,
            created_count=fresh_result.created_count,
            pending_count=fresh_result.pending_count,
            residual_amount=fresh_result.residual_amount,
            adjustment_amount=fresh_result.adjustment_amount,
            engine_version=fresh_result.engine_version
        )

        return {
            "status": "needs_review",
            "batch_id": str(batch_id),
            "residual_amount": str(fresh_result.residual_amount),
            "adjustment_amount": str(fresh_result.adjustment_amount) if fresh_result.adjustment_amount else None,
            "matched_count": fresh_result.matched_count,
            "created_count": fresh_result.created_count,
            "pending_count": fresh_result.pending_count,
            "applied_transaction_ids": [],
            "message": f"Fresh ledger state requires review: {fresh_result.candidates[0].reason_code if fresh_result.candidates else 'NEEDS_REVIEW'}"
        }

    # 4. Commit is driven strictly by fresh_result.candidates
    applied_tx_ids: List[UUID] = []
    account_deltas: Dict[UUID, Decimal] = {acc_id: Decimal("0.00") for acc_id in affected_account_ids}
    committed_adjustment_amount: Optional[Decimal] = None

    for fresh_cand in fresh_result.candidates:
        if fresh_cand.status != "accepted":
            continue

        c_type = fresh_cand.candidate_type
        stmt_line_id = fresh_cand.statement_line_id
        stmt_line = line_map.get(stmt_line_id) if stmt_line_id else None
        old_cand = old_cand_by_line.get(stmt_line_id) if stmt_line_id else None

        # Case A: Existing Match
        if c_type == "match":
            target_tx_id = fresh_cand.target_transaction_id
            if target_tx_id:
                target_tx = tx_repo.lock_transaction(conn, target_tx_id)
                if not target_tx or target_tx["household_id"] != household_id or target_tx["status"] != "committed" or target_tx.get("deleted_at") is not None:
                    raise ValueError(f"Target transaction {target_tx_id} is no longer valid for reconciliation commit.")

                is_estimated = (target_tx.get("account_leg_status") == "estimated")
                sp = fresh_cand.payload.get("settlement_patch") or fresh_cand.payload.get("evidence", {}).get("settlement_patch") or {}

                posted_on = date.fromisoformat(sp["posted_on"]) if sp.get("posted_on") else (stmt_line.get("posted_on") if stmt_line else None)
                household_row = accounts_repo.get_household(conn, household_id)
                rep_curr_target = household_row["reporting_currency"] if household_row else "CNY"

                if is_estimated and sp:
                    actual_settlement_amt = parse_decimal(sp.get("settlement_amount") or sp.get("actual_settlement_amount") or (stmt_line.get("amount") if stmt_line else "0"))
                    settlement_curr = sp.get("settlement_currency") or (stmt_line.get("currency") if stmt_line else curr)

                    is_from = (target_tx.get("from_account_id") == primary_account_id)
                    is_to = (target_tx.get("to_account_id") == primary_account_id)

                    if is_from:
                        old_leg_amt = parse_decimal(target_tx["from_amount"])
                        before_effect = -old_leg_amt
                        after_effect = -actual_settlement_amt
                        proj_delta = after_effect - before_effect
                        account_deltas[primary_account_id] += proj_delta
                        new_from_amt = actual_settlement_amt
                        new_from_curr = settlement_curr
                        new_to_amt = target_tx.get("to_amount")
                        new_to_curr = target_tx.get("to_currency")
                    elif is_to:
                        old_leg_amt = parse_decimal(target_tx["to_amount"])
                        before_effect = old_leg_amt
                        after_effect = actual_settlement_amt
                        proj_delta = after_effect - before_effect
                        account_deltas[primary_account_id] += proj_delta
                        new_from_amt = target_tx.get("from_amount")
                        new_from_curr = target_tx.get("from_currency")
                        new_to_amt = actual_settlement_amt
                        new_to_curr = settlement_curr
                    else:
                        new_from_amt = target_tx.get("from_amount")
                        new_from_curr = target_tx.get("from_currency")
                        new_to_amt = target_tx.get("to_amount")
                        new_to_curr = target_tx.get("to_currency")

                    # Historical reporting FX freeze
                    occ_on = target_tx["occurred_on"]
                    if settlement_curr == rep_curr_target:
                        rep_rate = Decimal("1.000000000000")
                        rep_amt = actual_settlement_amt
                    else:
                        rep_rate = fx_srv.get_rate(settlement_curr, rep_curr_target, as_of=occ_on)
                        if rep_rate is None:
                            raise ValueError(f"Reference FX rate unavailable for {settlement_curr}->{rep_curr_target} on {occ_on}")
                        rep_amt = quantize_money(actual_settlement_amt * rep_rate, rep_curr_target)
                    rep_locked_at = datetime.now(timezone.utc)

                    tx_repo.update_transaction_statement_confirmed(
                        conn=conn,
                        transaction_id=target_tx_id,
                        posted_on=posted_on,
                        account_leg_status="authoritative",
                        from_amount=new_from_amt,
                        from_currency=new_from_curr,
                        to_amount=new_to_amt,
                        to_currency=new_to_curr,
                        reporting_amount=rep_amt,
                        reporting_currency=rep_curr_target,
                        reporting_fx_rate=rep_rate,
                        reporting_fx_locked_at=rep_locked_at,
                        statement_batch_id=batch_id
                    )

                    audit_repo.insert_audit_event(
                        conn=conn,
                        household_id=household_id,
                        actor_type="device" if device_id else "user",
                        actor_user_id=user_id,
                        actor_device_id=device_id,
                        reconciliation_batch_id=batch_id,
                        entity_type="transaction",
                        entity_id=target_tx_id,
                        action="reconcile",
                        before_data={
                            "from_amount": str(target_tx.get("from_amount")),
                            "from_currency": target_tx.get("from_currency"),
                            "account_leg_status": target_tx.get("account_leg_status"),
                            "reporting_amount": str(target_tx.get("reporting_amount")) if target_tx.get("reporting_amount") else None,
                            "reporting_fx_rate": str(target_tx.get("reporting_fx_rate")) if target_tx.get("reporting_fx_rate") else None
                        },
                        after_data={
                            "from_amount": str(new_from_amt) if new_from_amt else None,
                            "from_currency": new_from_curr,
                            "account_leg_status": "authoritative",
                            "reporting_amount": str(rep_amt),
                            "reporting_currency": rep_curr_target,
                            "reporting_fx_rate": str(rep_rate),
                            "verification_status": "statement_confirmed",
                            "statement_batch_id": str(batch_id)
                        }
                    )
                else:
                    # Already authoritative: freeze reporting FX if not already locked
                    rep_amt = target_tx.get("reporting_amount")
                    rep_curr_tx = target_tx.get("reporting_currency")
                    rep_rate = target_tx.get("reporting_fx_rate")
                    rep_locked_at = target_tx.get("reporting_fx_locked_at")

                    if rep_locked_at is None:
                        leg_amt = parse_decimal(target_tx.get("from_amount") or target_tx.get("to_amount") or target_tx.get("original_amount"))
                        curr_leg = target_tx.get("from_currency") or target_tx.get("to_currency") or curr
                        occ_on = target_tx["occurred_on"]
                        if curr_leg == rep_curr_target:
                            rep_rate = Decimal("1.000000000000")
                            rep_amt = leg_amt
                        else:
                            rep_rate = fx_srv.get_rate(curr_leg, rep_curr_target, as_of=occ_on)
                            if rep_rate is None:
                                rep_rate = Decimal("1.000000000000")
                                rep_amt = leg_amt
                            else:
                                rep_amt = quantize_money(leg_amt * rep_rate, rep_curr_target)
                        rep_curr_tx = rep_curr_target
                        rep_locked_at = datetime.now(timezone.utc)

                    tx_repo.update_transaction_statement_confirmed(
                        conn=conn,
                        transaction_id=target_tx_id,
                        posted_on=posted_on,
                        reporting_amount=rep_amt,
                        reporting_currency=rep_curr_tx,
                        reporting_fx_rate=rep_rate,
                        reporting_fx_locked_at=rep_locked_at,
                        statement_batch_id=batch_id
                    )

                applied_tx_ids.append(target_tx_id)
                persisted_payload = merge_canonical_line_evidence(fresh_cand.payload, stmt_line_id)
                if old_cand:
                    reconciliation_repo.update_reconciliation_candidate_full(
                        conn=conn,
                        candidate_id=old_cand["id"],
                        candidate_type="match",
                        status="applied",
                        payload=persisted_payload,
                        target_transaction_id=target_tx_id,
                        applied_transaction_id=target_tx_id,
                        confidence=fresh_cand.confidence
                    )
                else:
                    reconciliation_repo.create_reconciliation_candidate(
                        conn=conn,
                        candidate_id=fresh_cand.id,
                        batch_id=batch_id,
                        statement_line_id=stmt_line_id,
                        candidate_type="match",
                        status="applied",
                        target_transaction_id=target_tx_id,
                        applied_transaction_id=target_tx_id,
                        payload=persisted_payload,
                        confidence=fresh_cand.confidence
                    )
                if stmt_line_id:
                    reconciliation_repo.update_statement_line_status(
                        conn=conn,
                        line_id=stmt_line_id,
                        match_status="matched",
                        matched_transaction_id=target_tx_id
                    )

        # Case B: Create Transaction (Expense, Income, Fee)
        elif c_type == "create_transaction":
            tx_data = fresh_cand.payload.get("transaction") or fresh_cand.payload.get("line") or fresh_cand.payload or {}
            amt_raw = tx_data.get("amount") or (stmt_line.get("amount") if stmt_line else "0")
            amt = parse_decimal(amt_raw)
            c_curr = tx_data.get("currency") or (stmt_line.get("currency") if stmt_line else curr)
            ttype = tx_data.get("transaction_type") or ("income" if (stmt_line and stmt_line.get("direction") == "credit") else "expense")
            occ_on_val = tx_data.get("occurred_on") or (stmt_line.get("transaction_on") if stmt_line else None)
            occ_on = date.fromisoformat(str(occ_on_val)) if occ_on_val else (stmt_line.get("transaction_on") if stmt_line else date.today())
            merchant = tx_data.get("merchant") or (stmt_line.get("description_raw") if stmt_line else "Merchant")
            cat_id = UUID(str(tx_data["category_id"])) if tx_data.get("category_id") else None

            new_tx_id = uuid4()
            from_acc = primary_account_id if ttype in ("expense", "fee") else None
            to_acc = primary_account_id if ttype in ("income", "cash_income") else None
            from_amt = amt if from_acc else None
            from_cur = c_curr if from_acc else None
            to_amt = amt if to_acc else None
            to_cur = c_curr if to_acc else None

            household_row = accounts_repo.get_household(conn, household_id)
            rep_curr_target = household_row["reporting_currency"] if household_row else "CNY"
            if c_curr == rep_curr_target:
                rep_rate = Decimal("1.000000000000")
                rep_amt = amt
            else:
                rep_rate = fx_srv.get_rate(c_curr, rep_curr_target, as_of=occ_on)
                if rep_rate is None:
                    rep_rate = Decimal("1.000000000000")
                    rep_amt = amt
                else:
                    rep_amt = quantize_money(amt * rep_rate, rep_curr_target)
            rep_locked_at = datetime.now(timezone.utc)

            tx_repo.create_transaction(
                conn=conn,
                tx_id=new_tx_id,
                household_id=household_id,
                transaction_type=ttype,
                occurred_on=occ_on,
                original_amount=amt,
                original_currency=c_curr,
                from_amount=from_amt,
                from_currency=from_cur,
                to_amount=to_amt,
                to_currency=to_cur,
                from_account_id=from_acc,
                to_account_id=to_acc,
                category_id=cat_id,
                merchant=merchant,
                reporting_amount=rep_amt,
                reporting_currency=rep_curr_target,
                reporting_fx_rate=rep_rate,
                reporting_fx_locked_at=rep_locked_at,
                source="statement",
                status="committed",
                verification_status="statement_confirmed",
                statement_batch_id=batch_id
            )
            applied_tx_ids.append(new_tx_id)

            persisted_payload = merge_canonical_line_evidence(fresh_cand.payload, stmt_line_id)
            if old_cand:
                reconciliation_repo.update_reconciliation_candidate_full(
                    conn=conn,
                    candidate_id=old_cand["id"],
                    candidate_type="create_transaction",
                    status="applied",
                    payload=persisted_payload,
                    applied_transaction_id=new_tx_id,
                    confidence=fresh_cand.confidence
                )
            else:
                reconciliation_repo.create_reconciliation_candidate(
                    conn=conn,
                    candidate_id=fresh_cand.id,
                    batch_id=batch_id,
                    statement_line_id=stmt_line_id,
                    candidate_type="create_transaction",
                    status="applied",
                    applied_transaction_id=new_tx_id,
                    payload=persisted_payload,
                    confidence=fresh_cand.confidence
                )

            if stmt_line_id:
                reconciliation_repo.update_statement_line_status(
                    conn=conn,
                    line_id=stmt_line_id,
                    match_status="matched",
                    matched_transaction_id=new_tx_id
                )

            if ttype in ("expense", "fee"):
                account_deltas[primary_account_id] -= amt
            else:
                account_deltas[primary_account_id] += amt

            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type="device" if device_id else "user",
                actor_user_id=user_id,
                actor_device_id=device_id,
                reconciliation_batch_id=batch_id,
                entity_type="transaction",
                entity_id=new_tx_id,
                action="create",
                after_data={
                    "transaction_type": ttype,
                    "amount": str(amt),
                    "currency": c_curr,
                    "merchant": merchant
                }
            )

        # Case C: Create Transfer
        elif c_type == "create_transfer":
            t_data = fresh_cand.payload.get("transfer", {})
            from_acc_id = UUID(t_data["from_account_id"])
            to_acc_id = UUID(t_data["to_account_id"])
            from_amt = parse_decimal(t_data["from_amount"])
            from_curr = t_data["from_currency"]
            to_amt = parse_decimal(t_data["to_amount"])
            to_curr = t_data["to_currency"]
            fx_rate = parse_decimal(t_data.get("effective_fx_rate", "1.0"))
            occ_on = date.fromisoformat(t_data["occurred_on"]) if t_data.get("occurred_on") else (stmt_line.get("transaction_on") if stmt_line else date.today())

            new_tx_id = uuid4()
            tx_repo.create_transaction(
                conn=conn,
                tx_id=new_tx_id,
                household_id=household_id,
                transaction_type="transfer",
                occurred_on=occ_on,
                original_amount=from_amt,
                original_currency=from_curr,
                from_amount=from_amt,
                from_currency=from_curr,
                to_amount=to_amt,
                to_currency=to_curr,
                from_account_id=from_acc_id,
                to_account_id=to_acc_id,
                effective_fx_rate=fx_rate,
                source="statement",
                status="committed",
                verification_status="statement_confirmed",
                statement_batch_id=batch_id
            )
            applied_tx_ids.append(new_tx_id)

            persisted_payload = merge_canonical_line_evidence(fresh_cand.payload, stmt_line_id)
            if old_cand:
                reconciliation_repo.update_reconciliation_candidate_full(
                    conn=conn,
                    candidate_id=old_cand["id"],
                    candidate_type="create_transfer",
                    status="applied",
                    payload=persisted_payload,
                    applied_transaction_id=new_tx_id,
                    confidence=fresh_cand.confidence
                )
            else:
                reconciliation_repo.create_reconciliation_candidate(
                    conn=conn,
                    candidate_id=fresh_cand.id,
                    batch_id=batch_id,
                    statement_line_id=stmt_line_id,
                    candidate_type="create_transfer",
                    status="applied",
                    applied_transaction_id=new_tx_id,
                    payload=persisted_payload,
                    confidence=fresh_cand.confidence
                )

            if stmt_line_id:
                reconciliation_repo.update_statement_line_status(
                    conn=conn,
                    line_id=stmt_line_id,
                    match_status="matched",
                    matched_transaction_id=new_tx_id
                )

            if from_acc_id not in account_deltas:
                account_deltas[from_acc_id] = Decimal("0.00")
            account_deltas[from_acc_id] -= from_amt

            if to_acc_id not in account_deltas:
                account_deltas[to_acc_id] = Decimal("0.00")
            account_deltas[to_acc_id] += to_amt

            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type="device" if device_id else "user",
                actor_user_id=user_id,
                actor_device_id=device_id,
                reconciliation_batch_id=batch_id,
                entity_type="transaction",
                entity_id=new_tx_id,
                action="create",
                after_data={
                    "transaction_type": "transfer",
                    "from_account_id": str(from_acc_id),
                    "to_account_id": str(to_acc_id),
                    "from_amount": str(from_amt),
                    "to_amount": str(to_amt)
                }
            )

        # Case D: Refund
        elif c_type == "refund":
            ref_data = fresh_cand.payload.get("refund", {})
            orig_exp_id = UUID(ref_data["original_expense_id"])
            amt = parse_decimal(ref_data["amount"])
            ref_curr = ref_data["currency"]
            occ_on = date.fromisoformat(ref_data["occurred_on"]) if ref_data.get("occurred_on") else (stmt_line.get("transaction_on") if stmt_line else date.today())
            merchant = ref_data.get("merchant") or "Refund"
            cat_id = UUID(ref_data["category_id"]) if ref_data.get("category_id") else None

            # Verify refund limits under lock
            active_refunds = tx_repo.get_active_refunds_for_expense(conn, orig_exp_id)
            tot_already_refunded = sum((parse_decimal(r.get("from_amount") or r.get("to_amount") or r.get("original_amount")) for r in active_refunds), Decimal("0.00"))
            orig_exp = tx_repo.get_transaction(conn, orig_exp_id)
            orig_amt = parse_decimal(orig_exp.get("from_amount") or orig_exp.get("original_amount"))
            if tot_already_refunded + amt > orig_amt:
                raise ValueError(f"Refund {amt} exceeds remaining refundable amount ({orig_amt - tot_already_refunded})")

            new_tx_id = uuid4()
            tx_repo.create_transaction(
                conn=conn,
                tx_id=new_tx_id,
                household_id=household_id,
                transaction_type="refund",
                occurred_on=occ_on,
                original_amount=amt,
                original_currency=ref_curr,
                to_amount=amt,
                to_currency=ref_curr,
                to_account_id=primary_account_id,
                category_id=cat_id,
                merchant=merchant,
                source="statement",
                status="committed",
                verification_status="statement_confirmed",
                statement_batch_id=batch_id
            )
            link_id = uuid4()
            tx_repo.create_transaction_link(
                conn=conn,
                link_id=link_id,
                source_transaction_id=new_tx_id,
                target_transaction_id=orig_exp_id,
                relation_type="refund_of"
            )
            applied_tx_ids.append(new_tx_id)

            persisted_payload = merge_canonical_line_evidence(fresh_cand.payload, stmt_line_id)
            if old_cand:
                reconciliation_repo.update_reconciliation_candidate_full(
                    conn=conn,
                    candidate_id=old_cand["id"],
                    candidate_type="refund",
                    status="applied",
                    payload=persisted_payload,
                    applied_transaction_id=new_tx_id,
                    confidence=fresh_cand.confidence
                )
            else:
                reconciliation_repo.create_reconciliation_candidate(
                    conn=conn,
                    candidate_id=fresh_cand.id,
                    batch_id=batch_id,
                    statement_line_id=stmt_line_id,
                    candidate_type="refund",
                    status="applied",
                    applied_transaction_id=new_tx_id,
                    payload=persisted_payload,
                    confidence=fresh_cand.confidence
                )

            if stmt_line_id:
                reconciliation_repo.update_statement_line_status(
                    conn=conn,
                    line_id=stmt_line_id,
                    match_status="matched",
                    matched_transaction_id=new_tx_id
                )

            account_deltas[primary_account_id] += amt

            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type="device" if device_id else "user",
                actor_user_id=user_id,
                actor_device_id=device_id,
                reconciliation_batch_id=batch_id,
                entity_type="transaction",
                entity_id=new_tx_id,
                action="create",
                after_data={
                    "transaction_type": "refund",
                    "refund_of": str(orig_exp_id),
                    "amount": str(amt),
                    "currency": ref_curr
                }
            )

        # Case E: Recognize Installment
        elif c_type == "recognize_installment":
            inst_data = fresh_cand.payload.get("installment", {})
            plan_id = UUID(inst_data["plan_id"])
            period_id = UUID(inst_data["period_id"])
            amt = parse_decimal(inst_data["scheduled_amount"])
            inst_curr = inst_data["currency"]
            merchant = inst_data.get("merchant") or "Installment"
            occ_on = date.fromisoformat(inst_data["occurred_on"]) if inst_data.get("occurred_on") else (stmt_line.get("transaction_on") if stmt_line else date.today())
            cat_id = UUID(inst_data["category_id"]) if inst_data.get("category_id") else None
            is_first = inst_data.get("is_first_period", False)
            is_last = inst_data.get("is_last_period", False)

            household_row = accounts_repo.get_household(conn, household_id)
            rep_curr_target = household_row["reporting_currency"] if household_row else "CNY"
            if inst_curr == rep_curr_target:
                rep_rate = Decimal("1.000000000000")
                rep_amt = amt
            else:
                rep_rate = fx_srv.get_rate(inst_curr, rep_curr_target, as_of=occ_on)
                if rep_rate is None:
                    rep_rate = Decimal("1.000000000000")
                    rep_amt = amt
                else:
                    rep_amt = quantize_money(amt * rep_rate, rep_curr_target)
            rep_locked_at = datetime.now(timezone.utc)

            new_tx_id = uuid4()
            tx_repo.create_transaction(
                conn=conn,
                tx_id=new_tx_id,
                household_id=household_id,
                transaction_type="expense",
                occurred_on=occ_on,
                original_amount=amt,
                original_currency=inst_curr,
                from_amount=amt,
                from_currency=inst_curr,
                from_account_id=primary_account_id,
                category_id=cat_id,
                merchant=merchant,
                reporting_amount=rep_amt,
                reporting_currency=rep_curr_target,
                reporting_fx_rate=rep_rate,
                reporting_fx_locked_at=rep_locked_at,
                source="installment",
                status="committed",
                verification_status="statement_confirmed",
                statement_batch_id=batch_id
            )
            billed_ok = installments_repo.update_installment_period_billed_atomic(
                conn=conn,
                period_id=period_id,
                expense_transaction_id=new_tx_id,
                statement_line_id=stmt_line_id
            )

            if not billed_ok:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM transactions WHERE id = %s;", (new_tx_id,))
            else:
                if is_first:
                    first_stmt_month = date(occ_on.year, occ_on.month, 1)
                    installments_repo.update_installment_plan_first_statement_month_and_status(conn, plan_id, "active", first_stmt_month)
                    installments_repo.populate_scheduled_period_recognition_months(conn, plan_id, first_stmt_month)

                installments_repo.check_and_update_plan_completion(conn, plan_id)

                applied_tx_ids.append(new_tx_id)

                persisted_payload = merge_canonical_line_evidence(fresh_cand.payload, stmt_line_id)
                if old_cand:
                    reconciliation_repo.update_reconciliation_candidate_full(
                        conn=conn,
                        candidate_id=old_cand["id"],
                        candidate_type="recognize_installment",
                        status="applied",
                        payload=persisted_payload,
                        applied_transaction_id=new_tx_id,
                        confidence=fresh_cand.confidence
                    )
                else:
                    reconciliation_repo.create_reconciliation_candidate(
                        conn=conn,
                        candidate_id=fresh_cand.id,
                        batch_id=batch_id,
                        statement_line_id=stmt_line_id,
                        candidate_type="recognize_installment",
                        status="applied",
                        applied_transaction_id=new_tx_id,
                        payload=persisted_payload,
                        confidence=fresh_cand.confidence
                    )

                if stmt_line_id:
                    reconciliation_repo.update_statement_line_status(
                        conn=conn,
                        line_id=stmt_line_id,
                        match_status="matched",
                        matched_transaction_id=new_tx_id
                    )

                account_deltas[primary_account_id] -= amt

                audit_repo.insert_audit_event(
                    conn=conn,
                    household_id=household_id,
                    actor_type="device" if device_id else "user",
                    actor_user_id=user_id,
                    actor_device_id=device_id,
                    reconciliation_batch_id=batch_id,
                    entity_type="transaction",
                    entity_id=new_tx_id,
                    action="create",
                    after_data={
                        "transaction_type": "expense",
                        "installment_period_id": str(period_id),
                        "amount": str(amt),
                        "currency": inst_curr
                    }
                )

        # Case F: Credit Card Snapshot
        elif c_type == "snapshot":
            cc_data = fresh_cand.payload.get("credit_card_snapshot") or {}
            if is_credit and cc_data:
                stmt_date_val = cc_data.get("statement_date")
                p_end_val = cc_data.get("statement_period_end")
                if stmt_date_val:
                    d = date.fromisoformat(stmt_date_val)
                    as_of_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
                elif p_end_val:
                    d = date.fromisoformat(p_end_val)
                    as_of_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
                else:
                    as_of_dt = batch.get("created_at") or datetime.now(timezone.utc)

                p_start = date.fromisoformat(cc_data["statement_period_start"]) if cc_data.get("statement_period_start") else None
                p_end = date.fromisoformat(cc_data["statement_period_end"]) if cc_data.get("statement_period_end") else None
                stmt_bal = parse_decimal(cc_data["statement_balance"]) if cc_data.get("statement_balance") is not None else None
                rem_due = parse_decimal(cc_data["remaining_statement_due"]) if cc_data.get("remaining_statement_due") is not None else None
                unbilled_bal = parse_decimal(cc_data["unbilled_balance"]) if cc_data.get("unbilled_balance") is not None else None
                curr_out = parse_decimal(cc_data["current_outstanding"]) if cc_data.get("current_outstanding") is not None else None
                snap_curr = cc_data.get("currency") or curr

                credit_cards_repo.create_credit_card_snapshot(
                    conn=conn,
                    snapshot_id=uuid4(),
                    household_id=household_id,
                    account_id=primary_account_id,
                    as_of=as_of_dt,
                    statement_period_start=p_start,
                    statement_period_end=p_end,
                    statement_balance=stmt_bal,
                    remaining_statement_due=rem_due,
                    unbilled_balance=unbilled_bal,
                    current_outstanding=curr_out,
                    currency=snap_curr,
                    source="statement",
                    reconciliation_batch_id=batch_id
                )

            old_cand = next((c for c in candidates if c["candidate_type"] == "snapshot"), None)
            if old_cand:
                reconciliation_repo.update_reconciliation_candidate_full(
                    conn=conn,
                    candidate_id=old_cand["id"],
                    candidate_type="snapshot",
                    status="applied",
                    payload=fresh_cand.payload,
                    confidence=fresh_cand.confidence
                )
            else:
                reconciliation_repo.create_reconciliation_candidate(
                    conn=conn,
                    candidate_id=fresh_cand.id,
                    batch_id=batch_id,
                    statement_line_id=None,
                    candidate_type="snapshot",
                    status="applied",
                    payload=fresh_cand.payload,
                    confidence=fresh_cand.confidence
                )

        # Case G: Adjustment
        elif c_type == "adjustment":
            adj_amt = parse_decimal(fresh_cand.payload.get("adjustment_amount", "0"))
            if adj_amt != Decimal("0.00"):
                new_tx_id = uuid4()
                adj_date = batch.get("period_end") or date.today()
                if adj_amt > 0:
                    to_amt, to_cur, to_acc = adj_amt, curr, primary_account_id
                    from_amt, from_cur, from_acc = None, None, None
                else:
                    from_amt, from_cur, from_acc = abs(adj_amt), curr, primary_account_id
                    to_amt, to_cur, to_acc = None, None, None

                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=new_tx_id,
                    household_id=household_id,
                    transaction_type="reconciliation_adjustment",
                    occurred_on=adj_date,
                    original_amount=abs(adj_amt),
                    original_currency=curr,
                    from_amount=from_amt,
                    from_currency=from_cur,
                    from_account_id=from_acc,
                    to_amount=to_amt,
                    to_currency=to_cur,
                    to_account_id=to_acc,
                    source="reconciliation",
                    status="committed",
                    verification_status="statement_confirmed",
                    statement_batch_id=batch_id
                )
                committed_adjustment_amount = adj_amt
                applied_tx_ids.append(new_tx_id)

                if old_adj_cand:
                    reconciliation_repo.update_reconciliation_candidate_full(
                        conn=conn,
                        candidate_id=old_adj_cand["id"],
                        candidate_type="adjustment",
                        status="applied",
                        payload=fresh_cand.payload,
                        applied_transaction_id=new_tx_id
                    )
                else:
                    reconciliation_repo.create_reconciliation_candidate(
                        conn=conn,
                        candidate_id=fresh_cand.id,
                        batch_id=batch_id,
                        statement_line_id=None,
                        candidate_type="adjustment",
                        status="applied",
                        applied_transaction_id=new_tx_id,
                        payload=fresh_cand.payload
                    )

                account_deltas[primary_account_id] += adj_amt

                audit_repo.insert_audit_event(
                    conn=conn,
                    household_id=household_id,
                    actor_type="device" if device_id else "user",
                    actor_user_id=user_id,
                    actor_device_id=device_id,
                    reconciliation_batch_id=batch_id,
                    entity_type="transaction",
                    entity_id=new_tx_id,
                    action="create",
                    after_data={
                        "transaction_type": "reconciliation_adjustment",
                        "amount": str(abs(adj_amt)),
                        "currency": curr
                    }
                )
            else:
                if old_adj_cand:
                    reconciliation_repo.update_reconciliation_candidate_full(
                        conn=conn,
                        candidate_id=old_adj_cand["id"],
                        candidate_type="adjustment",
                        status="applied",
                        payload=fresh_cand.payload
                    )

    # If old batch had an adjustment candidate but fresh result generated zero adjustment:
    if old_adj_cand and not any(fc.candidate_type == "adjustment" for fc in fresh_result.candidates):
        reconciliation_repo.update_reconciliation_candidate_full(
            conn=conn,
            candidate_id=old_adj_cand["id"],
            candidate_type="adjustment",
            status="applied",
            payload={"adjustment_amount": "0.00", "currency": curr}
        )

    # 5. Update account_state for all affected accounts
    for acc_id, delta in account_deltas.items():
        if delta != Decimal("0.00") and acc_id in account_states:
            current_bal = parse_decimal(account_states[acc_id]["ledger_balance"])
            acc_info = accounts_repo.get_account(conn, acc_id)
            acc_curr = acc_info["currency"] if acc_info else curr
            new_bal = quantize_money(current_bal + delta, acc_curr)
            accounts_repo.update_account_state_projection(
                conn=conn,
                account_id=acc_id,
                new_balance=new_bal,
                last_transaction_at=datetime.now(timezone.utc)
            )

    # 6. Mark batch committed with fresh counts and residual
    now_dt = datetime.now(timezone.utc)
    reconciliation_repo.update_reconciliation_batch_stats(
        conn=conn,
        batch_id=batch_id,
        status="committed",
        matched_count=fresh_result.matched_count,
        created_count=fresh_result.created_count,
        pending_count=fresh_result.pending_count,
        residual_amount=fresh_result.residual_amount,
        adjustment_amount=committed_adjustment_amount,
        committed_at=now_dt
    )

    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        reconciliation_batch_id=batch_id,
        entity_type="reconciliation_batch",
        entity_id=batch_id,
        action="commit",
        after_data={
            "status": "committed",
            "applied_transactions_count": len(applied_tx_ids)
        }
    )

    return {
        "status": "committed",
        "batch_id": str(batch_id),
        "residual_amount": str(fresh_result.residual_amount),
        "adjustment_amount": str(committed_adjustment_amount) if committed_adjustment_amount is not None else None,
        "matched_count": fresh_result.matched_count,
        "created_count": fresh_result.created_count,
        "pending_count": fresh_result.pending_count,
        "applied_transaction_ids": [str(tid) for tid in applied_tx_ids]
    }


