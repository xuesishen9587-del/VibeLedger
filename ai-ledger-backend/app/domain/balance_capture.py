from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictStr, AwareDatetime


class Confidence(BaseModel):
    amount: float = Field(0,ge=0,le=1)
    currency: float = Field(0,ge=0,le=1)
    account: float = Field(0,ge=0,le=1)
    scope: float = Field(0,ge=0,le=1)
    date: float = Field(0,ge=0,le=1)


class ExtractedBalance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: str = Field(min_length=1,max_length=80)
    label: str = Field(max_length=240)
    account: str | None = Field(None,max_length=240)
    amount: StrictStr | None = Field(None,max_length=80)
    currency: str | None = Field(None,max_length=3)
    meaning: Literal["asset","debt","overpayment","total","unsupported"] = "unsupported"
    debt_scope: Literal["total_debt","outstanding_principal","monthly_bill","unknown"] = "unknown"
    as_of: str | None = Field(None,max_length=40)
    current_screen: bool = False
    display_unit: StrictStr = Field("0.01",max_length=30)
    approximate: bool = False
    overlap_uncertain: bool = True
    confidence: Confidence = Field(default_factory=Confidence)


class ExtractedTotal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: StrictStr = Field(max_length=80)
    currency: str = Field(max_length=3)
    covered_row_ids: list[str] = Field(max_length=50)
    scope: Literal["complete","incomplete","uncertain"]
    display_unit: StrictStr = Field("0.01",max_length=30)
    explicitly_rounded: bool = False


class BalanceExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[ExtractedBalance] = Field(min_length=1,max_length=50)
    totals: list[ExtractedTotal] = Field(default_factory=list,max_length=20)


class BalanceDraftRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: str = Field(min_length=1,max_length=80)
    selected: bool
    account_id: UUID | None = None
    balance: StrictStr | None = Field(None,max_length=80)
    currency: str | None = Field(None,max_length=3)
    as_of: AwareDatetime | None = None
    time_basis: Literal["explicit","capture","date_only"] | None = None
    expected_latest_snapshot_id: UUID | None = None
    expected_account_version: int | None = Field(None,ge=0)
    exclusion_reason: str | None = Field(None,max_length=1000)
    reuse_snapshot_id: UUID | None = None


class BalanceDraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    rows: list[BalanceDraftRow] = Field(min_length=1,max_length=50)
    acknowledge_evidence: bool = False
