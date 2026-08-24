from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from uuid import UUID
from datetime import date

from app.domain.reconciliation.models import (
    CandidateProposal,
    AUTO_ADJUST_THRESHOLD_CNY,
    RECONCILIATION_RESIDUAL_TOO_LARGE
)
from app.domain.money import parse_decimal, quantize_money


def simulate_candidate_effects(
    candidates: List[CandidateProposal],
    account_id: UUID,
    account_currency: str
) -> Decimal:
    """
    Simulates the signed delta on the selected account's balance from all accepted/proposed candidates.
    - Expense / debit transaction: -amount
    - Income / credit transaction: +amount
    - Transfer out (from_account): -from_amount
    - Transfer in (to_account): +to_amount
    - Refund (into account): +amount
    - Installment recognized (debit expense): -scheduled_amount
    - Existing match: 0 (already in ledger or settlement delta if estimated)
    """
    acc_id_str = str(account_id)
    total_delta = Decimal("0.00")

    for cand in candidates:
        # We only simulate explainable new financial effects (not adjustments)
        if cand.candidate_type == "create_transaction":
            tx_data = cand.payload.get("transaction", {})
            amt = parse_decimal(tx_data.get("amount", "0"))
            ttype = tx_data.get("transaction_type", "expense")
            if ttype in ("expense", "fee"):
                total_delta -= amt
            elif ttype in ("income", "cash_income"):
                total_delta += amt
        elif cand.candidate_type == "create_transfer":
            tx_data = cand.payload.get("transfer", {})
            from_acc = tx_data.get("from_account_id")
            to_acc = tx_data.get("to_account_id")
            if from_acc == acc_id_str:
                amt = parse_decimal(tx_data.get("from_amount", "0"))
                total_delta -= amt
            elif to_acc == acc_id_str:
                amt = parse_decimal(tx_data.get("to_amount", "0"))
                total_delta += amt
        elif cand.candidate_type == "refund":
            tx_data = cand.payload.get("refund", {})
            amt = parse_decimal(tx_data.get("amount", "0"))
            total_delta += amt
        elif cand.candidate_type == "recognize_installment":
            inst_data = cand.payload.get("installment", {})
            amt = parse_decimal(inst_data.get("scheduled_amount", "0"))
            total_delta -= amt
        elif cand.candidate_type == "match":
            # Existing matched transaction is already in ledger.
            # If the matched transaction had an estimated leg status and a settlement_patch:
            # During RESIDUAL SIMULATION ONLY, simulate the signed settlement delta as an explainable effect.
            # Phase 8 will finalize the settlement mutation and reporting FX freeze.
            if cand.status == "accepted" and "settlement_patch" in cand.payload:
                sp = cand.payload.get("settlement_patch") or {}
                mt = cand.payload.get("matched_transaction") or {}
                if sp.get("settlement_currency") == account_currency:
                    actual_settle = parse_decimal(sp.get("settlement_amount", "0"))
                    est_amt = parse_decimal(mt.get("amount", "0"))
                    total_delta += (est_amt - actual_settle)


    return quantize_money(total_delta, account_currency)



def evaluate_residual_and_batch_readiness(
    baseline_projected_balance: Decimal,
    authoritative_balance: Optional[Decimal],
    candidates: List[CandidateProposal],
    account_id: UUID,
    account_currency: str,
    fx_rate_to_cny: Optional[Decimal] = None
) -> Tuple[str, Optional[Decimal], Optional[CandidateProposal]]:
    """
    Computes final residual after all simulated candidate effects:
    residual = authoritative_balance - (baseline_projected_balance + simulated_delta)
    When authoritative_balance is None:
    - residual is None
    - no adjustment candidate is proposed
    - batch_status is determined solely by candidate review state ('needs_review' vs 'ready')
    Returns:
    - batch_status: 'ready' or 'needs_review'
    - residual_amount: Optional[Decimal]
    - adjustment_candidate: Optional[CandidateProposal]
    """
    has_needs_review_candidate = any(c.status == "needs_review" for c in candidates)

    if authoritative_balance is None:
        batch_status = "needs_review" if has_needs_review_candidate else "ready"
        return batch_status, None, None

    simulated_delta = simulate_candidate_effects(candidates, account_id, account_currency)
    final_projected_balance = baseline_projected_balance + simulated_delta
    residual = quantize_money(authoritative_balance - final_projected_balance, account_currency)

    # Convert residual to CNY for threshold comparison
    if account_currency == "CNY":
        residual_cny = residual
    else:
        rate = fx_rate_to_cny if fx_rate_to_cny is not None else Decimal("1.0")
        residual_cny = quantize_money(residual * rate, "CNY")

    has_needs_review_candidate = any(c.status == "needs_review" for c in candidates)
    adjustment_candidate: Optional[CandidateProposal] = None

    if residual == Decimal("0.00"):
        batch_status = "needs_review" if has_needs_review_candidate else "ready"
    elif abs(residual_cny) <= AUTO_ADJUST_THRESHOLD_CNY:
        if not has_needs_review_candidate:
            # Small unexplained residual and clean batch -> propose adjustment
            adjustment_candidate = CandidateProposal(
                candidate_type="adjustment",
                status="accepted",
                payload={
                    "adjustment_amount": str(residual),
                    "currency": account_currency
                }
            )
            batch_status = "ready"
        else:
            # Batch has unresolved semantic ambiguity -> do not auto-adjust
            adjustment_candidate = CandidateProposal(
                candidate_type="adjustment",
                status="needs_review",
                payload={
                    "adjustment_amount": str(residual),
                    "currency": account_currency
                },
                reason_code="TYPE_AMBIGUOUS",
                reason_detail="Batch contains unreviewed candidate ambiguities"
            )
            batch_status = "needs_review"
    else:
        # Residual too large (> 200 CNY)
        adjustment_candidate = CandidateProposal(
            candidate_type="adjustment",
            status="needs_review",
            payload={
                "adjustment_amount": str(residual),
                "currency": account_currency
            },
            reason_code=RECONCILIATION_RESIDUAL_TOO_LARGE,
            reason_detail=f"Reconciliation residual ({residual} {account_currency} ≈ {residual_cny} CNY) exceeds auto-adjustment limit (200 CNY)"
        )
        batch_status = "needs_review"

    return batch_status, residual, adjustment_candidate
