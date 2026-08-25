import os
import tempfile
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID, uuid4
from datetime import date, datetime, timezone
from decimal import Decimal

from app.domain.money import parse_decimal, quantize_money, validate_currency_code
from app.domain.transactions import (
    AccountResourceNotFoundError,
    AccountInactiveError,
    AccountTypeMismatchError,
    BatchNotFoundError,
    BatchVersionConflictError,
    CandidateResourceNotFoundError,
    InvalidCandidateStateError,
    InvalidCandidatePayloadError,
    IncompatibleTargetTransactionError,
    StatementParseFailedError,
    CategoryResourceNotFoundError,
    CategoryMismatchError,
    SameAccountTransferError
)
from app.domain.reconciliation.models import NormalizedStatementLine, CandidateProposal
from app.domain.reconciliation.scoring import compute_match_score, validate_target_match_compatibility
from app.domain.reconciliation.residuals import (
    evaluate_residual_and_batch_readiness,
    simulate_candidate_effects,
    evaluate_credit_card_statement_cycle
)
from app.services.statement_parser import (
    BaseStatementParser,
    GeminiStatementParser,
    validate_and_normalize_extraction
)
from app.services.reference_fx_service import ReferenceFxService
from app.services.reconciliation_service import (
    create_statement_reconciliation_batch,
    commit_statement_batch,
    ledger_balance_as_of
)
import app.repositories.accounts as accounts_repo
import app.repositories.transactions as tx_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.installments as installments_repo
import app.repositories.audit as audit_repo

logger = logging.getLogger(__name__)


