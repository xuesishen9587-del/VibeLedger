from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import date, datetime
from decimal import Decimal

# --- Household ---

def create_household(
    conn,
    household_id: UUID,
    name: str,
    ledger_start_date: date,
    reporting_currency: str = 'CNY',
    status: str = 'active'
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO households (id, name, reporting_currency, ledger_start_date, status)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (household_id, name, reporting_currency, ledger_start_date, status)
        )

def get_household(conn, household_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, reporting_currency, ledger_start_date, status, created_at, updated_at
            FROM households
            WHERE id = %s;
            """,
            (household_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "reporting_currency": row[2],
            "ledger_start_date": row[3],
            "status": row[4],
            "created_at": row[5],
            "updated_at": row[6]
        }

# --- User ---

def create_user(
    conn,
    user_id: UUID,
    auth_subject: str,
    display_name: str,
    email: Optional[str] = None,
    default_currency: str = 'CNY',
    status: str = 'active'
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (id, auth_subject, email, display_name, default_currency, status)
            VALUES (%s, %s, %s, %s, %s, %s);
            """,
            (user_id, auth_subject, email, display_name, default_currency, status)
        )

def get_user(conn, user_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, auth_subject, email, display_name, default_currency, status, created_at, updated_at
            FROM users
            WHERE id = %s;
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "auth_subject": row[1],
            "email": row[2],
            "display_name": row[3],
            "default_currency": row[4],
            "status": row[5],
            "created_at": row[6],
            "updated_at": row[7]
        }

# --- Membership ---

def add_household_member(
    conn,
    household_id: UUID,
    user_id: UUID,
    role: str = 'member'
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO household_members (household_id, user_id, role)
            VALUES (%s, %s, %s);
            """,
            (household_id, user_id, role)
        )

def get_household_members(conn, household_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_id, role, joined_at
            FROM household_members
            WHERE household_id = %s;
            """,
            (household_id,)
        )
        rows = cur.fetchall()
        members = []
        for r in rows:
            members.append({
                "user_id": r[0],
                "role": r[1],
                "joined_at": r[2]
            })
        return members

# --- Device ---

