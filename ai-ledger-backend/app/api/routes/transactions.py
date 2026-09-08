from datetime import date
from typing import Literal, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from app.api.deps import get_db_connection, get_auth_context, require_idempotency_key
from app.domain.spending import fail
from app.repositories import spending as repo
from app.services import spending_service as service
from app.services.reference_fx_service import FrankfurterFxProvider

router = APIRouter(prefix="/api/v1/transactions", tags=["Spending"])

def get_fx_provider():
    return FrankfurterFxProvider()

class CreateTransactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_type: Literal["expense", "refund", "cash_income"]
    occurred_on: date
    original_amount: StrictStr
    original_currency: Literal["CNY", "SGD", "USD", "EUR", "JPY"]
    category_id: UUID
    account_id: Optional[UUID] = None
    merchant: Optional[str] = Field(None, max_length=240)
    remarks: Optional[str] = Field(None, max_length=2000)
    refund_of_transaction_id: Optional[UUID] = None
    account_review_acknowledged: bool = False

class PatchTransactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    occurred_on: Optional[date] = None
    original_amount: Optional[StrictStr] = None
    original_currency: Optional[Literal["CNY", "SGD", "USD", "EUR", "JPY"]] = None
    category_id: Optional[UUID] = None
    account_id: Optional[UUID] = None
    merchant: Optional[str] = Field(None, max_length=240)
    remarks: Optional[str] = Field(None, max_length=2000)
    refund_of_transaction_id: Optional[UUID] = None
    account_review_acknowledged: Optional[bool] = None
    reason: Optional[str] = Field(None, max_length=1000)

class VoidTransactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    delete_reason: str = Field(min_length=1, max_length=1000)

def page(conn, household_id, filters, cursor=None, limit=50):
    if filters.get("from") and filters.get("to") and filters["from"] > filters["to"]:
        fail("INVALID_DATE", "The date range is reversed.")
    position = None
    if cursor:
        try:
            day, identity = cursor.split("|")
            position = (date.fromisoformat(day), UUID(identity))
        except (ValueError, TypeError):
            fail("INVALID_CURSOR", "Invalid spending cursor.")
    records = repo.list_records(conn, household_id, filters, position, limit)
    more = len(records) > limit
    records = records[:limit]
    return {"items": [service.serialize(row) for row in records],
            "next_cursor": f"{records[-1]['occurred_on']}|{records[-1]['id']}" if more else None}

@router.get("")
def list_transactions(from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    transaction_type: Optional[Literal["expense", "refund", "cash_income"]] = None,
    account_id: Optional[UUID] = None, category_id: Optional[UUID] = None,
    merchant: Optional[str] = None, status: Literal["committed", "voided"] = "committed",
    missing_account: Optional[bool] = None, category_uncertain: Optional[bool] = None,
    needs_metadata_review: Optional[bool] = None, cursor: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200), actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return page(conn, actor.household_id, {"from": from_date, "to": to_date,
        "transaction_type": transaction_type, "account_id": account_id, "category_id": category_id,
        "merchant": merchant, "status": status, "missing_account": missing_account,
        "category_uncertain": category_uncertain, "needs_metadata_review": needs_metadata_review}, cursor, limit)

@router.post("")
def create_transaction(payload: CreateTransactionRequest, key=Depends(require_idempotency_key),
    actor=Depends(get_auth_context), conn=Depends(get_db_connection), provider=Depends(get_fx_provider)):
    result, code = service.command(conn, actor, key, "create", payload.model_dump(mode="json"), provider=provider)
    return JSONResponse(result, status_code=code)

@router.get("/{transaction_id}")
def get_transaction(transaction_id: UUID, actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return service.serialize(service.require_record(conn, actor.household_id, transaction_id))

@router.patch("/{transaction_id}")
def patch_transaction(transaction_id: UUID, payload: PatchTransactionRequest,
    key=Depends(require_idempotency_key), actor=Depends(get_auth_context), conn=Depends(get_db_connection), provider=Depends(get_fx_provider)):
    result, code = service.command(conn, actor, key, "patch", payload.model_dump(mode="json", exclude_unset=True), transaction_id, provider)
    return JSONResponse(result, status_code=code)

@router.post("/{transaction_id}/void")
def void_transaction(transaction_id: UUID, payload: VoidTransactionRequest,
    key=Depends(require_idempotency_key), actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    result, code = service.command(conn, actor, key, "void", payload.model_dump(mode="json"), transaction_id)
    return JSONResponse(result, status_code=code)
