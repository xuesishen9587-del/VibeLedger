from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, status, Query
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_device
from app.db import transaction
import app.services.investment_service as investment_service


router = APIRouter(prefix="/api/v1/investment-accounts", tags=["Investments"])


class ManualInvestmentSnapshotRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=200, description="Idempotency key for request deduplication")
    as_of: str = Field(..., description="ISO 8601 timezone-aware timestamp for valuation")
    total_asset_value: str = Field(..., description="Authoritative account-level total asset valuation")
    currency: str = Field(..., min_length=3, max_length=3, description="Currency code (e.g. CNY, USD)")
    source: str = Field("dashboard_manual", description="Snapshot capture source: dashboard_manual, shortcut, statement")


class InvestmentPnLPeriodResponse(BaseModel):
    period_id: str
    pnl_amount: str
    currency: str
    status: str


class ManualInvestmentSnapshotResponse(BaseModel):
    status: str
    snapshot_id: str
    investment_pnl: Optional[InvestmentPnLPeriodResponse] = None


class PerformancePeriodItem(BaseModel):
    period_start: str
    period_end: str
    opening_value: str
    closing_value: str
    contributions: str
    withdrawals: str
    pnl_amount: str
    status: str


class InvestmentPerformanceResponse(BaseModel):
    account_id: str
    currency: str
    periods: List[PerformancePeriodItem]


@router.post(
    "/{account_id}/snapshots",
    summary="Create Manual Investment Valuation Snapshot",
    status_code=status.HTTP_201_CREATED,
    response_model=ManualInvestmentSnapshotResponse
)
def create_investment_snapshot(
    account_id: UUID,
    payload: ManualInvestmentSnapshotRequest,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Submits an authoritative investment valuation snapshot for an investment account.
    Establishes baseline on first snapshot or computes confirmed P&L against prior snapshot.
    Updates account_state projection without generating synthetic cash transactions.
    """
    with transaction(conn):
        result = investment_service.create_manual_investment_snapshot(
            conn=conn,
            household_id=device["household_id"],
            account_id=account_id,
            payload=payload.model_dump(),
            user_id=device.get("user_id"),
            device_id=device.get("device_id") or device.get("id")
        )
        return result


@router.get(
    "/{account_id}/performance",
    summary="Get Investment Account Performance",
    status_code=status.HTTP_200_OK,
    response_model=InvestmentPerformanceResponse
)
def get_investment_account_performance(
    account_id: UUID,
    from_date: Optional[date] = Query(None, alias="from", description="Optional filter start date"),
    to_date: Optional[date] = Query(None, alias="to", description="Optional filter end date"),
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves confirmed historical investment P&L performance periods for an investment account.
    """
    with transaction(conn):
        return investment_service.get_investment_performance(
            conn=conn,
            household_id=device["household_id"],
            account_id=account_id,
            from_date=from_date,
            to_date=to_date
        )