def upload_and_process_statement(
    conn,
    household_id: UUID,
    account_id: UUID,
    file_bytes: bytes,
    filename: Optional[str] = None,
    password: Optional[str] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    default_expense_category_id: Optional[UUID] = None,
    default_income_category_id: Optional[UUID] = None,
    user_id: Optional[UUID] = None,
    device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    parser: Optional[BaseStatementParser] = None,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Synchronously orchestrates Statement PDF upload, parsing, deterministic reconciliation,
    and temporary file zero-retention lifecycle.
    """
    # 1. Validate Account existence, household membership, active status, and account type
    account = accounts_repo.get_account(conn, account_id)
    if not account or account["household_id"] != household_id:
        raise AccountResourceNotFoundError(account_id)

    if account["status"] != "active":
        raise AccountInactiveError(account_id)

    if account["account_type"] == "investment":
        raise AccountTypeMismatchError("Investment statements are not supported in Phase 7.")

    # 2. Validate PDF file format
    if not file_bytes or not (file_bytes.startswith(b"%PDF-") or (len(file_bytes) > 4 and b"%PDF" in file_bytes[:1024])):
        raise StatementParseFailedError("Uploaded file is not a valid PDF document.")

    # 3. Secure Temporary File with guaranteed zero-retention deletion
    temp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    temp_path = temp_file.name
    try:
        temp_file.write(file_bytes)
        temp_file.close()

        # 4. Extract statement data via parser (Gemini or Mock)
        active_parser = parser or GeminiStatementParser()
        account_context = {
            "name": account["name"],
            "institution": account.get("institution"),
            "currency": account["currency"],
            "account_type": account["account_type"]
        }

        try:
            extraction = active_parser.extract_statement(
                pdf_path=temp_path,
                password=password,
                account_context=account_context
            )
        except Exception as pe:
            logger.warning(f"Statement extraction failed for account {account_id}: {pe}")
            raise pe

        # 5. Deterministic post-extraction validation and normalization
        auth_bal, stmt_bal, curr_out, unbilled_bal, p_start, p_end, norm_lines = validate_and_normalize_extraction(
            extraction=extraction,
            account=account,
            caller_period_start=period_start,
            caller_period_end=period_end
        )

        # Build workflow-only snapshot candidate payload for credit card accounts
        cc_snapshot_payload: Optional[Dict[str, Any]] = None
        if account.get("account_type") == "credit":
            rem_due = None
            if extraction.remaining_statement_due is not None:
                rem_due = quantize_money(extraction.remaining_statement_due, account["currency"])
                if rem_due < Decimal("0.00"):
                    raise StatementParseFailedError("Remaining statement due must be non-negative.")

            has_cc_facts = (stmt_bal is not None or rem_due is not None or unbilled_bal is not None or curr_out is not None)
            if has_cc_facts:
                cc_data: Dict[str, Any] = {}
                if extraction.statement_date is not None:
                    cc_data["statement_date"] = extraction.statement_date.isoformat()
                if p_start is not None:
                    cc_data["statement_period_start"] = p_start.isoformat()
                if p_end is not None:
                    cc_data["statement_period_end"] = p_end.isoformat()
                if stmt_bal is not None:
                    cc_data["statement_balance"] = str(stmt_bal)
                if rem_due is not None:
                    cc_data["remaining_statement_due"] = str(rem_due)
                if unbilled_bal is not None:
                    cc_data["unbilled_balance"] = str(unbilled_bal)
                if curr_out is not None:
                    cc_data["current_outstanding"] = str(curr_out)
                cc_data["currency"] = account["currency"]
                cc_snapshot_payload = {"credit_card_snapshot": cc_data}

        # 6. Execute deterministic reconciliation engine and persist batch
        batch_record = create_statement_reconciliation_batch(
            conn=conn,
            household_id=household_id,
            account_id=account_id,
            lines=norm_lines,
            authoritative_balance=auth_bal,
            statement_balance=stmt_bal,
            current_outstanding=curr_out,
            unbilled_balance=unbilled_bal,
            period_start=p_start,
            period_end=p_end,
            source_request_id=source_request_id,
            user_id=user_id,
            default_expense_category_id=default_expense_category_id,
            default_income_category_id=default_income_category_id,
            household_movements=None,
            fx_service=fx_service,
            credit_card_snapshot_payload=cc_snapshot_payload
        )

        # Update parser_version on batch record
        b_id_str = batch_record["batch_id"]
        b_id = UUID(b_id_str)
        p_version = getattr(active_parser, "version", "gemini-statement-v1.0")

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reconciliation_batches SET parser_version = %s WHERE id = %s RETURNING row_version;",
                (p_version, b_id)
            )
            row = cur.fetchone()
            current_row_ver = row[0] if row else 0

        # 7. Audit statement upload event (NEVER include PDF password)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device" if device_id else "user",
            actor_user_id=user_id,
            actor_device_id=device_id,
            request_id=source_request_id,
            reconciliation_batch_id=b_id,
            entity_type="reconciliation_batch",
            entity_id=b_id,
            action="create",
            after_data={
                "account_id": str(account_id),
                "batch_type": "statement",
                "line_count": len(norm_lines),
                "matched_count": batch_record["matched_count"],
                "created_count": batch_record["created_count"],
                "pending_count": batch_record["pending_count"],
                "status": batch_record["status"]
            }
        )

        return {
            "status": batch_record["status"],
            "batch_id": b_id_str,
            "account_id": str(account_id),
            "batch_type": "statement",
            "currency": account["currency"],
            "row_version": current_row_ver,
            "summary": {
                "line_count": len(norm_lines),
                "matched_count": batch_record["matched_count"],
                "created_count": batch_record["created_count"],
                "pending_count": batch_record["pending_count"],
                "residual_amount": str(batch_record["residual_amount"]) if batch_record.get("residual_amount") is not None and batch_record["residual_amount"] != "None" else None,
                "currency": account["currency"]
            }
        }

    finally:
        # Zero-retention policy: remove temporary PDF file immediately on success or failure
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception as e:
                logger.warning(f"Failed to delete temp PDF file {temp_path}: {e}")


def get_statement_batch_summary(conn, batch_id: UUID, household_id: UUID) -> Dict[str, Any]:
    """
    Retrieves workflow summary for a statement reconciliation batch.
    """
    batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(batch_id)

    curr = batch["currency"]
    lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
    candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)

    pending_count = sum(1 for c in candidates if c["status"] in ("proposed", "needs_review"))
    matched_count = sum(1 for c in candidates if c["candidate_type"] == "match" and c["status"] in ("accepted", "applied"))
    created_count = sum(1 for c in candidates if c["candidate_type"] in ("create_transaction", "create_transfer", "refund", "recognize_installment") and c["status"] in ("accepted", "applied"))

    res_str = str(quantize_money(batch["residual_amount"], curr)) if batch["residual_amount"] is not None else None
    adj_str = str(quantize_money(batch["adjustment_amount"], curr)) if batch["adjustment_amount"] is not None else None

    return {
        "status": batch["status"],
        "batch_id": str(batch["id"]),
        "account_id": str(batch["account_id"]),
        "batch_type": batch["batch_type"],
        "currency": curr,
        "authoritative_balance": str(quantize_money(batch["authoritative_balance"], curr)) if batch["authoritative_balance"] is not None else None,
        "statement_balance": str(quantize_money(batch["statement_balance"], curr)) if batch["statement_balance"] is not None else None,
        "current_outstanding": str(quantize_money(batch["current_outstanding"], curr)) if batch["current_outstanding"] is not None else None,
        "unbilled_balance": str(quantize_money(batch["unbilled_balance"], curr)) if batch["unbilled_balance"] is not None else None,
        "residual_amount": res_str,
        "adjustment_amount": adj_str,
        "row_version": batch["row_version"],
        "created_at": batch["created_at"].isoformat() if batch["created_at"] else None,
        "committed_at": batch["committed_at"].isoformat() if batch["committed_at"] else None,
        "summary": {
            "line_count": len(lines),
            "matched_count": matched_count,
            "created_count": created_count,
            "pending_count": pending_count,
            "residual_amount": res_str,
            "currency": curr
        }
    }


def get_statement_batch_preview(
    conn,
    batch_id: UUID,
    household_id: UUID,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Returns read-only preview for a statement reconciliation batch. Mutates zero database state.
    """
    batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(batch_id)

    curr = batch["currency"]
    household = accounts_repo.get_household(conn, household_id)
    rep_curr = household["reporting_currency"]
    candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
    lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
    line_map = {l["id"]: l for l in lines}

    # Format candidates for preview
    formatted_candidates = []
    for c in candidates:
        cand_dict = {
            "id": str(c["id"]),
            "candidate_type": c["candidate_type"],
            "status": c["status"],
            "statement_line_id": str(c["statement_line_id"]) if c.get("statement_line_id") else None,
            "target_transaction_id": str(c["target_transaction_id"]) if c.get("target_transaction_id") else None,
            "confidence": str(c["confidence"]) if c.get("confidence") is not None else None,
            "reason_code": c.get("reason_code"),
            "reason_detail": c.get("reason_detail"),
            "payload": c.get("payload")
        }
        if c.get("statement_line_id") and c["statement_line_id"] in line_map:
            sl = line_map[c["statement_line_id"]]
            cand_dict["statement_line"] = {
                "transaction_on": sl["transaction_on"].isoformat() if sl.get("transaction_on") else None,
                "posted_on": sl["posted_on"].isoformat() if sl.get("posted_on") else None,
                "description": sl["description_raw"],
                "amount": str(sl["amount"]),
                "currency": sl["currency"],
                "direction": sl["direction"],
                "line_type": sl["line_type"]
            }
        formatted_candidates.append(cand_dict)

    res_str = str(quantize_money(batch["residual_amount"], curr)) if batch["residual_amount"] is not None else None
    adj_str = str(quantize_money(batch["adjustment_amount"], curr)) if batch["adjustment_amount"] is not None else None

    return {
        "batch": {
            "id": str(batch["id"]),
            "account_id": str(batch["account_id"]),
            "batch_type": batch["batch_type"],
            "status": batch["status"],
            "currency": curr,
            "row_version": batch["row_version"],
            "period_start": batch["period_start"].isoformat() if batch.get("period_start") else None,
            "period_end": batch["period_end"].isoformat() if batch.get("period_end") else None
        },
        "authoritative_balance": str(quantize_money(batch["authoritative_balance"], curr)) if batch["authoritative_balance"] is not None else None,
        "residual_amount": res_str,
        "adjustment_amount": adj_str,
        "reporting_currency": rep_curr,
        "summary": {
            "line_count": len(lines),
            "matched_count": batch["matched_count"],
            "created_count": batch["created_count"],
            "pending_count": batch["pending_count"],
            "residual_amount": res_str,
            "currency": curr
        },
        "candidates": formatted_candidates
    }


def list_statement_lines_for_batch(
    conn,
    batch_id: UUID,
    household_id: UUID,
    match_status: Optional[str] = None,
    line_type: Optional[str] = None
) -> Dict[str, Any]:
    """
    Returns filtered statement lines for inspection.
    """
    batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(batch_id)

    raw_lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
    items = []

    for l in raw_lines:
        if match_status and l["match_status"] != match_status:
            continue
        if line_type and l["line_type"] != line_type:
            continue

        items.append({
            "id": str(l["id"]),
            "transaction_on": l["transaction_on"].isoformat() if l.get("transaction_on") else None,
            "posted_on": l["posted_on"].isoformat() if l.get("posted_on") else None,
            "description": l["description_raw"],
            "amount": str(l["amount"]),
            "currency": l["currency"],
            "direction": l["direction"],
            "line_type": l["line_type"],
            "match_status": l["match_status"],
            "matched_transaction_id": str(l["matched_transaction_id"]) if l.get("matched_transaction_id") else None
        })

    return {"items": items}


def _row_to_normalized_line(st_line_row: Dict[str, Any]) -> NormalizedStatementLine:
    return NormalizedStatementLine(
        id=st_line_row["id"],
        description_raw=st_line_row["description_raw"],
        direction=st_line_row["direction"],
        line_type=st_line_row.get("line_type", "unknown"),
        settlement_amount=parse_decimal(st_line_row.get("amount") or st_line_row.get("settlement_amount")),
        settlement_currency=st_line_row.get("currency") or st_line_row.get("settlement_currency"),
        transaction_on=st_line_row.get("transaction_on"),
        posted_on=st_line_row.get("posted_on"),
        description_normalized=st_line_row.get("description_normalized") or st_line_row["description_raw"],
        original_amount=parse_decimal(st_line_row["original_amount"]) if st_line_row.get("original_amount") is not None else None,
        original_currency=st_line_row.get("original_currency"),
        merchant_hint=st_line_row.get("merchant_hint"),
        external_reference=st_line_row.get("external_reference"),
        source_page_no=st_line_row.get("source_page_no"),
        source_row_no=st_line_row.get("source_row_no") or 1,
        confidence=st_line_row.get("confidence")
    )


def validate_candidate_payload_for_type(
    conn,
    candidate_type: str,
    merged_payload: Dict[str, Any],
    account: Dict[str, Any],
    household_id: UUID,
    for_accept: bool = False,
    statement_line: Optional[Dict[str, Any]] = None
) -> None:
    """
    Validates complete merged candidate payload according to candidate type invariants.
    """
    if candidate_type == "adjustment":
        raise InvalidCandidatePayloadError("Reconciliation adjustments are engine-owned and cannot be manually modified.")

    if candidate_type in ("create_transaction", "match"):
        tx_data = merged_payload.get("transaction", merged_payload)
        tx_type = tx_data.get("transaction_type")
        
        line_dir = statement_line.get("direction") if statement_line else None
        
        if tx_type:
            if tx_type not in ("expense", "fee", "cash_income"):
                raise InvalidCandidatePayloadError(f"Unsupported transaction type '{tx_type}' for create_transaction candidate.")
        elif for_accept:
            if line_dir == "debit":
                tx_type = "expense"
            elif line_dir == "credit":
                tx_type = "cash_income"
            else:
                raise InvalidCandidatePayloadError("Cannot accept candidate with unknown transaction semantics. Please specify transaction_type.")

        amt_val = tx_data.get("amount") or (statement_line.get("settlement_amount") or statement_line.get("amount") if statement_line else None)
        if amt_val is not None:
            amt = parse_decimal(amt_val)
            if amt <= Decimal("0.00"):
                raise InvalidCandidatePayloadError("Transaction amount must be strictly positive.")
        elif for_accept:
            raise InvalidCandidatePayloadError("Transaction amount is required.")

        curr_val = tx_data.get("currency") or (statement_line.get("settlement_currency") or statement_line.get("currency") if statement_line else None)
        if curr_val and curr_val.upper() != account["currency"].upper():
            raise InvalidCandidatePayloadError(f"Transaction currency '{curr_val}' must match account currency '{account['currency']}'.")

        # Account leg validation
        if tx_type in ("expense", "fee"):
            from_acc = tx_data.get("from_account_id")
            if from_acc and str(from_acc) != str(account["id"]):
                raise InvalidCandidatePayloadError("Expense/Fee from_account_id must match the reconciled account.")
        elif tx_type == "cash_income":
            to_acc = tx_data.get("to_account_id")
            if to_acc and str(to_acc) != str(account["id"]):
                raise InvalidCandidatePayloadError("Cash income to_account_id must match the reconciled account.")

        cat_id_str = tx_data.get("category_id")
        if cat_id_str:
            try:
                cat_id = UUID(str(cat_id_str))
                cat = accounts_repo.get_category(conn, cat_id)
                if not cat or cat["household_id"] != household_id or cat["status"] != "active":
                    raise CategoryResourceNotFoundError(cat_id)
                if tx_type in ("expense", "fee") and cat["category_type"] != "expense":
                    raise InvalidCandidatePayloadError(f"Category type '{cat['category_type']}' is not valid for {tx_type} transaction.")
                if tx_type == "cash_income" and cat["category_type"] != "income":
                    raise InvalidCandidatePayloadError(f"Category type '{cat['category_type']}' is not valid for cash_income transaction.")
            except (ValueError, TypeError):
                raise InvalidCandidatePayloadError(f"Invalid category_id: {cat_id_str}")
        elif for_accept and candidate_type == "create_transaction":
            raise InvalidCandidatePayloadError(f"Active category is required to accept {tx_type or 'transaction'} candidate.")

    elif candidate_type == "create_transfer":
        tf_data = merged_payload.get("transfer", merged_payload)
        from_acc_str = tf_data.get("from_account_id")
        to_acc_str = tf_data.get("to_account_id")
        from_amt_val = tf_data.get("from_amount")
        to_amt_val = tf_data.get("to_amount")
        from_curr_val = tf_data.get("from_currency")
        to_curr_val = tf_data.get("to_currency")

        if for_accept:
            if not from_acc_str or not to_acc_str:
                raise InvalidCandidatePayloadError("Transfer requires both from_account_id and to_account_id.")
            if from_amt_val is None or to_amt_val is None:
                raise InvalidCandidatePayloadError("Transfer requires both from_amount and to_amount.")
            if not from_curr_val or not to_curr_val:
                raise InvalidCandidatePayloadError("Transfer requires both from_currency and to_currency.")

        from_acc = None
        to_acc = None
        if from_acc_str:
            try:
                from_acc = accounts_repo.get_account(conn, UUID(str(from_acc_str)))
                if not from_acc or from_acc["household_id"] != household_id or from_acc["status"] != "active":
                    raise AccountResourceNotFoundError(UUID(str(from_acc_str)))
            except (ValueError, TypeError):
                raise InvalidCandidatePayloadError(f"Invalid from_account_id: {from_acc_str}")

        if to_acc_str:
            try:
                to_acc = accounts_repo.get_account(conn, UUID(str(to_acc_str)))
                if not to_acc or to_acc["household_id"] != household_id or to_acc["status"] != "active":
                    raise AccountResourceNotFoundError(UUID(str(to_acc_str)))
            except (ValueError, TypeError):
                raise InvalidCandidatePayloadError(f"Invalid to_account_id: {to_acc_str}")

        if from_acc_str and to_acc_str:
            if str(from_acc_str) == str(to_acc_str):
                raise SameAccountTransferError("Transfer source and destination must be different.")
            if str(from_acc["id"]) != str(account["id"]) and str(to_acc["id"]) != str(account["id"]):
                raise InvalidCandidatePayloadError("One leg of the transfer must belong to the reconciled account.")

        if from_amt_val is not None:
            if parse_decimal(from_amt_val) <= Decimal("0.00"):
                raise InvalidCandidatePayloadError("Transfer from_amount must be strictly positive.")
        if to_amt_val is not None:
            if parse_decimal(to_amt_val) <= Decimal("0.00"):
                raise InvalidCandidatePayloadError("Transfer to_amount must be strictly positive.")

        if from_acc and from_curr_val:
            if str(from_curr_val).strip().upper() != from_acc["currency"].upper():
                raise InvalidCandidatePayloadError(f"from_currency '{from_curr_val}' must match from_account currency '{from_acc['currency']}'.")
        if to_acc and to_curr_val:
            if str(to_curr_val).strip().upper() != to_acc["currency"].upper():
                raise InvalidCandidatePayloadError(f"to_currency '{to_curr_val}' must match to_account currency '{to_acc['currency']}'.")

    elif candidate_type == "refund":
        rf_data = merged_payload.get("refund", merged_payload)
        orig_expense_id_str = rf_data.get("original_expense_id") or rf_data.get("original_transaction_id") or rf_data.get("target_transaction_id")
        if orig_expense_id_str:
            try:
                orig_tx = tx_repo.get_transaction(conn, UUID(str(orig_expense_id_str)))
            except (ValueError, TypeError):
                raise InvalidCandidatePayloadError(f"Invalid original_expense_id: {orig_expense_id_str}")

            if not orig_tx or orig_tx["household_id"] != household_id:
                raise IncompatibleTargetTransactionError("Original expense transaction not found in household.")
            if orig_tx["status"] != "committed" or orig_tx.get("deleted_at") is not None:
                raise IncompatibleTargetTransactionError("Original expense transaction is not active.")
            if orig_tx["transaction_type"] != "expense":
                raise IncompatibleTargetTransactionError("Refund target must be an expense transaction.")
            if orig_tx.get("from_account_id") and str(orig_tx["from_account_id"]) != str(account["id"]):
                raise IncompatibleTargetTransactionError("Original expense does not belong to the reconciled account.")

            amt_val = rf_data.get("amount") or (statement_line.get("settlement_amount") or statement_line.get("amount") if statement_line else None)
            if amt_val is not None:
                rf_amt = parse_decimal(amt_val)
                if rf_amt <= Decimal("0.00"):
                    raise InvalidCandidatePayloadError("Refund amount must be strictly positive.")

                rf_curr = rf_data.get("currency") or (statement_line.get("settlement_currency") or statement_line.get("currency") if statement_line else None)
                if rf_curr and orig_tx.get("from_currency") and str(rf_curr).strip().upper() != orig_tx["from_currency"].upper():
                    raise InvalidCandidatePayloadError(f"Refund currency '{rf_curr}' must match original expense currency '{orig_tx['from_currency']}'.")

                existing_refunds = tx_repo.get_active_refunds_for_expense(conn, orig_tx["id"])
                total_refunded = sum(parse_decimal(r.get("from_amount") or r.get("to_amount") or r.get("original_amount")) for r in existing_refunds)
                remaining = parse_decimal(orig_tx["from_amount"]) - total_refunded
                if rf_amt > remaining:
                    raise InvalidCandidatePayloadError(f"Refund amount {rf_amt} exceeds remaining refundable amount {remaining}.")
        elif for_accept:
            raise InvalidCandidatePayloadError("Refund candidate requires a valid original_expense_id.")

    elif candidate_type == "recognize_installment":
        inst_data = merged_payload.get("installment", merged_payload)
        plan_id_str = inst_data.get("plan_id")
        period_id_str = inst_data.get("period_id")
        period_no = inst_data.get("period_no") if inst_data.get("period_no") is not None else inst_data.get("period_number")

        if plan_id_str:
            try:
                plan = installments_repo.get_installment_plan(conn, UUID(str(plan_id_str)))
            except (ValueError, TypeError):
                raise InvalidCandidatePayloadError(f"Invalid plan_id: {plan_id_str}")

            if not plan or plan["household_id"] != household_id:
                raise InvalidCandidatePayloadError("Installment plan not found in household.")
            if plan["credit_account_id"] != account["id"]:
                raise InvalidCandidatePayloadError("Installment plan does not match the reconciled account.")
            if plan["status"] not in ("pending_first_bill", "active"):
                raise InvalidCandidatePayloadError(f"Installment plan status '{plan['status']}' is not eligible for period recognition.")

            periods = installments_repo.list_periods_for_plan(conn, plan["id"])
            target_period = None
            if period_id_str:
                try:
                    p_uuid = UUID(str(period_id_str))
                    target_period = next((p for p in periods if p["id"] == p_uuid), None)
                except (ValueError, TypeError):
                    raise InvalidCandidatePayloadError(f"Invalid period_id: {period_id_str}")
                if not target_period:
                    raise InvalidCandidatePayloadError(f"Installment period_id {period_id_str} not found in plan.")

            if period_no is not None:
                p_by_no = next((p for p in periods if p["period_no"] == int(period_no)), None)
                if not p_by_no:
                    raise InvalidCandidatePayloadError(f"Installment period_no {period_no} not found in plan.")
                if target_period and target_period["id"] != p_by_no["id"]:
                    raise InvalidCandidatePayloadError("Installment period_id and period_no do not match.")
                target_period = p_by_no

            if target_period:
                if target_period["status"] != "scheduled":
                    raise InvalidCandidatePayloadError(f"Installment period {target_period['period_no']} is not scheduled (current status: {target_period['status']}).")

                sched_amt = inst_data.get("scheduled_amount")
                if sched_amt is not None:
                    if parse_decimal(sched_amt) != parse_decimal(target_period["scheduled_amount"]):
                        raise InvalidCandidatePayloadError("Installment scheduled_amount does not match period scheduled_amount.")

                inst_curr = inst_data.get("currency")
                if inst_curr and str(inst_curr).strip().upper() != target_period["currency"].upper():
                    raise InvalidCandidatePayloadError("Installment currency does not match period currency.")
            elif for_accept:
                raise InvalidCandidatePayloadError("Installment candidate requires period_id or period_no.")

            cat_id_str = inst_data.get("category_id")
            if cat_id_str:
                try:
                    cat_id = UUID(str(cat_id_str))
                    cat = accounts_repo.get_category(conn, cat_id)
                    if not cat or cat["household_id"] != household_id or cat["status"] != "active":
                        raise CategoryResourceNotFoundError(cat_id)
                    if cat["category_type"] != "expense":
                        raise InvalidCandidatePayloadError("Installment category must be an expense category.")
                except (ValueError, TypeError):
                    raise InvalidCandidatePayloadError(f"Invalid category_id: {cat_id_str}")
            elif for_accept:
                raise InvalidCandidatePayloadError("Active category is required to accept installment candidate.")
        elif for_accept:
            raise InvalidCandidatePayloadError("Installment candidate requires plan_id.")


def accept_candidate(
    conn,
    candidate_id: UUID,
    household_id: UUID,
    user_id: Optional[UUID] = None,
    target_transaction_id: Optional[UUID] = None,
    device_id: Optional[UUID] = None,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Accepts a reconciliation candidate proposal.
    Optionally binds an explicit target_transaction_id.
    Recomputes batch statistics and readiness.
    """
    # 1. Look up candidate
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, batch_id, statement_line_id, candidate_type, status,
                   target_transaction_id, payload, confidence, reason_code, reason_detail
            FROM reconciliation_candidates
            WHERE id = %s
            FOR UPDATE;
            """,
            (candidate_id,)
        )
        cand_row = cur.fetchone()
        if not cand_row:
            raise CandidateResourceNotFoundError(candidate_id)

    c_id, b_id, sl_id, c_type, c_status, current_target_tx, payload, conf, r_code, r_detail = cand_row

    # 2. Lock batch and verify household
    batch = reconciliation_repo.lock_reconciliation_batch(conn, b_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(b_id)

    if batch["status"] in ("committed", "rejected", "failed"):
        raise InvalidCandidateStateError(f"Cannot accept candidate on '{batch['status']}' batch.")

    st_line_row = reconciliation_repo.get_statement_line(conn, sl_id) if sl_id else None
    account = accounts_repo.get_account(conn, batch["account_id"])

    selected_target_tx = None
    if target_transaction_id or (c_type == "match" and current_target_tx):
        selected_target_tx = target_transaction_id or current_target_tx
        target_tx = tx_repo.get_transaction(conn, selected_target_tx)
        if not target_tx or target_tx["household_id"] != household_id:
            raise IncompatibleTargetTransactionError("Target transaction does not exist or does not belong to household.")
        if target_tx.get("deleted_at") is not None:
            raise IncompatibleTargetTransactionError("Target transaction is deleted.")
        if target_tx["status"] != "committed":
            raise IncompatibleTargetTransactionError("Target transaction is not committed.")

        # Ensure target transaction involves the batch account and satisfies deterministic compatibility
        if st_line_row:
            norm_line = _row_to_normalized_line(st_line_row)
            validate_target_match_compatibility(norm_line, target_tx, batch["account_id"])

        c_type = "match"
    else:
        # Validate that candidate payload has sufficient deterministic facts to be accepted
        validate_candidate_payload_for_type(
            conn=conn,
            candidate_type=c_type,
            merged_payload=payload or {},
            account=account,
            household_id=household_id,
            for_accept=True,
            statement_line=st_line_row
        )

    # 4. Update candidate status to 'accepted'
    reconciliation_repo.update_candidate_status(
        conn=conn,
        candidate_id=candidate_id,
        status="accepted",
        resolved_by_user_id=user_id
    )

    if selected_target_tx:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reconciliation_candidates SET target_transaction_id = %s, candidate_type = %s WHERE id = %s;",
                (selected_target_tx, c_type, candidate_id)
            )

    # 5. Update statement line match_status
    if sl_id:
        new_m_status = "matched" if (c_type == "match" or selected_target_tx) else "new_candidate"
        reconciliation_repo.update_statement_line_status(
            conn=conn,
            line_id=sl_id,
            match_status=new_m_status,
            matched_transaction_id=selected_target_tx
        )

    # 6. Audit candidate acceptance
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        reconciliation_batch_id=b_id,
        entity_type="reconciliation_candidate",
        entity_id=candidate_id,
        action="confirm",
        after_data={
            "candidate_type": c_type,
            "status": "accepted",
            "target_transaction_id": str(selected_target_tx) if selected_target_tx else None
        }
    )

    # 7. Recompute batch summary & readiness
    recompute_statement_batch_after_review(conn, b_id, household_id, fx_service=fx_service)

    return get_statement_batch_summary(conn, b_id, household_id)


