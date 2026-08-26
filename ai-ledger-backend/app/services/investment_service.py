import os
import hashlib
import json
import logging
import tempfile
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID, uuid4

from app.domain.money import quantize_money, parse_decimal, validate_currency_code
from app.domain.investments import (
    InvestmentCapitalFlow,
    InvestmentStatementExtractionResult,
    calculate_investment_pnl
)
from app.domain.transactions import (
    AccountResourceNotFoundError,
    AccountInactiveError,
    AccountTypeMismatchError,
    InvalidSnapshotError,
    IdempotencyKeyReuseError,
    StatementParseFailedError,
    InvalidRequestStateError
)
import app.repositories.accounts as accounts_repo
import app.repositories.snapshots as snapshots_repo
import app.repositories.investments as investments_repo
import app.repositories.ingestion as ingestion_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo
import app.repositories.audit as audit_repo
from app.services.statement_parser import (
    BaseStatementParser,
    GeminiStatementParser,
    validate_and_normalize_investment_extraction
)
from app.services.reference_fx_service import ReferenceFxService

logger = logging.getLogger(__name__)


def create_manual_investment_snapshot(
    conn,
    household_id: UUID,
    account_id: UUID,
    payload: Dict[str, Any],
    user_id: Optional[UUID] = None,
    device_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Creates an authoritative investment valuation snapshot for an investment account.
    If no previous valuation snapshot exists, establishes the baseline with no P&L.
    If a previous valuation snapshot exists, computes P&L = closing - opening - contributions + withdrawals
    using known committed transfers.
    Updates account_state projection exactly once without creating synthetic Transactions.
    """
    # 1. Validate Account existence, household membership, active status, and account type
    account = accounts_repo.get_account(conn, account_id)
    if not account or account["household_id"] != household_id:
        raise AccountResourceNotFoundError(account_id)

    if account["status"] != "active":
        raise AccountInactiveError(account_id)

    if account["account_type"] != "investment":
        raise AccountTypeMismatchError(f"Account {account_id} is not an investment account (type: {account['account_type']}).")

    # 2. Validate Idempotency Key (Required for device auth)
    idempotency_key = payload.get("idempotency_key")
    if not idempotency_key or not isinstance(idempotency_key, str) or not (8 <= len(idempotency_key) <= 200):
        raise InvalidSnapshotError("Idempotency key is required and must be between 8 and 200 characters.")

    # 3. Validate as_of, total_asset_value, currency, source
    as_of_str = payload.get("as_of")
    if not as_of_str:
        raise InvalidSnapshotError("Missing required 'as_of' timestamp.")
    try:
        as_of = datetime.fromisoformat(as_of_str)
    except Exception:
        raise InvalidSnapshotError("Invalid ISO 'as_of' timestamp format.")

    if as_of.tzinfo is None:
        raise InvalidSnapshotError("'as_of' timestamp must be timezone-aware.")

    raw_val = payload.get("total_asset_value")
    if raw_val is None:
        raise InvalidSnapshotError("Missing required 'total_asset_value'.")
    total_asset_val = parse_decimal(raw_val)
    if total_asset_val < Decimal("0.00"):
        raise InvalidSnapshotError("Total asset value must be non-negative.")

    account_currency = account["currency"].upper()
    req_curr = payload.get("currency")
    if not req_curr or str(req_curr).strip().upper() != account_currency:
        raise InvalidSnapshotError(f"Snapshot currency '{req_curr}' must match investment account currency '{account_currency}'.")

    source = payload.get("source", "dashboard_manual")
    if source not in ("dashboard_manual", "shortcut", "statement"):
        raise InvalidSnapshotError(f"Invalid snapshot source: {source}")

    quantized_val = quantize_money(total_asset_val, account_currency)

    # 4. Check Idempotency via ingestion_requests using PostgreSQL uniqueness (Clarification A)
    req_canon = {
        "account_id": str(account_id),
        "as_of": as_of.isoformat(),
        "total_asset_value": f"{quantized_val:.6f}",
        "currency": account_currency,
        "source": source
    }
    req_hash = hashlib.sha256(json.dumps(req_canon, sort_keys=True).encode("utf-8")).digest()

    request_id = uuid4()
    if device_id:
        created = ingestion_repo.create_ingestion_request(
            conn=conn,
            request_id=request_id,
            device_id=device_id,
            idempotency_key=idempotency_key,
            request_kind="snapshot",
            request_hash=req_hash,
            status="processing"
        )
        if not created:
            # Another transaction inserted this key first -> lock/read row
            existing_req = ingestion_repo.lock_by_device_and_key(conn, device_id, idempotency_key)
            if not existing_req:
                existing_req = ingestion_repo.get_by_device_and_key(conn, device_id, idempotency_key)
            if existing_req:
                if existing_req["request_hash"] != req_hash:
                    raise IdempotencyKeyReuseError("Idempotency key reuse with different payload.")
                if existing_req["status"] == "committed" and existing_req.get("response_payload"):
                    return existing_req["response_payload"]
                if existing_req["status"] in ("received", "processing"):
                    raise InvalidRequestStateError("Request with this idempotency key is currently being processed.", retryable=True)
                if existing_req.get("response_payload"):
                    return existing_req["response_payload"]

    # 5. Lock account_state to serialize concurrent valuations on the same account
    accounts_repo.lock_account_states(conn, [account_id])

    # 6. Re-read latest authoritative investment_valuation snapshot under the lock
    opening_snap = investments_repo.get_latest_authoritative_investment_valuation_snapshot(
        conn=conn,
        household_id=household_id,
        account_id=account_id
    )

    if opening_snap is None:
        # First Snapshot Baseline: establishes authoritative valuation, no prior P&L period
        snap_id = uuid4()
        snapshots_repo.create_account_snapshot(
            conn=conn,
            snapshot_id=snap_id,
            household_id=household_id,
            account_id=account_id,
            as_of=as_of,
            balance=quantized_val,
            currency=account_currency,
            snapshot_type="investment_valuation",
            source=source,
            source_request_id=request_id if device_id else None,
            is_authoritative=True,
            created_by_user_id=user_id
        )

        accounts_repo.update_account_state_after_reconciliation(
            conn=conn,
            account_id=account_id,
            new_balance=quantized_val,
            snapshot_as_of=as_of,
            last_transaction_at=None
        )

        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device" if device_id else "user",
            actor_user_id=user_id,
            actor_device_id=device_id,
            entity_type="account_snapshot",
            entity_id=snap_id,
            action="create",
            after_data={
                "snapshot_type": "investment_valuation",
                "balance": str(quantized_val),
                "currency": account_currency,
                "as_of": as_of.isoformat()
            }
        )

        res_payload = {
            "status": "committed",
            "snapshot_id": str(snap_id),
            "investment_pnl": None
        }

        if device_id:
            ingestion_repo.update_ingestion_request_status(conn, request_id, status="committed", response_payload=res_payload)

        return res_payload

    # Semantic Replay Check (Clarification E)
    if as_of == opening_snap["as_of"] and quantized_val == opening_snap["balance"] and account_currency == opening_snap["currency"]:
        existing_pnl = investments_repo.get_investment_pnl_period_by_closing_snapshot(conn, opening_snap["id"])
        pnl_data = None
        if existing_pnl:
            pnl_data = {
                "period_id": str(existing_pnl["id"]),
                "opening_snapshot_id": str(existing_pnl["opening_snapshot_id"]),
                "closing_snapshot_id": str(existing_pnl["closing_snapshot_id"]),
                "period_start": existing_pnl["period_start"].isoformat(),
                "period_end": existing_pnl["period_end"].isoformat(),
                "opening_value": str(existing_pnl["opening_value"]) if existing_pnl.get("opening_value") is not None else None,
                "closing_value": str(existing_pnl["closing_value"]) if existing_pnl.get("closing_value") is not None else None,
                "contributions": str(existing_pnl["contributions_amount"]),
                "withdrawals": str(existing_pnl["withdrawals_amount"]),
                "pnl_amount": str(existing_pnl["pnl_amount"]),
                "currency": existing_pnl["currency"],
                "status": existing_pnl["status"]
            }
        res_payload = {
            "status": "committed",
            "snapshot_id": str(opening_snap["id"]),
            "investment_pnl": pnl_data
        }
        if device_id:
            ingestion_repo.update_ingestion_request_status(conn, request_id, status="committed", response_payload=res_payload)
        return res_payload

    # Subsequent Snapshot: Closing as_of must be strictly later than opening snapshot as_of
    if as_of <= opening_snap["as_of"]:
        raise InvalidSnapshotError(
            f"New investment snapshot as_of ({as_of.isoformat()}) must be strictly later than previous authoritative snapshot as_of ({opening_snap['as_of'].isoformat()})."
        )

    # 7. Collect known committed capital-flow transfers in (opening_snap.as_of, as_of]
    contribs, withdrws = investments_repo.get_known_committed_transfers(
        conn=conn,
        household_id=household_id,
        account_id=account_id,
        account_currency=account_currency,
        opening_as_of=opening_snap["as_of"],
        closing_as_of=as_of
    )

    contrib_total = quantize_money(sum((c["amount"] for c in contribs), Decimal("0.00")), account_currency)
    withdrw_total = quantize_money(sum((w["amount"] for w in withdrws), Decimal("0.00")), account_currency)

    # 8. Compute P&L = closing - opening - contributions + withdrawals
    pnl = calculate_investment_pnl(
        opening_value=opening_snap["balance"],
        closing_value=quantized_val,
        contributions=contrib_total,
        withdrawals=withdrw_total,
        currency=account_currency
    )

    # 9. Atomically persist closing snapshot and confirmed P&L period
    closing_snap_id = uuid4()
    snapshots_repo.create_account_snapshot(
        conn=conn,
        snapshot_id=closing_snap_id,
        household_id=household_id,
        account_id=account_id,
        as_of=as_of,
        balance=quantized_val,
        currency=account_currency,
        snapshot_type="investment_valuation",
        source=source,
        source_request_id=request_id if device_id else None,
        is_authoritative=True,
        created_by_user_id=user_id
    )

    pnl_period_id = uuid4()
    investments_repo.create_investment_pnl_period(
        conn=conn,
        period_id=pnl_period_id,
        household_id=household_id,
        account_id=account_id,
        opening_snapshot_id=opening_snap["id"],
        closing_snapshot_id=closing_snap_id,
        period_start=opening_snap["as_of"],
        period_end=as_of,
        contributions_amount=contrib_total,
        withdrawals_amount=withdrw_total,
        pnl_amount=pnl,
        currency=account_currency,
        status="confirmed",
        calculation_version=1
    )

    # Update account_state projection using authoritative reconciliation helper
    accounts_repo.update_account_state_after_reconciliation(
        conn=conn,
        account_id=account_id,
        new_balance=quantized_val,
        snapshot_as_of=as_of,
        last_transaction_at=None
    )

    # Audit events
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        entity_type="account_snapshot",
        entity_id=closing_snap_id,
        action="create",
        after_data={
            "snapshot_type": "investment_valuation",
            "balance": str(quantized_val),
            "currency": account_currency,
            "as_of": as_of.isoformat()
        }
    )

    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        entity_type="investment_pnl_period",
        entity_id=pnl_period_id,
        action="confirm",
        after_data={
            "pnl_amount": str(pnl),
            "currency": account_currency,
            "period_start": opening_snap["as_of"].isoformat(),
            "period_end": as_of.isoformat(),
            "contributions": str(contrib_total),
            "withdrawals": str(withdrw_total)
        }
    )

    res_payload = {
        "status": "committed",
        "snapshot_id": str(closing_snap_id),
        "investment_pnl": {
            "period_id": str(pnl_period_id),
            "opening_snapshot_id": str(opening_snap["id"]),
            "closing_snapshot_id": str(closing_snap_id),
            "period_start": opening_snap["as_of"].isoformat(),
            "period_end": as_of.isoformat(),
            "opening_value": str(opening_snap["balance"]),
            "closing_value": str(quantized_val),
            "contributions": str(contrib_total),
            "withdrawals": str(withdrw_total),
            "pnl_amount": str(pnl),
            "currency": account_currency,
            "status": "confirmed"
        }
    }

    if device_id:
        ingestion_repo.update_ingestion_request_status(conn, request_id, status="committed", response_payload=res_payload)

    return res_payload


def get_investment_performance(
    conn,
    household_id: UUID,
    account_id: UUID,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None
) -> Dict[str, Any]:
    """
    Retrieves chronological performance history for an investment account.
    Returns confirmed investment_pnl_periods with opening and closing valuations.
    """
    account = accounts_repo.get_account(conn, account_id)
    if not account or account["household_id"] != household_id:
        raise AccountResourceNotFoundError(account_id)

    if account["account_type"] != "investment":
        raise AccountTypeMismatchError(f"Account {account_id} is not an investment account.")

    periods = investments_repo.list_investment_pnl_periods(
        conn=conn,
        household_id=household_id,
        account_id=account_id,
        from_date=from_date,
        to_date=to_date
    )

    items = []
    for p in periods:
        p_curr = p["currency"]
        items.append({
            "period_id": str(p["id"]),
            "period_start": p["period_start"].isoformat(),
            "period_end": p["period_end"].isoformat(),
            "opening_snapshot_id": str(p["opening_snapshot_id"]),
            "closing_snapshot_id": str(p["closing_snapshot_id"]),
            "opening_value": str(quantize_money(parse_decimal(p["opening_value"]), p_curr)) if p.get("opening_value") is not None else None,
            "closing_value": str(quantize_money(parse_decimal(p["closing_value"]), p_curr)) if p.get("closing_value") is not None else None,
            "contributions": str(quantize_money(parse_decimal(p["contributions_amount"]), p_curr)),
            "withdrawals": str(quantize_money(parse_decimal(p["withdrawals_amount"]), p_curr)),
            "pnl_amount": str(quantize_money(parse_decimal(p["pnl_amount"]), p_curr)),
            "currency": p_curr,
            "status": p["status"],
            "calculation_version": p["calculation_version"]
        })

    return {
        "account_id": str(account_id),
        "currency": account["currency"],
        "periods": items
    }


def process_investment_statement(
    conn,
    household_id: UUID,
    account_id: Optional[UUID] = None,
    account: Optional[Dict[str, Any]] = None,
    file_bytes: bytes = b"",
    file_name: str = "statement.pdf",
    filename: Optional[str] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    parser: Optional[BaseStatementParser] = None,
    password: Optional[str] = None,
    user_id: Optional[UUID] = None,
    device_id: Optional[UUID] = None,
    fx_service: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Processes an investment statement PDF:
    - Extracts account-level valuation and external capital flows.
    - Validates against domain invariants (non-negative valuation, currency match).
    - Matches statement flows against known committed transfers.
    - Generates reconciliation batch and candidate proposals.
    """
    if account is None:
        if account_id is None:
            raise ValueError("Either account_id or account must be provided.")
        account = accounts_repo.get_account(conn, account_id)
    else:
        account_id = account["id"]

    if not account or account["household_id"] != household_id:
        raise AccountResourceNotFoundError(account_id)

    if account["account_type"] != "investment":
        raise AccountTypeMismatchError(f"Account {account_id} is not an investment account.")

    # 1. Parse PDF using provided parser
    stmt_parser = parser or GeminiStatementParser()
    temp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    temp_path = temp_file.name
    try:
        temp_file.write(file_bytes)
        temp_file.close()
        extraction = stmt_parser.extract_investment_statement(
            pdf_path=temp_path,
            account_context=account,
            password=password
        )
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception as e:
                logger.warning(f"Failed to delete temp PDF file {temp_path}: {e}")

    # 2. Validate and normalize extraction
    (
        total_asset_val,
        account_curr,
        val_as_of,
        p_start,
        p_end,
        op_val,
        op_as_of,
        norm_flows,
        evidence_complete
    ) = validate_and_normalize_investment_extraction(
        extraction=extraction,
        account=account,
        caller_period_start=period_start,
        caller_period_end=period_end
    )

    # 3. Lookup previous authoritative investment snapshot
    opening_snap = investments_repo.get_latest_authoritative_investment_valuation_snapshot(
        conn=conn,
        household_id=household_id,
        account_id=account_id
    )

    is_ambiguous = False
    ambiguity_reasons = []

    if not evidence_complete:
        is_ambiguous = True
        ambiguity_reasons.append("Capital-flow evidence on statement is marked incomplete or ambiguous.")

    # Check opening consistency if statement explicitly provides opening NAV
    if op_val is not None and opening_snap is not None:
        if op_as_of is not None:
            op_snap_date = opening_snap["as_of"].astimezone(timezone.utc).date() if isinstance(opening_snap["as_of"], datetime) else opening_snap["as_of"]
            if op_as_of == op_snap_date:
                if op_val != opening_snap["balance"]:
                    is_ambiguous = True
                    ambiguity_reasons.append(f"Statement opening NAV ({op_val}) contradicts existing authoritative baseline ({opening_snap['balance']}).")

    val_as_of_dt = datetime.combine(val_as_of, datetime.max.time(), tzinfo=timezone.utc)
    opening_as_of_dt = opening_snap["as_of"] if opening_snap else (
        datetime.combine(p_start, datetime.min.time(), tzinfo=timezone.utc) if p_start else val_as_of_dt
    )

    # Chronology validation (Section 9 & Clarification C)
    if opening_snap is not None:
        if opening_snap["as_of"] > val_as_of_dt:
            is_ambiguous = True
            ambiguity_reasons.append(f"Statement valuation date ({val_as_of.isoformat()}) is earlier than existing authoritative snapshot ({opening_snap['as_of'].isoformat()}). Out-of-order statement.")
        elif opening_snap["as_of"] == val_as_of_dt and opening_snap["balance"] != total_asset_val:
            is_ambiguous = True
            ambiguity_reasons.append(f"Statement ending NAV ({total_asset_val}) conflicts with existing authoritative snapshot balance ({opening_snap['balance']}) as of {val_as_of.isoformat()}.")

    # 4. Canonical capital flows derived from ledger committed transfers in period (Section 3)
    if opening_snap is not None:
        db_contribs, db_withdrws = investments_repo.get_known_committed_transfers(
            conn=conn,
            household_id=household_id,
            account_id=account_id,
            account_currency=account_curr,
            opening_as_of=opening_snap["as_of"],
            closing_as_of=val_as_of_dt
        )
    else:
        db_contribs, db_withdrws = [], []

    canonical_contrib_amt = quantize_money(sum((t["amount"] for t in db_contribs), Decimal("0.00")), account_curr)
    canonical_withdrw_amt = quantize_money(sum((t["amount"] for t in db_withdrws), Decimal("0.00")), account_curr)

    # 5. Flow matching against committed transfers (Section 4 & Clarification B)
    matched_contrib_ids: List[UUID] = []
    matched_withdrw_ids: List[UUID] = []
    matched_flow_evidence: List[Dict[str, Any]] = []
    unresolved_flows: List[Dict[str, Any]] = []

    used_transfer_ids = set()

    from app.domain.investments import is_flow_compatible_with_transfer

    for idx, flow in enumerate(norm_flows):
        if flow.direction == "contribution":
            candidates = [
                t for t in db_contribs
                if t["id"] not in used_transfer_ids
                and is_flow_compatible_with_transfer(flow, t, account_id, account_curr)
            ]

            if len(candidates) == 1:
                matched_t = candidates[0]
                matched_contrib_ids.append(matched_t["id"])
                used_transfer_ids.add(matched_t["id"])
                matched_flow_evidence.append({
                    "flow_index": idx,
                    "direction": flow.direction,
                    "amount": str(flow.amount),
                    "currency": flow.currency,
                    "occurred_on": flow.occurred_on.isoformat() if flow.occurred_on else None,
                    "posted_on": flow.posted_on.isoformat() if flow.posted_on else None,
                    "external_reference": flow.external_reference,
                    "selected_transfer_id": str(matched_t["id"])
                })
            else:
                is_ambiguous = True
                unresolved_flows.append({
                    "flow_index": idx,
                    "evidence_kind": "statement_flow",
                    "direction": flow.direction,
                    "amount": str(flow.amount),
                    "currency": flow.currency,
                    "occurred_on": flow.occurred_on.isoformat() if flow.occurred_on else None,
                    "posted_on": flow.posted_on.isoformat() if flow.posted_on else None,
                    "external_reference": flow.external_reference,
                    "candidate_transfer_ids": [str(c["id"]) for c in candidates]
                })
        elif flow.direction == "withdrawal":
            candidates = [
                t for t in db_withdrws
                if t["id"] not in used_transfer_ids
                and is_flow_compatible_with_transfer(flow, t, account_id, account_curr)
            ]

            if len(candidates) == 1:
                matched_t = candidates[0]
                matched_withdrw_ids.append(matched_t["id"])
                used_transfer_ids.add(matched_t["id"])
                matched_flow_evidence.append({
                    "flow_index": idx,
                    "direction": flow.direction,
                    "amount": str(flow.amount),
                    "currency": flow.currency,
                    "occurred_on": flow.occurred_on.isoformat() if flow.occurred_on else None,
                    "posted_on": flow.posted_on.isoformat() if flow.posted_on else None,
                    "external_reference": flow.external_reference,
                    "selected_transfer_id": str(matched_t["id"])
                })
            else:
                is_ambiguous = True
                unresolved_flows.append({
                    "flow_index": idx,
                    "evidence_kind": "statement_flow",
                    "direction": flow.direction,
                    "amount": str(flow.amount),
                    "currency": flow.currency,
                    "occurred_on": flow.occurred_on.isoformat() if flow.occurred_on else None,
                    "posted_on": flow.posted_on.isoformat() if flow.posted_on else None,
                    "external_reference": flow.external_reference,
                    "candidate_transfer_ids": [str(c["id"]) for c in candidates]
                })

    # Represent unrepresented ledger flows as unresolved workflow evidence (Item 2)
    next_idx = len(norm_flows)
    for t in db_contribs:
        if t["id"] not in used_transfer_ids:
            is_ambiguous = True
            ambiguity_reasons.append(f"Committed ledger contribution {t['id']} ({t['amount']} {t['currency']}) is unrepresented on statement.")
            unresolved_flows.append({
                "flow_index": next_idx,
                "evidence_kind": "ledger_unrepresented",
                "direction": "contribution",
                "amount": str(t["amount"]),
                "currency": t["currency"],
                "occurred_on": t["occurred_on"].isoformat() if t.get("occurred_on") else None,
                "posted_on": t["posted_on"].isoformat() if t.get("posted_on") else None,
                "external_reference": t.get("remarks"),
                "candidate_transfer_ids": [str(t["id"])]
            })
            next_idx += 1

    for t in db_withdrws:
        if t["id"] not in used_transfer_ids:
            is_ambiguous = True
            ambiguity_reasons.append(f"Committed ledger withdrawal {t['id']} ({t['amount']} {t['currency']}) is unrepresented on statement.")
            unresolved_flows.append({
                "flow_index": next_idx,
                "evidence_kind": "ledger_unrepresented",
                "direction": "withdrawal",
                "amount": str(t["amount"]),
                "currency": t["currency"],
                "occurred_on": t["occurred_on"].isoformat() if t.get("occurred_on") else None,
                "posted_on": t["posted_on"].isoformat() if t.get("posted_on") else None,
                "external_reference": t.get("remarks"),
                "candidate_transfer_ids": [str(t["id"])]
            })
            next_idx += 1

    # 6. Calculate PnL preview using CANONICAL ledger totals
    if opening_snap is not None:
        pnl_preview = calculate_investment_pnl(
            opening_value=opening_snap["balance"],
            closing_value=total_asset_val,
            contributions=canonical_contrib_amt,
            withdrawals=canonical_withdrw_amt,
            currency=account_curr
        )
    else:
        pnl_preview = None

    # 7. Create reconciliation batch and stage candidates
    batch_id = uuid4()
    batch_status = "needs_review" if is_ambiguous else "ready"

    reconciliation_repo.create_reconciliation_batch(
        conn=conn,
        batch_id=batch_id,
        household_id=household_id,
        account_id=account_id,
        period_start=p_start,
        period_end=p_end,
        authoritative_balance=total_asset_val,
        currency=account_curr,
        status=batch_status,
        batch_type="statement",
        created_by_user_id=user_id
    )

    reconciliation_repo.update_reconciliation_batch_stats(
        conn=conn,
        batch_id=batch_id,
        status=batch_status,
        matched_count=len(norm_flows) - sum(1 for uf in unresolved_flows if uf.get("evidence_kind") == "statement_flow"),
        created_count=0,
        pending_count=len(unresolved_flows) if is_ambiguous else 0,
        residual_amount=None,
        adjustment_amount=None
    )

    # Stage Candidate 1: Snapshot
    snap_cand_id = uuid4()
    snap_payload = {
        "investment_snapshot": {
            "as_of": val_as_of_dt.isoformat(),
            "total_asset_value": str(total_asset_val),
            "currency": account_curr,
            "source": "statement"
        }
    }
    reconciliation_repo.create_reconciliation_candidate(
        conn=conn,
        candidate_id=snap_cand_id,
        batch_id=batch_id,
        statement_line_id=None,
        candidate_type="snapshot",
        status="needs_review" if is_ambiguous else "accepted",
        payload=snap_payload,
        confidence=Decimal("1.00"),
        reason_code="INVESTMENT_VALUATION_SNAPSHOT",
        reason_detail=f"Extracted ending valuation {total_asset_val} {account_curr}"
    )

    # Stage Candidate 2: Investment PnL (if opening snapshot exists or needed)
    pnl_cand_id = uuid4()
    pnl_payload = {
        "investment_pnl": {
            "opening_snapshot_id": str(opening_snap["id"]) if opening_snap else None,
            "period_start": opening_as_of_dt.isoformat() if opening_as_of_dt else None,
            "period_end": val_as_of_dt.isoformat(),
            "opening_value": str(opening_snap["balance"]) if opening_snap else (str(op_val) if op_val is not None else None),
            "closing_value": str(total_asset_val),
            "contributions_amount": str(canonical_contrib_amt),
            "withdrawals_amount": str(canonical_withdrw_amt),
            "pnl_amount": str(pnl_preview) if pnl_preview is not None else None,
            "currency": account_curr,
            "matched_contribution_transfer_ids": [str(tid) for tid in matched_contrib_ids],
            "matched_withdrawal_transfer_ids": [str(tid) for tid in matched_withdrw_ids],
            "matched_flow_evidence": matched_flow_evidence,
            "unresolved_flow_evidence": unresolved_flows,
            "flow_resolutions": []
        }
    }
    reconciliation_repo.create_reconciliation_candidate(
        conn=conn,
        candidate_id=pnl_cand_id,
        batch_id=batch_id,
        statement_line_id=None,
        candidate_type="investment_pnl",
        status="needs_review" if is_ambiguous else "accepted",
        payload=pnl_payload,
        confidence=Decimal("0.70") if is_ambiguous else Decimal("1.00"),
        reason_code="AMBIGUOUS_INVESTMENT_CAPITAL_FLOW" if is_ambiguous else "INVESTMENT_PNL_CONFIRMED",
        reason_detail="; ".join(ambiguity_reasons) if is_ambiguous else "Deterministic investment P&L calculation ready for commit."
    )

    if is_ambiguous:
        return {
            "status": "needs_review",
            "batch_id": str(batch_id),
            "reason_code": "AMBIGUOUS_INVESTMENT_CAPITAL_FLOW",
            "display_summary": "投资账户较上次结算存在未匹配或模糊的资金流动。"
        }

    return {
        "status": "ready",
        "batch_id": str(batch_id),
        "matched_count": len(norm_flows),
        "created_count": 0,
        "pending_count": 0,
        "residual_amount": None,
        "adjustment_amount": None
    }


def commit_investment_statement_batch(
    conn,
    batch_id: UUID,
    batch: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    user_id: Optional[UUID] = None,
    device_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Atomically commits a ready investment statement batch:
    - Locks account_state for the investment account.
    - Re-validates snapshot candidate and investment_pnl candidate.
    - Fresh-reads full canonical ledger transfer set.
    - Re-validates all evidence and flow resolutions against the fresh canonical set.
    - Re-computes P&L from fresh canonical ledger state.
    - Inserts closing investment_valuation snapshot.
    - Inserts confirmed investment_pnl_period (if prior valuation exists).
    - Updates account_state projection.
    - Marks candidates applied, batch committed.
    - Inserts audit records.
    """
    account_id = batch["account_id"]
    household_id = batch["household_id"]
    account_curr = batch["currency"].upper()

    # 1. Lock account_state
    accounts_repo.lock_account_states(conn, [account_id])

    # 2. Extract snapshot candidate and investment_pnl candidate
    snap_cand = next((c for c in candidates if c["candidate_type"] == "snapshot" and c["status"] in ("accepted", "applied")), None)
    pnl_cand = next((c for c in candidates if c["candidate_type"] == "investment_pnl" and c["status"] in ("accepted", "applied")), None)

    if not snap_cand:
        raise ValueError("Missing accepted snapshot candidate in investment statement batch.")

    snap_payload = snap_cand.get("payload", {}).get("investment_snapshot", {})
    as_of_dt = datetime.fromisoformat(snap_payload["as_of"])
    closing_val = quantize_money(parse_decimal(snap_payload["total_asset_value"]), account_curr)

    # Re-read ABSOLUTE latest authoritative investment_valuation snapshot (Section 9)
    latest_snap = investments_repo.get_latest_authoritative_investment_valuation_snapshot(
        conn=conn,
        household_id=household_id,
        account_id=account_id
    )

    # Chronology & Conflict Validation under lock (Clarification C & E)
    if latest_snap is not None:
        if latest_snap["as_of"] > as_of_dt:
            # Out-of-order statement: transition batch to needs_review and perform zero financial writes
            reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
            return {
                "status": "needs_review",
                "batch_id": str(batch_id),
                "reason": "OUT_OF_ORDER_STATEMENT"
            }
        elif latest_snap["as_of"] == as_of_dt:
            if latest_snap["balance"] != closing_val or latest_snap["currency"] != account_curr:
                reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
                return {
                    "status": "needs_review",
                    "batch_id": str(batch_id),
                    "reason": "AUTHORITATIVE_DATA_CONFLICT"
                }
            else:
                # Semantic Replay / No-op (Clarification E)
                existing_pnl_period = investments_repo.get_investment_pnl_period_by_closing_snapshot(conn, latest_snap["id"])
                pnl_data = None
                if existing_pnl_period:
                    pnl_data = {
                        "period_id": str(existing_pnl_period["id"]),
                        "opening_snapshot_id": str(existing_pnl_period["opening_snapshot_id"]),
                        "closing_snapshot_id": str(existing_pnl_period["closing_snapshot_id"]),
                        "period_start": existing_pnl_period["period_start"].isoformat(),
                        "period_end": existing_pnl_period["period_end"].isoformat(),
                        "opening_value": str(existing_pnl_period["opening_value"]) if existing_pnl_period.get("opening_value") is not None else None,
                        "closing_value": str(existing_pnl_period["closing_value"]) if existing_pnl_period.get("closing_value") is not None else None,
                        "contributions": str(existing_pnl_period["contributions_amount"]),
                        "withdrawals": str(existing_pnl_period["withdrawals_amount"]),
                        "pnl_amount": str(existing_pnl_period["pnl_amount"]),
                        "currency": existing_pnl_period["currency"]
                    }
                for c in candidates:
                    reconciliation_repo.update_candidate_status(conn, c["id"], status="applied", resolved_by_user_id=user_id)
                reconciliation_repo.update_reconciliation_batch(
                    conn=conn,
                    batch_id=batch_id,
                    status="committed",
                    committed_at=datetime.now(timezone.utc)
                )
                return {
                    "status": "committed",
                    "batch_id": str(batch_id),
                    "snapshot_id": str(latest_snap["id"]),
                    "investment_pnl": pnl_data,
                    "replay": True
                }

    opening_snap = latest_snap

    # Fresh-read the full canonical capital-flow set under lock (Item 4)
    if opening_snap is not None:
        fresh_contribs, fresh_withdrws = investments_repo.get_known_committed_transfers(
            conn=conn,
            household_id=household_id,
            account_id=account_id,
            account_currency=account_curr,
            opening_as_of=opening_snap["as_of"],
            closing_as_of=as_of_dt
        )
    else:
        fresh_contribs, fresh_withdrws = [], []

    fresh_contrib_total = quantize_money(sum((c["amount"] for c in fresh_contribs), Decimal("0.00")), account_curr)
    fresh_withdrw_total = quantize_money(sum((w["amount"] for w in fresh_withdrws), Decimal("0.00")), account_curr)

    # Fresh commit evidence revalidation (Item 5)
    if pnl_cand:
        p_data = pnl_cand.get("payload", {}).get("investment_pnl", {})
        matched_c_ids = {UUID(str(tid)) for tid in p_data.get("matched_contribution_transfer_ids", [])}
        matched_w_ids = {UUID(str(tid)) for tid in p_data.get("matched_withdrawal_transfer_ids", [])}
        flow_resolutions = p_data.get("flow_resolutions", [])
        unresolved_flows = p_data.get("unresolved_flow_evidence", [])

        resolved_tids = {UUID(str(r["selected_transfer_id"])) for r in flow_resolutions if r.get("selected_transfer_id")}
        all_accounted_tids = matched_c_ids | matched_w_ids | resolved_tids

        fresh_c_ids = {t["id"] for t in fresh_contribs}
        fresh_w_ids = {t["id"] for t in fresh_withdrws}
        all_fresh_tids = fresh_c_ids | fresh_w_ids

        # Case B: New committed transfer appeared that is not explained / accounted for
        if not all_fresh_tids.issubset(all_accounted_tids) and len(all_fresh_tids) > len(all_accounted_tids):
            reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
            reconciliation_repo.update_candidate_status(conn, pnl_cand["id"], status="needs_review")
            return {
                "status": "needs_review",
                "batch_id": str(batch_id),
                "reason": "AMBIGUOUS_INVESTMENT_CAPITAL_FLOW"
            }

        # Case C: Previously selected transfer was voided/deleted/changed or no longer satisfies compatibility
        from app.domain.investments import is_flow_compatible_with_transfer
        fresh_tx_map = {t["id"]: t for t in fresh_contribs + fresh_withdrws}

        # Validate matched flow evidence against fresh canonical transfers using production compatibility helper
        matched_flow_evidence = p_data.get("matched_flow_evidence", [])
        for mfe in matched_flow_evidence:
            sel_tid_str = mfe.get("selected_transfer_id")
            if not sel_tid_str:
                continue
            sel_tid = UUID(str(sel_tid_str))
            if sel_tid not in fresh_tx_map:
                reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
                reconciliation_repo.update_candidate_status(conn, pnl_cand["id"], status="needs_review")
                return {
                    "status": "needs_review",
                    "batch_id": str(batch_id),
                    "reason": "AMBIGUOUS_INVESTMENT_CAPITAL_FLOW"
                }
            if not is_flow_compatible_with_transfer(mfe, fresh_tx_map[sel_tid], account_id, account_curr):
                reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
                reconciliation_repo.update_candidate_status(conn, pnl_cand["id"], status="needs_review")
                return {
                    "status": "needs_review",
                    "batch_id": str(batch_id),
                    "reason": "AMBIGUOUS_INVESTMENT_CAPITAL_FLOW"
                }

        for tid in matched_c_ids:
            if tid not in fresh_c_ids:
                reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
                reconciliation_repo.update_candidate_status(conn, pnl_cand["id"], status="needs_review")
                return {
                    "status": "needs_review",
                    "batch_id": str(batch_id),
                    "reason": "INVALID_OR_MODIFIED_MATCHED_TRANSFER"
                }

        for tid in matched_w_ids:
            if tid not in fresh_w_ids:
                reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
                reconciliation_repo.update_candidate_status(conn, pnl_cand["id"], status="needs_review")
                return {
                    "status": "needs_review",
                    "batch_id": str(batch_id),
                    "reason": "INVALID_OR_MODIFIED_MATCHED_TRANSFER"
                }

        for res in flow_resolutions:
            sel_tid_str = res.get("selected_transfer_id")
            f_idx = res.get("flow_index")
            if not sel_tid_str:
                continue
            sel_tid = UUID(str(sel_tid_str))
            if sel_tid not in fresh_tx_map:
                reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
                reconciliation_repo.update_candidate_status(conn, pnl_cand["id"], status="needs_review")
                return {
                    "status": "needs_review",
                    "batch_id": str(batch_id),
                    "reason": "INVALID_OR_MODIFIED_MATCHED_TRANSFER"
                }
            target_uf = next((uf for uf in unresolved_flows if uf.get("flow_index") == f_idx), None)
            if target_uf and not is_flow_compatible_with_transfer(target_uf, fresh_tx_map[sel_tid], account_id, account_curr):
                reconciliation_repo.update_reconciliation_batch(conn, batch_id, status="needs_review")
                reconciliation_repo.update_candidate_status(conn, pnl_cand["id"], status="needs_review")
                return {
                    "status": "needs_review",
                    "batch_id": str(batch_id),
                    "reason": "INVALID_OR_MODIFIED_MATCHED_TRANSFER"
                }

    # 3. Create closing snapshot
    closing_snap_id = uuid4()
    snapshots_repo.create_account_snapshot(
        conn=conn,
        snapshot_id=closing_snap_id,
        household_id=household_id,
        account_id=account_id,
        as_of=as_of_dt,
        balance=closing_val,
        currency=account_curr,
        snapshot_type="investment_valuation",
        source="statement",
        reconciliation_batch_id=batch_id,
        is_authoritative=True,
        created_by_user_id=user_id
    )

    # 4. If opening snapshot exists, compute P&L using fresh canonical totals and create confirmed investment_pnl_period
    pnl_period_id = None
    pnl_val = None
    if opening_snap is not None:
        pnl_val = calculate_investment_pnl(
            opening_value=opening_snap["balance"],
            closing_value=closing_val,
            contributions=fresh_contrib_total,
            withdrawals=fresh_withdrw_total,
            currency=account_curr
        )
        pnl_period_id = uuid4()
        investments_repo.create_investment_pnl_period(
            conn=conn,
            period_id=pnl_period_id,
            household_id=household_id,
            account_id=account_id,
            opening_snapshot_id=opening_snap["id"],
            closing_snapshot_id=closing_snap_id,
            period_start=opening_snap["as_of"],
            period_end=as_of_dt,
            contributions_amount=fresh_contrib_total,
            withdrawals_amount=fresh_withdrw_total,
            pnl_amount=pnl_val,
            currency=account_curr,
            status="confirmed",
            calculation_version=1,
            reconciliation_batch_id=batch_id
        )

    # 5. Update account_state projection to closing_val using authoritative reconciliation helper
    accounts_repo.update_account_state_after_reconciliation(
        conn=conn,
        account_id=account_id,
        new_balance=closing_val,
        snapshot_as_of=as_of_dt,
        last_transaction_at=None
    )

    # 6. Mark candidates applied
    for c in candidates:
        reconciliation_repo.update_candidate_status(conn, c["id"], status="applied", resolved_by_user_id=user_id)

    # 7. Mark batch committed
    reconciliation_repo.update_reconciliation_batch(
        conn=conn,
        batch_id=batch_id,
        status="committed",
        committed_at=datetime.now(timezone.utc)
    )

    # 8. Audit events
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        reconciliation_batch_id=batch_id,
        entity_type="account_snapshot",
        entity_id=closing_snap_id,
        action="create",
        after_data={
            "snapshot_type": "investment_valuation",
            "balance": str(closing_val),
            "currency": account_curr,
            "as_of": as_of_dt.isoformat()
        }
    )

    if pnl_period_id is not None:
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device" if device_id else "user",
            actor_user_id=user_id,
            actor_device_id=device_id,
            reconciliation_batch_id=batch_id,
            entity_type="investment_pnl_period",
            entity_id=pnl_period_id,
            action="confirm",
            after_data={
                "pnl_amount": str(pnl_val),
                "currency": account_curr,
                "period_start": opening_snap["as_of"].isoformat(),
                "period_end": as_of_dt.isoformat(),
                "contributions": str(fresh_contrib_total),
                "withdrawals": str(fresh_withdrw_total)
            }
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
            "closing_snapshot_id": str(closing_snap_id),
            "pnl_period_id": str(pnl_period_id) if pnl_period_id else None
        }
    )

    return {
        "status": "committed",
        "batch_id": str(batch_id),
        "snapshot_id": str(closing_snap_id),
        "investment_pnl": {
            "period_id": str(pnl_period_id) if pnl_period_id else None,
            "opening_snapshot_id": str(opening_snap["id"]) if opening_snap else None,
            "closing_snapshot_id": str(closing_snap_id),
            "period_start": opening_snap["as_of"].isoformat() if opening_snap else None,
            "period_end": as_of_dt.isoformat(),
            "opening_value": str(opening_snap["balance"]) if opening_snap else None,
            "closing_value": str(closing_val),
            "contributions": str(fresh_contrib_total),
            "withdrawals": str(fresh_withdrw_total),
            "pnl_amount": str(pnl_val) if pnl_val is not None else None,
            "currency": account_curr
        } if opening_snap is not None else None
    }
