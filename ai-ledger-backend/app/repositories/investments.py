from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID
from datetime import datetime, date, timezone
from decimal import Decimal

from app.domain.money import quantize_money, parse_decimal


def _map_snapshot_row(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "id": row[0],
        "household_id": row[1],
        "account_id": row[2],
        "as_of": row[3],
        "balance": parse_decimal(row[4]),
        "currency": row[5],
        "snapshot_type": row[6],
        "source": row[7],
        "reconciliation_batch_id": row[8],
        "source_request_id": row[9],
        "is_authoritative": row[10],
        "created_by_user_id": row[11],
        "created_at": row[12]
    }


def _map_pnl_row(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "id": row[0],
        "household_id": row[1],
        "account_id": row[2],
        "opening_snapshot_id": row[3],
        "closing_snapshot_id": row[4],
        "period_start": row[5],
        "period_end": row[6],
        "contributions_amount": parse_decimal(row[7]),
        "withdrawals_amount": parse_decimal(row[8]),
        "pnl_amount": parse_decimal(row[9]),
        "currency": row[10],
        "status": row[11],
        "calculation_version": row[12],
        "reconciliation_batch_id": row[13],
        "created_at": row[14],
        "updated_at": row[15],
        "opening_value": parse_decimal(row[16]) if len(row) > 16 and row[16] is not None else None,
        "closing_value": parse_decimal(row[17]) if len(row) > 17 and row[17] is not None else None
    }


def get_latest_authoritative_investment_valuation_snapshot(
    conn,
    household_id: UUID,
    account_id: UUID,
    before_as_of: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """
    Returns the latest authoritative investment_valuation snapshot for the given account.
    If before_as_of is specified, filters as_of < before_as_of.
    Deterministic ordering: as_of DESC, created_at DESC, id DESC.
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
        return _map_snapshot_row(row)


def create_investment_pnl_period(
    conn,
    period_id: UUID,
    household_id: UUID,
    account_id: UUID,
    opening_snapshot_id: UUID,
    closing_snapshot_id: UUID,
    period_start: datetime,
    period_end: datetime,
    contributions_amount: Decimal,
    withdrawals_amount: Decimal,
    pnl_amount: Decimal,
    currency: str,
    status: str = "confirmed",
    calculation_version: int = 1,
    reconciliation_batch_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Inserts a confirmed (or provisional) investment P&L period into investment_pnl_periods.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO investment_pnl_periods (
                id, household_id, account_id, opening_snapshot_id, closing_snapshot_id,
                period_start, period_end, contributions_amount, withdrawals_amount,
                pnl_amount, currency, status, calculation_version, reconciliation_batch_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, household_id, account_id, opening_snapshot_id, closing_snapshot_id,
                      period_start, period_end, contributions_amount, withdrawals_amount,
                      pnl_amount, currency, status, calculation_version, reconciliation_batch_id,
                      created_at, updated_at;
            """,
            (
                period_id, household_id, account_id, opening_snapshot_id, closing_snapshot_id,
                period_start, period_end, contributions_amount, withdrawals_amount,
                pnl_amount, currency, status, calculation_version, reconciliation_batch_id
            )
        )
        row = cur.fetchone()
        return _map_pnl_row(row)


def get_investment_pnl_period(conn, period_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Retrieves an investment P&L period by ID with linked opening and closing snapshot balances.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.household_id, p.account_id, p.opening_snapshot_id, p.closing_snapshot_id,
                   p.period_start, p.period_end, p.contributions_amount, p.withdrawals_amount,
                   p.pnl_amount, p.currency, p.status, p.calculation_version, p.reconciliation_batch_id,
                   p.created_at, p.updated_at,
                   s_open.balance AS opening_value,
                   s_close.balance AS closing_value
            FROM investment_pnl_periods p
            LEFT JOIN account_snapshots s_open ON s_open.id = p.opening_snapshot_id
            LEFT JOIN account_snapshots s_close ON s_close.id = p.closing_snapshot_id
            WHERE p.id = %s;
            """,
            (period_id,)
        )
        row = cur.fetchone()
        return _map_pnl_row(row)


def get_investment_pnl_period_by_closing_snapshot(conn, closing_snapshot_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Retrieves a confirmed investment P&L period where closing_snapshot_id matches.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.household_id, p.account_id, p.opening_snapshot_id, p.closing_snapshot_id,
                   p.period_start, p.period_end, p.contributions_amount, p.withdrawals_amount,
                   p.pnl_amount, p.currency, p.status, p.calculation_version, p.reconciliation_batch_id,
                   p.created_at, p.updated_at,
                   s_open.balance AS opening_value,
                   s_close.balance AS closing_value
            FROM investment_pnl_periods p
            LEFT JOIN account_snapshots s_open ON s_open.id = p.opening_snapshot_id
            LEFT JOIN account_snapshots s_close ON s_close.id = p.closing_snapshot_id
            WHERE p.closing_snapshot_id = %s
              AND p.status = 'confirmed'
            LIMIT 1;
            """,
            (closing_snapshot_id,)
        )
        row = cur.fetchone()
        return _map_pnl_row(row)


