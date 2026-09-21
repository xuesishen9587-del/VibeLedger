"""Derived gains from observations; an estimate never creates flow evidence."""
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from uuid import UUID
from app.domain.spending import fail, money_text
from app.domain.balances import signed_money


class PeriodInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opening_snapshot_id: UUID
    closing_snapshot_id: UUID
    contributions_amount: StrictStr = Field(max_length=80)
    withdrawals_amount: StrictStr = Field(max_length=80)
    expected_version: int | None = Field(ge=0)
    notes: str | None = Field(None, max_length=2000)


class VoidInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)


class ReviewSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    investment_review_change_ratio: StrictStr = Field(max_length=20)


def flow_amount(value, currency):
    amount = signed_money(value, currency)
    if amount < 0:
        fail("INVALID_AMOUNT", "Complete flow totals must be nonnegative; zero is valid.")
    return amount


def interval(account, opening, closing, inputs, threshold, pair_changed=False):
    confirmed = inputs is not None and inputs["status"] == "active" and not inputs.get("pair_invalidated")
    additions = inputs["contributions_amount"] if confirmed else Decimal(0)
    withdrawals = inputs["withdrawals_amount"] if confirmed else Decimal(0)
    change = closing["balance"] - opening["balance"]
    unusual = not confirmed and (abs(change) >= abs(opening["balance"]) * threshold if opening["balance"] else change != 0)
    currency = account["currency"]
    return {"id": str(opening["id"])+":"+str(closing["id"]), "account_id": str(account["id"]),
        "account_name": account["name"], "currency": currency,
        "opening_snapshot_id": str(opening["id"]), "closing_snapshot_id": str(closing["id"]),
        "period_start": opening["as_of"].isoformat(), "period_end": closing["as_of"].isoformat(),
        "opening_time_basis": opening["time_basis"], "closing_time_basis": closing["time_basis"],
        "opening_value": money_text(opening["balance"], currency), "closing_value": money_text(closing["balance"], currency),
        "gain": money_text(change-additions+withdrawals, currency),
        "gain_status": "user_confirmed" if confirmed else "estimated",
        "assumption": None if confirmed else "zero_flows", "needs_review": unusual,
        "review_reason": "UNUSUAL_ESTIMATED_CHANGE" if unusual else None,
        "reason": "PAIR_CHANGED" if pair_changed and not confirmed else None,
        "effective_contributions": money_text(additions, currency), "effective_withdrawals": money_text(withdrawals, currency),
        "input_id": str(inputs["id"]) if inputs else None, "input_status": inputs["status"] if inputs else None,
        "input_version": inputs["row_version"] if inputs else None,
        "confirmed_by_user_id": str(inputs["confirmed_by_user_id"]) if confirmed else None,
        "confirmed_at": inputs["confirmed_at"].isoformat() if confirmed else None,
        "confirmed_inputs": {"contributions_amount":money_text(additions,currency),
            "withdrawals_amount":money_text(withdrawals,currency)} if confirmed else None,
        "notes": inputs.get("notes") if inputs else None}