def create_device(
    conn,
    device_id: UUID,
    user_id: UUID,
    device_name: str,
    platform: str,
    token_hash: bytes,
    client_version: Optional[str] = None,
    status: str = 'active'
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devices (id, user_id, device_name, platform, token_hash, client_version, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
            """,
            (device_id, user_id, device_name, platform, token_hash, client_version, status)
        )

def get_device(conn, device_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_id, device_name, platform, token_hash, status, client_version, created_at, last_seen_at, revoked_at
            FROM devices
            WHERE id = %s;
            """,
            (device_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "user_id": row[1],
            "device_name": row[2],
            "platform": row[3],
            "token_hash": row[4],
            "status": row[5],
            "client_version": row[6],
            "created_at": row[7],
            "last_seen_at": row[8],
            "revoked_at": row[9]
        }

def get_device_by_token_hash(conn, token_hash: bytes) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_id, device_name, platform, token_hash, status, client_version, created_at, last_seen_at, revoked_at
            FROM devices
            WHERE token_hash = %s;
            """,
            (token_hash,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "user_id": row[1],
            "device_name": row[2],
            "platform": row[3],
            "token_hash": row[4],
            "status": row[5],
            "client_version": row[6],
            "created_at": row[7],
            "last_seen_at": row[8],
            "revoked_at": row[9]
        }

# --- Account & State ---

def create_account(
    conn,
    account_id: UUID,
    household_id: UUID,
    name: str,
    account_type: str,
    currency: str,
    institution: Optional[str] = None,
    owner_user_id: Optional[UUID] = None,
    linked_cash_account_id: Optional[UUID] = None,
    billing_day: Optional[int] = None,
    due_day: Optional[int] = None,
    status: str = 'active'
) -> None:
    """
    Atomically creates accounts row and its associated account_state row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (
                id, household_id, name, institution, account_type, currency,
                owner_user_id, linked_cash_account_id, billing_day, due_day, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                account_id, household_id, name, institution, account_type, currency,
                owner_user_id, linked_cash_account_id, billing_day, due_day, status
            )
        )
        cur.execute(
            """
            INSERT INTO account_state (
                account_id, ledger_balance, initialized_at, last_transaction_at,
                last_authoritative_snapshot_at, row_version, updated_at
            ) VALUES (%s, 0.000000, NULL, NULL, NULL, 0, now());
            """,
            (account_id,)
        )

def get_account(conn, account_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, name, institution, account_type, currency,
                   owner_user_id, linked_cash_account_id, billing_day, due_day, status,
                   row_version, created_at, updated_at
            FROM accounts
            WHERE id = %s;
            """,
            (account_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "institution": row[3],
            "account_type": row[4],
            "currency": row[5],
            "owner_user_id": row[6],
            "linked_cash_account_id": row[7],
            "billing_day": row[8],
            "due_day": row[9],
            "status": row[10],
            "row_version": row[11],
            "created_at": row[12],
            "updated_at": row[13]
        }

def get_account_state(conn, account_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id, ledger_balance, initialized_at, last_transaction_at,
                   last_authoritative_snapshot_at, row_version, updated_at
            FROM account_state
            WHERE account_id = %s;
            """,
            (account_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "account_id": row[0],
            "ledger_balance": row[1],
            "initialized_at": row[2],
            "last_transaction_at": row[3],
            "last_authoritative_snapshot_at": row[4],
            "row_version": row[5],
            "updated_at": row[6]
        }

def list_accounts(conn, household_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, name, institution, account_type, currency,
                   owner_user_id, linked_cash_account_id, billing_day, due_day, status,
                   row_version, created_at, updated_at
            FROM accounts
            WHERE household_id = %s
            ORDER BY name;
            """,
            (household_id,)
        )
        rows = cur.fetchall()
        accounts = []
        for r in rows:
            accounts.append({
                "id": r[0],
                "household_id": r[1],
                "name": r[2],
                "institution": r[3],
                "account_type": r[4],
                "currency": r[5],
                "owner_user_id": r[6],
                "linked_cash_account_id": r[7],
                "billing_day": r[8],
                "due_day": r[9],
                "status": r[10],
                "row_version": r[11],
                "created_at": r[12],
                "updated_at": r[13]
            })
        return accounts

def lock_account_state(conn, account_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Acquires an exclusive lock (FOR UPDATE) on a single account's state row.
    """
    states = lock_account_states(conn, [account_id])
    return states.get(account_id)

def lock_account_states(conn, account_ids: List[UUID]) -> Dict[UUID, Dict[str, Any]]:
    """
    Acquires exclusive locks (FOR UPDATE) on account_state rows in deterministic sorted UUID order.
    Guarantees deadlock-free concurrency across multi-account transactions (e.g. transfers).
    """
    if not account_ids:
        return {}
    
    unique_sorted_ids = sorted(list(set(account_ids)))
    locked_states: Dict[UUID, Dict[str, Any]] = {}
    
    with conn.cursor() as cur:
        for aid in unique_sorted_ids:
            cur.execute(
                """
                SELECT account_id, ledger_balance, initialized_at, last_transaction_at,
                       last_authoritative_snapshot_at, row_version, updated_at
                FROM account_state
                WHERE account_id = %s
                FOR UPDATE;
                """,
                (aid,)
            )
            row = cur.fetchone()
            if row:
                locked_states[aid] = {
                    "account_id": row[0],
                    "ledger_balance": row[1],
                    "initialized_at": row[2],
                    "last_transaction_at": row[3],
                    "last_authoritative_snapshot_at": row[4],
                    "row_version": row[5],
                    "updated_at": row[6]
                }
    return locked_states

def update_account_state_projection(
    conn,
    account_id: UUID,
    new_balance: Decimal,
    last_transaction_at: Optional[datetime] = None,
    initialized_at: Optional[datetime] = None
) -> None:
    """
    Updates the derived ledger balance projection and increments row_version.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE account_state
            SET ledger_balance = %s,
                last_transaction_at = COALESCE(%s, last_transaction_at),
                initialized_at = COALESCE(%s, initialized_at),
                row_version = row_version + 1,
                updated_at = now()
            WHERE account_id = %s;
            """,
            (new_balance, last_transaction_at, initialized_at, account_id)
        )

# --- Aliases & Categories ---

def create_account_alias(
    conn,
    alias_id: UUID,
    account_id: UUID,
    alias_text: str,
    normalized_alias: str,
    status: str = 'active'
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO account_aliases (id, account_id, alias_text, normalized_alias, status)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (alias_id, account_id, alias_text, normalized_alias, status)
        )

def list_account_aliases(conn, account_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, account_id, alias_text, normalized_alias, status, created_at, deleted_at
            FROM account_aliases
            WHERE account_id = %s;
            """,
            (account_id,)
        )
        rows = cur.fetchall()
        aliases = []
        for r in rows:
            aliases.append({
                "id": r[0],
                "account_id": r[1],
                "alias_text": r[2],
                "normalized_alias": r[3],
                "status": r[4],
                "created_at": r[5],
                "deleted_at": r[6]
            })
        return aliases

def create_category(
    conn,
    category_id: UUID,
    household_id: UUID,
    name: str,
    category_type: str,
    status: str = 'active'
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO categories (id, household_id, name, category_type, status)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (category_id, household_id, name, category_type, status)
        )

def get_category(conn, category_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, name, category_type, status, created_at, updated_at
            FROM categories
            WHERE id = %s;
            """,
            (category_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "status": row[4],
            "created_at": row[5],
            "updated_at": row[6]
        }

def list_categories(conn, household_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, name, category_type, status, created_at, updated_at
            FROM categories
            WHERE household_id = %s;
            """,
            (household_id,)
        )
        rows = cur.fetchall()
        categories = []
        for r in rows:
            categories.append({
                "id": r[0],
                "household_id": r[1],
                "name": r[2],
                "category_type": r[3],
                "status": r[4],
                "created_at": r[5],
                "updated_at": r[6]
            })
        return categories
