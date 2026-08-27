from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, Query

from app.api.deps import get_db_connection, get_authenticated_actor
import app.services.work_queue_service as work_queue_service

router = APIRouter(prefix="/api/v1/work-queue", tags=["Work Queue"])


@router.get("", summary="Get Pending Work Queue")
def get_work_queue_endpoint(
    type_filter: Optional[str] = Query(None, alias="type", description="Filter by work type ('ingestion' or 'reconciliation')"),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves pending actionable workflows for the caller's household.
    Routes to work_queue_service for domain query orchestration.
    """
    return work_queue_service.get_household_work_queue(
        conn=conn,
        household_id=device["household_id"],
        type_filter=type_filter
    )