def patch_candidate(
    conn,
    candidate_id: UUID,
    household_id: UUID,
    payload: Dict[str, Any],
    user_id: Optional[UUID] = None,
    device_id: Optional[UUID] = None,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Edits a candidate payload (e.g. assigning category, counter-account, or adjusting amounts).
    Revalidates domain constraints and recomputes batch state.
    """
    # 1. Look up candidate
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, batch_id, statement_line_id, candidate_type, status,
                   target_transaction_id, payload, confidence, reason_code, reason_detail
            FROM reconciliation_candidates
            WHERE id = %s
            FOR UPDATE;
            """,
            (candidate_id,)
        )
        cand_row = cur.fetchone()
        if not cand_row:
            raise CandidateResourceNotFoundError(candidate_id)

    c_id, b_id, sl_id, c_type, c_status, current_target_tx, old_payload, conf, r_code, r_detail = cand_row

    # 2. Lock batch
    batch = reconciliation_repo.lock_reconciliation_batch(conn, b_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(b_id)

    if batch["status"] in ("committed", "rejected", "failed"):
        raise InvalidCandidateStateError(f"Cannot edit candidate on '{batch['status']}' batch.")

    # 3. Validate new payload based on candidate_type
    if not isinstance(payload, dict):
        raise InvalidCandidatePayloadError("Payload must be a JSON object.")

    account = accounts_repo.get_account(conn, batch["account_id"])
    st_line_row = reconciliation_repo.get_statement_line(conn, sl_id) if sl_id else None

    # 4. Merge payload preserving evidence and sub-dictionaries
    merged_payload = dict(old_payload if isinstance(old_payload, dict) else {})
    for k, v in payload.items():
        if isinstance(v, dict) and isinstance(merged_payload.get(k), dict):
            merged_sub = dict(merged_payload[k])
            merged_sub.update(v)
            merged_payload[k] = merged_sub
        else:
            merged_payload[k] = v

    validate_candidate_payload_for_type(
        conn=conn,
        candidate_type=c_type,
        merged_payload=merged_payload,
        account=account,
        household_id=household_id,
        for_accept=False,
        statement_line=st_line_row
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reconciliation_candidates
            SET payload = %s::jsonb,
                updated_at = now()
            WHERE id = %s;
            """,
            (json.dumps(merged_payload), candidate_id)
        )

    # 5. Audit candidate edit
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        reconciliation_batch_id=b_id,
        entity_type="reconciliation_candidate",
        entity_id=candidate_id,
        action="update",
        after_data={"payload": merged_payload}
    )

    # 6. Recompute batch
    recompute_statement_batch_after_review(conn, b_id, household_id, fx_service=fx_service)

    return get_statement_batch_summary(conn, b_id, household_id)


def reject_candidate(
    conn,
    candidate_id: UUID,
    household_id: UUID,
    reason: Optional[str] = None,
    user_id: Optional[UUID] = None,
    device_id: Optional[UUID] = None,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Rejects a reconciliation candidate proposal.
    Marks statement line 'ignored' while retaining statement evidence.
    Recomputes batch summary & readiness.
    """
    # 1. Look up candidate
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, batch_id, statement_line_id, candidate_type, status,
                   target_transaction_id, payload, confidence, reason_code, reason_detail
            FROM reconciliation_candidates
            WHERE id = %s
            FOR UPDATE;
            """,
            (candidate_id,)
        )
        cand_row = cur.fetchone()
        if not cand_row:
            raise CandidateResourceNotFoundError(candidate_id)

    c_id, b_id, sl_id, c_type, c_status, current_target_tx, payload, conf, r_code, r_detail = cand_row

    # 2. Lock batch
    batch = reconciliation_repo.lock_reconciliation_batch(conn, b_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(b_id)

    if batch["status"] in ("committed", "rejected", "failed"):
        raise InvalidCandidateStateError(f"Cannot reject candidate on '{batch['status']}' batch.")

    # 3. Mark candidate rejected
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reconciliation_candidates
            SET status = 'rejected',
                reason_detail = COALESCE(%s, reason_detail),
                resolved_by_user_id = %s,
                resolved_at = now(),
                updated_at = now()
            WHERE id = %s;
            """,
            (reason, user_id, candidate_id)
        )

    # 4. Mark statement line ignored if applicable
    if sl_id:
        reconciliation_repo.update_statement_line_status(
            conn=conn,
            line_id=sl_id,
            match_status="ignored",
            matched_transaction_id=None
        )

    # 5. Audit rejection
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        reconciliation_batch_id=b_id,
        entity_type="reconciliation_candidate",
        entity_id=candidate_id,
        action="reject",
        after_data={
            "candidate_id": str(candidate_id),
            "reason": reason
        }
    )

    # 6. Recompute batch
    recompute_statement_batch_after_review(conn, b_id, household_id, fx_service=fx_service)

    return get_statement_batch_summary(conn, b_id, household_id)


