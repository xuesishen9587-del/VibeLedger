from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Dict, Any
from uuid import UUID

def create_credit_card_snapshot(
    conn,
    snapshot_id: UUID,
    household_id: UUID,
    account_id: UUID,
    as_of: datetime,
    statement_period_start: Optional[date] = None,
    statement_period_end: Optional[date] = None,
    statement_balance: Optional[Decimal] = None,
    remaining_statement_due: Optional[Decimal] = None,
    unbilled_balance: Optional[Decimal] = None,
    current_outstanding: Optional[Decimal] = None,
    currency: str = "CNY",
    source: str = "statement",
    reconciliation_batch_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Inserts a credit card snapshot record. If a snapshot already exists for the given
    (reconciliation_batch_id, account_id), it updates the existing record.
    """
    query = """
        INSERT INTO credit_card_snapshots (
            id, household_id, account_id, as_of,
            statement_period_start, statement_period_end,
            statement_balance, remaining_statement_due,
            unbilled_balance, current_outstanding,
            currency, source, reconciliation_batch_id, created_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s, now()
        )
        ON CONFLICT (reconciliation_batch_id, account_id) WHERE reconciliation_batch_id IS NOT NULL
        DO UPDATE SET
            as_of = EXCLUDED.as_of,
            statement_period_start = EXCLUDED.statement_period_start,
            statement_period_end = EXCLUDED.statement_period_end,
            statement_balance = EXCLUDED.statement_balance,
            remaining_statement_due = EXCLUDED.remaining_statement_due,
            unbilled_balance = EXCLUDED.unbilled_balance,
            current_outstanding = EXCLUDED.current_outstanding,
            currency = EXCLUDED.currency,
            source = EXCLUDED.source
        RETURNING id, household_id, account_id, as_of,
                  statement_period_start, statement_period_end,
                  statement_balance, remaining_statement_due,
                  unbilled_balance, current_outstanding,
                  currency, source, reconciliation_batch_id, created_at;
    """
    with conn.cursor() as cur:
        cur.execute(query, (
            snapshot_id, household_id, account_id, as_of,
            statement_period_start, statement_period_end,
            statement_balance, remaining_statement_due,
            unbilled_balance, current_outstanding,
            currency, source, reconciliation_batch_id
        ))
        row = cur.fetchone()
        return {
            "id": row[0],
            "household_id": row[1],
            "account_id": row[2],
            "as_of": row[3],
            "statement_period_start": row[4],
            "statement_period_end": row[5],
            "statement_balance": row[6],
            "remaining_statement_due": row[7],
            "unbilled_balance": row[8],
            "current_outstanding": row[9],
            "currency": row[10],
            "source": row[11],
            "reconciliation_batch_id": row[12],
            "created_at": row[13]
        }

def get_latest_credit_card_snapshot(
    conn,
    account_id: UUID,
    household_id: Optional[UUID] = None
) -> Optional[Dict[str, Any]]:
    """
    Retrieves the latest authoritative credit card snapshot for an account.
    """
    query = """
        SELECT id, household_id, account_id, as_of,
               statement_period_start, statement_period_end,
               statement_balance, remaining_statement_due,
               unbilled_balance, current_outstanding,
               currency, source, reconciliation_batch_id, created_at
        FROM credit_card_snapshots
        WHERE account_id = %s
    """
    params = [account_id]
    if household_id is not None:
        query += " AND household_id = %s"
        params.append(household_id)

    query += " ORDER BY as_of DESC, created_at DESC LIMIT 1;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "account_id": row[2],
            "as_of": row[3],
            "statement_period_start": row[4],
            "statement_period_end": row[5],
            "statement_balance": row[6],
            "remaining_statement_due": row[7],
            "unbilled_balance": row[8],
            "current_outstanding": row[9],
            "currency": row[10],
            "source": row[11],
            "reconciliation_batch_id": row[12],
            "created_at": row[13]
        }

def get_credit_card_snapshot_by_batch_id(
    conn,
    batch_id: UUID
) -> Optional[Dict[str, Any]]:
    """
    Retrieves the credit card snapshot linked to a reconciliation batch.
    """
    query = """
        SELECT id, household_id, account_id, as_of,
               statement_period_start, statement_period_end,
               statement_balance, remaining_statement_due,
               unbilled_balance, current_outstanding,
               currency, source, reconciliation_batch_id, created_at
        FROM credit_card_snapshots
        WHERE reconciliation_batch_id = %s
        LIMIT 1;
    """
    with conn.cursor() as cur:
        cur.execute(query, (batch_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "account_id": row[2],
            "as_of": row[3],
            "statement_period_start": row[4],
            "statement_period_end": row[5],
            "statement_balance": row[6],
            "remaining_statement_due": row[7],
            "unbilled_balance": row[8],
            "current_outstanding": row[9],
            "currency": row[10],
            "source": row[11],
            "reconciliation_batch_id": row[12],
            "created_at": row[13]
        }

def get_current_credit_card_state(
    conn,
    account_id: UUID,
    household_id: UUID
) -> Optional[Dict[str, Any]]:
    """
    Deterministically computes current credit-card state by taking the latest
    authoritative statement snapshot and subtracting subsequent committed repayment
    transfers (floored at zero).
    """
    snap = get_latest_credit_card_snapshot(conn, account_id, household_id)
    if not snap:
        return None

    snap_period_end = snap.get("statement_period_end")
    snap_as_of = snap.get("as_of")
    snap_batch_id = snap.get("reconciliation_batch_id")

    query = """
        SELECT COALESCE(SUM(to_amount), 0.00)
        FROM transactions
        WHERE household_id = %s
          AND to_account_id = %s
          AND transaction_type = 'transfer'
          AND status = 'committed'
          AND deleted_at IS NULL
    """
    params: list = [household_id, account_id]

    if snap_batch_id is not None:
        query += " AND (statement_batch_id IS NULL OR statement_batch_id != %s)"
        params.append(snap_batch_id)

    if snap_period_end is not None:
        query += " AND occurred_on > %s"
        params.append(snap_period_end)
    elif snap_as_of is not None:
        query += " AND (occurred_at > %s OR (occurred_at IS NULL AND occurred_on > %s::date))"
        params.extend([snap_as_of, snap_as_of])

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        repayments = Decimal(str(row[0])) if row and row[0] is not None else Decimal("0.00")

    curr = snap["currency"]
    stmt_bal = snap["statement_balance"]
    rem_due = snap["remaining_statement_due"]
    curr_out = snap["current_outstanding"]
    unbilled_bal = snap["unbilled_balance"]

    cur_rem_due = max(rem_due - repayments, Decimal("0.00")) if rem_due is not None else None
    cur_curr_out = max(curr_out - repayments, Decimal("0.00")) if curr_out is not None else None

    return {
        "as_of": snap["as_of"],
        "statement_period_start": snap.get("statement_period_start"),
        "statement_period_end": snap.get("statement_period_end"),
        "statement_balance": stmt_bal,
        "remaining_statement_due": cur_rem_due,
        "unbilled_balance": unbilled_bal,
        "current_outstanding": cur_curr_out,
        "currency": curr,
        "source": snap.get("source")
    }
