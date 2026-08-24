from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
from datetime import date, datetime
from decimal import Decimal
from pydantic import BaseModel, Field, ConfigDict

from app.domain.money import parse_decimal, quantize_money, validate_currency_code


class ParsedStatementLine(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_page_no: Optional[int] = Field(None, ge=1)
    source_row_no: Optional[int] = Field(None, ge=1)
    transaction_on: Optional[date] = None
    posted_on: Optional[date] = None
    description_raw: str = Field(..., min_length=1)
    amount: Decimal = Field(..., gt=0)
    currency: str = Field(..., min_length=3, max_length=3)
    direction: str = Field(..., pattern="^(debit|credit|unknown)$")
    line_type: str = Field(..., pattern="^(expense|income|transfer|refund|fee|unknown)$")
    original_amount: Optional[Decimal] = None
    original_currency: Optional[str] = None
    merchant_hint: Optional[str] = None
    external_reference: Optional[str] = None
    confidence: Optional[Decimal] = None


class StatementExtractionResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    period_start: Optional[date] = None
    period_end: Optional[date] = None
    statement_date: Optional[date] = None
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None
    statement_balance: Optional[Decimal] = None
    remaining_statement_due: Optional[Decimal] = None
    unbilled_balance: Optional[Decimal] = None
    current_outstanding: Optional[Decimal] = None
    currency: Optional[str] = None
    lines: List[ParsedStatementLine] = []
    metadata: Dict[str, Any] = {}
    parser_version: str = "gemini-statement-v1.0"
