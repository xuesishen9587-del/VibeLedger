"""No browser/device identity or client-selected household scope on this route."""
from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict
from app.auth.scheduler import require_scheduler
from app.db import get_connection
from app.domain.spending import fail
from app.services.spending_schedules import run_due

router = APIRouter(tags=["Internal operations"])


class RunSchedules(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/internal/spending-schedules/run", dependencies=[Depends(require_scheduler)])
def run_schedules(request: Request, payload: RunSchedules | None = Body(None)):
    if request.query_params:
        fail("INVALID_REQUEST", "This operation accepts no household or date parameters.")
    # Native-currency schedule writes have no Gemini or network FX dependency.
    # Each period commits its own durable receipt; an interrupted run can retry.
    return run_due(get_connection)
