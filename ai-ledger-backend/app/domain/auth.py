from typing import Optional, Dict, Any

class AuthError(Exception):
    """Base exception for all domain authentication and authorization errors."""
    def __init__(self, message: str, code: str = "UNAUTHORIZED", details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class AuthRequiredError(AuthError):
    """Raised when authentication credentials are missing or malformed."""
    def __init__(self, message: str = "Authentication required.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="UNAUTHORIZED", details=details)


class InvalidCredentialsError(AuthError):
    """Raised when credentials (token hash or JWT signature/claims) are invalid or expired."""
    def __init__(self, message: str = "Invalid credentials.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="UNAUTHORIZED", details=details)


class DeviceRevokedError(AuthError):
    """Raised when the presenting device has been explicitly revoked."""
    def __init__(self, message: str = "Device token has been revoked.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="DEVICE_REVOKED", details=details)


class UserDisabledError(AuthError):
    """Raised when the resolved user is deactivated (is_active = FALSE)."""
    def __init__(self, message: str = "User account is disabled.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="USER_DISABLED", details=details)


class UserNotInHouseholdError(AuthError):
    """Raised when an authenticated user has zero active household memberships."""
    def __init__(self, message: str = "User does not belong to any active household.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="USER_NOT_IN_HOUSEHOLD", details=details)


class AmbiguousHouseholdMembershipError(AuthError):
    """Raised when an authenticated user belongs to more than one active household (fails closed)."""
    def __init__(self, message: str = "User belongs to multiple households; single active household membership required.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="AMBIGUOUS_HOUSEHOLD_MEMBERSHIP", details=details)


class HouseholdInactiveError(AuthError):
    """Raised when the target household is inactive or deleted."""
    def __init__(self, message: str = "Target household is inactive.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="HOUSEHOLD_INACTIVE", details=details)


class HouseholdPermissionDeniedError(AuthError):
    """Raised when the caller's household role lacks permission for the requested action."""
    def __init__(self, message: str = "Household permission denied.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="HOUSEHOLD_PERMISSION_DENIED", details=details)


class DeviceNotFoundError(AuthError):
    """Raised when a requested device is not found or outside the authorized household."""
    def __init__(self, message: str = "Device not found.", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="DEVICE_NOT_FOUND", details=details)