def list_investment_pnl_periods(
    conn,
    household_id: UUID,
    account_id: UUID,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    status: Optional[str] = "confirmed"
) -> List[Dict[str, Any]]:
    """
    Lists investment P&L periods for an account joined with opening and closing valuations.
    Deterministic ordering: period_start ASC, period_end ASC, id ASC.
    """
    query = """
        SELECT p.id, p.household_id, p.account_id, p.opening_snapshot_id, p.closing_snapshot_id,
               p.period_start, p.period_end, p.contributions_amount, p.withdrawals_amount,
               p.pnl_amount, p.currency, p.status, p.calculation_version, p.reconciliation_batch_id,
               p.created_at, p.updated_at,
               op.balance AS opening_value,
               cl.balance AS closing_value
        FROM investment_pnl_periods p
        JOIN account_snapshots op ON p.opening_snapshot_id = op.id
        JOIN account_snapshots cl ON p.closing_snapshot_id = cl.id
        WHERE p.household_id = %s
          AND p.account_id = %s
    """
    params: List[Any] = [household_id, account_id]
    if status:
        query += " AND p.status = %s"
        params.append(status)
    if from_date:
        query += " AND p.period_end::date >= %s"
        params.append(from_date)
    if to_date:
        query += " AND p.period_start::date <= %s"
        params.append(to_date)

    query += " ORDER BY p.period_start ASC, p.period_end ASC, p.id ASC;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        return [_map_pnl_row(r) for r in rows if r]


def get_known_committed_transfers(
    conn,
    household_id: UUID,
    account_id: UUID,
    account_currency: str,
    opening_as_of: datetime,
    closing_as_of: datetime
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Retrieves committed transfer capital flows in (opening_as_of, closing_as_of] for an investment account.
    Returns (contributions, withdrawals).
    """
    opening_date = opening_as_of.astimezone(timezone.utc).date() if isinstance(opening_as_of, datetime) else opening_as_of
    closing_date = closing_as_of.astimezone(timezone.utc).date() if isinstance(closing_as_of, datetime) else closing_as_of

    # Contributions: transfers into the investment account (to_account_id = account_id)
    query_contrib = """
        SELECT id, occurred_on, occurred_at, to_amount AS amount, to_currency AS currency,
               from_account_id, to_account_id, remarks, status
        FROM transactions
        WHERE household_id = %s
          AND transaction_type = 'transfer'
          AND to_account_id = %s
          AND status = 'committed'
          AND deleted_at IS NULL
          AND (
              (occurred_at IS NOT NULL AND occurred_at > %s AND occurred_at <= %s)
              OR (occurred_at IS NULL AND occurred_on > %s AND occurred_on <= %s)
          )
        ORDER BY COALESCE(occurred_at, occurred_on::timestamp with time zone) ASC, id ASC;
    """
    params_contrib = (household_id, account_id, opening_as_of, closing_as_of, opening_date, closing_date)

    # Withdrawals: transfers out of the investment account (from_account_id = account_id)
    query_withdr = """
        SELECT id, occurred_on, occurred_at, from_amount AS amount, from_currency AS currency,
               from_account_id, to_account_id, remarks, status
        FROM transactions
        WHERE household_id = %s
          AND transaction_type = 'transfer'
          AND from_account_id = %s
          AND status = 'committed'
          AND deleted_at IS NULL
          AND (
              (occurred_at IS NOT NULL AND occurred_at > %s AND occurred_at <= %s)
              OR (occurred_at IS NULL AND occurred_on > %s AND occurred_on <= %s)
          )
        ORDER BY COALESCE(occurred_at, occurred_on::timestamp with time zone) ASC, id ASC;
    """
    params_withdr = (household_id, account_id, opening_as_of, closing_as_of, opening_date, closing_date)

    with conn.cursor() as cur:
        cur.execute(query_contrib, params_contrib)
        c_rows = cur.fetchall()
        contributions = []
        for r in c_rows:
            amt = parse_decimal(r[3])
            curr = str(r[4]).strip().upper()
            if curr == account_currency.upper():
                contributions.append({
                    "id": r[0],
                    "occurred_on": r[1],
                    "occurred_at": r[2],
                    "amount": amt,
                    "currency": curr,
                    "from_account_id": r[5],
                    "to_account_id": r[6],
                    "description": r[7],
                    "status": r[8]
                })

        cur.execute(query_withdr, params_withdr)
        w_rows = cur.fetchall()
        withdrawals = []
        for r in w_rows:
            amt = parse_decimal(r[3])
            curr = str(r[4]).strip().upper()
            if curr == account_currency.upper():
                withdrawals.append({
                    "id": r[0],
                    "occurred_on": r[1],
                    "occurred_at": r[2],
                    "amount": amt,
                    "currency": curr,
                    "from_account_id": r[5],
                    "to_account_id": r[6],
                    "description": r[7],
                    "status": r[8]
                })

    return contributions, withdrawals
