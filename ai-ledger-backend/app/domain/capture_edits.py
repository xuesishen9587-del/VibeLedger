"""Bounded structured edits shared by API and model revision validation."""
from datetime import date
from typing import Optional, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictStr

class ReviseRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Optional[int] = Field(None, ge=0)
    correction_note: Optional[str] = Field(None, min_length=1, max_length=2000)
    occurred_on: Optional[date] = None
    merchant: Optional[str] = Field(None, max_length=240)
    original_amount: Optional[StrictStr] = Field(None, max_length=80)
    original_currency: Optional[Literal["CNY", "USD", "SGD", "EUR", "JPY"]] = None
    from_account_id: Optional[UUID] = None
    category_id: Optional[UUID] = None
    payment_mode: Optional[Literal["one_off", "installment"]] = None
    total_periods: Optional[int] = Field(None, ge=1, le=1200)
    remarks: Optional[str] = Field(None, max_length=2000)
    intent: Optional[Literal["expense", "refund", "transfer", "repayment", "failed", "pending", "unknown"]] = None
    action: Optional[Literal["record_full_purchase", "use_schedule_period"]] = None
    schedule_id: Optional[UUID] = None
    period_no: Optional[int] = Field(None, ge=1)
    expected_schedule_version: Optional[int] = Field(None, ge=0)

