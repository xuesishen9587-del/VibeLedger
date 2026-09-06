"""
VibeLedger Astra-Simplified Schema Repository
Provides household-scoped SQL repository operations for the simplified 16-table architecture.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from typing import Any, Dict, List, Optional
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor


# ----------------------------------------------------------------------
# Helper Utilities
# ----------------------------------------------------------------------

def _to_json(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val)


# ----------------------------------------------------------------------
# Households
# ----------------------------------------------------------------------

def create_household(
    conn,
    name: str,
    reporting_currency: str = "CNY",
    started_on: Optional[date] = None,
    tz_name: str = "Asia/Singapore",
    investment_review_change_ratio: Decimal = Decimal("0.2000"),
    household_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    if started_on is None:
        started_on = date.today()
    if household_id is None:
        household_id = uuid.uuid4()

    if investment_review_change_ratio <= 0 or investment_review_change_ratio > 1:
        raise ValueError("investment_review_change_ratio must be > 0 and <= 1")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO households (
                id, name, reporting_currency, started_on, timezone,
                investment_review_change_ratio, status, row_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'active', 0
            ) RETURNING *;
            """,
            (
                str(household_id),
                name,
                reporting_currency,
                started_on,
                tz_name,
                investment_review_change_ratio,
            ),
        )
        return dict(cur.fetchone())


def get_household(conn, household_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM households WHERE id = %s;", (str(household_id),))
        row = cur.fetchone()
        return dict(row) if row else None


def update_household_settings(
    conn,
    household_id: uuid.UUID,
    expected_version: int,
    tz_name: Optional[str] = None,
    investment_review_change_ratio: Optional[Decimal] = None,
) -> Dict[str, Any]:
    if investment_review_change_ratio is not None:
        if investment_review_change_ratio <= 0 or investment_review_change_ratio > 1:
            raise ValueError("investment_review_change_ratio must be > 0 and <= 1")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE households
            SET timezone = COALESCE(%s, timezone),
                investment_review_change_ratio = COALESCE(%s, investment_review_change_ratio),
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %s AND row_version = %s
            RETURNING *;
            """,
            (
                tz_name,
                investment_review_change_ratio,
                str(household_id),
                expected_version,
            ),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Household {household_id} version mismatch or not found")
        return dict(row)


# ----------------------------------------------------------------------
# Users & Household Members
# ----------------------------------------------------------------------

def create_user(
    conn,
    auth_subject: str,
    email: str,
    display_name: str,
    user_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    if user_id is None:
        user_id = uuid.uuid4()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO users (id, auth_subject, email, display_name, status, row_version)
            VALUES (%s, %s, %s, %s, 'active', 0)
            RETURNING *;
            """,
            (str(user_id), auth_subject, email, display_name),
        )
        return dict(cur.fetchone())


def get_user_by_auth_subject(conn, auth_subject: str) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE auth_subject = %s;", (auth_subject,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_user(conn, user_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE id = %s;", (str(user_id),))
        row = cur.fetchone()
        return dict(row) if row else None


def add_household_member(
    conn,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
) -> Dict[str, Any]:
    if role not in ("owner", "member"):
        raise ValueError(f"Invalid member role: {role}")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO household_members (household_id, user_id, role, joined_at)
            VALUES (%s, %s, %s, now())
            RETURNING *;
            """,
            (str(household_id), str(user_id), role),
        )
        return dict(cur.fetchone())


def get_household_member(
    conn, household_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM household_members
            WHERE household_id = %s AND user_id = %s;
            """,
            (str(household_id), str(user_id)),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_household_members(conn, household_id: uuid.UUID) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT hm.*, u.email, u.display_name, u.status as user_status
            FROM household_members hm
            JOIN users u ON hm.user_id = u.id
            WHERE hm.household_id = %s
            ORDER BY hm.joined_at ASC;
            """,
            (str(household_id),),
        )
        return [dict(r) for r in cur.fetchall()]


# ----------------------------------------------------------------------
# Devices
# ----------------------------------------------------------------------

def create_device(
    conn,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    platform: str,
    token_hash: bytes,
    client_version: Optional[str] = None,
    device_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    if device_id is None:
        device_id = uuid.uuid4()
    if platform not in ("ios", "macos", "web", "other"):
        raise ValueError(f"Invalid device platform: {platform}")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO devices (
                id, household_id, user_id, name, platform,
                token_hash, status, client_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'active', %s
            ) RETURNING id, household_id, user_id, name, platform, status, client_version, created_at, last_seen_at;
            """,
            (
                str(device_id),
                str(household_id),
                str(user_id),
                name,
                platform,
                psycopg2.Binary(token_hash),
                client_version,
            ),
        )
        return dict(cur.fetchone())


