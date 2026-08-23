import hashlib
import json
from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
from datetime import datetime, date, timezone
from decimal import Decimal

from app.domain.money import parse_decimal, validate_currency_code, quantize_money
from app.domain.transactions import (
    AccountResourceNotFoundError,
    AccountInactiveError,
    AccountTypeMismatchError,
    CurrencyMismatchError,
    IdempotencyKeyReuseError,
    BatchNotFoundError,
    BatchVersionConflictError,
    InvalidSnapshotError,
    InvalidBatchStateError
)
from app.services.reference_fx_service import ReferenceFxService
from app.services.expense_service import validate_image_payload
import app.repositories.accounts as accounts_repo
import app.repositories.transactions as tx_repo
import app.repositories.snapshots as snapshots_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.ingestion as ingestion_repo
import app.repositories.audit as audit_repo

def ledger_balance_as_of(conn, account_id: UUID, as_of_dt: datetime) -> Decimal:
    """
    Computes the deterministic historical ledger balance for an account as of a given timestamp T.
    
    Algorithm:
    1. Look up the latest authoritative account_snapshot at or before T.
    2. If found, start with that snapshot's balance as the baseline anchor,
       and add all committed transaction deltas strictly after that snapshot's date through T.
    3. If no authoritative snapshot exists, start at 0.00 and add all committed
       transaction deltas from the beginning through T.
    """
    account = accounts_repo.get_account(conn, account_id)
    if not account:
        raise AccountResourceNotFoundError(account_id)
    curr = account["currency"]
    target_date = as_of_dt.date() if isinstance(as_of_dt, datetime) else as_of_dt

    latest_snap = snapshots_repo.get_latest_authoritative_snapshot(conn, account_id, as_of_dt)

    if latest_snap:
        balance = latest_snap["balance"]
        anchor_date = latest_snap["as_of"].date()
        
        if target_date > anchor_date:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT from_account_id, to_account_id, from_amount, to_amount
                    FROM transactions
                    WHERE (from_account_id = %s OR to_account_id = %s)
                      AND status = 'committed'
                      AND occurred_on > %s
                      AND occurred_on <= %s
                    ORDER BY occurred_on ASC, created_at ASC;
                    """,
                    (account_id, account_id, anchor_date, target_date)
                )
                rows = cur.fetchall()
                for r in rows:
                    from_acc, to_acc, from_amt, to_amt = r[0], r[1], r[2], r[3]
                    if from_acc == account_id and from_amt is not None:
                        balance -= from_amt
                    if to_acc == account_id and to_amt is not None:
                        balance += to_amt
    else:
        balance = Decimal("0.00")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT from_account_id, to_account_id, from_amount, to_amount
                FROM transactions
                WHERE (from_account_id = %s OR to_account_id = %s)
                  AND status = 'committed'
                  AND occurred_on <= %s
                ORDER BY occurred_on ASC, created_at ASC;
                """,
                (account_id, account_id, target_date)
            )
            rows = cur.fetchall()
            for r in rows:
                from_acc, to_acc, from_amt, to_amt = r[0], r[1], r[2], r[3]
                if from_acc == account_id and from_amt is not None:
                    balance -= from_amt
                if to_acc == account_id and to_amt is not None:
                    balance += to_amt

    return quantize_money(balance, curr)

def recompute_account_current_balance(conn, account_id: UUID) -> Decimal:
    """
    Recomputes the current account balance projection across all time.
    Uses the latest authoritative snapshot across all time as baseline, plus any transactions after it.
    """
    account = accounts_repo.get_account(conn, account_id)
    if not account:
        raise AccountResourceNotFoundError(account_id)
    curr = account["currency"]

    latest_snap = snapshots_repo.get_latest_authoritative_snapshot(conn, account_id, as_of_dt=None)
    if latest_snap:
        bal = latest_snap["balance"]
        anchor_date = latest_snap["as_of"].date()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT from_account_id, to_account_id, from_amount, to_amount
                FROM transactions
                WHERE (from_account_id = %s OR to_account_id = %s)
                  AND status = 'committed'
                  AND occurred_on > %s
                ORDER BY occurred_on ASC, created_at ASC;
                """,
                (account_id, account_id, anchor_date)
            )
            rows = cur.fetchall()
            for r in rows:
                from_acc, to_acc, from_amt, to_amt = r[0], r[1], r[2], r[3]
                if from_acc == account_id and from_amt is not None:
                    bal -= from_amt
                if to_acc == account_id and to_amt is not None:
                    bal += to_amt
    else:
        bal = Decimal("0.00")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT from_account_id, to_account_id, from_amount, to_amount
                FROM transactions
                WHERE (from_account_id = %s OR to_account_id = %s)
                  AND status = 'committed'
                ORDER BY occurred_on ASC, created_at ASC;
                """,
                (account_id, account_id)
            )
            rows = cur.fetchall()
            for r in rows:
                from_acc, to_acc, from_amt, to_amt = r[0], r[1], r[2], r[3]
                if from_acc == account_id and from_amt is not None:
                    bal -= from_amt
                if to_acc == account_id and to_amt is not None:
                    bal += to_amt

    return quantize_money(bal, curr)

