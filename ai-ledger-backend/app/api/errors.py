from typing import Tuple, Dict, Any, Optional
from fastapi import Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.domain.auth import (
    AuthError,
    AuthRequiredError,
    InvalidCredentialsError,
    DeviceRevokedError as AuthDeviceRevokedError,
    UserDisabledError,
    UserNotInHouseholdError as AuthUserNotInHouseholdError,
    AmbiguousHouseholdMembershipError,
    HouseholdInactiveError,
    HouseholdPermissionDeniedError,
    DeviceNotFoundError,
)
from app.domain.transactions import (
    LedgerDomainError,
    IdempotencyKeyReuseError,
    RequestNotFoundError,
    DeviceAuthenticationError,
    DeviceRevokedError,
    HouseholdMismatchError,
    AccountNotFoundError,
    CategoryNotFoundError,
    TransactionNotFoundError,
    AccountInactiveError,
    CategoryMismatchError,
    CurrencyMismatchError,
    InvalidAmountError,
    SameAccountTransferError,
    InvalidTransactionShapeError,
    RefundExceedsOriginalError,
    TransactionAlreadyVoidedError,
    AmbiguousAccountError,
    InvalidImagePayloadError,
    FxRateUnavailableError,
    FxProviderUnavailableError,
    GeminiDependencyError,
    InvalidRequestStateError,
    InvalidPaymentModeError,
    ResourceNotFoundError,
    AccountResourceNotFoundError,
    CategoryResourceNotFoundError,
    TransactionResourceNotFoundError,
    InstallmentPlanResourceNotFoundError,
    AliasResourceNotFoundError,
    RowVersionConflictError,
    AccountNameConflictError,
    CategoryNameConflictError,
    AccountAliasConflictError,
    AccountTypeMismatchError,
    CurrencyImmutableError,
    AccountTypeImmutableError,
    UserNotInHouseholdError,
    LinkedAccountInvalidError,
    BatchResourceNotFoundError,
    BatchNotFoundError,
    BatchVersionConflictError,
    CandidateResourceNotFoundError,
    StatementParseFailedError,
    StatementPasswordRequiredError,
    StatementPasswordInvalidError,
    DependencyUnavailableError,
    InvalidSnapshotError,
    InvalidBatchStateError
)

def build_error_response(
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: Optional[dict] = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": details or {}
            },
            "detail": message
        }
    )

def extract_error_details(exc: Exception) -> Tuple[int, str, Dict[str, Any]]:
    """
    Extracts canonical (status_code, error_code, response_payload_dict) for any exception.
    Single source of truth used for both live HTTP responses and durable command receipts.
    """
    if isinstance(exc, RequestValidationError):
        status_code = 422
        code = "INVALID_REQUEST"
        msg = "Request input validation failed."
        payload = {
            "error": {
                "code": code,
                "message": msg,
                "retryable": False,
                "details": {"errors": [str(e) for e in exc.errors()]}
            },
            "detail": msg
        }
        return status_code, code, payload

    if isinstance(exc, LedgerDomainError):
        if isinstance(exc, BatchVersionConflictError):
            status_code, code, retryable = 409, exc.code, True
        elif isinstance(exc, (IdempotencyKeyReuseError, RowVersionConflictError, TransactionAlreadyVoidedError)):
            status_code, code, retryable = 409, exc.code, False
        elif isinstance(exc, (RequestNotFoundError, ResourceNotFoundError)):
            status_code, code, retryable = 404, exc.code, isinstance(exc, RequestNotFoundError)
        elif isinstance(exc, (DeviceAuthenticationError, DeviceRevokedError)):
            status_code, code, retryable = 401, exc.code, False
        elif isinstance(exc, HouseholdMismatchError):
            status_code, code, retryable = 403, exc.code, False
        elif isinstance(exc, (FxProviderUnavailableError, GeminiDependencyError, DependencyUnavailableError)):
            status_code, code, retryable = 503, exc.code, True
        elif isinstance(exc, (StatementPasswordRequiredError, StatementPasswordInvalidError, StatementParseFailedError)):
            status_code, code, retryable = 400, exc.code, False
        elif isinstance(exc, (AccountNotFoundError, CategoryNotFoundError, TransactionNotFoundError)):
            status_code, code, retryable = 422, exc.code, False
        elif isinstance(exc, (AccountNameConflictError, CategoryNameConflictError, AccountAliasConflictError)):
            status_code, code, retryable = 422, exc.code, False
        else:
            status_code, code, retryable = 422, getattr(exc, "code", "VALIDATION_ERROR"), False

        msg = exc.message if hasattr(exc, "message") else str(exc)
        details = getattr(exc, "details", {}) or {}
        payload = {
            "error": {
                "code": code,
                "message": msg,
                "retryable": retryable,
                "details": details
            },
            "detail": msg
        }
        return status_code, code, payload

    if isinstance(exc, AuthError):
        details = getattr(exc, "details", {}) or {}
        if isinstance(exc, (AuthRequiredError, InvalidCredentialsError, AuthDeviceRevokedError)):
            status_code, code = 401, exc.code
        elif isinstance(exc, DeviceNotFoundError):
            status_code, code = 404, exc.code
        elif isinstance(exc, HouseholdPermissionDeniedError):
            status_code, code = 403, "FORBIDDEN"
        else:
            status_code, code = 403, getattr(exc, "code", "FORBIDDEN")
        payload = {
            "error": {
                "code": code,
                "message": exc.message,
                "retryable": False,
                "details": details
            },
            "detail": exc.message
        }
        return status_code, code, payload

    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            payload = exc.detail
            code = exc.detail["error"].get("code", f"HTTP_{status_code}")
            return status_code, code, payload

        code = "HTTP_ERROR"
        if status_code == 400:
            code = "BAD_REQUEST"
        elif status_code == 401:
            code = "UNAUTHORIZED"
        elif status_code == 403:
            code = "FORBIDDEN"
        elif status_code == 404:
            code = "NOT_FOUND"
        elif status_code == 409:
            code = "CONFLICT"
        elif status_code == 422:
            code = "INVALID_REQUEST"

        msg = str(exc.detail) if exc.detail else "An HTTP error occurred."
        payload = {
            "error": {
                "code": code,
                "message": msg,
                "retryable": (status_code >= 500),
                "details": {}
            },
            "detail": msg
        }
        return status_code, code, payload

    status_code = 500
    code = "INTERNAL_SERVER_ERROR"
    msg = "An unexpected internal error occurred."
    payload = {
        "error": {
            "code": code,
            "message": msg,
            "retryable": True,
            "details": {}
        },
        "detail": msg
    }
    return status_code, code, payload

async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    status_code, _, payload = extract_error_details(exc)
    return JSONResponse(status_code=status_code, content=payload)

async def ledger_domain_exception_handler(request: Request, exc: LedgerDomainError) -> JSONResponse:
    status_code, _, payload = extract_error_details(exc)
    return JSONResponse(status_code=status_code, content=payload)

async def auth_domain_exception_handler(request: Request, exc: AuthError) -> JSONResponse:
    status_code, _, payload = extract_error_details(exc)
    return JSONResponse(status_code=status_code, content=payload)

async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    status_code, _, payload = extract_error_details(exc)
    return JSONResponse(status_code=status_code, content=payload)

async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    status_code, _, payload = extract_error_details(exc)
    return JSONResponse(status_code=status_code, content=payload)
