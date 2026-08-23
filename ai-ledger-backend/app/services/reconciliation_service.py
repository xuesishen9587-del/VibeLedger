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

    # Determine baseline effective date for historical projection
    as_of_date = period_end or (lines[-1].effective_date if lines and lines[-1].effective_date else date.today())
    as_of_dt = datetime.combine(as_of_date, datetime.max.time(), tzinfo=timezone.utc)

    # 1. Historical ledger balance as-of statement end date
    baseline_projected_balance = ledger_balance_as_of(conn, account_id, as_of_dt)

    # 2. Fetch existing committed transactions around period
    if lines:
        valid_dates = [l.effective_date for l in lines if l.effective_date is not None]
        if valid_dates:
            min_date = min(valid_dates)
            max_date = max(valid_dates)
        else:
            min_date = period_start or as_of_date
            max_date = as_of_date
    else:
        min_date = period_start or as_of_date
        max_date = as_of_date

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

    # 5. Fetch household accounts & movements for transfer resolution
    hh_accounts = accounts_repo.list_accounts(conn, household_id)
    hh_movements = []
    def _extract_acc_id_str(val):
        if not val:
            return None
        if isinstance(val, dict):
            return str(val.get("id")) if val.get("id") else None
        return str(val)

    for hh_acc in hh_accounts:
        if hh_acc["id"] == account_id:
            continue
        acc_txs, _ = tx_repo.list_transactions_with_filters(
            conn=conn,
            household_id=household_id,
            account_id=hh_acc["id"],
            from_date=tx_from_date,
            to_date=tx_to_date,
            limit=100
        )
        hh_acc_str = str(hh_acc["id"])
        for t in acc_txs:
            t_from = _extract_acc_id_str(t.get("from_account_id") or t.get("from_account"))
            t_to = _extract_acc_id_str(t.get("to_account_id") or t.get("to_account"))
            if t_from == hh_acc_str:
                hh_movements.append({
                    "account_id": hh_acc["id"],
                    "direction": "debit",
                    "amount": t.get("from_amount") or t.get("original_amount"),
                    "currency": t.get("from_currency") or t.get("original_currency"),
                    "occurred_on": t.get("occurred_on"),
                    "transaction_id": t["id"]
                })
            if t_to == hh_acc_str:
                hh_movements.append({
                    "account_id": hh_acc["id"],
                    "direction": "credit",
                    "amount": t.get("to_amount") or t.get("original_amount"),
                    "currency": t.get("to_currency") or t.get("original_currency"),
                    "occurred_on": t.get("occurred_on"),
                    "transaction_id": t["id"]
                })


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
        default_expense_category_id=default_expense_category_id,
        default_income_category_id=default_income_category_id,
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

    if batch["status"] not in ("ready", "needs_review"):
        raise ValueError(f"Batch status {batch['status']} is not committable")

    if batch["batch_type"] != "statement":
        raise ValueError(f"Expected statement batch type, got {batch['batch_type']}")

    household_id = batch["household_id"]
    primary_account_id = batch["account_id"]
    curr = batch["currency"]

    household = accounts_repo.get_household(conn, household_id)
    reporting_currency = household["reporting_currency"] if household else "CNY"

    candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
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

    # 3. Re-read current committed ledger state & revalidate candidates
    # CRITICAL: If a missing transaction was committed concurrently (e.g. by shortcut), match it instead of creating duplicate!
    applied_tx_ids: List[UUID] = []
    account_deltas: Dict[UUID, Decimal] = {acc_id: Decimal("0.00") for acc_id in affected_account_ids}

    for c in candidates:
        cand_status = c["status"]
        if cand_status not in ("accepted", "applied"):
            continue

        c_type = c["candidate_type"]
        stmt_line_id = c.get("statement_line_id")
        stmt_line = line_map.get(stmt_line_id) if stmt_line_id else None

        # Case A: Existing Match
        if c_type == "match":
            tx_id = c.get("target_transaction_id")
            if tx_id:
                # Update verification status on existing transaction
                posted_on = stmt_line.get("posted_on") if stmt_line else None
                settlement_patch = c["payload"].get("settlement_patch")
                if settlement_patch:
                    # Foreign card estimated settlement update
                    settle_amt = parse_decimal(settlement_patch["settlement_amount"])
                    settle_curr = settlement_patch["settlement_currency"]
                    # Compute delta from current committed transaction before updating it
                    orig_tx = tx_repo.get_transaction(conn, tx_id)
                    orig_amt = parse_decimal(orig_tx.get("from_amount") or orig_tx.get("original_amount")) if orig_tx else settle_amt
                    delta = orig_amt - settle_amt
                    account_deltas[primary_account_id] += delta

                    tx_repo.update_transaction_statement_confirmed(
                        conn=conn,
                        transaction_id=tx_id,
                        posted_on=posted_on,
                        account_leg_status="authoritative",
                        from_amount=settle_amt,
                        statement_batch_id=batch_id
                    )
                else:
                    tx_repo.update_transaction_statement_confirmed(
                        conn=conn,
                        transaction_id=tx_id,
                        posted_on=posted_on,
                        statement_batch_id=batch_id
                    )

                
                applied_tx_ids.append(tx_id)
                reconciliation_repo.update_candidate_applied(conn, c["id"], status="applied", applied_transaction_id=tx_id)
                if stmt_line_id:
                    reconciliation_repo.update_statement_line_status(conn, stmt_line_id, match_status="matched", matched_transaction_id=tx_id)

        # Case B: Create Transaction (Expense, Income, Fee)
        elif c_type == "create_transaction":
            tx_data = c["payload"].get("transaction", {})
            amt = parse_decimal(tx_data.get("amount", "0"))
            c_curr = tx_data.get("currency", curr)
            ttype = tx_data.get("transaction_type", "expense")
            occ_on = date.fromisoformat(tx_data["occurred_on"]) if tx_data.get("occurred_on") else (stmt_line.get("transaction_on") or date.today())
            merchant = tx_data.get("merchant") or (stmt_line.get("description_raw") if stmt_line else "Merchant")
            cat_id = UUID(tx_data["category_id"]) if tx_data.get("category_id") else None

            # Re-read ledger: check if concurrent shortcut already committed this exact transaction
            from datetime import timedelta
            dup_candidates, _ = tx_repo.list_transactions_with_filters(
                conn=conn,
                household_id=household_id,
                account_id=primary_account_id,
                from_date=occ_on - timedelta(days=5),
                to_date=occ_on + timedelta(days=5),
                transaction_type=ttype,
                limit=50
            )
            found_concurrent_tx = None
            for dtx in dup_candidates:
                d_amt = parse_decimal(dtx.get("from_amount") or dtx.get("to_amount") or dtx.get("original_amount") or "0")
                d_curr = str(dtx.get("from_currency") or dtx.get("to_currency") or dtx.get("original_currency") or "").strip().upper()
                c_curr_str = str(c_curr or "").strip().upper()
                if d_amt == amt and d_curr == c_curr_str:
                    # Match found!
                    found_concurrent_tx = dtx
                    break


            if found_concurrent_tx:
                # Match to the concurrent transaction instead of creating duplicate!
                matched_id = found_concurrent_tx["id"]
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
            is_first = inst_data.get("is_first_period", False)
            is_last = inst_data.get("is_last_period", False)

            # Create expense transaction
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
                merchant=merchant,
                source="installment",
                status="committed",
                verification_status="statement_confirmed"
            )
            # Update installment_period
            installments_repo.update_installment_period_billed(
                conn=conn,
                period_id=period_id,
                expense_transaction_id=new_tx_id,
                statement_line_id=stmt_line_id
            )
            # Update installment_plan status
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
            adj_amt = parse_decimal(c["payload"].get("adjustment_amount", "0"))
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
                    verification_status="statement_confirmed"
                )
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

    # 4. Update account_state for all affected accounts
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


    # 5. Mark batch committed
    now_dt = datetime.now(timezone.utc)
    reconciliation_repo.update_reconciliation_batch(
        conn=conn,
        batch_id=batch_id,
        status="committed",
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
        "residual_amount": str(batch.get("residual_amount") or "0.00"),
        "matched_count": batch.get("matched_count", 0),
        "created_count": batch.get("created_count", 0),
        "applied_transaction_ids": [str(tid) for tid in applied_tx_ids]
    }
