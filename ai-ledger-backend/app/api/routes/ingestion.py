from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, Depends, Body
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_device
from app.db import transaction
import app.services.expense_service as expense_service
from app.services.reference_fx_service import ReferenceFxService
from app.services.gemini_service import GeminiService

router = APIRouter(prefix="/api/v1/ingestion-requests", tags=["Ingestion Requests"])

class ReviseRequestPayload(BaseModel):
    correction_note: Optional[str] = Field(None, description="Natural language correction note")
    occurred_on: Optional[str] = Field(None, description="Structured date revision (YYYY-MM-DD)")
    merchant: Optional[str] = Field(None, description="Structured merchant revision")
    original_amount: Optional[str] = Field(None, description="Structured amount revision")
    original_currency: Optional[str] = Field(None, description="Structured currency revision")
    from_account_id: Optional[UUID] = Field(None, description="Structured account ID revision")
    category_id: Optional[UUID] = Field(None, description="Structured category ID revision")

class RejectRequestPayload(BaseModel):
    reason: Optional[str] = Field("User rejected draft", description="Reason for rejecting the ingestion request")

@router.get("/by-key/{idempotency_key}", summary="Recover Ingestion Request by Idempotency Key")
def get_by_key(
    idempotency_key: str,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves the current state and replayable response of an ingestion request
    within the authenticated device scope.
    """
    return expense_service.get_by_idempotency_key(
        conn=conn,
        device_id=device["device_id"],
        idempotency_key=idempotency_key
    )

@router.post("/{request_id}/confirm", summary="Confirm Pending Draft Request")
def confirm_request(
    request_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Confirms a needs_confirmation draft request.
    Re-validates against current accounts/categories and atomically commits the transaction or installment plan.
    """
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()
    with transaction(conn):
        return expense_service.confirm_ingestion_request(
            conn=conn,
            request_id=request_id,
            device=device,
            reference_fx_service=fx_svc
        )

@router.post("/{request_id}/revise", summary="Revise Pending Draft Request")
def revise_request(
    request_id: UUID,
    payload: ReviseRequestPayload = Body(default_factory=ReviseRequestPayload),
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Revises a pending draft request using natural-language correction notes or structured fields.
    Maintains the exact same request identity and idempotency key.
    """
    gemini_svc = getattr(router, "_gemini_service", None) or GeminiService()
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()

    structured_fields = {
        k: v for k, v in payload.model_dump().items()
        if k != "correction_note" and v is not None
    }

    with transaction(conn):
        return expense_service.revise_ingestion_request(
            conn=conn,
            request_id=request_id,
            device=device,
            correction_note=payload.correction_note,
            structured_fields=structured_fields if structured_fields else None,
            gemini_service=gemini_svc,
            reference_fx_service=fx_svc
        )

@router.post("/{request_id}/reject", summary="Reject Pending Ingestion Request")
def reject_request(
    request_id: UUID,
    payload: RejectRequestPayload = Body(default_factory=RejectRequestPayload),
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Rejects a pending or confirmable ingestion request.
    Produces zero financial transactions, balance changes, or installment plans.
    """
    with transaction(conn):
        return expense_service.reject_ingestion_request(
            conn=conn,
            request_id=request_id,
            device=device,
            reason=payload.reason
        )