def is_first_account_observation(conn, account_id: UUID) -> bool:
    """
    Returns True if an account has neither a prior authoritative snapshot nor an opening_balance transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM account_snapshots WHERE account_id = %s AND is_authoritative = true LIMIT 1;",
            (account_id,)
        )
        if cur.fetchone():
            return False

        cur.execute(
            """
            SELECT 1 FROM transactions
            WHERE (from_account_id = %s OR to_account_id = %s)
              AND transaction_type = 'opening_balance'
              AND status = 'committed'
            LIMIT 1;
            """,
            (account_id, account_id)
        )
        if cur.fetchone():
            return False

    return True

def sum_committed_transaction_deltas(conn, account_id: UUID, start_date: date, end_date: date) -> Decimal:
    """
    Sums committed transaction deltas for an account between start_date and end_date (inclusive).
    """
    delta = Decimal("0.00")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT from_account_id, to_account_id, from_amount, to_amount
            FROM transactions
            WHERE (from_account_id = %s OR to_account_id = %s)
              AND status = 'committed'
              AND occurred_on >= %s
              AND occurred_on <= %s;
            """,
            (account_id, account_id, start_date, end_date)
        )
        rows = cur.fetchall()
        for r in rows:
            from_acc, to_acc, from_amt, to_amt = r[0], r[1], r[2], r[3]
            if from_acc == account_id and from_amt is not None:
                delta -= from_amt
            if to_acc == account_id and to_amt is not None:
                delta += to_amt
    return delta

