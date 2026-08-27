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
    details: dict = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": details or {}
            }
        }
    )

async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return build_error_response(
        status_code=422,
        code="INVALID_REQUEST",
        message="Request input validation failed.",
        retryable=False,
        details={"errors": [str(e) for e in exc.errors()]}
    )

async def ledger_domain_exception_handler(request: Request, exc: LedgerDomainError) -> JSONResponse:
    if isinstance(exc, BatchVersionConflictError):
        return build_error_response(409, exc.code, exc.message, retryable=True)

    if isinstance(exc, (IdempotencyKeyReuseError, RowVersionConflictError)):
        return build_error_response(409, exc.code, exc.message, retryable=False)

    if isinstance(exc, (RequestNotFoundError, ResourceNotFoundError)):
        return build_error_response(404, exc.code, exc.message, retryable=(isinstance(exc, RequestNotFoundError)))

    if isinstance(exc, (DeviceAuthenticationError, DeviceRevokedError)):
        return build_error_response(401, exc.code, exc.message, retryable=False)

    if isinstance(exc, HouseholdMismatchError):
        return build_error_response(403, exc.code, exc.message, retryable=False)

    if isinstance(exc, (FxProviderUnavailableError, GeminiDependencyError, DependencyUnavailableError)):
        return build_error_response(503, exc.code, exc.message, retryable=True)

    if isinstance(exc, (StatementPasswordRequiredError, StatementPasswordInvalidError, StatementParseFailedError)):
        return build_error_response(400, exc.code, exc.message, retryable=False)

    if isinstance(exc, (AccountNotFoundError, CategoryNotFoundError, TransactionNotFoundError)):
        return build_error_response(422, exc.code, exc.message, retryable=False)

    if isinstance(exc, (AccountNameConflictError, CategoryNameConflictError, AccountAliasConflictError)):
        return build_error_response(422, exc.code, exc.message, retryable=False)

    # All other domain validation errors are 422 Unprocessable Entity
    return build_error_response(422, exc.code, exc.message, retryable=False)


async def auth_domain_exception_handler(request: Request, exc: AuthError) -> JSONResponse:
    if isinstance(exc, (AuthRequiredError, InvalidCredentialsError)):
        return build_error_response(401, exc.code, exc.message, retryable=False, details=exc.details)

    if isinstance(exc, AuthDeviceRevokedError):
        return build_error_response(401, exc.code, exc.message, retryable=False, details=exc.details)

    if isinstance(exc, UserDisabledError):
        return build_error_response(403, exc.code, exc.message, retryable=False, details=exc.details)

    if isinstance(exc, AuthUserNotInHouseholdError):
        return build_error_response(403, exc.code, exc.message, retryable=False, details=exc.details)

    if isinstance(exc, AmbiguousHouseholdMembershipError):
        return build_error_response(403, exc.code, exc.message, retryable=False, details=exc.details)

    if isinstance(exc, HouseholdInactiveError):
        return build_error_response(403, exc.code, exc.message, retryable=False, details=exc.details)

    if isinstance(exc, HouseholdPermissionDeniedError):
        return build_error_response(403, "FORBIDDEN", exc.message, retryable=False, details=exc.details)

    if isinstance(exc, DeviceNotFoundError):
        return build_error_response(404, exc.code, exc.message, retryable=False, details=exc.details)

    return build_error_response(403, getattr(exc, "code", "FORBIDDEN"), exc.message, retryable=False, details=getattr(exc, "details", {}))


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    msg = str(exc.detail) if exc.detail else "An HTTP error occurred."
    code = "HTTP_ERROR"
    if exc.status_code == 401:
        code = "UNAUTHORIZED"
    elif exc.status_code == 403:
        code = "FORBIDDEN"
    elif exc.status_code == 404:
        code = "NOT_FOUND"
    elif exc.status_code == 409:
        code = "CONFLICT"
    elif exc.status_code == 422:
        code = "INVALID_REQUEST"

    return build_error_response(exc.status_code, code, msg, retryable=(exc.status_code >= 500))

async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return build_error_response(
        500,
        "INTERNAL_SERVER_ERROR",
        "An unexpected internal error occurred.",
        retryable=True
    )
