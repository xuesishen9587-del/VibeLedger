from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from uuid import UUID
from datetime import date

from app.domain.reconciliation.models import (
    NormalizedStatementLine,
    CandidateProposal,
    INSTALLMENT_PLAN_AMBIGUOUS
)
from app.domain.reconciliation.scoring import trigram_similarity
from app.domain.money import parse_decimal, quantize_money


def process_installment_line(
    line: NormalizedStatementLine,
    selected_account_id: UUID,
    installment_plans: List[Dict[str, Any]],
    installment_periods: Dict[UUID, List[Dict[str, Any]]],
    default_expense_category_id: Optional[UUID] = None
) -> Optional[CandidateProposal]:
    """
    Checks if a credit-card debit statement line corresponds to an active or pending installment plan:
    - Checks plan.credit_account_id == selected_account_id
    - Checks plan.status in ('pending_first_bill', 'active')
    - Enforces expected recognition month schedule (no wrong-month / future period recognition)
    - Matches scheduled period amount and merchant
    - Requires active expense category for auto-accepted recognition
    - Produces recognize_installment candidate for the CURRENT billed period
    """
    if line.direction != "debit":
        return None

    line_date = line.effective_date
    if not line_date:
        return None

    stmt_ym = (line_date.year, line_date.month)
    sel_acc_str = str(selected_account_id)
    line_curr = line.settlement_currency
    line_amount = quantize_money(line.settlement_amount, line_curr)
    line_desc = line.merchant_hint or line.description_normalized or line.description_raw

    matching_candidates = []

    for plan in installment_plans:
        plan_status = plan.get("status")
        if plan_status not in ("pending_first_bill", "active"):
            continue
        if str(plan.get("credit_account_id")) != sel_acc_str:
            continue

        plan_id = plan["id"]
        periods = installment_periods.get(plan_id, [])
        # Find next scheduled period (lowest period_no with status 'scheduled')
        scheduled_periods = [p for p in periods if p.get("status") == "scheduled"]
        if not scheduled_periods:
            continue

        scheduled_periods.sort(key=lambda p: p.get("period_no", 999))
        next_period = scheduled_periods[0]
        period_no = next_period.get("period_no", 1)

        first_month = plan.get("first_statement_month")
        if isinstance(first_month, str):
            first_month = date.fromisoformat(first_month)

        # Enforce expected recognition month
        if plan_status == "pending_first_bill":
            if period_no != 1:
                continue
            if first_month and stmt_ym != (first_month.year, first_month.month):
                continue
        elif plan_status == "active":
            if not first_month:
                continue
            # Calculate expected month for period_no
            total_m = first_month.year * 12 + (first_month.month - 1) + (period_no - 1)
            exp_y = total_m // 12
            exp_m = (total_m % 12) + 1
            if stmt_ym != (exp_y, exp_m):
                # Statement is from wrong month -> do not auto-recognize
                continue

        period_curr = next_period["currency"]
        period_amt = quantize_money(parse_decimal(next_period["scheduled_amount"]), period_curr)

        if period_amt == line_amount and period_curr == line_curr:
            # Check merchant similarity if plan has merchant
            plan_merchant = plan.get("merchant")
            sim = Decimal("1.00")
            if plan_merchant:
                sim = trigram_similarity(line_desc, plan_merchant)
                if sim < Decimal("0.30") and (plan_merchant.lower() not in line_desc.lower() and line_desc.lower() not in plan_merchant.lower()):
                    continue
            
            matching_candidates.append((plan, next_period, sim))

    if not matching_candidates:
        return None

    if len(matching_candidates) > 1:
        return CandidateProposal(
            candidate_type="recognize_installment",
            status="needs_review",
            statement_line_id=line.id,
            payload={
                "candidate_plan_ids": [str(item[0]["id"]) for item in matching_candidates],
                "amount": str(line_amount),
                "currency": line_curr
            },
            reason_code=INSTALLMENT_PLAN_AMBIGUOUS,
            reason_detail="Multiple candidate installment plans match this statement line"
        )

    matched_plan, matched_period, _ = matching_candidates[0]
    is_first = (matched_period.get("period_no") == 1)
    is_last = (matched_period.get("period_no") == matched_plan.get("total_periods"))

    cat_id = default_expense_category_id
    if cat_id is None:
        status = "needs_review"
        reason_code = "CATEGORY_REQUIRED"
        reason_detail = "Active expense category required to recognize installment period"
    else:
        status = "accepted"
        reason_code = None
        reason_detail = None

    return CandidateProposal(
        candidate_type="recognize_installment",
        status=status,
        statement_line_id=line.id,
        payload={
            "installment": {
                "plan_id": str(matched_plan["id"]),
                "period_id": str(matched_period["id"]),
                "period_no": matched_period.get("period_no"),
                "total_periods": matched_plan.get("total_periods"),
                "scheduled_amount": str(matched_period.get("scheduled_amount")),
                "currency": matched_period.get("currency"),
                "merchant": matched_plan.get("merchant") or line.merchant_hint,
                "category_id": str(cat_id) if cat_id else None,
                "is_first_period": is_first,
                "is_last_period": is_last,
                "occurred_on": line.effective_date.isoformat() if line.effective_date else None
            }
        },
        reason_code=reason_code,
        reason_detail=reason_detail
    )

