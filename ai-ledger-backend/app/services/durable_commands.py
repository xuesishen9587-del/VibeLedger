import hashlib
import json
from typing import Any, Callable, Dict, Optional, Tuple
from uuid import UUID, uuid4
from psycopg2.extras import RealDictCursor
from fastapi import HTTPException

from app.api.errors import extract_error_details
from app.auth.context import AuthContext
from app.domain.auth import DeviceNotFoundError
from app.domain.transactions import (
    LedgerDomainError,
    IdempotencyKeyReuseError,
)
from app.repositories.simplified_schema import acquire_household_finance_lock, lock_ingestion_requests_in_order


def to_json(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, default=str)


def compute_command_hash(operation: str, body: Dict[str, Any]) -> str:
    """
    Computes a deterministic SHA256 hash representing canonical command identity.
    Includes HTTP operation, canonical target resource path, and validated request body.
    Sorts keys to eliminate serialization noise.
    Never falls back to or includes the idempotency key as request content.
    """
    payload_str = json.dumps({"operation": operation, "body": body}, sort_keys=True, default=str)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


def get_audit_actor_info(auth_context: Any) -> Tuple[str, Optional[UUID], Optional[UUID]]:
    if getattr(auth_context, "is_system", False):
        return "system", None, None
    elif getattr(auth_context, "is_browser", False):
        return "user", auth_context.user_id, None
    elif getattr(auth_context, "is_device", False):
        return "device", auth_context.user_id, auth_context.device_id
    else:
        if getattr(auth_context, "device_id", None) is not None:
            return "device", auth_context.user_id, auth_context.device_id
        return "user", getattr(auth_context, "user_id", None), None


def execute_durable_command(
    conn: Any,
    auth_context: Any,
    idempotency_key: str,
    operation: str,
    body: Dict[str, Any],
    mutation_fn: Callable[[Any, UUID], Tuple[Dict[str, Any], int]],
    *, source_expense_request_ids=(),
) -> Tuple[Dict[str, Any], int]:
    """
    Orchestrates the canonical durable command receipt lifecycle:
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
    device_id = auth_context.device_id if getattr(auth_context, "is_device", False) else None
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

        if source_expense_request_ids:
            # The newly inserted command is private until this transaction commits.
            # Lock the command and existing capture receipts together in ID order,
            # before acquiring the household lock. Capture edits never acquire a
            # command receipt. Replays above do not depend on mutable source state.
            cur.execute("SELECT id,request_kind FROM ingestion_requests WHERE household_id=%s "
                        "AND id=ANY(%s::uuid[])", (str(household_id), [str(i) for i in source_expense_request_ids]))
            sources = cur.fetchall()
            if len(sources) != len(set(source_expense_request_ids)) or any(r["request_kind"] != "expense" for r in sources):
                raise HTTPException(404, "Expense draft not found.")
            lock_ingestion_requests_in_order(conn, household_id, [active_receipt_id, *source_expense_request_ids])

        # Lock ordering: receipt lock held -> household finance lock
        acquire_household_finance_lock(conn, household_id)

        cur.execute("SAVEPOINT durable_command_mutation;")
        try:
            result_payload, http_status = mutation_fn(conn, active_receipt_id)
        except (LedgerDomainError, DeviceNotFoundError, HTTPException) as exc:
            cur.execute("ROLLBACK TO SAVEPOINT durable_command_mutation;")
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
                (to_json(err_payload), status_code, failure_code, str(active_receipt_id)),
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
            (to_json(result_payload), http_status, str(active_receipt_id)),
        )
        conn.commit()
        return result_payload, http_status
