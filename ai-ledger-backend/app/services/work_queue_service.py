from typing import Optional, Dict, Any, List
from uuid import UUID

from app.domain.transactions import InvalidRequestError
import app.repositories.work_queue as work_queue_repo


def get_household_work_queue(
    conn,
    household_id: UUID,
    type_filter: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieves pending actionable workflows for the caller's household.
    Strictly validates type filter and isolates household tenant boundaries.
    """
    if type_filter is not None:
        valid_types = ("reconciliation", "ingestion")
        if type_filter not in valid_types:
            raise InvalidRequestError(
                f"Invalid work queue type '{type_filter}'. Allowed values: {', '.join(valid_types)}."
            )

    items: List[Dict[str, Any]] = []

    # 1. Reconciliation batches in needs_review
    if type_filter is None or type_filter == "reconciliation":
        batch_rows = work_queue_repo.list_pending_reconciliation_batches(conn, household_id)
        for b in batch_rows:
            summary_parts = [b["account_name"]]
            if b["batch_type"] == "statement":
                summary_parts.append("Statement")
            else:
                summary_parts.append("Snapshot Reconciliation")

            pend_cnt = b.get("pending_count")
            residual = b.get("residual_amount")
            b_curr = b.get("currency", "CNY")

            if pend_cnt and pend_cnt > 0:
                summary_parts.append(f"{pend_cnt} items need review")
            elif residual is not None:
                summary_parts.append(f"Residual: {residual:.2f} {b_curr}")

            items.append({
                "work_type": "reconciliation",
                "id": str(b["id"]),
                "status": b["status"],
                "summary": " · ".join(summary_parts),
                "created_at": b["created_at"].isoformat() if b.get("created_at") else None
            })

    # 2. Ingestion requests in needs_confirmation
    if type_filter is None or type_filter == "ingestion":
        ing_rows = work_queue_repo.list_pending_ingestion_requests(conn, household_id)
        for ir in ing_rows:
            draft = ir.get("draft_payload")
            resp_payload = ir.get("response_payload")
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
                "id": str(ir["id"]),
                "status": ir["status"],
                "summary": summary,
                "created_at": ir["created_at"].isoformat() if ir.get("created_at") else None
            })

    # Deterministic sort: created_at DESC
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    return {"items": items}
