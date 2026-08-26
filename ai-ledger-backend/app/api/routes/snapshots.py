from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_actor
from app.db import transaction
from app.services.reference_fx_service import ReferenceFxService
import app.services.snapshot_service as snapshot_service

router = APIRouter(prefix="/api/v1/accounts", tags=["Snapshots"])

class ImagePayload(BaseModel):
    mime_type: str = Field("image/jpeg", description="MIME type of the screenshot")
    base64: str = Field(..., min_length=1, description="Base64-encoded image data")

class SnapshotCreateRequest(BaseModel):
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=200, description="Idempotency key (required for device, optional for browser)")
    as_of: str = Field(..., description="Authoritative observation timestamp with timezone")
    balance: Optional[str] = Field(None, description="Observed account balance as decimal string")
    currency: Optional[str] = Field(None, description="3-letter uppercase currency code")
    source: Optional[str] = Field("dashboard_manual", description="Observation source (dashboard_manual, shortcut, statement)")
    image: Optional[ImagePayload] = Field(None, description="Optional screenshot image object")

@router.post("/{account_id}/snapshots", summary="Create Account Snapshot")
def create_account_snapshot_endpoint(
    account_id: UUID,
    payload: SnapshotCreateRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Submits an authoritative account balance observation.
    Backend converts snapshot submission into a reconciliation workflow:
    - Initial observation: creates opening_balance baseline and commits snapshot.
    - Subsequent small residual (<=200 CNY): auto-creates reconciliation_adjustment and commits snapshot.
    - Subsequent large residual (>200 CNY): enters needs_review state with zero ledger mutation.
    """
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()

    with transaction(conn):
        result = snapshot_service.create_snapshot_workflow(
            conn=conn,
            account_id=account_id,
            device=device,
            payload=payload.model_dump(exclude_unset=True),
            fx_service=fx_svc
        )
        return result
