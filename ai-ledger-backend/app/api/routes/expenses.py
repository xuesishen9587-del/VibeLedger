from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator
from app.api.deps import get_db_connection, require_device_auth
from app.api.routes.transactions import get_fx_provider
from app.domain.capture_images import MAX_ENCODED
from app.services import expense_capture, capture_receipts
from app.services.gemini_service import GeminiService

router = APIRouter(prefix="/api/v1/expenses", tags=["Expense capture"])

def get_capture_model():
    return GeminiService()

class ImagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mime_type: str = "image/jpeg"
    base64: str = Field(min_length=1, max_length=MAX_ENCODED)

class CreateExpenseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=200)
    captured_at: datetime
    client_version: Optional[str] = Field(None, max_length=120)
    image: ImagePayload
    note: Optional[str] = Field(None, max_length=2000)

    @field_validator("captured_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Capture timestamp requires a timezone.")
        return value

def capture_response(result):
    payload, status = result
    return JSONResponse(payload, status_code=status, headers={"Retry-After": "2"} if status == 202 else None)

@router.post("")
def create_expense(payload: CreateExpenseRequest, actor=Depends(require_device_auth),
                   conn=Depends(get_db_connection), model=Depends(get_capture_model), provider=Depends(get_fx_provider)):
    factory = capture_receipts.connection_factory(conn)
    return capture_response(expense_capture.process(factory, actor, payload.model_dump(), model, provider))
