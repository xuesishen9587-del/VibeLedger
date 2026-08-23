from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from uuid import UUID
from datetime import date

from app.domain.reconciliation.models import (
    NormalizedStatementLine,
    CandidateProposal,
    REFUND_ORIGINAL_NOT_FOUND,
    MULTIPLE_REFUND_ORIGINALS,
    REFUND_EXCEEDS_ORIGINAL,
    REFUND_LOOKBACK_DAYS,
    MERCHANT_STRONG_SIMILARITY
)
from app.domain.reconciliation.scoring import trigram_similarity
from app.domain.money import parse_decimal, quantize_money


def process_refund_line(
    line: NormalizedStatementLine,
    selected_account_id: UUID,
    candidate_expenses: List[Dict[str, Any]],
    existing_refund_totals: Dict[UUID, Decimal]
) -> CandidateProposal:
    """
    Evaluates refund matching for a refund statement line:
    - Lookback <= 180 days
    - Direction must be credit
    - Prior expenses in same household/account
    - Strong merchant similarity (>= 0.80) required for auto-accept
    - Refund amount <= remaining refundable amount
    - Detects multiple originals or missing original
    """
    line_date = line.effective_date
    line_curr = line.settlement_currency
    line_amount = quantize_money(line.settlement_amount, line_curr)
    line_desc = line.merchant_hint or line.description_normalized or line.description_raw

    # Direction check: refund must be a credit
    if line.direction != "credit":
        return CandidateProposal(
            candidate_type="refund",
            status="needs_review",
            statement_line_id=line.id,
            payload={
                "line": {
                    "direction": line.direction,
                    "amount": str(line_amount),
                    "currency": line_curr,
                    "occurred_on": line_date.isoformat() if line_date else None,
                    "description": line.description_raw
                }
            },
            reason_code="TYPE_AMBIGUOUS",
            reason_detail="Refund statement line has non-credit direction; manual review required"
        )

    plausible_expenses = []

    for exp in candidate_expenses:
        if exp.get("transaction_type") != "expense" or exp.get("status") != "committed" or exp.get("deleted_at") is not None:
            continue

        exp_date = exp.get("occurred_on")
        if isinstance(exp_date, str):
            exp_date = date.fromisoformat(exp_date)

        # Must occur on or before the refund date within 180 days
        if line_date and exp_date:
            days_diff = (line_date - exp_date).days
            if days_diff < 0 or days_diff > REFUND_LOOKBACK_DAYS:
                continue

        exp_curr = exp.get("from_currency") or exp.get("original_currency")
        if exp_curr != line_curr:
            continue

        exp_orig_amount = quantize_money(parse_decimal(exp.get("from_amount") or exp.get("original_amount")), exp_curr)
        exp_id = exp["id"]
        already_refunded = quantize_money(existing_refund_totals.get(exp_id, Decimal("0.00")), exp_curr)
        remaining_refundable = exp_orig_amount - already_refunded

        # Check if refund exceeds remaining refundable
        if line_amount > remaining_refundable:
            sim = trigram_similarity(line_desc, exp.get("merchant") or exp.get("remarks"))
            if sim >= Decimal("0.40") or (exp.get("merchant") and line_desc in exp["merchant"].lower()):
                return CandidateProposal(
                    candidate_type="refund",
                    status="needs_review",
                    statement_line_id=line.id,
                    target_transaction_id=exp_id,
                    payload={
                        "original_expense_id": str(exp_id),
                        "refund_amount": str(line_amount),
                        "original_amount": str(exp_orig_amount),
                        "already_refunded": str(already_refunded),
                        "remaining_refundable": str(remaining_refundable)
                    },
                    reason_code=REFUND_EXCEEDS_ORIGINAL,
                    reason_detail=f"Refund amount {line_amount} exceeds remaining refundable balance {remaining_refundable}"
                )
            continue

        # Merchant similarity check
        exp_desc = exp.get("merchant_normalized") or exp.get("merchant") or exp.get("remarks")
        sim = trigram_similarity(line_desc, exp_desc)
        if sim >= Decimal("0.40") or (exp.get("merchant") and line_desc in exp["merchant"].lower()):
            plausible_expenses.append((exp, sim, remaining_refundable))

    # Case 1: No plausible original expense found
    if not plausible_expenses:
        return CandidateProposal(
            candidate_type="refund",
            status="needs_review",
            statement_line_id=line.id,
            payload={
                "line": {
                    "amount": str(line_amount),
                    "currency": line_curr,
                    "occurred_on": line_date.isoformat() if line_date else None,
                    "description": line.description_raw
                }
            },
            reason_code=REFUND_ORIGINAL_NOT_FOUND,
            reason_detail="No matching original expense found within 180-day lookback window"
        )

    # Sort plausible expenses: exact amount match first, similarity DESC, date_diff ASC
    plausible_expenses.sort(
        key=lambda item: (
            -(1 if quantize_money(parse_decimal(item[0].get("from_amount") or item[0].get("original_amount")), line_curr) == line_amount else 0),
            -item[1],
            (line_date - item[0]["occurred_on"]).days if line_date and item[0].get("occurred_on") else 999
        )
    )

    # Case 2: Multiple equally plausible original expenses
    if len(plausible_expenses) > 1:
        top_exp, top_sim, _ = plausible_expenses[0]
        second_exp, second_sim, _ = plausible_expenses[1]
        if top_sim == second_sim or (top_sim - second_sim < Decimal("0.15") and quantize_money(parse_decimal(top_exp.get("from_amount") or top_exp.get("original_amount")), line_curr) == quantize_money(parse_decimal(second_exp.get("from_amount") or second_exp.get("original_amount")), line_curr)):
            return CandidateProposal(
                candidate_type="refund",
                status="needs_review",
                statement_line_id=line.id,
                payload={
                    "candidate_original_expense_ids": [str(item[0]["id"]) for item in plausible_expenses]
                },
                reason_code=MULTIPLE_REFUND_ORIGINALS,
                reason_detail="Multiple plausible original expenses found for this refund"
            )

    # Case 3: Unique matching original expense -> Check strong similarity threshold (>= 0.80)
    best_exp, best_sim, rem_ref = plausible_expenses[0]
    if best_sim >= MERCHANT_STRONG_SIMILARITY:
        status = "accepted"
        reason_code = None
        reason_detail = None
    else:
        status = "needs_review"
        reason_code = "MERCHANT_WEAK_MATCH"
        reason_detail = f"Merchant similarity ({best_sim}) is below strong threshold (0.80); manual confirmation required"

    return CandidateProposal(
        candidate_type="refund",
        status=status,
        statement_line_id=line.id,
        target_transaction_id=best_exp["id"],
        payload={
            "refund": {
                "original_expense_id": str(best_exp["id"]),
                "amount": str(line_amount),
                "currency": line_curr,
                "occurred_on": line_date.isoformat() if line_date else None,
                "category_id": str(best_exp["category_id"]) if best_exp.get("category_id") else None,
                "merchant": best_exp.get("merchant") or line.merchant_hint,
                "relation_type": "refund_of"
            }
        },
        confidence=best_sim,
        reason_code=reason_code,
        reason_detail=reason_detail
    )