def create_snapshot_workflow(
    conn,
    account_id: UUID,
    device: Dict[str, Any],
    payload: Dict[str, Any],
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Executes the Snapshot creation and reconciliation workflow.
    """
    household_id = device["household_id"]
    user_id = device.get("user_id")
    device_id = device.get("device_id")

    # 1. Validate Account
    account = accounts_repo.get_account(conn, account_id)
    if not account or account["household_id"] != household_id:
        raise AccountResourceNotFoundError(account_id)

    if account["status"] != "active":
        raise AccountInactiveError(account_id)

    if account["account_type"] == "investment":
        raise AccountTypeMismatchError("Generic balance snapshot cannot be applied to investment accounts.")

    # 2. Validate Idempotency Key (Required for device auth, length 8..200)
    idempotency_key = payload.get("idempotency_key")
    if not idempotency_key or not isinstance(idempotency_key, str) or not (8 <= len(idempotency_key) <= 200):
        raise InvalidSnapshotError("Idempotency key is required and must be between 8 and 200 characters.")

    # 3. Validate payload fields
    as_of_str = payload.get("as_of")
    if not as_of_str:
        raise InvalidSnapshotError("Missing required 'as_of' timestamp.")
    try:
        as_of = datetime.fromisoformat(as_of_str)
    except Exception:
        raise InvalidSnapshotError("Invalid ISO 'as_of' timestamp format.")

    if as_of.tzinfo is None:
        raise InvalidSnapshotError("'as_of' timestamp must be timezone-aware.")

    household = accounts_repo.get_household(conn, household_id)
    reporting_currency = household["reporting_currency"]
    ledger_start_date = household["ledger_start_date"]

    # Reject snapshots before household ledger_start_date
    if as_of.date() < ledger_start_date:
        raise InvalidSnapshotError(f"Snapshot as_of date ({as_of.date()}) cannot precede household ledger start date ({ledger_start_date}).")

    # Handle balance or image
    balance_val = payload.get("balance")
    if balance_val is None:
        if "image" in payload and payload["image"]:
            validate_image_payload(payload["image"])
            raise InvalidSnapshotError("Direct manual balance extraction is required in Phase 5.")
        else:
            raise InvalidSnapshotError("Missing required 'balance' field.")

    dec_balance = parse_decimal(balance_val)
    currency_val = payload.get("currency")
    if not currency_val:
        raise InvalidSnapshotError("Missing required 'currency' field.")

    curr = validate_currency_code(currency_val)
    if curr != account["currency"]:
        raise CurrencyMismatchError(f"Snapshot currency '{curr}' does not match account currency '{account['currency']}'.")

    dec_balance = quantize_money(dec_balance, curr)

    source = payload.get("source", "dashboard_manual")
    if source not in ("shortcut", "statement", "dashboard_manual"):
        raise InvalidSnapshotError(f"Invalid snapshot source: '{source}'.")

    # 4. Canonical Snapshot Request Hash (MUST include account_id and normalized content)
    canonical_dict = {
        "account_id": str(account_id),
        "as_of": as_of.isoformat(),
        "balance": str(dec_balance),
        "currency": curr,
        "source": source
    }
    canonical_raw = json.dumps(canonical_dict, sort_keys=True)
    request_hash = hashlib.sha256(canonical_raw.encode("utf-8")).digest()

    existing_req = ingestion_repo.get_by_device_and_key(conn, device_id, idempotency_key)
    if existing_req:
        if existing_req["request_hash"] != request_hash:
            raise IdempotencyKeyReuseError("This idempotency key was already used for different snapshot content or account.")
        if existing_req["response_payload"]:
            return existing_req["response_payload"]
        if existing_req["draft_payload"] and existing_req["status"] == "needs_confirmation":
            return existing_req["draft_payload"]

    request_id = uuid4()
    ingestion_repo.create_ingestion_request(
        conn=conn,
        request_id=request_id,
        device_id=device_id,
        idempotency_key=idempotency_key,
        request_kind="snapshot",
        request_hash=request_hash,
        status="processing",
        captured_at=as_of,
        draft_payload=canonical_dict
    )

    # 5. Evaluate Reconciliation under lock
    accounts_repo.lock_account_state(conn, account_id)
    is_first = is_first_account_observation(conn, account_id)

    if is_first:
        # First observation initialization
        effects = sum_committed_transaction_deltas(conn, account_id, ledger_start_date, as_of.date())
        opening_anchor = quantize_money(dec_balance - effects, curr)
        batch_id = uuid4()
        snapshot_id = uuid4()
        opening_tx_id = None
        last_tx_at = None

        # Insert reconciliation_batches FIRST so audit_events FK is satisfied
        reconciliation_repo.create_reconciliation_batch(
            conn=conn,
            batch_id=batch_id,
            household_id=household_id,
            account_id=account_id,
            batch_type="snapshot",
            status="committed",
            currency=curr,
            authoritative_balance=dec_balance,
            residual_amount=quantize_money(Decimal("0.00"), curr),
            adjustment_amount=None,
            period_end=as_of.date(),
            source_request_id=request_id,
            created_by_user_id=user_id,
            row_version=1,
            committed_at=datetime.now(timezone.utc)
        )

        if opening_anchor != Decimal("0.00"):
            opening_tx_id = uuid4()
            if opening_anchor > 0:
                to_amt, to_curr, to_acc = opening_anchor, curr, account_id
                from_amt, from_curr, from_acc = None, None, None
            else:
                from_amt, from_curr, from_acc = abs(opening_anchor), curr, account_id
                to_amt, to_curr, to_acc = None, None, None

            last_tx_at = datetime.combine(ledger_start_date, datetime.min.time(), tzinfo=timezone.utc)
            tx_repo.create_transaction(
                conn=conn,
                tx_id=opening_tx_id,
                household_id=household_id,
                transaction_type="opening_balance",
                occurred_on=ledger_start_date,
                original_amount=abs(opening_anchor),
                original_currency=curr,
                from_amount=from_amt,
                from_currency=from_curr,
                from_account_id=from_acc,
                to_amount=to_amt,
                to_currency=to_curr,
                to_account_id=to_acc,
                status="committed",
                source="reconciliation",
                reporting_amount=abs(opening_anchor) if curr == reporting_currency else None,
                reporting_currency=reporting_currency
            )
            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type="device" if device_id else "user",
                actor_user_id=user_id,
                actor_device_id=device_id,
                request_id=request_id,
                reconciliation_batch_id=batch_id,
                entity_type="transaction",
                entity_id=opening_tx_id,
                action="create",
                after_data={
                    "transaction_type": "opening_balance",
                    "account_id": str(account_id),
                    "amount": str(quantize_money(abs(opening_anchor), curr)),
                    "currency": curr,
                    "occurred_on": ledger_start_date.isoformat()
                }
            )

        snapshots_repo.create_account_snapshot(
            conn=conn,
            snapshot_id=snapshot_id,
            household_id=household_id,
            account_id=account_id,
            as_of=as_of,
            balance=dec_balance,
            currency=curr,
            snapshot_type="balance",
            source=source,
            reconciliation_batch_id=batch_id,
            source_request_id=request_id,
            is_authoritative=True,
            created_by_user_id=user_id
        )
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device" if device_id else "user",
            actor_user_id=user_id,
            actor_device_id=device_id,
            request_id=request_id,
            reconciliation_batch_id=batch_id,
            entity_type="account_snapshot",
            entity_id=snapshot_id,
            action="create",
            after_data={
                "account_id": str(account_id),
                "as_of": as_of.isoformat(),
                "balance": str(dec_balance),
                "currency": curr,
                "source": source
            }
        )

        current_balance = recompute_account_current_balance(conn, account_id)
        accounts_repo.update_account_state_after_reconciliation(
            conn=conn,
            account_id=account_id,
            new_balance=current_balance,
            snapshot_as_of=as_of,
            last_transaction_at=last_tx_at
        )

        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device" if device_id else "user",
            actor_user_id=user_id,
            actor_device_id=device_id,
            request_id=request_id,
            reconciliation_batch_id=batch_id,
            entity_type="reconciliation_batch",
            entity_id=batch_id,
            action="commit",
            after_data={
                "batch_type": "snapshot",
                "status": "committed",
                "authoritative_balance": str(dec_balance),
                "residual_amount": "0.00"
            }
        )

        response = {
            "status": "committed",
            "batch_id": str(batch_id),
            "snapshot_id": str(snapshot_id),
            "residual_amount": str(quantize_money(Decimal("0.00"), curr)),
            "adjustment_transaction_id": None
        }
        if opening_tx_id:
            response["opening_balance_transaction_id"] = str(opening_tx_id)

        ingestion_repo.update_ingestion_request_status(
            conn=conn,
            request_id=request_id,
            status="committed",
            response_payload=response,
            committed_at=datetime.now(timezone.utc)
        )
        return response

    else:
        # Subsequent observation reconciliation
        projected_balance = ledger_balance_as_of(conn, account_id, as_of)
        residual = quantize_money(dec_balance - projected_balance, curr)

        if fx_service is None:
            fx_service = ReferenceFxService()

        # Threshold eligibility is ALWAYS evaluated in CNY (<= 200 CNY)
        if curr == "CNY":
            residual_cny = residual
        else:
            fx_rate_cny = fx_service.get_rate(curr, "CNY", as_of=as_of.date())
            residual_cny = quantize_money(residual * fx_rate_cny, "CNY")

        is_auto_eligible = (abs(residual_cny) <= Decimal("200.00"))

        # Reporting currency calculation for presentation/audit
        if curr == reporting_currency:
            residual_reporting = residual
        else:
            fx_rate_rep = fx_service.get_rate(curr, reporting_currency, as_of=as_of.date())
            residual_reporting = quantize_money(residual * fx_rate_rep, reporting_currency)

        if is_auto_eligible:
            # Auto-commit path
            batch_id = uuid4()
            snapshot_id = uuid4()
            adj_tx_id = None
            last_tx_at = None

            # Insert reconciliation_batches FIRST so audit_events FK is satisfied
            reconciliation_repo.create_reconciliation_batch(
                conn=conn,
                batch_id=batch_id,
                household_id=household_id,
                account_id=account_id,
                batch_type="snapshot",
                status="committed",
                currency=curr,
                authoritative_balance=dec_balance,
                residual_amount=residual,
                adjustment_amount=residual if residual != Decimal("0.00") else None,
                period_end=as_of.date(),
                source_request_id=request_id,
                created_by_user_id=user_id,
                row_version=1,
                committed_at=datetime.now(timezone.utc)
            )

            if residual != Decimal("0.00"):
                adj_tx_id = uuid4()
                if residual > 0:
                    to_amt, to_curr, to_acc = residual, curr, account_id
                    from_amt, from_curr, from_acc = None, None, None
                else:
                    from_amt, from_curr, from_acc = abs(residual), curr, account_id
                    to_amt, to_curr, to_acc = None, None, None

                last_tx_at = as_of
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=adj_tx_id,
                    household_id=household_id,
                    transaction_type="reconciliation_adjustment",
                    occurred_on=as_of.date(),
                    original_amount=abs(residual),
                    original_currency=curr,
                    from_amount=from_amt,
                    from_currency=from_curr,
                    from_account_id=from_acc,
                    to_amount=to_amt,
                    to_currency=to_curr,
                    to_account_id=to_acc,
                    status="committed",
                    source="reconciliation",
                    reporting_amount=abs(residual_reporting),
                    reporting_currency=reporting_currency
                )
                audit_repo.insert_audit_event(
                    conn=conn,
                    household_id=household_id,
                    actor_type="device" if device_id else "user",
                    actor_user_id=user_id,
                    actor_device_id=device_id,
                    request_id=request_id,
                    reconciliation_batch_id=batch_id,
                    entity_type="transaction",
                    entity_id=adj_tx_id,
                    action="create",
                    after_data={
                        "transaction_type": "reconciliation_adjustment",
                        "account_id": str(account_id),
                        "amount": str(quantize_money(abs(residual), curr)),
                        "currency": curr,
                        "occurred_on": as_of.date().isoformat()
                    }
                )

            snapshots_repo.create_account_snapshot(
                conn=conn,
                snapshot_id=snapshot_id,
                household_id=household_id,
                account_id=account_id,
                as_of=as_of,
                balance=dec_balance,
                currency=curr,
                snapshot_type="balance",
                source=source,
                reconciliation_batch_id=batch_id,
                source_request_id=request_id,
                is_authoritative=True,
                created_by_user_id=user_id
            )
            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type="device" if device_id else "user",
                actor_user_id=user_id,
                actor_device_id=device_id,
                request_id=request_id,
                reconciliation_batch_id=batch_id,
                entity_type="account_snapshot",
                entity_id=snapshot_id,
                action="create",
                after_data={
                    "account_id": str(account_id),
                    "as_of": as_of.isoformat(),
                    "balance": str(dec_balance),
                    "currency": curr,
                    "source": source
                }
            )

            current_balance = recompute_account_current_balance(conn, account_id)
            accounts_repo.update_account_state_after_reconciliation(
                conn=conn,
                account_id=account_id,
                new_balance=current_balance,
                snapshot_as_of=as_of,
                last_transaction_at=last_tx_at
            )

            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type="device" if device_id else "user",
                actor_user_id=user_id,
                actor_device_id=device_id,
                request_id=request_id,
                reconciliation_batch_id=batch_id,
                entity_type="reconciliation_batch",
                entity_id=batch_id,
                action="commit",
                after_data={
                    "batch_type": "snapshot",
                    "status": "committed",
                    "authoritative_balance": str(dec_balance),
                    "residual_amount": str(residual)
                }
            )

            response = {
                "status": "committed",
                "batch_id": str(batch_id),
                "snapshot_id": str(snapshot_id),
                "residual_amount": str(residual),
                "adjustment_transaction_id": str(adj_tx_id) if adj_tx_id else None
            }

            ingestion_repo.update_ingestion_request_status(
                conn=conn,
                request_id=request_id,
                status="committed",
                response_payload=response,
                committed_at=datetime.now(timezone.utc)
            )
            return response

        else:
            # needs_review path (> 200 CNY)
            batch_id = uuid4()
            candidate_id = uuid4()

            reconciliation_repo.create_reconciliation_batch(
                conn=conn,
                batch_id=batch_id,
                household_id=household_id,
                account_id=account_id,
                batch_type="snapshot",
                status="needs_review",
                currency=curr,
                authoritative_balance=dec_balance,
                residual_amount=residual,
                adjustment_amount=None,
                period_end=as_of.date(),
                source_request_id=request_id,
                created_by_user_id=user_id,
                row_version=0
            )

            # Preserve exact metadata (as_of timestamp, source, amount, currency) in candidate payload
            candidate_payload = {
                "adjustment_amount": str(residual),
                "currency": curr,
                "occurred_on": as_of.date().isoformat(),
                "as_of": as_of.isoformat(),
                "source": source
            }

            reconciliation_repo.create_reconciliation_candidate(
                conn=conn,
                candidate_id=candidate_id,
                batch_id=batch_id,
                candidate_type="adjustment",
                status="needs_review",
                payload=candidate_payload,
                confidence=Decimal("1.0000"),
                reason_code="THRESHOLD_EXCEEDED",
                reason_detail="Residual exceeds 200 CNY auto-adjustment threshold"
            )

            curr_sym = "¥" if curr == "CNY" else f"{curr} "
            display_summary = f"账户实际余额与账本相差 {curr_sym}{abs(residual):,.2f}"

            response = {
                "status": "needs_review",
                "batch_id": str(batch_id),
                "residual_amount": str(residual),
                "display_summary": display_summary
            }

            ingestion_repo.update_ingestion_request_status(
                conn=conn,
                request_id=request_id,
                status="needs_confirmation",
                draft_payload=response
            )
            return response

def get_reconciliation_batch_summary(conn, batch_id: UUID, household_id: UUID) -> Dict[str, Any]:
    """
    Returns the reconciliation batch summary.
    """
    batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(batch_id)

    curr = batch["currency"]
    candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
    pending_count = len([c for c in candidates if c["status"] in ("proposed", "needs_review")])

    auth_bal = str(quantize_money(batch["authoritative_balance"], curr)) if batch["authoritative_balance"] is not None else None
    res_amt = str(quantize_money(batch["residual_amount"], curr)) if batch["residual_amount"] is not None else None
    adj_amt = str(quantize_money(batch["adjustment_amount"], curr)) if batch["adjustment_amount"] is not None else None

    summary = {
        "status": batch["status"],
        "batch_id": str(batch["id"]),
        "account_id": str(batch["account_id"]),
        "batch_type": batch["batch_type"],
        "currency": curr,
        "authoritative_balance": auth_bal,
        "residual_amount": res_amt,
        "adjustment_amount": adj_amt,
        "row_version": batch["row_version"],
        "created_at": batch["created_at"].isoformat() if batch["created_at"] else None,
        "committed_at": batch["committed_at"].isoformat() if batch["committed_at"] else None,
        "summary": {
            "residual_amount": res_amt,
            "currency": curr,
            "pending_count": pending_count if batch["status"] == "needs_review" else 0
        }
    }
    return summary

def get_reconciliation_preview(
    conn,
    batch_id: UUID,
    household_id: UUID,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Returns read-only preview calculation for a reconciliation batch.
    Mutates zero database state.
    """
    batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(batch_id)

    curr = batch["currency"]
    household = accounts_repo.get_household(conn, household_id)
    reporting_currency = household["reporting_currency"]
    candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)

    # Recover exact original as_of if preserved in candidate payload or draft request
    as_of_dt = None
    for c in candidates:
        if isinstance(c.get("payload"), dict) and "as_of" in c["payload"]:
            try:
                as_of_dt = datetime.fromisoformat(c["payload"]["as_of"])
                break
            except Exception:
                pass

    if as_of_dt is None and batch["source_request_id"]:
        req = ingestion_repo.get_ingestion_request(conn, batch["source_request_id"])
        if req and req["captured_at"]:
            as_of_dt = req["captured_at"]

    if as_of_dt is None:
        as_of_date = batch["period_end"] or (batch["created_at"].date() if batch["created_at"] else date.today())
        as_of_dt = datetime.combine(as_of_date, datetime.max.time(), tzinfo=timezone.utc)
    else:
        as_of_date = as_of_dt.date()

    is_first = is_first_account_observation(conn, batch["account_id"])
    if is_first:
        effects = sum_committed_transaction_deltas(conn, batch["account_id"], household["ledger_start_date"], as_of_date)
        projected_balance = quantize_money(batch["authoritative_balance"], curr)
        residual = quantize_money(Decimal("0.00"), curr)
        residual_reporting = quantize_money(Decimal("0.00"), reporting_currency)
        auto_eligible = True
    else:
        projected_balance = ledger_balance_as_of(conn, batch["account_id"], as_of_dt)
        residual = quantize_money((batch["authoritative_balance"] or Decimal("0.00")) - projected_balance, curr)

        if fx_service is None:
            fx_service = ReferenceFxService()

        # Threshold check in CNY
        if curr == "CNY":
            residual_cny = residual
        else:
            fx_rate_cny = fx_service.get_rate(curr, "CNY", as_of=as_of_date)
            residual_cny = quantize_money(residual * fx_rate_cny, "CNY")

        auto_eligible = (abs(residual_cny) <= Decimal("200.00"))

        if curr == reporting_currency:
            residual_reporting = residual
        else:
            fx_rate = fx_service.get_rate(curr, reporting_currency, as_of=as_of_date)
            residual_reporting = quantize_money(residual * fx_rate, reporting_currency)

    proposed_adj = None
    if residual != Decimal("0.00"):
        proposed_adj = {
            "amount": str(quantize_money(abs(residual), curr)),
            "currency": curr,
            "direction": "positive" if residual > 0 else "negative"
        }

    auth_bal_str = str(quantize_money(batch["authoritative_balance"], curr)) if batch["authoritative_balance"] is not None else None
    proj_bal_str = str(quantize_money(projected_balance, curr))
    res_amt_str = str(quantize_money(residual, curr))
    res_rep_str = str(quantize_money(residual_reporting, reporting_currency))

    return {
        "batch": {
            "id": str(batch["id"]),
            "account_id": str(batch["account_id"]),
            "status": batch["status"],
            "batch_type": batch["batch_type"],
            "currency": curr,
            "row_version": batch["row_version"],
            "period_start": batch["period_start"].isoformat() if batch["period_start"] else None,
            "period_end": batch["period_end"].isoformat() if batch["period_end"] else None
        },
        "authoritative_balance": auth_bal_str,
        "projected_balance": proj_bal_str,
        "residual_amount": res_amt_str,
        "residual_reporting_amount": res_rep_str,
        "reporting_currency": reporting_currency,
        "auto_adjustment_eligible": auto_eligible,
        "proposed_adjustment": proposed_adj,
        "summary": {
            "residual_amount": res_amt_str,
            "currency": curr,
            "pending_count": len([c for c in candidates if c["status"] in ("proposed", "needs_review")]) if batch["status"] == "needs_review" else 0
        },
        "candidates": [
            {
                "id": str(c["id"]),
                "candidate_type": c["candidate_type"],
                "status": c["status"],
                "payload": c["payload"],
                "reason_code": c["reason_code"],
                "reason_detail": c["reason_detail"]
            }
            for c in candidates
        ]
    }