def recompute_statement_batch_after_review(
    conn,
    batch_id: UUID,
    household_id: UUID,
    fx_service: Optional[ReferenceFxService] = None
) -> None:
    """
    Re-evaluates batch status, counts, and residual after candidate mutations.
    Increments row_version.
    """
    batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != household_id:
        return

    curr = batch["currency"]
    candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)

    pending_count = sum(1 for c in candidates if c["status"] in ("proposed", "needs_review"))
    matched_count = sum(1 for c in candidates if c["candidate_type"] == "match" and c["status"] in ("accepted", "applied"))
    created_count = sum(1 for c in candidates if c["candidate_type"] in ("create_transaction", "create_transfer", "refund", "recognize_installment") and c["status"] in ("accepted", "applied"))

    account = accounts_repo.get_account(conn, batch["account_id"])
    is_credit = account and account.get("account_type") == "credit"
    cycle_ok = None
    if is_credit and batch.get("statement_balance") is not None:
        db_lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
        cycle_ok = evaluate_credit_card_statement_cycle(db_lines, batch["statement_balance"], curr)

    existing_adj = next((c for c in candidates if c["candidate_type"] == "adjustment"), None)

    # If authoritative balance is absent, residual remains None and no adjustment candidate is created
    if batch.get("authoritative_balance") is None:
        batch_status = "needs_review" if (pending_count > 0 or cycle_ok is False) else "ready"
        if existing_adj:
            reconciliation_repo.update_reconciliation_candidate_full(
                conn=conn,
                candidate_id=existing_adj["id"],
                candidate_type="adjustment",
                status="rejected",
                payload={"adjustment_amount": "0.00", "currency": curr},
                reason_code="RESIDUAL_EXPLAINED",
                reason_detail="Authoritative balance is absent or cycle contradiction exists."
            )
        reconciliation_repo.update_reconciliation_batch_stats(
            conn=conn,
            batch_id=batch_id,
            status=batch_status,
            matched_count=matched_count,
            created_count=created_count,
            pending_count=pending_count,
            residual_amount=None,
            adjustment_amount=None
        )
        return

    # If authoritative balance is present, calculate residual simulation
    as_of_date = batch.get("period_end") or (batch["created_at"].date() if batch.get("created_at") else date.today())
    as_of_dt = datetime.combine(as_of_date, datetime.max.time(), tzinfo=timezone.utc)
    baseline_bal = ledger_balance_as_of(conn, batch["account_id"], as_of_dt)

    cand_objs: List[CandidateProposal] = []
    for c in candidates:
        if c["candidate_type"] == "adjustment":
            continue
        cand_objs.append(CandidateProposal(
            id=c["id"],
            candidate_type=c["candidate_type"],
            status=c["status"],
            statement_line_id=c.get("statement_line_id"),
            target_transaction_id=c.get("target_transaction_id"),
            payload=c.get("payload") or {}
        ))

    fx_srv = fx_service or ReferenceFxService()
    fx_rate_cny = fx_srv.get_rate(curr, "CNY", as_of=as_of_date) if curr != "CNY" else Decimal("1.00")

    batch_status, residual, adj_cand = evaluate_residual_and_batch_readiness(
        baseline_projected_balance=baseline_bal,
        authoritative_balance=batch["authoritative_balance"],
        candidates=cand_objs,
        account_id=batch["account_id"],
        account_currency=curr,
        fx_rate_to_cny=fx_rate_cny
    )

    if cycle_ok is False:
        batch_status = "needs_review"
        adj_cand = None

    existing_adj = next((c for c in candidates if c["candidate_type"] == "adjustment"), None)
    if adj_cand is not None:
        if existing_adj:
            reconciliation_repo.update_reconciliation_candidate_full(
                conn=conn,
                candidate_id=existing_adj["id"],
                candidate_type="adjustment",
                status=adj_cand.status,
                payload=adj_cand.payload,
                confidence=adj_cand.confidence,
                reason_code=adj_cand.reason_code,
                reason_detail=adj_cand.reason_detail
            )
        else:
            reconciliation_repo.create_reconciliation_candidate(
                conn=conn,
                candidate_id=adj_cand.id,
                batch_id=batch_id,
                statement_line_id=None,
                candidate_type="adjustment",
                status=adj_cand.status,
                payload=adj_cand.payload,
                confidence=adj_cand.confidence,
                reason_code=adj_cand.reason_code,
                reason_detail=adj_cand.reason_detail
            )
    else:
        if existing_adj:
            # Stale adjustment candidate must be marked rejected/cleared
            reconciliation_repo.update_reconciliation_candidate_full(
                conn=conn,
                candidate_id=existing_adj["id"],
                candidate_type="adjustment",
                status="rejected",
                payload={"adjustment_amount": "0.00", "currency": curr},
                reason_code="RESIDUAL_EXPLAINED",
                reason_detail="Residual is zero or explained by other evidence."
            )

    adj_amount = parse_decimal(adj_cand.payload["adjustment_amount"]) if (adj_cand and adj_cand.status == "accepted") else None

    reconciliation_repo.update_reconciliation_batch_stats(
        conn=conn,
        batch_id=batch_id,
        status=batch_status,
        matched_count=matched_count,
        created_count=created_count,
        pending_count=pending_count,
        residual_amount=residual,
        adjustment_amount=adj_amount
    )
