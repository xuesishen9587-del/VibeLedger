from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any
from uuid import UUID

from app.domain.money import quantize_money


@dataclass(frozen=True)
class InvestmentCapitalFlow:
    """
    Represents an explicit external capital flow (deposit/withdrawal)
    extracted from an investment statement or provided for review.
    """
    direction: str  # 'contribution' | 'withdrawal'
    amount: Decimal
    currency: str
    occurred_on: Optional[date] = None
    posted_on: Optional[date] = None
    description: Optional[str] = None
    external_reference: Optional[str] = None

    def __post_init__(self):
        if self.direction not in ("contribution", "withdrawal"):
            raise ValueError(f"Invalid capital flow direction: {self.direction}. Must be 'contribution' or 'withdrawal'.")
        if self.amount <= Decimal("0.00"):
            raise ValueError(f"Capital flow amount must be strictly positive, got {self.amount}.")
        if not self.currency or len(self.currency) != 3:
            raise ValueError(f"Invalid currency code: {self.currency}.")


@dataclass
class InvestmentStatementExtractionResult:
    """
    Typed extraction result for investment statements (Product v1).
    Captures account-level valuation and external capital flows only.
    """
    total_asset_value: Decimal
    currency: str
    valuation_as_of: Optional[date] = None
    statement_period_start: Optional[date] = None
    statement_period_end: Optional[date] = None
    opening_total_asset_value: Optional[Decimal] = None
    opening_valuation_as_of: Optional[date] = None
    clear_capital_flows: List[InvestmentCapitalFlow] = field(default_factory=list)
    capital_flow_evidence_complete: bool = True
    broker_reported_pnl: Optional[Decimal] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def calculate_investment_pnl(
    opening_value: Decimal,
    closing_value: Decimal,
    contributions: Decimal,
    withdrawals: Decimal,
    currency: str
) -> Decimal:
    """
    Canonical VibeLedger Investment P&L Formula:
        P&L = closing_value - opening_value - contributions + withdrawals

    Where:
        - closing_value: Ending authoritative asset valuation
        - opening_value: Starting authoritative asset valuation
        - contributions: Total external capital added into the investment account during the period (>= 0)
        - withdrawals: Total external capital removed from the investment account during the period (>= 0)

    Returns signed P&L quantized to account currency minor units.
    """
    if contributions < Decimal("0.00"):
        raise ValueError(f"Contributions amount must be non-negative, got {contributions}.")
    if withdrawals < Decimal("0.00"):
        raise ValueError(f"Withdrawals amount must be non-negative, got {withdrawals}.")

    raw_pnl = closing_value - opening_value - contributions + withdrawals
    return quantize_money(raw_pnl, currency)


def is_flow_compatible_with_transfer(
    flow: Any,
    transfer: Dict[str, Any],
    account_id: UUID,
    account_currency: str
) -> bool:
    """
    Deterministic compatibility check between a statement flow (or unresolved flow evidence)
    and a committed ledger transfer transaction.

    Rules:
    1. Transfer must be committed, not deleted, and transaction_type == 'transfer'.
    2. Direction & Account Leg:
       - 'contribution': to_account_id == account_id, to_amount == flow_amount, to_currency == account_currency == flow_currency
       - 'withdrawal': from_account_id == account_id, from_amount == flow_amount, from_currency == account_currency == flow_currency
    3. Date compatibility:
       - If flow occurred_on is present: require transfer occurred_on == flow occurred_on.
       - Else if flow posted_on is present: require (transfer posted_on or transfer occurred_on) == flow posted_on.
    4. External reference compatibility:
       - If both flow external_reference and transfer remarks/reference are present and non-empty:
         Must be compatible (equal or substring). If contradictory: not compatible.
       - If absent on either side, absence does not block compatibility.
    """
    from app.domain.money import parse_decimal

    if not transfer:
        return False
    if transfer.get("status") and transfer.get("status") != "committed":
        return False
    if transfer.get("deleted_at") is not None:
        return False
    if transfer.get("transaction_type") and transfer.get("transaction_type") != "transfer":
        return False

    # Extract flow fields
    if hasattr(flow, "direction"):
        f_dir = getattr(flow, "direction")
        f_amt = getattr(flow, "amount")
        f_curr = getattr(flow, "currency")
        f_occ = getattr(flow, "occurred_on")
        f_post = getattr(flow, "posted_on")
        f_ref = getattr(flow, "external_reference")
    elif isinstance(flow, dict):
        f_dir = flow.get("direction")
        f_amt = flow.get("amount")
        f_curr = flow.get("currency")
        f_occ = flow.get("occurred_on")
        f_post = flow.get("posted_on")
        f_ref = flow.get("external_reference")
    else:
        return False

    if isinstance(f_occ, str):
        try:
            f_occ = date.fromisoformat(f_occ)
        except Exception:
            pass
    if isinstance(f_post, str):
        try:
            f_post = date.fromisoformat(f_post)
        except Exception:
            pass

    f_amt_dec = parse_decimal(f_amt) if f_amt is not None else None
    if f_amt_dec is None or f_amt_dec <= Decimal("0.00"):
        return False

    acc_curr = account_currency.strip().upper()
    if f_curr and str(f_curr).strip().upper() != acc_curr:
        return False

    # Direction & Leg
    if f_dir == "contribution":
        if str(transfer.get("to_account_id")) != str(account_id):
            return False
        tx_curr = str(transfer.get("to_currency") or "").strip().upper()
        if tx_curr != acc_curr:
            return False
        tx_amt = parse_decimal(transfer.get("to_amount") if transfer.get("to_amount") is not None else transfer.get("amount"))
        if tx_amt != f_amt_dec:
            return False
    elif f_dir == "withdrawal":
        if str(transfer.get("from_account_id")) != str(account_id):
            return False
        tx_curr = str(transfer.get("from_currency") or "").strip().upper()
        if tx_curr != acc_curr:
            return False
        tx_amt = parse_decimal(transfer.get("from_amount") if transfer.get("from_amount") is not None else transfer.get("amount"))
        if tx_amt != f_amt_dec:
            return False
    else:
        return False

    # Date compatibility
    tx_occ = transfer.get("occurred_on") or transfer.get("transaction_on")
    if isinstance(tx_occ, datetime):
        tx_occ = tx_occ.date()
    elif isinstance(tx_occ, str):
        try:
            tx_occ = date.fromisoformat(tx_occ)
        except Exception:
            pass

    tx_post = transfer.get("posted_on")
    if isinstance(tx_post, datetime):
        tx_post = tx_post.date()
    elif isinstance(tx_post, str):
        try:
            tx_post = date.fromisoformat(tx_post)
        except Exception:
            pass

    if f_occ is not None:
        if tx_occ != f_occ:
            return False
    elif f_post is not None:
        effective_tx_date = tx_post if tx_post is not None else tx_occ
        if effective_tx_date != f_post:
            return False

    # External reference compatibility
    f_ref_str = str(f_ref).strip() if f_ref is not None and str(f_ref).strip() else None
    tx_ref_raw = transfer.get("remarks") or transfer.get("external_reference") or transfer.get("description")
    tx_ref_str = str(tx_ref_raw).strip() if tx_ref_raw is not None and str(tx_ref_raw).strip() else None

    if f_ref_str and tx_ref_str:
        if f_ref_str != tx_ref_str and f_ref_str not in tx_ref_str and tx_ref_str not in f_ref_str:
            return False

    return True