def get_device_by_token_hash(conn, token_hash: bytes) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT d.id, d.household_id, d.user_id, d.name, d.platform, d.status,
                   d.client_version, d.created_at, d.last_seen_at, d.revoked_at,
                   u.status as user_status, hm.role as member_role
            FROM devices d
            JOIN users u ON d.user_id = u.id
            JOIN household_members hm ON d.household_id = hm.household_id AND d.user_id = hm.user_id
            WHERE d.token_hash = %s;
            """,
            (psycopg2.Binary(token_hash),),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def revoke_device(conn, household_id: uuid.UUID, device_id: uuid.UUID) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devices
            SET status = 'revoked', revoked_at = now()
            WHERE household_id = %s AND id = %s AND status = 'active';
            """,
            (str(household_id), str(device_id)),
        )
        return cur.rowcount > 0


def touch_device(conn, device_id: uuid.UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devices SET last_seen_at = now() WHERE id = %s;",
            (str(device_id),),
        )


# ----------------------------------------------------------------------
# Accounts
# ----------------------------------------------------------------------

def create_account(
    conn,
    household_id: uuid.UUID,
    name: str,
    balance_scope: str,
    account_type: str,
    currency: str,
    owner_user_id: Optional[uuid.UUID] = None,
    risk_level: Optional[str] = None,
    opened_on: Optional[date] = None,
    statement_import_enabled: bool = False,
    account_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    if account_id is None:
        account_id = uuid.uuid4()
    if opened_on is None:
        opened_on = date.today()

    if account_type not in ("cash", "savings", "investment", "credit"):
        raise ValueError(f"Invalid account_type: {account_type}")

    if account_type == "credit" and risk_level is not None:
        raise ValueError("Credit accounts must have risk_level=None")

    if risk_level is not None and risk_level not in ("very_low", "low", "medium", "high"):
        raise ValueError(f"Invalid risk_level: {risk_level}")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO accounts (
                id, household_id, name, balance_scope, account_type, currency,
                owner_user_id, risk_level, opened_on, status,
                statement_import_enabled, row_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, 0
            ) RETURNING *;
            """,
            (
                str(account_id),
                str(household_id),
                name,
                balance_scope,
                account_type,
                currency.upper(),
                str(owner_user_id) if owner_user_id else None,
                risk_level,
                opened_on,
                statement_import_enabled,
            ),
        )
        return dict(cur.fetchone())


def get_account(conn, household_id: uuid.UUID, account_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM accounts WHERE household_id = %s AND id = %s;",
            (str(household_id), str(account_id)),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_accounts(
    conn, household_id: uuid.UUID, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if status:
            cur.execute(
                """
                SELECT * FROM accounts
                WHERE household_id = %s AND status = %s
                ORDER BY name ASC;
                """,
                (str(household_id), status),
            )
        else:
            cur.execute(
                """
                SELECT * FROM accounts
                WHERE household_id = %s
                ORDER BY status ASC, name ASC;
                """,
                (str(household_id),),
            )
        return [dict(r) for r in cur.fetchall()]


def close_account(
    conn,
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    expected_version: int,
    closed_on: date,
) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE accounts
            SET status = 'closed',
                closed_on = %s,
                row_version = row_version + 1,
                updated_at = now()
            WHERE household_id = %s AND id = %s AND row_version = %s AND status = 'active'
            RETURNING *;
            """,
            (closed_on, str(household_id), str(account_id), expected_version),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Account {account_id} not found, already closed, or version mismatch")
        return dict(row)


def reopen_account(
    conn,
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    expected_version: int,
) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE accounts
            SET status = 'active',
                closed_on = NULL,
                row_version = row_version + 1,
                updated_at = now()
            WHERE household_id = %s AND id = %s AND row_version = %s AND status = 'closed'
            RETURNING *;
            """,
            (str(household_id), str(account_id), expected_version),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Account {account_id} not found, not closed, or version mismatch")
        return dict(row)


def cancel_account(
    conn,
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    expected_version: int,
) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE accounts
            SET status = 'cancelled',
                row_version = row_version + 1,
                updated_at = now()
            WHERE household_id = %s AND id = %s AND row_version = %s AND status = 'active'
            RETURNING *;
            """,
            (str(household_id), str(account_id), expected_version),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Account {account_id} not found or version mismatch")
        return dict(row)


# ----------------------------------------------------------------------
# Account Aliases
# ----------------------------------------------------------------------

def create_alias(
    conn,
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    alias_text: str,
    normalized_alias: Optional[str] = None,
) -> Dict[str, Any]:
    if normalized_alias is None:
        normalized_alias = alias_text.strip().lower()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO account_aliases (
                id, household_id, account_id, alias_text, normalized_alias, status, row_version
            ) VALUES (
                gen_random_uuid(), %s, %s, %s, %s, 'active', 0
            ) RETURNING *;
            """,
            (str(household_id), str(account_id), alias_text, normalized_alias),
        )
        return dict(cur.fetchone())


def list_aliases(
    conn, household_id: uuid.UUID, account_id: Optional[uuid.UUID] = None
) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if account_id:
            cur.execute(
                """
                SELECT * FROM account_aliases
                WHERE household_id = %s AND account_id = %s AND status = 'active'
                ORDER BY alias_text ASC;
                """,
                (str(household_id), str(account_id)),
            )
        else:
            cur.execute(
                """
                SELECT * FROM account_aliases
                WHERE household_id = %s AND status = 'active'
                ORDER BY alias_text ASC;
                """,
                (str(household_id),),
            )
        return [dict(r) for r in cur.fetchall()]


# ----------------------------------------------------------------------
# Categories
# ----------------------------------------------------------------------

def create_category(
    conn,
    household_id: uuid.UUID,
    name: str,
    category_type: str,
    description: Optional[str] = None,
    is_fallback: bool = False,
    category_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    if category_id is None:
        category_id = uuid.uuid4()

    if category_type not in ("expense", "income"):
        raise ValueError(f"Invalid category_type: {category_type}")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO categories (
                id, household_id, name, category_type, description, is_fallback, status, row_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'active', 0
            ) RETURNING *;
            """,
            (
                str(category_id),
                str(household_id),
                name,
                category_type,
                description,
                is_fallback,
            ),
        )
        return dict(cur.fetchone())


