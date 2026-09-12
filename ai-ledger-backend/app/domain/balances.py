"""Signed native balance facts and explicit observation times."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictStr, AwareDatetime
from app.domain.spending import MINOR_UNITS, fail


def signed_money(value, currency):
    if currency not in MINOR_UNITS:
        fail("INVALID_CURRENCY", "Select a supported balance currency.")
    try:
        if isinstance(value, (float, bool)):
            raise ValueError()
        amount = Decimal(value)
        if not amount.is_finite() or abs(amount) >= Decimal("1e14") or amount != amount.quantize(Decimal(10) ** -MINOR_UNITS[currency]):
            raise ValueError()
        return amount
    except (ValueError, TypeError, InvalidOperation):
        fail("INVALID_BALANCE", "Use a finite decimal balance in currency minor units; zero is valid.")


def instant(value):
    try:
        # Pydantic serializes UTC datetimes with Z; Python 3.10's
        # fromisoformat accepts the equivalent explicit offset, but not Z.
        if isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        fail("INVALID_DATE", "An observation timestamp with timezone is required.")


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: UUID
    balance: StrictStr = Field(max_length=80)
    currency: Literal["CNY", "SGD", "USD", "EUR", "JPY"]
    as_of: AwareDatetime
    time_basis: Literal["explicit", "capture", "date_only"]
    expected_latest_snapshot_id: UUID | None
    expected_account_version: int = Field(ge=0)
    notes: str | None = Field(None, max_length=2000)


class BalanceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: list[Observation] = Field(min_length=1, max_length=50)


class SnapshotAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    expected_latest_snapshot_id: UUID | None
    expected_account_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)
    reopen_account: bool = False


class SnapshotCorrection(SnapshotAction):
    balance: StrictStr = Field(max_length=80)
    currency: Literal["CNY", "SGD", "USD", "EUR", "JPY"]
    as_of: AwareDatetime
    time_basis: Literal["explicit", "capture", "date_only"]
    notes: str | None = Field(None, max_length=2000)
