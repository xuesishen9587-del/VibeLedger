from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from app.api.deps import get_db_connection, get_auth_context, require_idempotency_key
from app.domain.balances import BalanceUpdate, SnapshotAction, SnapshotCorrection
from app.services import balance_service as service
from app.repositories import spending as repo

router = APIRouter(prefix="/api/v1", tags=["Balance observations"])

@router.post("/balance-updates")
def save(payload: BalanceUpdate, actor=Depends(get_auth_context), conn=Depends(get_db_connection), key=Depends(require_idempotency_key)):
    result, status = service.save(conn, actor, key, payload.model_dump(mode="json"))
    return JSONResponse(result, status_code=status)

@router.get("/accounts/{account_id}/snapshots")
def snapshots(account_id: UUID, include_voided: bool = False, cursor: UUID | None = None,
              limit: int = Query(50, ge=1, le=200), actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    heads = service.head_fields(conn, actor.household_id, account_id)
    rows = repo.rows(conn, "SELECT * FROM account_snapshots WHERE household_id=%s AND account_id=%s "
        "AND (%s OR status='active') AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
        (actor.household_id, account_id, include_voided, cursor, cursor, limit+1))
    return {**heads, "items": [service.output(r) for r in rows[:limit]],
            "next_cursor": str(rows[limit-1]["id"]) if len(rows)>limit else None}

@router.post("/snapshots/{identity}/correct")
def correct(identity: UUID, payload: SnapshotCorrection, actor=Depends(get_auth_context), conn=Depends(get_db_connection), key=Depends(require_idempotency_key)):
    result, status = service.change(conn, actor, key, identity, payload.model_dump(mode="json"), correcting=True)
    return JSONResponse(result, status_code=status)

@router.post("/snapshots/{identity}/void")
def void(identity: UUID, payload: SnapshotAction, actor=Depends(get_auth_context), conn=Depends(get_db_connection), key=Depends(require_idempotency_key)):
    result, status = service.change(conn, actor, key, identity, payload.model_dump(mode="json"))
    return JSONResponse(result, status_code=status)
