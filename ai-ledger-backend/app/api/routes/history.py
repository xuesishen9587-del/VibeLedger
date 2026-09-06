from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, Depends, Query, HTTPException, status

from app.api.deps import get_db_connection, get_authenticated_actor
import app.repositories.audit as audit_repo

router = APIRouter(prefix="/api/v1/history", tags=["History"])

@router.get("", summary="Get Entity Change History")
def get_entity_history(
    entity_type: str = Query(..., description="Target entity type, e.g. account, category, transaction"),
    entity_id: UUID = Query(..., description="Target entity ID"),
    limit: int = Query(50, ge=1, le=200, description="Items per page"),
    cursor: Optional[str] = Query(None, description="Pagination cursor (event id)"),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves append-only change history for a specific entity within the authenticated household.
    If the entity exists in another household or is unauthorized, returns 404.
    """
    household_id = device["household_id"]

    # Verify household ownership of the entity if applicable
    with conn.cursor() as cur:
        if entity_type == "account":
            cur.execute("SELECT household_id FROM accounts WHERE id = %s;", (str(entity_id),))
            row = cur.fetchone()
            if row and row[0] != household_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")
        elif entity_type == "category":
            cur.execute("SELECT household_id FROM categories WHERE id = %s;", (str(entity_id),))
            row = cur.fetchone()
            if row and row[0] != household_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found.")
        elif entity_type == "account_alias":
            cur.execute("SELECT household_id FROM account_aliases WHERE id = %s;", (str(entity_id),))
            row = cur.fetchone()
            if row and row[0] != household_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account alias not found.")

    items, next_cursor = audit_repo.list_audit_events_with_filters(
        conn=conn,
        household_id=household_id,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        cursor=cursor
    )

    formatted_items = []
    for event in items:
        formatted_items.append({
            "id": event["id"],
            "household_id": str(event["household_id"]),
            "actor_type": event["actor_type"],
            "actor_user_id": str(event["actor_user_id"]) if event.get("actor_user_id") else None,
            "actor_device_id": str(event["actor_device_id"]) if event.get("actor_device_id") else None,
            "source_request_id": str(event["source_request_id"]) if event.get("source_request_id") else None,
            "entity_type": event["entity_type"],
            "entity_id": str(event["entity_id"]),
            "action": event["action"],
            "before_data": event.get("before_data"),
            "after_data": event.get("after_data"),
            "reason": event.get("reason"),
            "created_at": event["created_at"].isoformat() if event.get("created_at") else None
        })

    return {
        "items": formatted_items,
        "next_cursor": next_cursor
    }
