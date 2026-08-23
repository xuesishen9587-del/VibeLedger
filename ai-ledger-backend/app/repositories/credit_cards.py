from typing import Optional, Dict, Any
from uuid import UUID

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
               currency, source, created_at
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
            "created_at": row[12]
        }
