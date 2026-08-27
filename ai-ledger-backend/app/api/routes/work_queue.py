from typing import Optional, Dict, Any, List
from uuid import UUID
from fastapi import APIRouter, Depends, Query

from app.api.deps import get_db_connection, get_authenticated_actor

router = APIRouter(prefix="/api/v1/work-queue", tags=["Work Queue"])


@router.get("", summary="Get Pending Work Queue")
def get_work_queue_endpoint(
    type_filter: Optional[str] = Query(None, alias="type", description="Filter by work type ('ingestion' or 'reconciliation')"),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves pending actionable workflows for the caller's household:
    - Ingestion requests in status 'needs_confirmation'
    - Reconciliation batches in status 'needs_review'
    """
    household_id = device["household_id"]
    items: List[Dict[str, Any]] = []

    with conn.cursor() as cur:
        # 1. Reconciliation batches in needs_review
        if type_filter is None or type_filter == "reconciliation":
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
            batch_rows = cur.fetchall()
            for r in batch_rows:
                b_id, b_type, b_status, b_curr, stmt_bal, residual, pend_cnt, acc_name, created_at = r
                summary_parts = [acc_name]
                if b_type == "statement":
                    summary_parts.append("Statement")
                else:
                    summary_parts.append("Snapshot Reconciliation")

                if pend_cnt and pend_cnt > 0:
                    summary_parts.append(f"{pend_cnt} items need review")
                elif residual is not None:
                    summary_parts.append(f"Residual: {residual:.2f} {b_curr}")

                items.append({
                    "work_type": "reconciliation",
                    "id": str(b_id),
                    "status": b_status,
                    "summary": " · ".join(summary_parts),
                    "created_at": created_at.isoformat() if created_at else None
                })

        # 2. Ingestion requests in needs_confirmation
        if type_filter is None or type_filter == "ingestion":
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
            ing_rows = cur.fetchall()
            for r in ing_rows:
                i_id, i_status, i_kind, draft, resp_payload, created_at = r
                summary = "Pending Draft"
                if resp_payload and isinstance(resp_payload, dict) and resp_payload.get("display_summary"):
                    summary = resp_payload["display_summary"].replace("\n", " · ")
                elif draft and isinstance(draft, dict):
                    parts = []
                    if draft.get("original_amount") and draft.get("original_currency"):
                        parts.append(f"{draft['original_amount']} {draft['original_currency']}")
                    if draft.get("merchant"):
                        parts.append(draft["merchant"])
                    if draft.get("from_account") and isinstance(draft["from_account"], dict):
                        parts.append(draft["from_account"].get("name", "Unknown Account"))
                    if parts:
                        summary = " · ".join(parts)

                items.append({
                    "work_type": "ingestion",
                    "id": str(i_id),
                    "status": i_status,
                    "summary": summary,
                    "created_at": created_at.isoformat() if created_at else None
                })

    # Sort combined items by created_at DESC
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    return {"items": items}