def get_category(conn, household_id: uuid.UUID, category_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM categories WHERE household_id = %s AND id = %s;",
            (str(household_id), str(category_id)),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_categories(
    conn,
    household_id: uuid.UUID,
    category_type: Optional[str] = None,
    status: Optional[str] = "active",
) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        query = "SELECT * FROM categories WHERE household_id = %s"
        params = [str(household_id)]
        if category_type:
            query += " AND category_type = %s"
            params.append(category_type)
        if status:
            query += " AND status = %s"
            params.append(status)
        query += " ORDER BY category_type ASC, is_fallback ASC, name ASC;"
        cur.execute(query, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def get_fallback_category(
    conn, household_id: uuid.UUID, category_type: str
) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM categories
            WHERE household_id = %s AND category_type = %s AND is_fallback = true AND status = 'active';
            """,
            (str(household_id), category_type),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ----------------------------------------------------------------------
# Ingestion Requests
# ----------------------------------------------------------------------

def create_ingestion_request(
    conn,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    actor_scope: str,
    idempotency_key: str,
    request_kind: str,
    operation: str,
    request_hash: Optional[str] = None,
    image_sha256: Optional[str] = None,
    device_id: Optional[uuid.UUID] = None,
    draft_payload: Optional[Dict[str, Any]] = None,
    captured_at: Optional[datetime] = None,
    client_version: Optional[str] = None,
    request_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    if request_id is None:
        request_id = uuid.uuid4()

    if len(idempotency_key) < 8 or len(idempotency_key) > 200:
        raise ValueError("idempotency_key must be between 8 and 200 characters")

    if not (actor_scope.startswith("device:") or actor_scope.startswith("user:") or actor_scope.startswith("system:")):
        raise ValueError(f"Invalid actor_scope prefix: {actor_scope}")

    if request_kind not in ("expense", "balance_capture", "statement", "command"):
        raise ValueError(f"Invalid request_kind: {request_kind}")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO ingestion_requests (
                id, household_id, user_id, device_id, actor_scope,
                idempotency_key, request_kind, operation, request_hash,
                image_sha256, status, captured_at, client_version,
                draft_payload, row_version, last_editor_scope
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, 'processing', %s, %s,
                %s, 0, %s
            ) RETURNING *;
            """,
            (
                str(request_id),
                str(household_id),
                str(user_id),
                str(device_id) if device_id else None,
                actor_scope,
                idempotency_key,
                request_kind,
                operation,
                request_hash,
                image_sha256,
                captured_at,
                client_version,
                _to_json(draft_payload),
                actor_scope,
            ),
        )
        return dict(cur.fetchone())


def get_ingestion_request_by_key(
    conn,
    household_id: uuid.UUID,
    actor_scope: str,
    idempotency_key: str,
) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM ingestion_requests
            WHERE household_id = %s AND actor_scope = %s AND idempotency_key = %s;
            """,
            (str(household_id), actor_scope, idempotency_key),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def lock_ingestion_request(conn, request_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Acquires a row lock on an ingestion request."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM ingestion_requests WHERE id = %s FOR UPDATE;",
            (str(request_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_request_draft(
    conn,
    household_id: uuid.UUID,
    request_id: uuid.UUID,
    expected_version: int,
    draft_payload: Dict[str, Any],
    last_editor_scope: str,
) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE ingestion_requests
            SET draft_payload = %s,
                last_editor_scope = %s,
                status = 'needs_confirmation',
                row_version = row_version + 1,
                updated_at = now()
            WHERE household_id = %s AND id = %s AND row_version = %s
            RETURNING *;
            """,
            (
                _to_json(draft_payload),
                last_editor_scope,
                str(household_id),
                str(request_id),
                expected_version,
            ),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Request {request_id} not found or version mismatch")
        return dict(row)


def finalize_ingestion_request(
    conn,
    household_id: uuid.UUID,
    request_id: uuid.UUID,
    expected_version: int,
    status: str,
    response_payload: Dict[str, Any],
    response_http_status: int,
    failure_code: Optional[str] = None,
) -> Dict[str, Any]:
    if status not in ("committed", "rejected", "failed"):
        raise ValueError(f"Terminal status must be committed, rejected, or failed. Got: {status}")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        committed_at_sql = "now()" if status == "committed" else "NULL"
        cur.execute(
            f"""
            UPDATE ingestion_requests
            SET status = %s,
                response_payload = %s,
                response_http_status = %s,
                failure_code = %s,
                committed_at = {committed_at_sql},
                row_version = row_version + 1,
                updated_at = now()
            WHERE household_id = %s AND id = %s AND row_version = %s
            RETURNING *;
            """,
            (
                status,
                _to_json(response_payload),
                response_http_status,
                failure_code,
                str(household_id),
                str(request_id),
                expected_version,
            ),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Request {request_id} not found or version mismatch")
        return dict(row)


# ----------------------------------------------------------------------
# Audit Events
# ----------------------------------------------------------------------

VALID_AUDIT_ACTIONS = {
    "create", "update", "void", "replace", "close", "reopen",
    "confirm_flows", "fill_reporting_fx", "acknowledge_metadata",
    "skip_period", "link_statement", "pause_schedule",
    "resume_schedule", "cancel_schedule",
}


def insert_audit_event(
    conn,
    household_id: uuid.UUID,
    actor_type: str,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    actor_user_id: Optional[uuid.UUID] = None,
    actor_device_id: Optional[uuid.UUID] = None,
    source_request_id: Optional[uuid.UUID] = None,
    before_data: Optional[Dict[str, Any]] = None,
    after_data: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    if actor_type not in ("user", "device", "system"):
        raise ValueError(f"Invalid actor_type: {actor_type}")

    if action not in VALID_AUDIT_ACTIONS:
        raise ValueError(f"Invalid audit action: {action}")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO audit_events (
                household_id, actor_type, actor_user_id, actor_device_id,
                source_request_id, entity_type, entity_id, action,
                before_data, after_data, reason, created_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, now()
            ) RETURNING *;
            """,
            (
                str(household_id),
                actor_type,
                str(actor_user_id) if actor_user_id else None,
                str(actor_device_id) if actor_device_id else None,
                str(source_request_id) if source_request_id else None,
                entity_type,
                str(entity_id),
                action,
                _to_json(before_data),
                _to_json(after_data),
                reason,
            ),
        )
        return dict(cur.fetchone())


def list_audit_events(
    conn,
    household_id: uuid.UUID,
    entity_type: Optional[str] = None,
    entity_id: Optional[uuid.UUID] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        query = "SELECT * FROM audit_events WHERE household_id = %s"
        params = [str(household_id)]
        if entity_type:
            query += " AND entity_type = %s"
            params.append(entity_type)
        if entity_id:
            query += " AND entity_id = %s"
            params.append(str(entity_id))
        query += " ORDER BY id DESC LIMIT %s;"
        params.append(limit)
        cur.execute(query, tuple(params))
        return [dict(r) for r in cur.fetchall()]
