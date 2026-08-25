from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import datetime
from decimal import Decimal

def create_account_snapshot(
    conn,
    snapshot_id: UUID,
    household_id: UUID,
    account_id: UUID,
    as_of: datetime,
    balance: Decimal,
    currency: str,
    snapshot_type: str = "balance",
    source: str = "dashboard_manual",
    reconciliation_batch_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    is_authoritative: bool = True,
    created_by_user_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Inserts a row into account_snapshots.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO account_snapshots (
                id, household_id, account_id, as_of, balance, currency,
                snapshot_type, source, reconciliation_batch_id, source_request_id,
                is_authoritative, created_by_user_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, household_id, account_id, as_of, balance, currency,
                      snapshot_type, source, reconciliation_batch_id, source_request_id,
                      is_authoritative, created_by_user_id, created_at;
            """,
            (
                snapshot_id, household_id, account_id, as_of, balance, currency,
                snapshot_type, source, reconciliation_batch_id, source_request_id,
                is_authoritative, created_by_user_id
            )
        )
        row = cur.fetchone()
        return _map_snapshot_row(row)

def get_snapshot(conn, snapshot_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, account_id, as_of, balance, currency,
                   snapshot_type, source, reconciliation_batch_id, source_request_id,
                   is_authoritative, created_by_user_id, created_at
            FROM account_snapshots
            WHERE id = %s;
            """,
            (snapshot_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_snapshot_row(row)

def get_snapshot_by_batch_id(conn, reconciliation_batch_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, account_id, as_of, balance, currency,
                   snapshot_type, source, reconciliation_batch_id, source_request_id,
                   is_authoritative, created_by_user_id, created_at
            FROM account_snapshots
            WHERE reconciliation_batch_id = %s
            ORDER BY created_at DESC
            LIMIT 1;
            """,
            (reconciliation_batch_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_snapshot_row(row)

def get_latest_authoritative_snapshot(
    conn,
    account_id: UUID,
    as_of_dt: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """
    Returns the latest authoritative snapshot for the given account at or before as_of_dt.
    """
    with conn.cursor() as cur:
        if as_of_dt is not None:
            cur.execute(
                """
                SELECT id, household_id, account_id, as_of, balance, currency,
                       snapshot_type, source, reconciliation_batch_id, source_request_id,
                       is_authoritative, created_by_user_id, created_at
                FROM account_snapshots
                WHERE account_id = %s AND is_authoritative = true AND as_of <= %s
                ORDER BY as_of DESC, created_at DESC
                LIMIT 1;
                """,
                (account_id, as_of_dt)
            )
        else:
            cur.execute(
                """
                SELECT id, household_id, account_id, as_of, balance, currency,
                       snapshot_type, source, reconciliation_batch_id, source_request_id,
                       is_authoritative, created_by_user_id, created_at
                FROM account_snapshots
                WHERE account_id = %s AND is_authoritative = true
                ORDER BY as_of DESC, created_at DESC
                LIMIT 1;
                """,
                (account_id,)
            )
        row = cur.fetchone()
        if not row:
            return None
        return _map_snapshot_row(row)

def list_snapshots_for_account(
    conn,
    account_id: UUID,
    limit: int = 50
) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, account_id, as_of, balance, currency,
                   snapshot_type, source, reconciliation_batch_id, source_request_id,
                   is_authoritative, created_by_user_id, created_at
            FROM account_snapshots
            WHERE account_id = %s
            ORDER BY as_of DESC, created_at DESC
            LIMIT %s;
            """,
            (account_id, limit)
        )
        rows = cur.fetchall()
        return [_map_snapshot_row(r) for r in rows]

def get_latest_authoritative_investment_valuation_snapshot(
    conn,
    household_id: UUID,
    account_id: UUID,
    before_as_of: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """
    Returns the latest authoritative investment_valuation snapshot for the given account.
    Filters by snapshot_type = 'investment_valuation' and is_authoritative = true.
    """
    query = """
        SELECT id, household_id, account_id, as_of, balance, currency,
               snapshot_type, source, reconciliation_batch_id, source_request_id,
               is_authoritative, created_by_user_id, created_at
        FROM account_snapshots
        WHERE household_id = %s
          AND account_id = %s
          AND snapshot_type = 'investment_valuation'
          AND is_authoritative = true
    """
    params: List[Any] = [household_id, account_id]
    if before_as_of is not None:
        query += " AND as_of < %s"
        params.append(before_as_of)

    query += " ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return _map_snapshot_row(row)


def _map_snapshot_row(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "household_id": row[1],
        "account_id": row[2],
        "as_of": row[3],
        "balance": row[4],
        "currency": row[5],
        "snapshot_type": row[6],
        "source": row[7],
        "reconciliation_batch_id": row[8],
        "source_request_id": row[9],
        "is_authoritative": row[10],
        "created_by_user_id": row[11],
        "created_at": row[12]
    }

