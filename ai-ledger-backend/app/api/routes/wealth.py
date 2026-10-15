from datetime import date, datetime
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from app.api.deps import get_auth_context, get_db_connection
from app.services import wealth_reports, balance_estimates

router = APIRouter(prefix="/api/v1/reports", tags=["Last reported wealth"])

@router.get("/wealth")
def wealth(as_of: str | None = None, owner: str | None = Query(default=None,
           description="Current account owner: a household member UUID or 'unassigned'; omit for the entire household."),
           actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return wealth_reports.report(conn, actor.household_id, as_of, historical_risk=as_of is not None, owner=owner)

@router.get("/wealth-history")
def history(start: date = Query(alias="from"), end: date = Query(alias="to"), actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return wealth_reports.history(conn, actor.household_id, start, end)

class BalanceEstimate(BaseModel):
    account_id: UUID
    currency: str
    status: Literal["ready", "no_snapshot", "unsupported_account", "ambiguous_same_day", "currency_mismatch"]
    snapshot_id: UUID | None
    baseline_date: date | None
    baseline_balance: str | None
    recorded_change: str | None
    estimated_balance: str | None
    transaction_count: int = Field(ge=0)
    same_day_count: int = Field(ge=0)
    currency_mismatch_count: int = Field(ge=0)


class BalanceEstimateReport(BaseModel):
    as_of: datetime
    items: list[BalanceEstimate]


@router.get("/balance-estimates", response_model=BalanceEstimateReport)
def balance_estimate_suggestions(actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return balance_estimates.report(conn, actor.household_id)
