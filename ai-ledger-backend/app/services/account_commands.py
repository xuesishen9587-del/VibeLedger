from datetime import date, datetime
import hashlib
import json
from typing import Any, Callable, Dict, Optional, Tuple
from uuid import UUID, uuid4
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import HTTPException

from app.api.errors import extract_error_details
from app.auth.context import AuthContext
from app.domain.money import validate_currency_code
from app.domain.transactions import (
    LedgerDomainError,
    AccountResourceNotFoundError,
    RowVersionConflictError,
    AccountNameConflictError,
    CurrencyImmutableError,
    AccountTypeImmutableError,
    UserNotInHouseholdError,
    LinkedAccountInvalidError,
    IdempotencyKeyReuseError,
)
import app.repositories.accounts as accounts_repo
import app.repositories.audit as audit_repo
from app.repositories.simplified_schema import acquire_household_finance_lock


def _to_json(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, default=str)


def format_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    curr = acc["currency"]
    return {
        "id": str(acc["id"]),
        "household_id": str(acc["household_id"]) if acc.get("household_id") else None,
        "name": acc["name"],
        "balance_scope": acc.get("balance_scope", "liability" if acc["account_type"] == "credit" else "asset"),
        "account_type": acc["account_type"],
        "currency": curr,
        "owner_user_id": str(acc["owner_user_id"]) if acc.get("owner_user_id") else None,
        "risk_level": acc.get("risk_level"),
        "opened_on": acc["opened_on"].isoformat() if hasattr(acc.get("opened_on"), "isoformat") else (str(acc["opened_on"]) if acc.get("opened_on") else None),
        "closed_on": acc["closed_on"].isoformat() if hasattr(acc.get("closed_on"), "isoformat") else (str(acc["closed_on"]) if acc.get("closed_on") else None),
        "status": acc["status"],
        "statement_import_enabled": acc.get("statement_import_enabled", False),
        "row_version": acc.get("row_version", 0),
        "latest_snapshot": acc.get("latest_snapshot"),
    }


def compute_command_hash(operation: str, body: Dict[str, Any]) -> str:
    """
    Computes a deterministic SHA256 hash representing canonical command identity.
    Includes HTTP operation, canonical target resource path, and validated request body.
    Sorts keys to eliminate serialization noise.
    Never falls back to or includes the idempotency key as request content.
    """
    payload_str = json.dumps({"operation": operation, "body": body}, sort_keys=True, default=str)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


def _get_audit_actor_info(auth_context: AuthContext) -> Tuple[str, Optional[UUID], Optional[UUID]]:
    if auth_context.is_browser:
        return "user", auth_context.user_id, None
    elif auth_context.is_device:
        return "device", auth_context.user_id, auth_context.device_id
    else:
        if auth_context.device_id is not None:
            return "device", auth_context.user_id, auth_context.device_id
        return "user", auth_context.user_id, None


