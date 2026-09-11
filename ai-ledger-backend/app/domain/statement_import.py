from datetime import date
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from app.domain.balance_capture import ExtractedBalance, BalanceDraftRow


class StatementConfidence(BaseModel):
    amount: float = Field(0,ge=0,le=1)
    currency: float = Field(0,ge=0,le=1)
    date: float = Field(0,ge=0,le=1)
    intent: float = Field(0,ge=0,le=1)
    category: float = Field(0,ge=0,le=1)


class ExtractedStatementLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    occurred_on: date | None = None
    posted_on: date | None = None
    amount: StrictStr | None = Field(None,max_length=80)
    currency: str | None = Field(None,max_length=3)
    merchant: str | None = Field(None,max_length=240)
    kind: Literal["expense","refund","fee","transfer","repayment","income","opening_balance","investment_trade","unknown"]
    provider_transaction_id: str | None = Field(None,max_length=200)
    category: str | None = Field(None,max_length=120)
    confidence: StatementConfidence = Field(default_factory=StatementConfidence)


class StatementExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_hint: str | None = Field(None,max_length=240)
    account_currency: str | None = Field(None,max_length=3)
    account_confidence: float = Field(0,ge=0,le=1)
    period_start: date | None = None
    period_end: date | None = None
    lines: list[ExtractedStatementLine] = Field(max_length=1000)
    closing_balance: ExtractedBalance | None = None
    processed_pages: list[int] = Field(max_length=50)
    expected_line_count: int = Field(ge=0)
    complete: bool = False


class StatementLineEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: UUID
    action: Literal["create","link_existing","skip","use_schedule_period"]
    occurred_on: date | None = None
    original_amount: StrictStr | None = Field(None,max_length=80)
    original_currency: str | None = Field(None,max_length=3)
    merchant: str | None = Field(None,max_length=240)
    transaction_type: Literal["expense","refund"] | None = None
    category_id: UUID | None = None
    remarks: str | None = Field(None,max_length=2000)
    transaction_id: UUID | None = None
    expected_transaction_version: int | None = Field(None,ge=0)
    schedule_id: UUID | None = None
    period_no: int | None = Field(None,ge=1)
    expected_schedule_version: int | None = Field(None,ge=0)
    reason: str | None = Field(None,max_length=1000)
    confirm_facts: bool = False


class StatementDraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    lines: list[StatementLineEdit] = Field(max_length=1000)
    balance: BalanceDraftRow | None = None
    period_start: date | None = None
    period_end: date | None = None
    acknowledge_partial: bool = False
    confirm_account_identity: bool = False
