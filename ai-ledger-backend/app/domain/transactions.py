"""Shared errors for the simplified household runtime."""
from uuid import UUID

class LedgerDomainError(Exception):
    """Base error for household commands and provider boundaries."""
    def __init__(self, message: str, code: str = "LEDGER_DOMAIN_ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message

class IdempotencyKeyReuseError(LedgerDomainError):
    def __init__(self, message: str = "This idempotency key was already used for different content."):
        super().__init__(message, code="IDEMPOTENCY_KEY_REUSE")

class FxRateUnavailableError(LedgerDomainError):
    def __init__(self, message: str = "No reference FX rate available for the specified currencies."):
        super().__init__(message, code="FX_RATE_UNAVAILABLE")

class FxProviderUnavailableError(LedgerDomainError):
    def __init__(self, message: str = "Reference FX service provider is temporarily unavailable."):
        super().__init__(message, code="FX_PROVIDER_UNAVAILABLE")

class GeminiDependencyError(LedgerDomainError):
    def __init__(self, message: str = "AI extraction service is temporarily unavailable."):
        super().__init__(message, code="GEMINI_SERVICE_UNAVAILABLE")

class ResourceNotFoundError(LedgerDomainError):
    def __init__(self, message: str = "Resource not found.", code: str = "NOT_FOUND"):
        super().__init__(message, code=code)

class AccountResourceNotFoundError(ResourceNotFoundError):
    def __init__(self, account_id: UUID):
        super().__init__(f"Account {account_id} not found.", code="ACCOUNT_NOT_FOUND")

class CategoryResourceNotFoundError(ResourceNotFoundError):
    def __init__(self, category_id: UUID):
        super().__init__(f"Category {category_id} not found.", code="CATEGORY_NOT_FOUND")

class AliasResourceNotFoundError(ResourceNotFoundError):
    def __init__(self, alias_id: UUID):
        super().__init__(f"Account alias {alias_id} not found.", code="ALIAS_NOT_FOUND")

class RowVersionConflictError(LedgerDomainError):
    def __init__(self, message: str = "The resource has been modified concurrently. Reload before updating.", code: str = "ROW_VERSION_CONFLICT"):
        super().__init__(message, code=code)

class AccountNameConflictError(LedgerDomainError):
    def __init__(self, name: str):
        super().__init__(f"An active account with name '{name}' already exists in this household.", code="ACCOUNT_NAME_CONFLICT")

class CategoryNameConflictError(LedgerDomainError):
    def __init__(self, name: str, category_type: str):
        super().__init__(f"An active {category_type} category with name '{name}' already exists in this household.", code="CATEGORY_NAME_CONFLICT")

class AccountAliasConflictError(LedgerDomainError):
    def __init__(self, alias: str):
        super().__init__(f"An active alias '{alias}' already exists on this account.", code="ACCOUNT_ALIAS_CONFLICT")

class CurrencyImmutableError(LedgerDomainError):
    def __init__(self, message: str = "Account currency cannot be modified once financial history exists."):
        super().__init__(message, code="CURRENCY_IMMUTABLE")

class AccountTypeImmutableError(LedgerDomainError):
    def __init__(self, message: str = "Account type cannot be modified once financial history exists."):
        super().__init__(message, code="ACCOUNT_TYPE_IMMUTABLE")

class UserNotInHouseholdError(LedgerDomainError):
    def __init__(self, user_id: UUID):
        super().__init__(f"User {user_id} does not belong to the authenticated household.", code="USER_NOT_IN_HOUSEHOLD")

class LinkedAccountInvalidError(LedgerDomainError):
    def __init__(self, message: str = "Linked cash account must be an active cash account in the same household."):
        super().__init__(message, code="LINKED_ACCOUNT_INVALID")
