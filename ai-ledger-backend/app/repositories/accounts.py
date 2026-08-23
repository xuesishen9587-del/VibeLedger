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

add_user_to_household = add_household_member


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

def list_accounts(
    conn,
    household_id: UUID,
    status: Optional[str] = None,
    account_type: Optional[str] = None,
    owner_user_id: Optional[UUID] = None
) -> List[Dict[str, Any]]:
    """
    Lists accounts for a household with state projection (ledger_balance, last_authoritative_snapshot_at).
    Supports optional filtering by status, account_type, and owner_user_id.
    """
    query = """
        SELECT a.id, a.household_id, a.name, a.institution, a.account_type, a.currency,
               a.owner_user_id, a.linked_cash_account_id, a.billing_day, a.due_day, a.status,
               a.row_version, a.created_at, a.updated_at,
               s.ledger_balance, s.last_authoritative_snapshot_at
        FROM accounts a
        LEFT JOIN account_state s ON s.account_id = a.id
        WHERE a.household_id = %(household_id)s
    """
    params: Dict[str, Any] = {"household_id": household_id}

    if status is not None:
        query += " AND a.status = %(status)s"
        params["status"] = status
    if account_type is not None:
        query += " AND a.account_type = %(account_type)s"
        params["account_type"] = account_type
    if owner_user_id is not None:
        query += " AND a.owner_user_id = %(owner_user_id)s"
        params["owner_user_id"] = owner_user_id

    query += " ORDER BY a.name ASC;"

    with conn.cursor() as cur:
        cur.execute(query, params)
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
                "updated_at": r[13],
                "ledger_balance": r[14] if r[14] is not None else Decimal("0"),
                "last_authoritative_snapshot_at": r[15]
            })
        return accounts

def get_account_with_state(conn, account_id: UUID, household_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    query = """
        SELECT a.id, a.household_id, a.name, a.institution, a.account_type, a.currency,
               a.owner_user_id, a.linked_cash_account_id, a.billing_day, a.due_day, a.status,
               a.row_version, a.created_at, a.updated_at,
               s.ledger_balance, s.last_authoritative_snapshot_at
        FROM accounts a
        LEFT JOIN account_state s ON s.account_id = a.id
        WHERE a.id = %s
    """
    params = [account_id]
    if household_id is not None:
        query += " AND a.household_id = %s"
        params.append(household_id)

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
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
            "updated_at": row[13],
            "ledger_balance": row[14] if row[14] is not None else Decimal("0"),
            "last_authoritative_snapshot_at": row[15]
        }

def check_account_name_exists(
    conn,
    household_id: UUID,
    name: str,
    exclude_account_id: Optional[UUID] = None
) -> bool:
    query = """
        SELECT 1 FROM accounts
        WHERE household_id = %s
          AND lower(name) = lower(%s)
          AND status = 'active'
    """
    params = [household_id, name.strip()]
    if exclude_account_id is not None:
        query += " AND id <> %s"
        params.append(exclude_account_id)
    query += " LIMIT 1;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        return cur.fetchone() is not None

def has_financial_history(conn, account_id: UUID) -> bool:
    """
    Checks if account has any committed financial transactions or snapshots.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM transactions
            WHERE (from_account_id = %s OR to_account_id = %s)
              AND deleted_at IS NULL
            LIMIT 1;
            """,
            (account_id, account_id)
        )
        if cur.fetchone():
            return True

        cur.execute(
            """
            SELECT 1 FROM account_snapshots
            WHERE account_id = %s
            LIMIT 1;
            """,
            (account_id,)
        )
        if cur.fetchone():
            return True

        cur.execute(
            """
            SELECT 1 FROM credit_card_snapshots
            WHERE account_id = %s
            LIMIT 1;
            """,
            (account_id,)
        )
        if cur.fetchone():
            return True

    return False

