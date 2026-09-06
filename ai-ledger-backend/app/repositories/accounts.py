from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
from datetime import date, datetime
from decimal import Decimal
import psycopg2

from app.repositories import household_members as repo_household_members
from app.repositories import users as repo_users
from app.repositories import devices as repo_devices
from app.repositories import categories as repo_categories

# --- Backward compatibility delegation ---

def create_household(
    conn,
    household_id: UUID,
    name: str,
    reporting_currency: str = 'CNY',
    started_on: Optional[date] = None,
    ledger_start_date: Optional[date] = None,
    tz_name: str = 'Asia/Singapore',
    status: str = 'active'
) -> Dict[str, Any]:
    return repo_household_members.create_household(
        conn, household_id, name, reporting_currency,
        started_on=started_on, ledger_start_date=ledger_start_date,
        tz_name=tz_name, status=status
    )

def get_household(conn, household_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, reporting_currency, started_on, timezone, investment_review_change_ratio, status, row_version, created_at, updated_at
            FROM households
            WHERE id = %s;
            """,
            (household_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        if len(row) >= 10:
            return {
                "id": row[0],
                "name": row[1],
                "reporting_currency": row[2],
                "started_on": row[3],
                "ledger_start_date": row[3],  # alias
                "timezone": row[4],
                "investment_review_change_ratio": row[5],
                "status": row[6],
                "row_version": row[7],
                "created_at": row[8],
                "updated_at": row[9]
            }
        return {
            "id": row[0],
            "name": row[1],
            "reporting_currency": row[2],
            "started_on": None,
            "ledger_start_date": None,
            "timezone": "Asia/Singapore",
            "investment_review_change_ratio": Decimal("0.2000"),
            "status": row[3] if len(row) > 3 else "active",
            "row_version": row[4] if len(row) > 4 else 0,
            "created_at": row[5] if len(row) > 5 else None,
            "updated_at": row[6] if len(row) > 6 else None
        }

def create_user(
    conn,
    user_id: UUID,
    auth_subject: str,
    display_name: str,
    email: Optional[str] = None,
    default_currency: str = 'CNY',
    status: str = 'active'
) -> Dict[str, Any]:
    return repo_users.create_user(
        conn, user_id=user_id, auth_subject=auth_subject, display_name=display_name, email=email, status=status
    )

def get_user(conn, user_id: UUID) -> Optional[Dict[str, Any]]:
    return repo_users.get_user_by_id(conn, user_id)

def add_household_member(conn, household_id: UUID, user_id: UUID, role: str = 'member') -> None:
    repo_household_members.add_household_member(conn, household_id, user_id, role)

add_user_to_household = add_household_member

def get_household_members(conn, household_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, role, joined_at FROM household_members WHERE household_id = %s;", (household_id,))
        return [{"user_id": r[0], "role": r[1], "joined_at": r[2]} for r in cur.fetchall()]

def create_device(
    conn,
    device_id: UUID,
    user_id: UUID,
    device_name: str,
    platform: str,
    token_hash: bytes,
    client_version: Optional[str] = None,
    status: str = 'active',
    household_id: Optional[UUID] = None
) -> None:
    repo_devices.create_device(
        conn, device_id=device_id, user_id=user_id, device_name=device_name,
        platform=platform, token_hash=token_hash, client_version=client_version,
        status=status, household_id=household_id
    )

def get_device(conn, device_id: UUID) -> Optional[Dict[str, Any]]:
    return repo_devices.get_device_by_id(conn, device_id)

def get_device_by_token_hash(conn, token_hash: bytes) -> Optional[Dict[str, Any]]:
    return repo_devices.get_device_by_token_hash(conn, token_hash)

def create_category(conn, *args, **kwargs) -> Dict[str, Any]:
    if len(args) >= 2 and (isinstance(args[0], UUID) or (isinstance(args[0], str) and len(args[0]) == 36 and '-' in args[0])) and (isinstance(args[1], UUID) or (isinstance(args[1], str) and len(args[1]) == 36 and '-' in args[1])):
        category_id = UUID(str(args[0]))
        household_id = UUID(str(args[1]))
        name = str(args[2]) if len(args) > 2 else kwargs.get("name", "")
        category_type = str(args[3]) if len(args) > 3 else kwargs.get("category_type", "expense")
        description = args[4] if len(args) > 4 else kwargs.get("description")
        is_fallback = args[5] if len(args) > 5 else kwargs.get("is_fallback", False)
        status = args[6] if len(args) > 6 else kwargs.get("status", "active")
        return repo_categories.create_category(
            conn, household_id=household_id, name=name, category_type=category_type,
            description=description, is_fallback=is_fallback, category_id=category_id,
            status=status
        )
    return repo_categories.create_category(conn, *args, **kwargs)

# --- Simplified Account Repository ---

def create_account(
    conn,
    account_id: Optional[UUID] = None,
    household_id: Optional[UUID] = None,
    name: str = "",
    account_type: str = "cash",
    currency: str = "CNY",
    balance_scope: str = "Main account balance",
    owner_user_id: Optional[UUID] = None,
    risk_level: Optional[str] = None,
    opened_on: Optional[date] = None,
    closed_on: Optional[date] = None,
    statement_import_enabled: bool = False,
    status: str = "active",
    **kwargs
) -> Dict[str, Any]:
    """
    Creates an account record matching the simplified schema.
    Does NOT create account_state, opening transactions, or snapshots.
    """
    if account_id is None:
        account_id = uuid4()
    if opened_on is None:
        opened_on = date.today()

    if account_type not in ("cash", "savings", "investment", "credit"):
        raise ValueError(f"Invalid account_type: {account_type}")

    if account_type == "credit" and risk_level is not None:
        raise ValueError("Credit accounts must have risk_level=None")

    if risk_level is not None and risk_level not in ("very_low", "low", "medium", "high"):
        raise ValueError(f"Invalid risk_level: {risk_level}")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (
                id, household_id, name, balance_scope, account_type, currency,
                owner_user_id, risk_level, opened_on, closed_on, status,
                statement_import_enabled, row_version, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, now(), now())
            RETURNING id, household_id, name, balance_scope, account_type, currency,
                      owner_user_id, risk_level, opened_on, closed_on, status,
                      statement_import_enabled, row_version, created_at, updated_at;
            """,
            (
                account_id, household_id, name.strip(), balance_scope.strip(),
                account_type, currency.upper(), owner_user_id, risk_level,
                opened_on, closed_on, status, statement_import_enabled
            )
        )
        row = cur.fetchone()
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "balance_scope": row[3],
            "account_type": row[4],
            "currency": row[5],
            "owner_user_id": row[6],
            "risk_level": row[7],
            "opened_on": row[8],
            "closed_on": row[9],
            "status": row[10],
            "statement_import_enabled": row[11],
            "row_version": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

