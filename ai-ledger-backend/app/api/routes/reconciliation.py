from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_device
from app.db import transaction
from app.services.reference_fx_service import ReferenceFxService
import app.services.snapshot_service as snapshot_service

router = APIRouter(prefix="/api/v1/reconciliation-batches", tags=["Reconciliation"])

class BatchCommitRequest(BaseModel):
    row_version: Optional[int] = Field(None, description="Optional optimistic concurrency version")

@router.get("/{batch_id}", summary="Get Reconciliation Batch Summary")
def get_reconciliation_batch_endpoint(
    batch_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves the reconciliation batch workflow status and summary.
    """
    return snapshot_service.get_reconciliation_batch_summary(
        conn=conn,
        batch_id=batch_id,
        household_id=device["household_id"]
    )

@router.get("/{batch_id}/preview", summary="Get Reconciliation Preview")
def get_reconciliation_preview_endpoint(
    batch_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Returns read-only preview calculation for a reconciliation batch.
    Mutates zero database state.
    """
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()

    return snapshot_service.get_reconciliation_preview(
        conn=conn,
        batch_id=batch_id,
        household_id=device["household_id"],
        fx_service=fx_svc
    )

@router.post("/{batch_id}/commit", summary="Atomic Reconciliation Commit")
def commit_reconciliation_batch_endpoint(
    batch_id: UUID,
    payload: Optional[BatchCommitRequest] = None,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Atomically commits a reconciliation batch in a single transaction.
    Re-evaluates balance as-of and residual under exclusive locks.
    """
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()
    row_ver = payload.row_version if payload is not None else None

    with transaction(conn):
        result = snapshot_service.commit_reconciliation_batch(
            conn=conn,
            batch_id=batch_id,
            device=device,
            row_version=row_ver,
            fx_service=fx_svc
        )
        return result
