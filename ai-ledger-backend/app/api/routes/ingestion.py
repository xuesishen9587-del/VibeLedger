from datetime import date
from typing import Optional, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Body, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from app.api.deps import get_db_connection, get_auth_context, require_browser_auth
from app.api.routes.expenses import get_capture_model, capture_response
from app.api.routes.transactions import get_fx_provider
from app.domain.spending import fail
from app.domain.capture_edits import ReviseRequestPayload
from app.domain.balance_capture import BalanceDraftEdit
from app.services import balance_capture
from app.services import statement_import
from app.domain.statement_import import StatementDraftEdit
from app.services import expense_capture as service, capture_receipts as receipts
from app.repositories import spending as repo, simplified_schema as schema

router = APIRouter(prefix="/api/v1/ingestion-requests", tags=["Capture recovery"])

class DraftAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Optional[int] = Field(None, ge=0)
    reason: Optional[str] = Field(None, max_length=1000)

@router.get("/by-key/{key}")
def get_by_key(key: str = Path(min_length=8, max_length=200), actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    row = schema.get_ingestion_request_by_key(conn, actor.household_id, actor.actor_scope, key)
    if not row:
        fail("REQUEST_NOT_FOUND", "Request not found.", 404)
    return capture_response(receipts.response(row, recovery=True))

@router.post("/by-key/{key}/cancel")
def cancel_key(key: str = Path(min_length=8, max_length=200), actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return capture_response(service.cancel(receipts.connection_factory(conn), actor, key=key))

@router.get("")
def list_drafts(status: Literal["needs_confirmation"] = "needs_confirmation", cursor: Optional[UUID] = None,
                request_kind: Literal["expense", "balance_capture", "statement"] | None = None,
                limit: int = Query(50, ge=1, le=200), actor=Depends(require_browser_auth), conn=Depends(get_db_connection)):
    rows = repo.rows(conn, "SELECT * FROM ingestion_requests WHERE household_id=%s AND status=%s "
        "AND (%s IS NULL OR request_kind=%s) AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s", (actor.household_id, status, request_kind, request_kind, cursor, cursor, limit+1))
    return {"items": [receipts.response(row)[0] for row in rows[:limit]],
            "next_cursor": str(rows[limit-1]["id"]) if len(rows)>limit else None}

@router.get("/{identity}")
def get_request(identity: UUID, actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return capture_response(receipts.response(receipts.get(conn, actor, identity), recovery=True))

@router.post("/{identity}/confirm")
def confirm(identity: UUID, payload: Optional[DraftAction] = Body(None), actor=Depends(get_auth_context),
            conn=Depends(get_db_connection), provider=Depends(get_fx_provider)):
    row = receipts.get(conn, actor, identity)
    if row["request_kind"] == "statement":
        return capture_response(statement_import.confirm(receipts.connection_factory(conn), actor, identity,
            payload.expected_version if payload else None, provider))
    if row["request_kind"] == "balance_capture":
        return capture_response(balance_capture.confirm(receipts.connection_factory(conn), actor, identity,
            payload.expected_version if payload else None))
    return capture_response(service.confirm(receipts.connection_factory(conn), actor, identity,
                                            payload.expected_version if payload else None, provider))

@router.post("/{identity}/revise")
@router.patch("/{identity}/draft")
def revise(identity: UUID, payload: ReviseRequestPayload | BalanceDraftEdit | StatementDraftEdit, actor=Depends(get_auth_context),
           conn=Depends(get_db_connection), model=Depends(get_capture_model)):
    row = receipts.get(conn, actor, identity)
    if row["request_kind"] == "statement":
        if not isinstance(payload, StatementDraftEdit):
            fail("INVALID_REQUEST", "Supply statement line decisions and balance selection.")
        return capture_response(statement_import.revise(receipts.connection_factory(conn), actor, identity,
            payload.model_dump(mode="json", exclude_unset=True)))
    if row["request_kind"] == "balance_capture":
        if not isinstance(payload, BalanceDraftEdit):
            fail("INVALID_REQUEST", "Replace the balance rows explicitly.")
        return capture_response(balance_capture.revise(receipts.connection_factory(conn), actor, identity,
            payload.model_dump(mode="json")))
    if not isinstance(payload, ReviseRequestPayload):
        fail("INVALID_REQUEST", "Use expense draft fields.")
    return capture_response(service.revise(receipts.connection_factory(conn), actor, identity,
        payload.model_dump(mode="json", exclude_unset=True), model))

@router.post("/{identity}/reject")
def reject(identity: UUID, payload: Optional[DraftAction] = Body(None), actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return capture_response(service.cancel(receipts.connection_factory(conn), actor, identity=identity,
                                          expected_version=payload.expected_version if payload else None))