def get_account(conn, account_id: UUID, household_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    query = """
        SELECT id, household_id, name, balance_scope, account_type, currency,
               owner_user_id, risk_level, opened_on, closed_on, status,
               statement_import_enabled, row_version, created_at, updated_at
        FROM accounts
        WHERE id = %s
    """
    params: List[Any] = [account_id]
    if household_id is not None:
        query += " AND household_id = %s"
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
            "balance_scope": row[3],
            "account_type": row[4],
            "currency": row[5],
            "owner_user_id": row[6],
            "risk_level": row[7],
            "opened_on": row[8],
            "closed_on": row[9],
            "status": row[10],
            "statement_import_enabled": row[11],
            "row_version": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

def get_account_with_state(conn, account_id: UUID, household_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    return get_account(conn, account_id, household_id)

def list_accounts(
    conn,
    household_id: UUID,
    status: Optional[str] = None,
    account_type: Optional[str] = None,
    owner_user_id: Optional[UUID] = None
) -> List[Dict[str, Any]]:
    query = """
        SELECT id, household_id, name, balance_scope, account_type, currency,
               owner_user_id, risk_level, opened_on, closed_on, status,
               statement_import_enabled, row_version, created_at, updated_at
        FROM accounts
        WHERE household_id = %(household_id)s
    """
    params: Dict[str, Any] = {"household_id": household_id}

    if status is not None:
        query += " AND status = %(status)s"
        params["status"] = status
    if account_type is not None:
        query += " AND account_type = %(account_type)s"
        params["account_type"] = account_type
    if owner_user_id is not None:
        query += " AND owner_user_id = %(owner_user_id)s"
        params["owner_user_id"] = owner_user_id

    query += " ORDER BY status ASC, name ASC;"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        accounts = []
        for r in rows:
            accounts.append({
                "id": r[0],
                "household_id": r[1],
                "name": r[2],
                "balance_scope": r[3],
                "account_type": r[4],
                "currency": r[5],
                "owner_user_id": r[6],
                "risk_level": r[7],
                "opened_on": r[8],
                "closed_on": r[9],
                "status": r[10],
                "statement_import_enabled": r[11],
                "row_version": r[12],
                "created_at": r[13],
                "updated_at": r[14],
            })
        return accounts

def check_account_name_exists(
    conn,
    household_id: UUID,
    name: str,
    exclude_account_id: Optional[UUID] = None
) -> bool:
    query = "SELECT 1 FROM accounts WHERE household_id = %s AND lower(name) = lower(%s) AND status = 'active'"
    params: List[Any] = [household_id, name.strip()]
    if exclude_account_id is not None:
        query += " AND id <> %s"
        params.append(exclude_account_id)
    query += " LIMIT 1;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        return cur.fetchone() is not None

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

def has_financial_history(conn, account_id: UUID) -> bool:
    """
    Checks if account has any committed financial transactions, snapshots, schedules, inputs, or statement lines.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM transactions WHERE account_id = %s LIMIT 1;", (account_id,))
        if cur.fetchone():
            return True
        cur.execute("SELECT 1 FROM account_snapshots WHERE account_id = %s LIMIT 1;", (account_id,))
        if cur.fetchone():
            return True
        cur.execute("SELECT 1 FROM spending_schedules WHERE account_id = %s LIMIT 1;", (account_id,))
        if cur.fetchone():
            return True
        cur.execute("SELECT 1 FROM investment_period_inputs WHERE account_id = %s LIMIT 1;", (account_id,))
        if cur.fetchone():
            return True
        cur.execute("SELECT 1 FROM statement_lines WHERE account_id = %s LIMIT 1;", (account_id,))
        if cur.fetchone():
            return True
    return False

def update_account(
    conn,
    account_id: UUID,
    name: Optional[str] = None,
    balance_scope: Optional[str] = None,
    owner_user_id: Optional[UUID] = None,
    risk_level: Optional[str] = None,
    statement_import_enabled: Optional[bool] = None,
    opened_on: Optional[date] = None,
    account_type: Optional[str] = None,
    currency: Optional[str] = None,
    expected_row_version: Optional[int] = None,
    household_id: Optional[UUID] = None,
    fields_set: Optional[set] = None,
    **kwargs
) -> Optional[Dict[str, Any]]:
    set_clauses = ["updated_at = now()"]
    params: List[Any] = []

    if fields_set is not None:
        if "name" in fields_set and name is not None:
            set_clauses.append("name = %s")
            params.append(name.strip())
        if "balance_scope" in fields_set and balance_scope is not None:
            set_clauses.append("balance_scope = %s")
            params.append(balance_scope.strip())
        if "owner_user_id" in fields_set:
            set_clauses.append("owner_user_id = %s")
            params.append(owner_user_id)
        if "risk_level" in fields_set:
            set_clauses.append("risk_level = %s")
            params.append(risk_level)
        if "statement_import_enabled" in fields_set and statement_import_enabled is not None:
            set_clauses.append("statement_import_enabled = %s")
            params.append(statement_import_enabled)
        if "opened_on" in fields_set and opened_on is not None:
            set_clauses.append("opened_on = %s")
            params.append(opened_on)
        if "account_type" in fields_set and account_type is not None:
            set_clauses.append("account_type = %s")
            params.append(account_type)
        if "currency" in fields_set and currency is not None:
            set_clauses.append("currency = %s")
            params.append(currency.upper())
    else:
        if name is not None:
            set_clauses.append("name = %s")
            params.append(name.strip())
        if balance_scope is not None:
            set_clauses.append("balance_scope = %s")
            params.append(balance_scope.strip())
        if owner_user_id is not None:
            set_clauses.append("owner_user_id = %s")
            params.append(owner_user_id)
        if risk_level is not None:
            set_clauses.append("risk_level = %s")
            params.append(risk_level)
        if statement_import_enabled is not None:
            set_clauses.append("statement_import_enabled = %s")
            params.append(statement_import_enabled)
        if opened_on is not None:
            set_clauses.append("opened_on = %s")
            params.append(opened_on)
        if account_type is not None:
            set_clauses.append("account_type = %s")
            params.append(account_type)
        if currency is not None:
            set_clauses.append("currency = %s")
            params.append(currency.upper())

    set_clauses.append("row_version = row_version + 1")

    where_clauses = ["id = %s"]
    params.append(account_id)

    if household_id is not None:
        where_clauses.append("household_id = %s")
        params.append(household_id)

    if expected_row_version is not None:
        where_clauses.append("row_version = %s")
        params.append(expected_row_version)

    query = f"""
        UPDATE accounts
        SET {', '.join(set_clauses)}
        WHERE {' AND '.join(where_clauses)}
        RETURNING id, household_id, name, balance_scope, account_type, currency,
                  owner_user_id, risk_level, opened_on, closed_on, status,
                  statement_import_enabled, row_version, created_at, updated_at;
    """

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "balance_scope": row[3],
            "account_type": row[4],
            "currency": row[5],
            "owner_user_id": row[6],
            "risk_level": row[7],
            "opened_on": row[8],
            "closed_on": row[9],
            "status": row[10],
            "statement_import_enabled": row[11],
            "row_version": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

def validate_closing_snapshot_for_close(
    conn,
    household_id: UUID,
    account_id: UUID,
    closing_snapshot_id: UUID,
    account: Dict[str, Any],
    closed_on: date
) -> Dict[str, Any]:
    """
    Validates all canonical conditions for closing an account:
    - closing snapshot exists
    - belongs to household and account
    - is active
    - matching currency
    - is an explicit zero observation (balance == 0)
    - satisfies lifetime: closed_on >= opened_on, and snapshot as_of <= closed_on
    - NO later active observation exists
    """
    if closed_on < account["opened_on"]:
        raise ValueError("closed_on cannot be earlier than opened_on")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, account_id, as_of, balance, currency, status
            FROM account_snapshots
            WHERE household_id = %s AND account_id = %s AND id = %s;
            """,
            (household_id, account_id, closing_snapshot_id)
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Closing snapshot {closing_snapshot_id} not found for account {account_id}")

        snap_id, snap_hh, snap_acc, snap_as_of, snap_balance, snap_currency, snap_status = row

        if snap_status != "active":
            raise ValueError(f"Closing snapshot {closing_snapshot_id} is not active (status: {snap_status})")

        if snap_currency != account["currency"]:
            raise ValueError(f"Closing snapshot currency {snap_currency} does not match account currency {account['currency']}")

        if Decimal(str(snap_balance)) != Decimal("0"):
            raise ValueError(f"Closing snapshot balance must be explicit zero, got: {snap_balance}")

        # as_of date boundary check
        snap_date = snap_as_of.date() if hasattr(snap_as_of, "date") else snap_as_of
        if snap_date > closed_on:
            raise ValueError(f"Closing snapshot as_of ({snap_date}) cannot be later than closed_on ({closed_on})")

        # Check for any later active observation
        cur.execute(
            """
            SELECT id, as_of FROM account_snapshots
            WHERE household_id = %s AND account_id = %s AND status = 'active' AND as_of > %s
            LIMIT 1;
            """,
            (household_id, account_id, snap_as_of)
        )
        later_snap = cur.fetchone()
        if later_snap:
            raise ValueError(f"Cannot close account: later active balance observation exists (snapshot id: {later_snap[0]}, as_of: {later_snap[1]})")

        return {
            "id": snap_id,
            "as_of": snap_as_of,
            "balance": snap_balance,
            "currency": snap_currency,
            "status": snap_status
        }

def close_account(
    conn,
    household_id: UUID,
    account_id: UUID,
    expected_version: int,
    closed_on: date,
) -> Optional[Dict[str, Any]]:
    """
    Closes an active account with explicit closed_on date and row_version check.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts
            SET status = 'closed',
                closed_on = %s,
                row_version = row_version + 1,
                updated_at = now()
            WHERE household_id = %s AND id = %s AND row_version = %s AND status = 'active'
            RETURNING id, household_id, name, balance_scope, account_type, currency,
                      owner_user_id, risk_level, opened_on, closed_on, status,
                      statement_import_enabled, row_version, created_at, updated_at;
            """,
            (closed_on, household_id, account_id, expected_version)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "balance_scope": row[3],
            "account_type": row[4],
            "currency": row[5],
            "owner_user_id": row[6],
            "risk_level": row[7],
            "opened_on": row[8],
            "closed_on": row[9],
            "status": row[10],
            "statement_import_enabled": row[11],
            "row_version": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

def reopen_account(
    conn,
    household_id: UUID,
    account_id: UUID,
    expected_version: int,
) -> Optional[Dict[str, Any]]:
    """
    Reopens a closed account, clearing closed_on.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts
            SET status = 'active',
                closed_on = NULL,
                row_version = row_version + 1,
                updated_at = now()
            WHERE household_id = %s AND id = %s AND row_version = %s AND status = 'closed'
            RETURNING id, household_id, name, balance_scope, account_type, currency,
                      owner_user_id, risk_level, opened_on, closed_on, status,
                      statement_import_enabled, row_version, created_at, updated_at;
            """,
            (household_id, account_id, expected_version)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "balance_scope": row[3],
            "account_type": row[4],
            "currency": row[5],
            "owner_user_id": row[6],
            "risk_level": row[7],
            "opened_on": row[8],
            "closed_on": row[9],
            "status": row[10],
            "statement_import_enabled": row[11],
            "row_version": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

def cancel_account(
    conn,
    household_id: UUID,
    account_id: UUID,
    expected_version: int,
) -> Optional[Dict[str, Any]]:
    """
    Cancels an unused account with no financial references.
    """
    if has_financial_history(conn, account_id):
        raise ValueError("Cannot cancel account with existing financial references.")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts
            SET status = 'cancelled',
                row_version = row_version + 1,
                updated_at = now()
            WHERE household_id = %s AND id = %s AND row_version = %s AND status = 'active'
            RETURNING id, household_id, name, balance_scope, account_type, currency,
                      owner_user_id, risk_level, opened_on, closed_on, status,
                      statement_import_enabled, row_version, created_at, updated_at;
            """,
            (household_id, account_id, expected_version)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "balance_scope": row[3],
            "account_type": row[4],
            "currency": row[5],
            "owner_user_id": row[6],
            "risk_level": row[7],
            "opened_on": row[8],
            "closed_on": row[9],
            "status": row[10],
            "statement_import_enabled": row[11],
            "row_version": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

def deactivate_account(conn, account_id: UUID, expected_row_version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Legacy deactivate alias: maps to close_account with today's date."""
    acc = get_account(conn, account_id)
    if not acc:
        return None
    ver = expected_row_version if expected_row_version is not None else acc["row_version"]
    return close_account(conn, acc["household_id"], account_id, ver, date.today())

# --- Aliases ---

def create_account_alias(
    conn,
    alias_id: UUID,
    account_id: UUID,
    alias_text: str,
    normalized_alias: str,
    status: str = 'active',
    household_id: Optional[UUID] = None,
) -> None:
    if household_id is None:
        acc = get_account(conn, account_id)
        if acc:
            household_id = acc["household_id"]
        else:
            raise ValueError(f"Account {account_id} not found")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO account_aliases (id, household_id, account_id, alias_text, normalized_alias, status, row_version, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, 0, now(), now());
            """,
            (alias_id, household_id, account_id, alias_text, normalized_alias, status)
        )

def list_account_aliases(conn, account_id: UUID, household_id: Optional[UUID] = None) -> List[Dict[str, Any]]:
    query = "SELECT id, household_id, account_id, alias_text, normalized_alias, status, row_version, created_at, updated_at FROM account_aliases WHERE account_id = %s"
    params: List[Any] = [account_id]
    if household_id is not None:
        query += " AND household_id = %s"
        params.append(household_id)
    query += " ORDER BY status ASC, alias_text ASC;"
    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "household_id": r[1],
                "account_id": r[2],
                "alias_text": r[3],
                "normalized_alias": r[4],
                "status": r[5],
                "row_version": r[6],
                "created_at": r[7],
                "updated_at": r[8],
            }
            for r in rows
        ]

def get_account_alias(conn, alias_id: UUID, account_id: Optional[UUID] = None, household_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    query = "SELECT id, household_id, account_id, alias_text, normalized_alias, status, row_version, created_at, updated_at FROM account_aliases WHERE id = %s"
    params: List[Any] = [alias_id]
    if account_id is not None:
        query += " AND account_id = %s"
        params.append(account_id)
    if household_id is not None:
        query += " AND household_id = %s"
        params.append(household_id)

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "account_id": row[2],
            "alias_text": row[3],
            "normalized_alias": row[4],
            "status": row[5],
            "row_version": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }

def check_account_alias_exists(conn, account_id: UUID, normalized_alias: str, exclude_alias_id: Optional[UUID] = None, household_id: Optional[UUID] = None) -> bool:
    query = """
        SELECT 1 FROM account_aliases
        WHERE account_id = %s
          AND normalized_alias = %s
          AND status = 'active'
    """
    params: List[Any] = [account_id, normalized_alias.strip().lower()]
    if household_id is not None:
        query += " AND household_id = %s"
        params.append(household_id)
    if exclude_alias_id is not None:
        query += " AND id <> %s"
        params.append(exclude_alias_id)
    query += " LIMIT 1;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        return cur.fetchone() is not None

def update_account_alias(
    conn,
    household_id: UUID,
    account_id: UUID,
    alias_id: UUID,
    expected_version: Optional[int] = None,
    alias_text: Optional[str] = None,
    status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    set_clauses = ["updated_at = now()", "row_version = row_version + 1"]
    params: List[Any] = []
    if alias_text is not None:
        set_clauses.append("alias_text = %s")
        params.append(alias_text.strip())
        set_clauses.append("normalized_alias = %s")
        params.append(alias_text.strip().lower())
    if status is not None:
        set_clauses.append("status = %s")
        params.append(status)

    where_clauses = ["household_id = %s", "account_id = %s", "id = %s"]
    params.extend([household_id, account_id, alias_id])
    if expected_version is not None:
        where_clauses.append("row_version = %s")
        params.append(expected_version)

    query = f"""
        UPDATE account_aliases
        SET {', '.join(set_clauses)}
        WHERE {' AND '.join(where_clauses)}
        RETURNING id, household_id, account_id, alias_text, normalized_alias, status, row_version, created_at, updated_at;
    """
    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "account_id": row[2],
            "alias_text": row[3],
            "normalized_alias": row[4],
            "status": row[5],
            "row_version": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }

def deactivate_account_alias(
    conn,
    alias_id: UUID,
    account_id: UUID,
    household_id: Optional[UUID] = None,
    expected_version: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    where_clauses = ["account_id = %s", "id = %s", "status = 'active'"]
    params: List[Any] = [account_id, alias_id]
    if household_id is not None:
        where_clauses.append("household_id = %s")
        params.append(household_id)
    if expected_version is not None:
        where_clauses.append("row_version = %s")
        params.append(expected_version)

    query = f"""
        UPDATE account_aliases
        SET status = 'inactive',
            row_version = row_version + 1,
            updated_at = now()
        WHERE {' AND '.join(where_clauses)}
        RETURNING id, household_id, account_id, alias_text, normalized_alias, status, row_version, created_at, updated_at;
    """
    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "account_id": row[2],
            "alias_text": row[3],
            "normalized_alias": row[4],
            "status": row[5],
            "row_version": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }

# --- Category delegations ---
create_category = repo_categories.create_category
get_category = repo_categories.get_category
list_categories = repo_categories.list_categories
list_accounts_for_household = list_accounts
list_categories_for_household = list_categories
