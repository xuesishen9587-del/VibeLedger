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
from app.services.reference_fx_service import ReferenceFxService
from app.services.snapshot_service import ledger_balance_as_of

import app.repositories.accounts as accounts_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo
import app.repositories.installments as installments_repo
import app.repositories.audit as audit_repo


def create_statement_reconciliation_batch(
    conn,
    household_id: UUID,
    account_id: UUID,
    lines: List[NormalizedStatementLine],
    authoritative_balance: Decimal,
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
    fx_service: Optional[ReferenceFxService] = None
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
    auth_balance = quantize_money(authoritative_balance, account_curr)
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
    persisted_candidates = []
    for cand in result.candidates:
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
        "residual_amount": str(result.residual_amount),
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

    # 3. Reconstruct NormalizedStatementLines and re-run full deterministic engine against fresh ledger
    norm_lines: List[NormalizedStatementLine] = []
    for sl in statement_lines:
        line_no = sl.get("source_row_no") or 0
        amt = sl.get("amount") or Decimal("0.00")
        curr = sl.get("currency") or batch["currency"]
        norm_lines.append(NormalizedStatementLine(
            id=sl["id"],
            description_raw=sl["description_raw"],
            direction=sl["direction"],
            line_type=sl.get("line_type", "unknown"),
            settlement_amount=parse_decimal(amt),
            settlement_currency=curr,
            transaction_on=sl.get("transaction_on"),
            posted_on=sl.get("posted_on"),
            description_normalized=sl.get("description_normalized") or sl["description_raw"],
            original_amount=parse_decimal(sl["original_amount"]) if sl.get("original_amount") else None,
            original_currency=sl.get("original_currency"),
            merchant_hint=sl.get("merchant_hint"),
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
    hh_accounts = accounts_repo.list_accounts(conn, household_id)
    hh_movements: List[Dict[str, Any]] = []
    for c in candidates:
        if c.get("candidate_type") == "create_transfer" and c.get("status") in ("accepted", "applied"):
            t_data = c.get("payload", {}).get("transfer", {})
            to_acc = t_data.get("to_account_id")
            if to_acc:
                hh_movements.append({
                    "account_id": UUID(to_acc),
                    "direction": "credit",
                    "amount": parse_decimal(t_data.get("to_amount")),
                    "currency": t_data.get("to_currency"),
                    "occurred_on": date.fromisoformat(t_data.get("occurred_on")) if t_data.get("occurred_on") else as_of_date,
                    "is_counter_statement_leg": True
                })


    # Extract default categories from accepted candidates in preview
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


    fx_rate_cny = Decimal("1.00")
    if curr != "CNY":
        fx_srv = ReferenceFxService()
        fx_rate_cny = fx_srv.get_rate(curr, "CNY", as_of=as_of_date)

    auth_balance = quantize_money(batch.get("authoritative_balance", Decimal("0.00")), curr)

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
        default_expense_category_id=batch_exp_cat_id,
        default_income_category_id=batch_inc_cat_id,
        fx_rate_to_cny=fx_rate_cny
    )


    if fresh_result.batch_status == "needs_review":
        reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review", residual_amount=fresh_result.residual_amount)
        raise ValueError(f"Fresh ledger state requires review: {fresh_result.candidates[0].reason_code if fresh_result.candidates else 'NEEDS_REVIEW'}")

    # Map candidate proposals by line_id
    fresh_line_cands: Dict[UUID, CandidateProposal] = {}
    fresh_adj_cand: Optional[CandidateProposal] = None
    for fc in fresh_result.candidates:
        if fc.statement_line_id:
            fresh_line_cands[fc.statement_line_id] = fc
        elif fc.candidate_type == "adjustment":
            fresh_adj_cand = fc

    applied_tx_ids: List[UUID] = []
    account_deltas: Dict[UUID, Decimal] = {acc_id: Decimal("0.00") for acc_id in affected_account_ids}
    committed_adjustment_amount: Optional[Decimal] = None

    # 4. Apply all fresh accepted candidates

    for c in candidates:
        cand_status = c["status"]
        if cand_status not in ("accepted", "applied"):
            continue

        c_type = c["candidate_type"]
        stmt_line_id = c.get("statement_line_id")
        stmt_line = line_map.get(stmt_line_id) if stmt_line_id else None
        fresh_cand = fresh_line_cands.get(stmt_line_id) if stmt_line_id else None

        # Case A: Existing Match
        if c_type == "match":
            target_tx_id = (fresh_cand.target_transaction_id if fresh_cand else None) or c.get("target_transaction_id")
            if target_tx_id:
                posted_on = stmt_line.get("posted_on") if stmt_line else None
                # Phase 8 foreign-card boundary: mark statement-confirmed without mutating from_amount or account_leg_status
                tx_repo.update_transaction_statement_confirmed(
                    conn=conn,
                    transaction_id=target_tx_id,
                    posted_on=posted_on,
                    statement_batch_id=batch_id
                )
                applied_tx_ids.append(target_tx_id)
                reconciliation_repo.update_candidate_applied(conn, c["id"], status="applied", applied_transaction_id=target_tx_id)
                if stmt_line_id:
                    reconciliation_repo.update_statement_line_status(conn, stmt_line_id, match_status="matched", matched_transaction_id=target_tx_id)

        # Case B: Create Transaction (Expense, Income, Fee)
        elif c_type == "create_transaction":
            # Check if fresh engine matched an existing transaction (e.g. concurrent shortcut inserted it)
            if fresh_cand and fresh_cand.candidate_type == "match" and fresh_cand.target_transaction_id:
                matched_id = fresh_cand.target_transaction_id
                tx_repo.update_transaction_statement_confirmed(
                    conn=conn,
                    transaction_id=matched_id,
                    posted_on=stmt_line.get("posted_on") if stmt_line else None,
                    statement_batch_id=batch_id
                )
                applied_tx_ids.append(matched_id)
                reconciliation_repo.update_candidate_applied(conn, c["id"], status="applied", applied_transaction_id=matched_id)
                if stmt_line_id:
                    reconciliation_repo.update_statement_line_status(conn, stmt_line_id, match_status="matched", matched_transaction_id=matched_id)
            else:
                # Create new transaction
                tx_data = c["payload"].get("transaction", {})
                amt = parse_decimal(tx_data.get("amount", "0"))
                c_curr = tx_data.get("currency", curr)
                ttype = tx_data.get("transaction_type", "expense")
                occ_on = date.fromisoformat(tx_data["occurred_on"]) if tx_data.get("occurred_on") else (stmt_line.get("transaction_on") if stmt_line else date.today())
                merchant = tx_data.get("merchant") or (stmt_line.get("description_raw") if stmt_line else "Merchant")
                cat_id = UUID(tx_data["category_id"]) if tx_data.get("category_id") else None

                new_tx_id = uuid4()
                from_acc = primary_account_id if ttype in ("expense", "fee") else None
                to_acc = primary_account_id if ttype in ("income", "cash_income") else None
                from_amt = amt if from_acc else None
                from_cur = c_curr if from_acc else None
                to_amt = amt if to_acc else None
                to_cur = c_curr if to_acc else None

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
                    source="statement",
                    status="committed",
                    verification_status="statement_confirmed"
                )
                applied_tx_ids.append(new_tx_id)
                reconciliation_repo.update_candidate_applied(conn, c["id"], status="applied", applied_transaction_id=new_tx_id)
                if stmt_line_id:
                    reconciliation_repo.update_statement_line_status(conn, stmt_line_id, match_status="matched", matched_transaction_id=new_tx_id)

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
            t_data = c["payload"].get("transfer", {})
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
                verification_status="statement_confirmed"
            )
            applied_tx_ids.append(new_tx_id)
            reconciliation_repo.update_candidate_applied(conn, c["id"], status="applied", applied_transaction_id=new_tx_id)
            if stmt_line_id:
                reconciliation_repo.update_statement_line_status(conn, stmt_line_id, match_status="matched", matched_transaction_id=new_tx_id)

            if from_acc_id in account_deltas:
                account_deltas[from_acc_id] -= from_amt
            if to_acc_id in account_deltas:
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
            ref_data = c["payload"].get("refund", {})
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
                verification_status="statement_confirmed"
            )
            # Create transaction_link
            link_id = uuid4()
            tx_repo.create_transaction_link(
                conn=conn,
                link_id=link_id,
                source_transaction_id=new_tx_id,
                target_transaction_id=orig_exp_id,
                relation_type="refund_of"
            )
            applied_tx_ids.append(new_tx_id)
            reconciliation_repo.update_candidate_applied(conn, c["id"], status="applied", applied_transaction_id=new_tx_id)
            if stmt_line_id:
                reconciliation_repo.update_statement_line_status(conn, stmt_line_id, match_status="matched", matched_transaction_id=new_tx_id)

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
            inst_data = c["payload"].get("installment", {})
            plan_id = UUID(inst_data["plan_id"])
            period_id = UUID(inst_data["period_id"])
            amt = parse_decimal(inst_data["scheduled_amount"])
            inst_curr = inst_data["currency"]
            merchant = inst_data.get("merchant") or "Installment"
            occ_on = date.fromisoformat(inst_data["occurred_on"]) if inst_data.get("occurred_on") else (stmt_line.get("transaction_on") if stmt_line else date.today())
            cat_id = UUID(inst_data["category_id"]) if inst_data.get("category_id") else None
            is_first = inst_data.get("is_first_period", False)
            is_last = inst_data.get("is_last_period", False)
            # Atomic guard: create transaction and attempt to bill the period atomically
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
                source="installment",
                status="committed",
                verification_status="statement_confirmed"
            )
            billed_ok = installments_repo.update_installment_period_billed_atomic(
                conn=conn,
                period_id=period_id,
                expense_transaction_id=new_tx_id,
                statement_line_id=stmt_line_id
            )

            if not billed_ok:
                # Period already billed concurrently: clean up created transaction
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM transactions WHERE id = %s;", (new_tx_id,))
            else:
                if is_last:
                    installments_repo.update_installment_plan_status(conn, plan_id, "completed")
                elif is_first:
                    installments_repo.update_installment_plan_status(conn, plan_id, "active")

                applied_tx_ids.append(new_tx_id)
                reconciliation_repo.update_candidate_applied(conn, c["id"], status="applied", applied_transaction_id=new_tx_id)
                if stmt_line_id:
                    reconciliation_repo.update_statement_line_status(conn, stmt_line_id, match_status="matched", matched_transaction_id=new_tx_id)

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

        # Case F: Adjustment
        elif c_type == "adjustment":
            adj_amt = fresh_result.adjustment_amount
            if adj_amt is not None and adj_amt != Decimal("0.00"):
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
                    verification_status="statement_confirmed"
                )
                committed_adjustment_amount = adj_amt
                applied_tx_ids.append(new_tx_id)
                reconciliation_repo.update_candidate_applied(conn, c["id"], status="applied", applied_transaction_id=new_tx_id)
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
        "matched_count": fresh_result.matched_count,
        "created_count": fresh_result.created_count,
        "applied_transaction_ids": [str(tid) for tid in applied_tx_ids]
    }