def check_user_in_household(conn, user_id: UUID, household_id: UUID) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM household_members
            WHERE user_id = %s AND household_id = %s;
            """,
            (user_id, household_id)
        )
        return cur.fetchone() is not None

def update_account(
    conn,
    account_id: UUID,
    name: str,
    institution: Optional[str],
    owner_user_id: Optional[UUID],
    linked_cash_account_id: Optional[UUID],
    billing_day: Optional[int],
    due_day: Optional[int],
    account_type: Optional[str] = None,
    currency: Optional[str] = None,
    expected_row_version: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    Updates mutable metadata on accounts table with optimistic row_version checking.
    """
    with conn.cursor() as cur:
        query = """
            UPDATE accounts
            SET name = %s,
                institution = %s,
                owner_user_id = %s,
                linked_cash_account_id = %s,
                billing_day = %s,
                due_day = %s,
                account_type = COALESCE(%s, account_type),
                currency = COALESCE(%s, currency),
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %s
        """
        params = [
            name, institution, owner_user_id, linked_cash_account_id,
            billing_day, due_day, account_type, currency, account_id
        ]
        if expected_row_version is not None:
            query += " AND row_version = %s"
            params.append(expected_row_version)

        query += " RETURNING id, household_id, name, institution, account_type, currency, owner_user_id, linked_cash_account_id, billing_day, due_day, status, row_version, created_at, updated_at;"
        cur.execute(query, tuple(params))
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


def deactivate_account(
    conn,
    account_id: UUID,
    expected_row_version: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    Soft-deactivates an account (status='inactive') and increments row_version.
    """
    with conn.cursor() as cur:
        query = """
            UPDATE accounts
            SET status = 'inactive',
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %s
        """
        params = [account_id]
        if expected_row_version is not None:
            query += " AND row_version = %s"
            params.append(expected_row_version)

        query += " RETURNING id, household_id, name, institution, account_type, currency, owner_user_id, linked_cash_account_id, billing_day, due_day, status, row_version, created_at, updated_at;"
        cur.execute(query, tuple(params))
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

def check_account_alias_exists(conn, account_id: UUID, normalized_alias: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM account_aliases
            WHERE account_id = %s
              AND normalized_alias = %s
              AND deleted_at IS NULL
              AND status = 'active'
            LIMIT 1;
            """,
            (account_id, normalized_alias)
        )
        return cur.fetchone() is not None

def get_account_alias(conn, alias_id: UUID, account_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    query = """
        SELECT id, account_id, alias_text, normalized_alias, status, created_at, deleted_at
        FROM account_aliases
        WHERE id = %s
    """
    params = [alias_id]
    if account_id is not None:
        query += " AND account_id = %s"
        params.append(account_id)

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "account_id": row[1],
            "alias_text": row[2],
            "normalized_alias": row[3],
            "status": row[4],
            "created_at": row[5],
            "deleted_at": row[6]
        }

def deactivate_account_alias(conn, alias_id: UUID, account_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE account_aliases
            SET status = 'inactive',
                deleted_at = now()
            WHERE id = %s AND account_id = %s AND deleted_at IS NULL
            RETURNING id, account_id, alias_text, normalized_alias, status, created_at, deleted_at;
            """,
            (alias_id, account_id)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "account_id": row[1],
            "alias_text": row[2],
            "normalized_alias": row[3],
            "status": row[4],
            "created_at": row[5],
            "deleted_at": row[6]
        }

def list_account_aliases(conn, account_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, account_id, alias_text, normalized_alias, status, created_at, deleted_at
            FROM account_aliases
            WHERE account_id = %s AND deleted_at IS NULL AND status = 'active'
            ORDER BY created_at ASC;
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

def update_account_state_after_reconciliation(
    conn,
    account_id: UUID,
    new_balance: Decimal,
    snapshot_as_of: datetime,
    last_transaction_at: Optional[datetime] = None
) -> None:
    """
    Updates the derived ledger balance projection and last_authoritative_snapshot_at after reconciliation commit.
    Guarantees last_authoritative_snapshot_at is never moved backwards, sets initialized_at on first authoritative baseline,
    and updates last_transaction_at coherently.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE account_state
            SET ledger_balance = %s,
                last_authoritative_snapshot_at = CASE
                    WHEN last_authoritative_snapshot_at IS NULL THEN %s
                    WHEN %s > last_authoritative_snapshot_at THEN %s
                    ELSE last_authoritative_snapshot_at
                END,
                initialized_at = COALESCE(initialized_at, %s),
                last_transaction_at = CASE
                    WHEN %s IS NULL THEN last_transaction_at
                    WHEN last_transaction_at IS NULL THEN %s
                    WHEN %s > last_transaction_at THEN %s
                    ELSE last_transaction_at
                END,
                row_version = row_version + 1,
                updated_at = now()
            WHERE account_id = %s;
            """,
            (
                new_balance,
                snapshot_as_of, snapshot_as_of, snapshot_as_of,
                snapshot_as_of,
                last_transaction_at, last_transaction_at, last_transaction_at, last_transaction_at,
                account_id
            )
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

list_accounts_for_household = list_accounts
list_categories_for_household = list_categories
