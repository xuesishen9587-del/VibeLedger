from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime
from fastapi import APIRouter, Depends, Query

from app.api.deps import get_db_connection, get_authenticated_actor
import app.repositories.audit as audit_repo

router = APIRouter(prefix="/api/v1/audit-events", tags=["Audit Events"])


@router.get("", summary="List Audit Events (Read-Only)")
def list_audit_events_endpoint(
    entity_type: Optional[str] = Query(None, description="Filter by entity type (e.g. transaction, account, device)"),
    entity_id: Optional[UUID] = Query(None, description="Filter by entity ID"),
    from_date: Optional[datetime] = Query(None, alias="from", description="Filter events created at or after timestamp"),
    to_date: Optional[datetime] = Query(None, alias="to", description="Filter events created at or before timestamp"),
    actor_user_id: Optional[UUID] = Query(None, description="Filter by acting user ID"),
    limit: int = Query(50, ge=1, le=200, description="Items per page (1-200)"),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves immutable audit event history for the caller's household. Read-only.
    """
    items, next_cursor = audit_repo.list_audit_events_with_filters(
        conn=conn,
        household_id=device["household_id"],
        entity_type=entity_type,
        entity_id=entity_id,
        from_date=from_date,
        to_date=to_date,
        actor_user_id=actor_user_id,
        limit=limit,
        cursor=cursor
    )

    formatted_items = []
    for event in items:
        formatted_items.append({
            "id": event["id"],
            "household_id": str(event["household_id"]),
            "actor_type": event["actor_type"],
            "actor_user_id": str(event["actor_user_id"]) if event["actor_user_id"] else None,
            "actor_device_id": str(event["actor_device_id"]) if event["actor_device_id"] else None,
            "request_id": str(event["request_id"]) if event["request_id"] else None,
            "reconciliation_batch_id": str(event["reconciliation_batch_id"]) if event["reconciliation_batch_id"] else None,
            "entity_type": event["entity_type"],
            "entity_id": str(event["entity_id"]),
            "action": event["action"],
            "before_data": event["before_data"],
            "after_data": event["after_data"],
            "metadata": event["metadata"],
            "created_at": event["created_at"].isoformat() if event["created_at"] else None
        })

    return {
        "items": formatted_items,
        "next_cursor": next_cursor
    }
