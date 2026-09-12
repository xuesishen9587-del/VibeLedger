from datetime import date
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from app.api.deps import get_auth_context, get_db_connection, require_browser_auth, require_idempotency_key
from app.domain.investment_gains import PeriodInput, VoidInput, ReviewSettings
from app.services import investment_gains as service

router=APIRouter(prefix="/api/v1",tags=["Investment interval gains"])


@router.get("/reports/investments")
def report(account_id: UUID | None = None, start: date | None = Query(None,alias="from"),
           end: date | None = Query(None,alias="to"), actor=Depends(get_auth_context),conn=Depends(get_db_connection)):
    return service.report(conn,actor.household_id,account_id,start,end)


@router.put("/investment-period-inputs")
def put(payload: PeriodInput,actor=Depends(require_browser_auth),key=Depends(require_idempotency_key),conn=Depends(get_db_connection)):
    result,code=service.put(conn,actor,key,payload.model_dump(mode="json"))
    return JSONResponse(result,status_code=code)


@router.post("/investment-period-inputs/{identity}/void")
def void(identity: UUID,payload: VoidInput,actor=Depends(require_browser_auth),key=Depends(require_idempotency_key),conn=Depends(get_db_connection)):
    result,code=service.void(conn,actor,key,identity,payload.model_dump(mode="json"))
    return JSONResponse(result,status_code=code)


@router.patch("/household-settings")
def settings(payload: ReviewSettings,actor=Depends(require_browser_auth),key=Depends(require_idempotency_key),conn=Depends(get_db_connection)):
    result,code=service.update_settings(conn,actor,key,payload.model_dump(mode="json"))
    return JSONResponse(result,status_code=code)
