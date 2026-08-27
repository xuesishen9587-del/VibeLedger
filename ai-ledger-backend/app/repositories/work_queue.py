from typing import Optional, Dict, Any, List
from uuid import UUID


def list_pending_reconciliation_batches(conn, household_id: UUID) -> List[Dict[str, Any]]:
    """
    Fetches reconciliation batches in 'needs_review' status for the specified household.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rb.id, rb.batch_type, rb.status, rb.currency, rb.statement_balance,
                   rb.residual_amount, rb.pending_count, a.name AS account_name, rb.created_at
            FROM reconciliation_batches rb
            JOIN accounts a ON rb.account_id = a.id
            WHERE rb.household_id = %s AND rb.status = 'needs_review'
            ORDER BY rb.created_at DESC;
            """,
            (household_id,)
        )
        rows = cur.fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r[0],
            "batch_type": r[1],
            "status": r[2],
            "currency": r[3],
            "statement_balance": r[4],
            "residual_amount": r[5],
            "pending_count": r[6],
            "account_name": r[7],
            "created_at": r[8]
        })
    return results


def list_pending_ingestion_requests(conn, household_id: UUID) -> List[Dict[str, Any]]:
    """
    Fetches ingestion requests in 'needs_confirmation' status for the specified household.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ir.id, ir.status, ir.request_kind, ir.draft_payload, ir.response_payload, ir.created_at
            FROM ingestion_requests ir
            JOIN devices d ON ir.device_id = d.id
            JOIN household_members hm ON d.user_id = hm.user_id
            WHERE hm.household_id = %s AND ir.status = 'needs_confirmation'
            ORDER BY ir.created_at DESC;
            """,
            (household_id,)
        )
        rows = cur.fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r[0],
            "status": r[1],
            "request_kind": r[2],
            "draft_payload": r[3],
            "response_payload": r[4],
            "created_at": r[5]
        })
    return results