def commit_reconciliation_batch(
    conn,
    batch_id: UUID,
    device: Dict[str, Any],
    row_version: Optional[int] = None,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Atomically commits a reconciliation batch in a single transaction.
    Re-evaluates ledger_balance_as_of and residual under exclusive lock.
    """
    household_id = device["household_id"]
    user_id = device.get("user_id")
    device_id = device.get("device_id")

    # 1. Lock Batch
    batch = reconciliation_repo.lock_reconciliation_batch(conn, batch_id)
    if not batch or batch["household_id"] != household_id:
        raise BatchNotFoundError(batch_id)

    # Validate batch_type
    if batch["batch_type"] != "snapshot":
        raise InvalidBatchStateError(f"Batch type '{batch['batch_type']}' is not supported for snapshot reconciliation commit.")

    curr = batch["currency"]

    # 2. Check idempotent replay if already committed
    if batch["status"] == "committed":
        # Check if ingestion request already has canonical response payload
        if batch["source_request_id"]:
            req = ingestion_repo.get_ingestion_request(conn, batch["source_request_id"])
            if req and req.get("response_payload"):
                return req["response_payload"]

        # Deterministic reconstruction from snapshot, candidate, and audit events
        snap = snapshots_repo.get_snapshot_by_batch_id(conn, batch_id)
        candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
        adj_tx_id = None
        for c in candidates:
            if c.get("applied_transaction_id"):
                adj_tx_id = c["applied_transaction_id"]
                break

        opening_tx_id = None
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, after_data FROM audit_events
                WHERE reconciliation_batch_id = %s AND entity_type = 'transaction';
                """,
                (batch_id,)
            )
            for r in cur.fetchall():
                tx_eid, aft = r[0], r[1]
                if isinstance(aft, str):
                    try:
                        aft = json.loads(aft)
                    except Exception:
                        aft = {}
                if isinstance(aft, dict):
                    if aft.get("transaction_type") == "reconciliation_adjustment":
                        adj_tx_id = tx_eid
                    elif aft.get("transaction_type") == "opening_balance":
                        opening_tx_id = tx_eid

        res_str = str(quantize_money(batch["residual_amount"] or Decimal("0.00"), curr))
        replay_resp = {
            "status": "committed",
            "batch_id": str(batch_id),
            "snapshot_id": str(snap["id"]) if snap else None,
            "residual_amount": res_str,
            "adjustment_transaction_id": str(adj_tx_id) if adj_tx_id else None
        }
        if opening_tx_id:
            replay_resp["opening_balance_transaction_id"] = str(opening_tx_id)
        return replay_resp

    # Validate commit-eligible states: only 'ready' or 'needs_review'
    if batch["status"] not in ("ready", "needs_review"):
        raise InvalidBatchStateError(f"Reconciliation batch in '{batch['status']}' state cannot be committed.")

    # 3. Optimistic Concurrency check
    if row_version is not None and row_version != batch["row_version"]:
        raise BatchVersionConflictError("Reconciliation batch was concurrently modified. Reload before committing.")

    # 4. Lock Account State
    accounts_repo.lock_account_state(conn, batch["account_id"])
    household = accounts_repo.get_household(conn, household_id)
    reporting_currency = household["reporting_currency"]
    ledger_start_date = household["ledger_start_date"]

    # 5. Recover exact original metadata (as_of timestamp and source)
    candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
    as_of_dt = None
    source = "dashboard_manual"

    for c in candidates:
        if isinstance(c.get("payload"), dict):
            if "as_of" in c["payload"]:
                try:
                    as_of_dt = datetime.fromisoformat(c["payload"]["as_of"])
                except Exception:
                    pass
            if "source" in c["payload"]:
                source = c["payload"]["source"]
            if as_of_dt:
                break

    if as_of_dt is None and batch["source_request_id"]:
        req = ingestion_repo.get_ingestion_request(conn, batch["source_request_id"])
        if req:
            if req.get("captured_at"):
                as_of_dt = req["captured_at"]
            elif req.get("draft_payload") and isinstance(req["draft_payload"], dict) and "as_of" in req["draft_payload"]:
                try:
                    as_of_dt = datetime.fromisoformat(req["draft_payload"]["as_of"])
                except Exception:
                    pass
            if req.get("draft_payload") and isinstance(req["draft_payload"], dict) and "source" in req["draft_payload"]:
                source = req["draft_payload"]["source"]

    if as_of_dt is None:
        as_of_date = batch["period_end"] or (batch["created_at"].date() if batch["created_at"] else date.today())
        as_of_dt = datetime.combine(as_of_date, datetime.min.time(), tzinfo=timezone.utc)
    else:
        as_of_date = as_of_dt.date()

    auth_balance = quantize_money(batch["authoritative_balance"], curr)
    is_first = is_first_account_observation(conn, batch["account_id"])
    snapshot_id = uuid4()
    opening_tx_id = None
    adj_tx_id = None
    last_tx_at = None

    if is_first:
        effects = sum_committed_transaction_deltas(conn, batch["account_id"], ledger_start_date, as_of_date)
        opening_anchor = quantize_money(auth_balance - effects, curr)
        residual = quantize_money(Decimal("0.00"), curr)

        if opening_anchor != Decimal("0.00"):
            opening_tx_id = uuid4()
            if opening_anchor > 0:
                to_amt, to_curr, to_acc = opening_anchor, curr, batch["account_id"]
                from_amt, from_curr, from_acc = None, None, None
            else:
                from_amt, from_curr, from_acc = abs(opening_anchor), curr, batch["account_id"]
                to_amt, to_curr, to_acc = None, None, None

            last_tx_at = datetime.combine(ledger_start_date, datetime.min.time(), tzinfo=timezone.utc)
            tx_repo.create_transaction(
                conn=conn,
                tx_id=opening_tx_id,
                household_id=household_id,
                transaction_type="opening_balance",
                occurred_on=ledger_start_date,
                original_amount=abs(opening_anchor),
                original_currency=curr,
                from_amount=from_amt,
                from_currency=from_curr,
                from_account_id=from_acc,
                to_amount=to_amt,
                to_currency=to_curr,
                to_account_id=to_acc,
                status="committed",
                source="reconciliation",
                reporting_amount=abs(opening_anchor) if curr == reporting_currency else None,
                reporting_currency=reporting_currency
            )
            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type="device" if device_id else "user",
                actor_user_id=user_id,
                actor_device_id=device_id,
                request_id=batch["source_request_id"],
                reconciliation_batch_id=batch_id,
                entity_type="transaction",
                entity_id=opening_tx_id,
                action="create",
                after_data={
                    "transaction_type": "opening_balance",
                    "account_id": str(batch["account_id"]),
                    "amount": str(quantize_money(abs(opening_anchor), curr)),
                    "currency": curr,
                    "occurred_on": ledger_start_date.isoformat()
                }
            )
    else:
        # Recompute fresh projected balance and residual under lock
        projected_balance = ledger_balance_as_of(conn, batch["account_id"], as_of_dt)
        residual = quantize_money(auth_balance - projected_balance, curr)

        if residual != Decimal("0.00"):
            adj_tx_id = uuid4()
            if residual > 0:
                to_amt, to_curr, to_acc = residual, curr, batch["account_id"]
                from_amt, from_curr, from_acc = None, None, None
            else:
                from_amt, from_curr, from_acc = abs(residual), curr, batch["account_id"]
                to_amt, to_curr, to_acc = None, None, None

            if fx_service is None:
                fx_service = ReferenceFxService()

            if curr == reporting_currency:
                rep_amt = abs(residual)
            else:
                rate = fx_service.get_rate(curr, reporting_currency, as_of=as_of_date)
                rep_amt = quantize_money(abs(residual) * rate, reporting_currency)

            last_tx_at = as_of_dt
            tx_repo.create_transaction(
                conn=conn,
                tx_id=adj_tx_id,
                household_id=household_id,
                transaction_type="reconciliation_adjustment",
                occurred_on=as_of_date,
                original_amount=abs(residual),
                original_currency=curr,
                from_amount=from_amt,
                from_currency=from_curr,
                from_account_id=from_acc,
                to_amount=to_amt,
                to_currency=to_curr,
                to_account_id=to_acc,
                status="committed",
                source="reconciliation",
                reporting_amount=rep_amt,
                reporting_currency=reporting_currency
            )
            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type="device" if device_id else "user",
                actor_user_id=user_id,
                actor_device_id=device_id,
                request_id=batch["source_request_id"],
                reconciliation_batch_id=batch_id,
                entity_type="transaction",
                entity_id=adj_tx_id,
                action="create",
                after_data={
                    "transaction_type": "reconciliation_adjustment",
                    "account_id": str(batch["account_id"]),
                    "amount": str(quantize_money(abs(residual), curr)),
                    "currency": curr,
                    "occurred_on": as_of_date.isoformat()
                }
            )

    # 6. Create authoritative snapshot with exact instant and source
    snapshots_repo.create_account_snapshot(
        conn=conn,
        snapshot_id=snapshot_id,
        household_id=household_id,
        account_id=batch["account_id"],
        as_of=as_of_dt,
        balance=auth_balance,
        currency=curr,
        snapshot_type="balance",
        source=source,
        reconciliation_batch_id=batch_id,
        source_request_id=batch["source_request_id"],
        is_authoritative=True,
        created_by_user_id=user_id
    )
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        request_id=batch["source_request_id"],
        reconciliation_batch_id=batch_id,
        entity_type="account_snapshot",
        entity_id=snapshot_id,
        action="create",
        after_data={
            "account_id": str(batch["account_id"]),
            "as_of": as_of_dt.isoformat(),
            "balance": str(auth_balance),
            "currency": curr,
            "source": source
        }
    )

    # 7. Apply candidates (refresh candidate payload to match the exact applied adjustment amount)
    for c in candidates:
        applied_payload = {
            "adjustment_amount": str(residual),
            "currency": curr,
            "occurred_on": as_of_date.isoformat(),
            "as_of": as_of_dt.isoformat(),
            "source": source
        }
        reconciliation_repo.update_candidate_applied(
            conn=conn,
            candidate_id=c["id"],
            status="applied",
            payload=applied_payload,
            applied_transaction_id=adj_tx_id,
            resolved_by_user_id=user_id
        )

    # 8. Update Batch Status
    reconciliation_repo.update_reconciliation_batch(
        conn=conn,
        batch_id=batch_id,
        status="committed",
        residual_amount=residual,
        adjustment_amount=residual if residual != Decimal("0.00") else None,
        committed_at=datetime.now(timezone.utc)
    )

    # 9. Update Account State (with initialized_at set and last_transaction_at updated)
    current_balance = recompute_account_current_balance(conn, batch["account_id"])
    accounts_repo.update_account_state_after_reconciliation(
        conn=conn,
        account_id=batch["account_id"],
        new_balance=current_balance,
        snapshot_as_of=as_of_dt,
        last_transaction_at=last_tx_at
    )

    # 10. Audit Batch Commit
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="device" if device_id else "user",
        actor_user_id=user_id,
        actor_device_id=device_id,
        request_id=batch["source_request_id"],
        reconciliation_batch_id=batch_id,
        entity_type="reconciliation_batch",
        entity_id=batch_id,
        action="commit",
        after_data={
            "batch_type": batch["batch_type"],
            "status": "committed",
            "authoritative_balance": str(auth_balance),
            "residual_amount": str(residual)
        }
    )

    response = {
        "status": "committed",
        "batch_id": str(batch_id),
        "snapshot_id": str(snapshot_id),
        "residual_amount": str(residual),
        "adjustment_transaction_id": str(adj_tx_id) if adj_tx_id else None
    }
    if opening_tx_id:
        response["opening_balance_transaction_id"] = str(opening_tx_id)

    if batch["source_request_id"]:
        ingestion_repo.update_ingestion_request_status(
            conn=conn,
            request_id=batch["source_request_id"],
            status="committed",
            response_payload=response,
            committed_at=datetime.now(timezone.utc)
        )

    return response
