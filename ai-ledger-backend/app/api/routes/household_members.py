"""Read-only member names for account ownership; never grants account access."""
from typing import Any, Literal
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from app.api.deps import get_db_connection, require_browser_auth
from app.auth.context import AuthContext
from app.repositories.simplified_schema import list_household_members

router = APIRouter(prefix="/api/v1/household-members", tags=["Household members"])


class HouseholdMember(BaseModel):
    user_id: UUID
    display_name: str
    status: Literal["active", "disabled"]


class HouseholdMemberList(BaseModel):
    items: list[HouseholdMember]
    next_cursor: None = None


@router.get("", response_model=HouseholdMemberList, summary="List Current Household Members")
def members(
    actor: AuthContext = Depends(require_browser_auth),
    conn: Any = Depends(get_db_connection),
) -> HouseholdMemberList:
    # Include disabled members so existing ownership can still be named.
    # Do not return email, Auth subject, role or other identity metadata.
    return HouseholdMemberList(items=[
        HouseholdMember(user_id=row["user_id"], display_name=row["display_name"], status=row["user_status"])
        for row in list_household_members(conn, actor.household_id)
    ])
