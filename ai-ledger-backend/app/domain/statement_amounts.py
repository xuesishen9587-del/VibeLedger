"""Adapt statement display signs without weakening financial write validation."""
from decimal import Decimal, InvalidOperation
import re

_TRANSACTION_KINDS = {"expense", "refund", "fee", "repayment", "transfer", "income", "investment_trade"}
_DECIMAL_TEXT = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")


def statement_amount(value, kind):
    # Keep missing/malformed/ambiguous facts unchanged for the existing validator.
    # In particular, a minus sign cannot decide whether an unknown row is a refund.
    if kind not in _TRANSACTION_KINDS or not isinstance(value, str) or not _DECIMAL_TEXT.fullmatch(value):
        return value
    try:
        amount = Decimal(value)
        return format(amount.copy_abs(), "f") if amount.is_finite() else value
    except InvalidOperation:
        return value