def execute_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    operation: str,
    body: Dict[str, Any],
    mutation_fn: Callable[[Any, UUID], Tuple[Dict[str, Any], int]],
) -> Tuple[Dict[str, Any], int]:
    """
    Orchestrates the durable command receipt lifecycle for an account command:
    1. Compute deterministic SHA256 request_hash from operation + body.
    2. Check/reserve receipt in ingestion_requests.
       - If committed: return stored replay immediately before state checks.
       - If rejected: return stored rejection immediately before state checks.
       - If key reused for different operation or hash: raise IdempotencyKeyReuseError (409).
    3. Canonical lock ordering: receipt lock (held) -> household finance lock -> mutation.
    4. Deterministic rejection (LedgerDomainError, HTTPException):
       - Rollback to savepoint.
       - Store terminal 'rejected' receipt.
       - Commit and re-raise.
    5. Success:
       - Finalize receipt with status='committed', committed_at=now(), response_payload.
       - Commit and return (payload, http_status).
    6. Unexpected error:
       - conn.rollback() ensuring zero partial mutation, audit, or committed receipt survive.
    """
    request_hash = compute_command_hash(operation, body)
    household_id = auth_context.household_id
    user_id = auth_context.user_id
    device_id = auth_context.device_id if auth_context.is_device else None
    actor_scope = auth_context.actor_scope
    receipt_id = uuid4()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        while True:
            cur.execute(
                """
                INSERT INTO ingestion_requests (
                    id, household_id, user_id, device_id, actor_scope,
                    idempotency_key, request_kind, operation, request_hash,
                    status, captured_at, row_version, last_editor_scope
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, 'command', %s, %s,
                    'processing', now(), 0, %s
                )
                ON CONFLICT (household_id, actor_scope, idempotency_key)
                DO NOTHING
                RETURNING id, status, row_version, operation, request_hash, response_payload, response_http_status, failure_code;
                """,
                (
                    str(receipt_id),
                    str(household_id),
                    str(user_id),
                    str(device_id) if device_id else None,
                    actor_scope,
                    idempotency_key,
                    operation,
                    request_hash,
                    actor_scope,
                ),
            )
            inserted = cur.fetchone()
            if inserted is not None:
                existing = None
                active_receipt_id = receipt_id
                break

            cur.execute(
                """
                SELECT id, status, row_version, operation, request_hash, response_payload, response_http_status, failure_code
                FROM ingestion_requests
                WHERE household_id = %s AND actor_scope = %s AND idempotency_key = %s
                FOR UPDATE;
                """,
                (str(household_id), actor_scope, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                active_receipt_id = existing["id"]
                break
            # Concurrent uncommitted transaction rolled back; retry insert

        if existing is not None:
            if existing["operation"] != operation or existing["request_hash"] != request_hash:
                raise IdempotencyKeyReuseError(
                    f"Idempotency key '{idempotency_key}' was already used for a different command or payload."
                )

            if existing["status"] == "committed":
                return existing["response_payload"], existing["response_http_status"]

            if existing["status"] == "rejected":
                return existing["response_payload"], existing["response_http_status"]

            if existing["status"] == "processing":
                raise IdempotencyKeyReuseError("Command is currently processing.")

        # Lock ordering: receipt lock held -> household finance lock
        acquire_household_finance_lock(conn, household_id)

        cur.execute("SAVEPOINT account_command_mutation;")
        try:
            result_payload, http_status = mutation_fn(conn, active_receipt_id)
        except (LedgerDomainError, HTTPException) as exc:
            cur.execute("ROLLBACK TO SAVEPOINT account_command_mutation;")
            status_code, failure_code, err_payload = extract_error_details(exc)
            cur.execute(
                """
                UPDATE ingestion_requests
                SET status = 'rejected',
                    response_payload = %s,
                    response_http_status = %s,
                    failure_code = %s,
                    committed_at = NULL,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE id = %s;
                """,
                (_to_json(err_payload), status_code, failure_code, str(active_receipt_id)),
            )
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise

        cur.execute(
            """
            UPDATE ingestion_requests
            SET status = 'committed',
                response_payload = %s,
                response_http_status = %s,
                failure_code = NULL,
                committed_at = now(),
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %s;
            """,
            (_to_json(result_payload), http_status, str(active_receipt_id)),
        )
        conn.commit()
        return result_payload, http_status


def create_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = "POST /api/v1/accounts"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        currency = validate_currency_code(payload.currency)
        household_id = auth_context.household_id

        if payload.account_type == "credit" and payload.risk_level is not None:
            raise LinkedAccountInvalidError("Credit accounts cannot have a risk level.")

        balance_scope = payload.balance_scope.strip()
        opened_on = payload.opened_on or date.today()

        if payload.owner_user_id is not None:
            if not accounts_repo.check_user_in_household(c, payload.owner_user_id, household_id):
                raise UserNotInHouseholdError(payload.owner_user_id)

        if accounts_repo.check_account_name_exists(c, household_id, payload.name):
            raise AccountNameConflictError(payload.name)

        account_id = uuid4()
        try:
            created = accounts_repo.create_account(
                conn=c,
                account_id=account_id,
                household_id=household_id,
                name=payload.name.strip(),
                balance_scope=balance_scope,
                account_type=payload.account_type,
                currency=currency,
                owner_user_id=payload.owner_user_id,
                risk_level=payload.risk_level,
                opened_on=opened_on,
                closed_on=None,
                status='active',
                statement_import_enabled=payload.statement_import_enabled
            )
        except psycopg2.IntegrityError as e:
            if "uq_accounts_active_name" in str(e) or "accounts" in str(e):
                raise AccountNameConflictError(payload.name)
            raise

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="create",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            after_data={
                "name": payload.name.strip(),
                "balance_scope": balance_scope,
                "account_type": payload.account_type,
                "currency": currency,
                "owner_user_id": str(payload.owner_user_id) if payload.owner_user_id else None,
                "risk_level": payload.risk_level,
                "opened_on": opened_on.isoformat(),
                "closed_on": None,
                "status": "active",
                "statement_import_enabled": payload.statement_import_enabled,
            }
        )
        return format_account(created), 201

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def patch_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"PATCH /api/v1/accounts/{account_id}"
    body = payload.model_dump(mode="json", exclude_unset=True)
    fields_set = payload.model_fields_set

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        expected_ver = payload.expected_version

        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        if "name" in fields_set:
            if payload.name is None or not payload.name.strip():
                raise LinkedAccountInvalidError("Account name cannot be empty.")
            new_name = payload.name.strip()
        else:
            new_name = existing["name"]

        if "balance_scope" in fields_set:
            if payload.balance_scope is None or not payload.balance_scope.strip():
                raise LinkedAccountInvalidError("balance_scope cannot be empty.")
            new_scope = payload.balance_scope.strip()
        else:
            new_scope = existing.get("balance_scope", "asset")

        if "risk_level" in fields_set:
            new_risk = payload.risk_level
        else:
            new_risk = existing.get("risk_level")

        if "owner_user_id" in fields_set:
            new_owner = payload.owner_user_id
        else:
            new_owner = existing.get("owner_user_id")

        if "opened_on" in fields_set and payload.opened_on is not None:
            new_opened_on = payload.opened_on
        else:
            new_opened_on = existing["opened_on"]

        if "statement_import_enabled" in fields_set and payload.statement_import_enabled is not None:
            new_stmt_enabled = payload.statement_import_enabled
        else:
            new_stmt_enabled = existing.get("statement_import_enabled", False)

        if "account_type" in fields_set and payload.account_type is not None:
            new_type = payload.account_type
        else:
            new_type = existing["account_type"]

        if "currency" in fields_set and payload.currency is not None:
            new_currency = validate_currency_code(payload.currency)
        else:
            new_currency = existing["currency"]

        if new_type == "credit" and new_risk is not None:
            raise LinkedAccountInvalidError("Credit accounts cannot have a risk level.")

        if new_opened_on != existing["opened_on"]:
            if existing.get("closed_on") and new_opened_on > existing["closed_on"]:
                raise HTTPException(
                    status_code=400,
                    detail="Account opened_on cannot be after closed_on."
                )
            if not accounts_repo.check_account_observations_within_lifetime(
                c, household_id, account_id, new_opened_on
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Account has financial observations before new opened_on date."
                )

        if new_currency != existing["currency"]:
            if accounts_repo.has_financial_history(c, household_id, account_id):
                raise CurrencyImmutableError()

        if new_type != existing["account_type"]:
            if accounts_repo.has_financial_history(c, household_id, account_id):
                raise AccountTypeImmutableError()

        if new_name.lower() != existing["name"].lower():
            if accounts_repo.check_account_name_exists(c, household_id, new_name, exclude_account_id=account_id):
                raise AccountNameConflictError(new_name)

        if new_owner is not None and new_owner != existing.get("owner_user_id"):
            if not accounts_repo.check_user_in_household(c, new_owner, household_id):
                raise UserNotInHouseholdError(new_owner)

        updated = accounts_repo.update_account(
            conn=c,
            household_id=household_id,
            account_id=account_id,
            name=new_name,
            balance_scope=new_scope,
            account_type=new_type,
            currency=new_currency,
            owner_user_id=new_owner,
            risk_level=new_risk,
            opened_on=new_opened_on,
            statement_import_enabled=new_stmt_enabled,
            expected_row_version=expected_ver,
            fields_set=fields_set
        )
        if not updated:
            raise RowVersionConflictError()

        before_data = {
            "name": existing["name"],
            "balance_scope": existing.get("balance_scope"),
            "account_type": existing["account_type"],
            "currency": existing["currency"],
            "owner_user_id": str(existing["owner_user_id"]) if existing.get("owner_user_id") else None,
            "risk_level": existing.get("risk_level"),
            "opened_on": existing["opened_on"].isoformat() if hasattr(existing.get("opened_on"), "isoformat") else str(existing.get("opened_on")),
            "status": existing["status"],
            "statement_import_enabled": existing.get("statement_import_enabled", False),
        }
        after_data = {
            "name": updated["name"],
            "balance_scope": updated.get("balance_scope"),
            "account_type": updated["account_type"],
            "currency": updated["currency"],
            "owner_user_id": str(updated["owner_user_id"]) if updated.get("owner_user_id") else None,
            "risk_level": updated.get("risk_level"),
            "opened_on": updated["opened_on"].isoformat() if hasattr(updated.get("opened_on"), "isoformat") else str(updated.get("opened_on")),
            "status": updated["status"],
            "statement_import_enabled": updated.get("statement_import_enabled", False),
        }

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data=before_data,
            after_data=after_data,
            reason=payload.reason
        )
        return format_account(updated), 200

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def close_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"POST /api/v1/accounts/{account_id}/close"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        expected_ver = payload.expected_version

        if not payload.closing_snapshot_id:
            raise HTTPException(status_code=400, detail="closing_snapshot_id is required to close an account.")

        closed_on = payload.closed_on or date.today()

        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        if existing["status"] != "active":
            raise HTTPException(status_code=400, detail="Only active accounts can be closed.")

        if closed_on < existing["opened_on"]:
            raise HTTPException(status_code=400, detail="closed_on cannot be earlier than opened_on.")

        try:
            accounts_repo.validate_closing_snapshot_for_close(
                conn=c,
                household_id=household_id,
                account_id=account_id,
                closing_snapshot_id=payload.closing_snapshot_id,
                account=existing,
                closed_on=closed_on
            )
        except (ValueError, AccountResourceNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))

        updated = accounts_repo.close_account(c, household_id, account_id, expected_ver, closed_on)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="close",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data={"status": "active", "closed_on": None},
            after_data={
                "status": "closed",
                "closed_on": closed_on.isoformat(),
                "closing_snapshot_id": str(payload.closing_snapshot_id)
            },
            reason=payload.reason
        )
        return format_account(updated), 200

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def reopen_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"POST /api/v1/accounts/{account_id}/reopen"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        expected_ver = payload.expected_version

        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        if existing["status"] != "closed":
            raise HTTPException(status_code=400, detail="Only closed accounts can be reopened.")

        updated = accounts_repo.reopen_account(c, household_id, account_id, expected_ver)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="reopen",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data={"status": "closed", "closed_on": existing["closed_on"].isoformat() if existing.get("closed_on") else None},
            after_data={"status": "active", "closed_on": None},
            reason=payload.reason
        )
        return format_account(updated), 200

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def cancel_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"POST /api/v1/accounts/{account_id}/cancel"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        expected_ver = payload.expected_version

        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        if existing["status"] != "active":
            raise HTTPException(status_code=400, detail="Only active accounts can be cancelled.")

        if accounts_repo.has_financial_history(c, household_id, account_id):
            raise HTTPException(status_code=400, detail="Cannot cancel account with existing financial history.")

        updated = accounts_repo.cancel_account(c, household_id, account_id, expected_ver)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data={"status": "active"},
            after_data={"status": "cancelled"},
            reason=payload.reason
        )
        return format_account(updated), 200

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)
