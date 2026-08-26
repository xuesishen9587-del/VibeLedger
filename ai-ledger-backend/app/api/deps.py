from typing import Optional, Dict, Any, Generator
from fastapi import Header, Depends
from app.db import get_connection
from app.auth.context import AuthContext
from app.auth.service import AuthService
from app.domain.auth import (
    AuthRequiredError,
    HouseholdPermissionDeniedError,
)

def get_db_connection() -> Generator[Any, None, None]:
    """
    FastAPI dependency that yields an active database connection.
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        if conn and not conn.closed:
            conn.close()

def get_auth_context(
    authorization: Optional[str] = Header(None),
    conn: Any = Depends(get_db_connection)
) -> AuthContext:
    """
    FastAPI dependency that authenticates incoming requests via Bearer token (Device or Browser JWT).
    Derives trusted household_id and user_id.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthRequiredError("Missing or invalid Authorization Bearer header.")

    raw_token = authorization[7:].strip()
    if not raw_token:
        raise AuthRequiredError("Empty bearer token.")

    return AuthService.authenticate(conn, raw_token)

def require_device_auth(
    auth_context: AuthContext = Depends(get_auth_context)
) -> AuthContext:
    """Requires that the caller is authenticated via a registered device."""
    if not auth_context.is_device:
        raise HouseholdPermissionDeniedError("Device authentication required for this endpoint.")
    return auth_context

def require_browser_auth(
    auth_context: AuthContext = Depends(get_auth_context)
) -> AuthContext:
    """Requires that the caller is authenticated via a browser JWT session."""
    if not auth_context.is_browser:
        raise HouseholdPermissionDeniedError("Browser authentication required for this endpoint.")
    return auth_context

def require_household_member(
    auth_context: AuthContext = Depends(get_auth_context)
) -> AuthContext:
    """Requires that the caller has active member or owner role in the household."""
    if not auth_context.can_write:
        raise HouseholdPermissionDeniedError("Insufficient household permissions.")
    return auth_context

def require_household_owner(
    auth_context: AuthContext = Depends(get_auth_context)
) -> AuthContext:
    """Requires that the caller is an owner of the household."""
    if not auth_context.is_owner:
        raise HouseholdPermissionDeniedError("Household owner role required.")
    return auth_context

def get_authenticated_device(
    auth_context: AuthContext = Depends(get_auth_context)
) -> Dict[str, Any]:
    """
    Backward-compatible adapter dependency for existing routes.
    Returns a dictionary containing resolved identity and household context.
    """
    return {
        "device_id": auth_context.device_id,
        "user_id": auth_context.user_id,
        "household_id": auth_context.household_id,
        "household_role": auth_context.household_role,
        "auth_mode": auth_context.auth_mode,
        "auth_subject": auth_context.auth_subject,
    }
