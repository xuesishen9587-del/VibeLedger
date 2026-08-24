from typing import Optional, Dict, Any
from uuid import UUID
from datetime import date
from fastapi import APIRouter, Depends, UploadFile, File, Form, status
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_device
from app.config import get_settings
from app.db import transaction
from app.domain.transactions import InvalidRequestError
from app.services.reference_fx_service import ReferenceFxService
import app.services.statement_service as statement_service

router = APIRouter(prefix="/api/v1/accounts", tags=["Statements"])


@router.post("/{account_id}/statements", summary="Upload and Process Account Statement PDF", status_code=status.HTTP_201_CREATED)
async def upload_account_statement_endpoint(
    account_id: UUID,
    file: UploadFile = File(..., description="Statement PDF file"),
    password: Optional[str] = Form(None, description="Optional PDF decryption password"),
    period_start: Optional[date] = Form(None, description="Optional statement start date override"),
    period_end: Optional[date] = Form(None, description="Optional statement end date override"),
    default_expense_category_id: Optional[UUID] = Form(None, description="Default expense category ID"),
    default_income_category_id: Optional[UUID] = Form(None, description="Default income category ID"),
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Synchronously uploads and reconciles a statement PDF against the selected account.
    Returns the initial reconciliation batch state and metrics.
    """
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()
    parser = getattr(router, "_statement_parser", None)

    settings = get_settings()
    max_bytes = settings.MAX_STATEMENT_PDF_BYTES
    content = bytearray()
    chunk_size = 1024 * 1024
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > max_bytes:
            raise InvalidRequestError("Uploaded statement PDF exceeds the maximum allowed file size of 20MB.")

    file_bytes = bytes(content)

    with transaction(conn):
        result = statement_service.upload_and_process_statement(
            conn=conn,
            household_id=device["household_id"],
            account_id=account_id,
            file_bytes=file_bytes,
            filename=file.filename,
            password=password,
            period_start=period_start,
            period_end=period_end,
            default_expense_category_id=default_expense_category_id,
            default_income_category_id=default_income_category_id,
            user_id=device.get("user_id"),
            device_id=device.get("id"),
            parser=parser,
            fx_service=fx_svc
        )
        return result
