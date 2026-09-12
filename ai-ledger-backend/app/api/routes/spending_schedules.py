from datetime import date
from typing import Optional, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from app.api.deps import get_db_connection, require_browser_auth, require_idempotency_key
from app.api.routes.transactions import get_fx_provider
from app.domain.spending import fail
from app.repositories import spending as repo
from app.services import spending_schedules as service

router = APIRouter(prefix="/api/v1", tags=["Monthly spending schedules"])


class ScheduleTerms(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["recurring", "installment"]
    amount_per_period: StrictStr
    currency: Literal["CNY", "SGD", "USD", "EUR", "JPY"]
    period_count: Optional[int] = Field(None, ge=1, le=1200)
    start_month: date
    day_of_month: int = Field(ge=1, le=31)
    merchant: str = Field(min_length=1, max_length=240)
    category_id: UUID
    account_id: Optional[UUID] = None


class CreateSchedule(ScheduleTerms):
    acknowledged_due_through: date
    replaces_transaction_id: Optional[UUID] = None
    expected_transaction_version: Optional[int] = Field(None, ge=0)
    source_draft_request_id: Optional[UUID] = None
    expected_draft_version: Optional[int] = Field(None, ge=0)

    @model_validator(mode="after")
    def replacement_version(self):
        if (self.replaces_transaction_id is None) != (self.expected_transaction_version is None):
            raise ValueError("Replacement requires the expense ID and version together.")
        if (self.source_draft_request_id is None) != (self.expected_draft_version is None):
            raise ValueError("A source draft requires its ID and version together.")
        if self.source_draft_request_id and self.replaces_transaction_id:
            raise ValueError("Use a source draft or an existing expense, not both.")
        return self


class ChangeSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    reason: Optional[str] = Field(None, max_length=1000)


class PatchSchedule(ChangeSchedule):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    kind: Optional[Literal["recurring", "installment"]] = None
    amount_per_period: Optional[StrictStr] = None
    currency: Optional[Literal["CNY", "SGD", "USD", "EUR", "JPY"]] = None
    period_count: Optional[int] = Field(None, ge=1, le=1200)
    start_month: Optional[date] = None
    day_of_month: Optional[int] = Field(None, ge=1, le=31)
    merchant: Optional[str] = Field(None, min_length=1, max_length=240)
    category_id: Optional[UUID] = None
    account_id: Optional[UUID] = None

    @model_validator(mode="after")
    def required_terms(self):
        for name in self.model_fields_set - {"account_id", "period_count", "reason"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be cleared.")
        return self


class ResolvePeriod(ChangeSchedule):
    action: Literal["record_separately", "link_existing", "skip"]
    transaction_id: Optional[UUID] = None
    expected_transaction_version: Optional[int] = Field(None, ge=0)

    @model_validator(mode="after")
    def link_target(self):
        if self.action == "link_existing" and (self.transaction_id is None or self.expected_transaction_version is None):
            raise ValueError("Linking requires a transaction ID and version.")
        if self.action != "link_existing" and (self.transaction_id is not None or self.expected_transaction_version is not None):
            raise ValueError("Only linking accepts a transaction target.")
        return self


@router.post("/spending-schedules/preview")
def preview(payload: ScheduleTerms, actor=Depends(require_browser_auth), conn=Depends(get_db_connection)):
    return service.preview(conn, actor.household_id, payload.model_dump(mode="json"))


@router.post("/spending-schedules/materialize")
def materialize(actor=Depends(require_browser_auth), conn=Depends(get_db_connection), key=Depends(require_idempotency_key), provider=Depends(get_fx_provider)):
    result, code = service.materialize(conn, actor, key, provider)
    return JSONResponse(result, status_code=code)


@router.get("/spending-schedules")
def list_schedules(cursor: Optional[UUID] = None, limit: int = Query(50, ge=1, le=200),
                   actor=Depends(require_browser_auth), conn=Depends(get_db_connection)):
    rows = repo.rows(conn, "SELECT * FROM spending_schedules WHERE household_id=%s AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                     (actor.household_id, cursor, cursor, limit+1))
    return {"items": service.output(rows[:limit]), "next_cursor": str(rows[limit-1]["id"]) if len(rows)>limit else None}


@router.post("/spending-schedules")
def create(payload: CreateSchedule, actor=Depends(require_browser_auth), conn=Depends(get_db_connection), key=Depends(require_idempotency_key), provider=Depends(get_fx_provider)):
    result, code = service.create(conn, actor, key, payload.model_dump(mode="json"), provider)
    return JSONResponse(result, status_code=code)


@router.get("/spending-schedules/{identity}")
def get_schedule(identity: UUID, after_period: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                 actor=Depends(require_browser_auth), conn=Depends(get_db_connection)):
    row = service.output(service.get(conn, actor.household_id, identity))
    records = repo.rows(conn, "SELECT o.*,t.id AS transaction_id FROM schedule_occurrences o LEFT JOIN transactions t "
        "ON t.household_id=o.household_id AND t.schedule_occurrence_id=o.id "
        "WHERE o.household_id=%s AND o.schedule_id=%s AND o.period_no>%s ORDER BY o.period_no LIMIT %s",
        (actor.household_id, identity, after_period, limit+1))
    row["occurrences"] = service.output(records[:limit])
    row["next_period_cursor"] = records[limit-1]["period_no"] if len(records)>limit else None
    return row


@router.patch("/spending-schedules/{identity}")
def patch_schedule(identity: UUID, payload: PatchSchedule, actor=Depends(require_browser_auth),
                   conn=Depends(get_db_connection), key=Depends(require_idempotency_key), provider=Depends(get_fx_provider)):
    data = payload.model_dump(mode="json", exclude_unset=True)
    if not (data.keys() - {"expected_version", "reason"}):
        fail("INVALID_SCHEDULE", "Supply at least one changed term.")
    result, code = service.change(conn, actor, key, identity, "patch", data, provider)
    return JSONResponse(result, status_code=code)


@router.post("/spending-schedules/{identity}/{action}")
def transition(identity: UUID, action: Literal["pause", "resume", "cancel"], payload: ChangeSchedule,
               actor=Depends(require_browser_auth), conn=Depends(get_db_connection), key=Depends(require_idempotency_key), provider=Depends(get_fx_provider)):
    result, code = service.change(conn, actor, key, identity, action, payload.model_dump(mode="json"), provider)
    return JSONResponse(result, status_code=code)


@router.post("/schedule-occurrences/{identity}/resolve")
def resolve(identity: UUID, payload: ResolvePeriod, actor=Depends(require_browser_auth),
            conn=Depends(get_db_connection), key=Depends(require_idempotency_key), provider=Depends(get_fx_provider)):
    result, code = service.resolve(conn, actor, key, identity, payload.model_dump(mode="json"), provider)
    return JSONResponse(result, status_code=code)
