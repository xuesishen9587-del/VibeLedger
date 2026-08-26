from dataclasses import dataclass
from typing import Literal, Optional
from uuid import UUID

@dataclass(frozen=True)
class AuthContext:
    """
    Immutable representation of an authenticated request context.
    All downstream service and repository operations derive household and user scope
    strictly from this context.
    """
    auth_mode: Literal["device", "browser"]
    user_id: UUID
    household_id: UUID
    household_role: str
    device_id: Optional[UUID] = None
    auth_subject: Optional[str] = None

    @property
    def is_device(self) -> bool:
        return self.auth_mode == "device"

    @property
    def is_browser(self) -> bool:
        return self.auth_mode == "browser"

    @property
    def is_owner(self) -> bool:
        return self.household_role == "owner"

    @property
    def can_write(self) -> bool:
        return self.household_role in ("owner", "member")
