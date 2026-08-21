from decimal import Decimal
from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import date, datetime
from app.domain.money import parse_decimal, validate_currency_code, quantize_money, validate_fx_rate

# --- Domain Exceptions ---

class LedgerDomainError(Exception):
    """Base domain exception for VibeLedger Core Ledger."""
    def __init__(self, message: str, code: str = "LEDGER_DOMAIN_ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message

class HouseholdMismatchError(LedgerDomainError):
    def __init__(self, message: str = "Resource does not belong to the specified household."):
        super().__init__(message, code="HOUSEHOLD_MISMATCH")

class AccountNotFoundError(LedgerDomainError):
    def __init__(self, account_id: UUID):
        super().__init__(f"Account {account_id} not found.", code="ACCOUNT_NOT_FOUND")

class AccountInactiveError(LedgerDomainError):
    def __init__(self, account_id: UUID):
        super().__init__(f"Account {account_id} is inactive.", code="ACCOUNT_INACTIVE")

class CategoryNotFoundError(LedgerDomainError):
    def __init__(self, category_id: UUID):
        super().__init__(f"Category {category_id} not found.", code="CATEGORY_NOT_FOUND")

class CategoryMismatchError(LedgerDomainError):
    def __init__(self, message: str = "Category type or household does not match transaction requirements."):
        super().__init__(message, code="CATEGORY_MISMATCH")

class CurrencyMismatchError(LedgerDomainError):
    def __init__(self, message: str = "Transaction currency does not match account currency."):
        super().__init__(message, code="CURRENCY_MISMATCH")

class InvalidAmountError(LedgerDomainError):
    def __init__(self, message: str = "Transaction amounts must be strictly positive."):
        super().__init__(message, code="INVALID_AMOUNT")

class SameAccountTransferError(LedgerDomainError):
    def __init__(self, message: str = "Source and destination accounts in a transfer must be different."):
        super().__init__(message, code="SAME_ACCOUNT_TRANSFER")

class CrossCurrencyMissingLegError(LedgerDomainError):
    def __init__(self, message: str = "Cross-currency transfer requires explicit from_amount and to_amount legs."):
        super().__init__(message, code="CROSS_CURRENCY_MISSING_LEG")

class RefundExceedsOriginalError(LedgerDomainError):
    def __init__(self, message: str = "Total refund amount exceeds original refundable amount."):
        super().__init__(message, code="REFUND_EXCEEDS_ORIGINAL")

class TransactionNotFoundError(LedgerDomainError):
    def __init__(self, transaction_id: UUID):
        super().__init__(f"Transaction {transaction_id} not found.", code="TRANSACTION_NOT_FOUND")

class TransactionAlreadyVoidedError(LedgerDomainError):
    def __init__(self, transaction_id: UUID):
        super().__init__(f"Transaction {transaction_id} is already voided.", code="TRANSACTION_ALREADY_VOIDED")

class InvalidTransactionShapeError(LedgerDomainError):
    def __init__(self, message: str):
        super().__init__(message, code="INVALID_TRANSACTION_SHAPE")

class IdempotencyKeyReuseError(LedgerDomainError):
    def __init__(self, message: str = "This idempotency key was already used for different content."):
        super().__init__(message, code="IDEMPOTENCY_KEY_REUSE")

class DeviceAuthenticationError(LedgerDomainError):
    def __init__(self, message: str = "Invalid or missing device authentication token."):
        super().__init__(message, code="UNAUTHORIZED")

class DeviceRevokedError(LedgerDomainError):
    def __init__(self, message: str = "Device token is revoked or inactive."):
        super().__init__(message, code="DEVICE_REVOKED")

class RequestNotFoundError(LedgerDomainError):
    def __init__(self, message: str = "The request was not received by the server."):
        super().__init__(message, code="REQUEST_NOT_FOUND")

class AmbiguousAccountError(LedgerDomainError):
    def __init__(self, message: str = "Multiple plausible account candidates match the reference."):
        super().__init__(message, code="AMBIGUOUS_ACCOUNT")

class InvalidImagePayloadError(LedgerDomainError):
    def __init__(self, message: str = "Invalid image payload or unsupported format."):
        super().__init__(message, code="INVALID_IMAGE_PAYLOAD")

class FxRateUnavailableError(LedgerDomainError):
    def __init__(self, message: str = "No reference FX rate available for the specified currencies."):
        super().__init__(message, code="FX_RATE_UNAVAILABLE")

class FxProviderUnavailableError(LedgerDomainError):
    def __init__(self, message: str = "Reference FX service provider is temporarily unavailable."):
        super().__init__(message, code="FX_PROVIDER_UNAVAILABLE")

class GeminiDependencyError(LedgerDomainError):
    def __init__(self, message: str = "AI extraction service is temporarily unavailable."):
        super().__init__(message, code="GEMINI_SERVICE_UNAVAILABLE")

class InvalidRequestStateError(LedgerDomainError):
    def __init__(self, message: str = "The ingestion request is not in a valid state for this operation."):
        super().__init__(message, code="INVALID_REQUEST_STATE")

class InvalidPaymentModeError(LedgerDomainError):
    def __init__(self, message: str = "Invalid or unsupported payment mode."):
        super().__init__(message, code="INVALID_PAYMENT_MODE")


# --- Projection Calculation ---

def calculate_projection_deltas(
    transaction_type: str,
    from_account_id: Optional[UUID],
    to_account_id: Optional[UUID],
    from_amount: Optional[Decimal],
    to_amount: Optional[Decimal]
) -> Dict[UUID, Decimal]:
    """
    Computes universal signed balance changes for affected accounts.
    Universal ledger algebra:
      from_account leg: ledger_balance -= from_amount
      to_account leg:   ledger_balance += to_amount
    All leg amounts must be positive Decimal numbers.
    """
    deltas: Dict[UUID, Decimal] = {}

    if transaction_type in ("expense", "fee"):
        if not from_account_id or from_amount is None or from_amount <= 0:
            raise InvalidTransactionShapeError(f"{transaction_type} requires valid from_account_id and positive from_amount.")
        deltas[from_account_id] = -from_amount

    elif transaction_type in ("cash_income", "refund"):
        if not to_account_id or to_amount is None or to_amount <= 0:
            raise InvalidTransactionShapeError(f"{transaction_type} requires valid to_account_id and positive to_amount.")
        deltas[to_account_id] = to_amount

    elif transaction_type == "transfer":
        if not from_account_id or from_amount is None or from_amount <= 0:
            raise InvalidTransactionShapeError("transfer requires valid from_account_id and positive from_amount.")
        if not to_account_id or to_amount is None or to_amount <= 0:
            raise InvalidTransactionShapeError("transfer requires valid to_account_id and positive to_amount.")
        if from_account_id == to_account_id:
            raise SameAccountTransferError("transfer source and destination accounts must be distinct.")
        deltas[from_account_id] = -from_amount
        deltas[to_account_id] = to_amount

    elif transaction_type in ("opening_balance", "reconciliation_adjustment"):
        if from_account_id and to_account_id:
            raise InvalidTransactionShapeError(f"{transaction_type} must specify exactly one account leg, not both.")
        if not from_account_id and not to_account_id:
            raise InvalidTransactionShapeError(f"{transaction_type} must specify either from_account_id or to_account_id.")

        if to_account_id:
            if to_amount is None or to_amount <= 0:
                raise InvalidTransactionShapeError(f"Positive {transaction_type} requires positive to_amount.")
            deltas[to_account_id] = to_amount
        else:
            if from_amount is None or from_amount <= 0:
                raise InvalidTransactionShapeError(f"Negative {transaction_type} requires positive from_amount.")
            deltas[from_account_id] = -from_amount

    else:
        raise InvalidTransactionShapeError(f"Unsupported transaction type: {transaction_type}")

    return deltas
