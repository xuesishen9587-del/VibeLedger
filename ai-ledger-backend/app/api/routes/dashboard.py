from typing import Optional, Dict, Any
from datetime import date
from fastapi import APIRouter, Depends, Query

from app.api.deps import get_db_connection, get_authenticated_device
from app.services.reference_fx_service import ReferenceFxService
import app.services.dashboard_service as dashboard_service

router = APIRouter(prefix="/api/v1/dashboard", tags=["Dashboard"])

@router.get("/overview", summary="Dashboard Balance Sheet Overview")
def get_overview(
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Returns total assets, total liabilities, net worth, and data freshness metrics.
    """
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()
    return dashboard_service.get_overview(
        conn=conn,
        household_id=device["household_id"],
        fx_service=fx_svc
    )

@router.get("/cash-flow", summary="Dashboard Cash Flow Summary")
def get_cash_flow(
    from_date: date = Query(..., alias="from", description="Start date (YYYY-MM-DD) inclusive"),
    to_date: date = Query(..., alias="to", description="End date (YYYY-MM-DD) inclusive"),
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Computes cash income, ordinary expenses + fees - refunds, and net cash flow.
    Strictly excludes transfers, opening balances, reconciliation adjustments, and investment PnL.
    """
    fx_svc = getattr(router, "_reference_fx_service", None) or ReferenceFxService()
    return dashboard_service.get_cash_flow(
        conn=conn,
        household_id=device["household_id"],
        from_date=from_date,
        to_date=to_date,
        fx_service=fx_svc
    )

@router.get("/investments", summary="Dashboard Investment PnL Summary")
def get_investments(
    from_date: Optional[date] = Query(None, alias="from", description="Start date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, alias="to", description="End date (YYYY-MM-DD)"),
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Aggregates confirmed investment PnL periods for the household.
    """
    return dashboard_service.get_investments_summary(
        conn=conn,
        household_id=device["household_id"],
        from_date=from_date,
        to_date=to_date
    )

@router.get("/account-freshness", summary="Dashboard Account Freshness")
def get_account_freshness(
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Returns days since last authoritative balance confirmation per active account.
    """
    return dashboard_service.get_account_freshness(
        conn=conn,
        household_id=device["household_id"]
    )
