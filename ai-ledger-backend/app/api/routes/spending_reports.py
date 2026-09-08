import json
from datetime import date
from typing import Optional, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from app.api.deps import get_db_connection, get_auth_context, require_browser_auth, require_idempotency_key
from app.api.routes.transactions import page, get_fx_provider
from app.domain.spending import fail
from app.repositories import spending as repo, simplified_schema as settings
from app.services import spending_reports as reports
from app.services.reference_fx_service import FrankfurterFxProvider

router = APIRouter(prefix="/api/v1", tags=["Spending and Review"])


@router.get("/reports/spending")
def spending_report(from_date: date = Query(alias="from"), to_date: date = Query(alias="to"),
                    actor=Depends(get_auth_context), conn=Depends(get_db_connection)):
    return reports.report(conn, actor.household_id, from_date, to_date)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_date: Optional[date] = Field(None, alias="from")
    to_date: Optional[date] = Field(None, alias="to")


@router.post("/reports/refresh-fx")
def refresh_fx(payload: RefreshRequest, actor=Depends(require_browser_auth),
               key=Depends(require_idempotency_key), conn=Depends(get_db_connection), provider=Depends(get_fx_provider)):
    if payload.from_date and payload.to_date and payload.from_date > payload.to_date:
        fail("INVALID_DATE", "The date range is reversed.")
    result, code = reports.refresh(conn, actor, key, payload.model_dump(mode="json", by_alias=True), provider)
    return JSONResponse(result, status_code=code)


@router.get("/review")
def review(section: Literal["transaction", "draft", "schedule"] = "transaction",
           cursor: Optional[str] = None, limit: int = Query(50, ge=1, le=200),
           actor=Depends(require_browser_auth), conn=Depends(get_db_connection)):
    hh = actor.household_id
    counts = repo.rows(conn, "SELECT count(*) AS transactions, "
        "count(*) FILTER (WHERE account_id IS NULL AND NOT account_review_acknowledged) AS missing_account, "
        "count(*) FILTER (WHERE category_uncertain) AS category_uncertain FROM transactions "
        "WHERE household_id=%s AND status='committed' "
        "AND ((account_id IS NULL AND NOT account_review_acknowledged) OR category_uncertain)", (hh,))[0]
    counts["drafts"] = repo.rows(conn, "SELECT count(*) AS n FROM ingestion_requests WHERE household_id=%s "
        "AND status='needs_confirmation'", (hh,))[0]["n"]
    counts["schedule_occurrences"] = repo.rows(conn, "SELECT count(*) AS n FROM schedule_occurrences "
        "WHERE household_id=%s AND status='needs_confirmation'", (hh,))[0]["n"]
    if section == "transaction":
        result = page(conn, hh, {"status": "committed", "needs_metadata_review": True}, cursor, limit)
    else:
        position = None
        if cursor:
            try:
                position = UUID(cursor)
            except ValueError:
                fail("INVALID_CURSOR", "Invalid review cursor.")
        if section == "draft":
            query = "SELECT id,request_kind,draft_payload,row_version,created_at FROM ingestion_requests"
        else:
            query = "SELECT * FROM schedule_occurrences"
        records = repo.rows(conn, query + " WHERE household_id=%s AND status='needs_confirmation' "
                           "AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s", (hh, position, position, limit+1))
        result = {"items": json.loads(json.dumps(records[:limit], default=str)),
                  "next_cursor": str(records[limit-1]["id"]) if len(records)>limit else None}
    household = settings.get_household(conn, hh)
    return {"section": section, "counts": counts, **result,
            "investment_review_change_ratio": str(household["investment_review_change_ratio"]),
            "settings_row_version": household["row_version"],
            "supported_sections": ["transaction", "draft", "schedule"]}
