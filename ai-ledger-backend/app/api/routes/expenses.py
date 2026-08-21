from typing import Optional, Dict, Any
from datetime import datetime
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.api.deps import get_db_connection, get_authenticated_device
from app.db import transaction
import app.services.expense_service as expense_service
from app.services.gemini_service import GeminiService
from app.services.reference_fx_service import ReferenceFxService

router = APIRouter(prefix="/api/v1/expenses", tags=["Expenses"])

class ImagePayload(BaseModel):
    mime_type: str = Field("image/jpeg", description="MIME type of the screenshot")
    base64: str = Field(..., min_length=1, description="Base64-encoded image data")

class CreateExpenseRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=200, description="Client-generated unique idempotency key")
    captured_at: datetime = Field(..., description="Client capture timestamp with timezone")
    client_version: Optional[str] = Field(None, description="Client app/shortcut version")
    image: ImagePayload = Field(..., description="Screenshot image object")
    note: Optional[str] = Field(None, description="Optional user note")

    @field_validator("captured_at")
    @classmethod
    def validate_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("captured_at must be a timezone-aware timestamp (e.g. ISO 8601 with offset).")
        return v

@router.post("", summary="Idempotent Expense Ingestion")
def create_expense(
    payload: CreateExpenseRequest,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Ingests an expense receipt screenshot via iPhone Shortcut.
    Processes idempotency, runs expense-only AI extraction, deterministic validation,
    and commits one-off expense, foreign-card estimated expense, or installment plan.
    """
    gemini_svc = getattr(router, "_gemini_service", None) or GeminiService()
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()

    with transaction(conn):
        result = expense_service.process_expense_request(
            conn=conn,
            device=device,
            payload=payload.model_dump(),
            gemini_service=gemini_svc,
            reference_fx_service=fx_svc
        )
        return result
