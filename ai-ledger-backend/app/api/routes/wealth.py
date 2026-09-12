from datetime import date
from fastapi import APIRouter, Depends, Query
from app.api.deps import get_auth_context, get_db_connection
from app.services import wealth_reports

router = APIRouter(prefix="/api/v1/reports", tags=["Last reported wealth"])

@router.get("/wealth")
def wealth(as_of: str | None = None, actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return wealth_reports.report(conn, actor.household_id, as_of, historical_risk=as_of is not None)

@router.get("/wealth-history")
def history(start: date = Query(alias="from"), end: date = Query(alias="to"), actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return wealth_reports.history(conn, actor.household_id, start, end)
