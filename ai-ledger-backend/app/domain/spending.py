"""Validation shared by manual, captured and scheduled spending."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from fastapi import HTTPException

MINOR_UNITS = {"CNY": 2, "SGD": 2, "USD": 2, "EUR": 2, "JPY": 0}


def fail(code, message, status=422):
    raise HTTPException(status, {"error": {"code": code, "message": message,
                        "retryable": status >= 500, "details": {}}})


def money(value, currency):
    if currency not in MINOR_UNITS or isinstance(value, (float, bool)):
        fail("INVALID_AMOUNT", "Use a decimal string and a supported currency.")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0 or amount >= Decimal("1e14"):
            raise ValueError()
        if amount != amount.quantize(Decimal(10) ** -MINOR_UNITS[currency]):
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError, TypeError):
        fail("INVALID_AMOUNT", "Amount must be positive, finite and use currency minor units.")


def money_text(value, currency):
    return format(value, f".{MINOR_UNITS[currency]}f")


def local_today(household):
    return datetime.now(timezone.utc).astimezone(ZoneInfo(household["timezone"])).date()


def business_date(value, household):
    try:
        result = value if isinstance(value, date) else date.fromisoformat(value)
    except (ValueError, TypeError):
        fail("INVALID_DATE", "A business date is required.")
    if result > local_today(household):
        fail("INVALID_DATE", "Future spending cannot be recorded.")
    return result


def metadata_reasons(row):
    reasons = []
    if row.get("account_id") is None and not row.get("account_review_acknowledged", False):
        reasons.append("MISSING_ACCOUNT")
    if row.get("category_uncertain", False):
        reasons.append("CATEGORY_UNCERTAIN")
    return reasons
