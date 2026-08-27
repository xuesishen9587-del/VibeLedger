from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from uuid import UUID
from datetime import date

from app.domain.reconciliation.models import (
    NormalizedStatementLine,
    CandidateProposal,
    ReconciliationResult,
    INCOME_TRANSFER_REFUND_AMBIGUOUS,
    TYPE_AMBIGUOUS
)
from app.domain.reconciliation.normalizer import normalize_description
from app.domain.reconciliation.matcher import match_statement_lines_to_transactions
from app.domain.reconciliation.installments import process_installment_line
from app.domain.reconciliation.refunds import process_refund_line
from app.domain.reconciliation.transfers import process_transfer_line
from app.domain.reconciliation.residuals import evaluate_residual_and_batch_readiness
from app.domain.money import parse_decimal, quantize_money
import re


def _is_fee_line(line: NormalizedStatementLine) -> bool:
    if line.line_type == "fee":
        return True
    desc = line.description_raw or ""
    if "手续费" in desc or "年费" in desc or "服务费" in desc:
        return True
    if re.search(r'\b(fee|fees)\b', desc, re.IGNORECASE):
        return True
    return False


def _is_refund_line(line: NormalizedStatementLine) -> bool:
    if line.line_type == "refund":
        return True
    desc = line.description_raw or ""
    if "退款" in desc or "冲正" in desc:
        return True
    if re.search(r'\b(refund|refunds|reversed)\b', desc, re.IGNORECASE):
        return True
    return False


def _is_transfer_line(line: NormalizedStatementLine) -> bool:
    if line.line_type == "transfer":
        return True
    desc = line.description_raw or ""
    if "转账" in desc or "同行转账" in desc or "跨行转账" in desc or "还款" in desc:
        return True
    if re.search(r'\b(transfer|transfers|wire)\b', desc, re.IGNORECASE):
        return True
    return False


def _is_income_line(line: NormalizedStatementLine) -> bool:
    if line.line_type == "income":
        return True
    desc = line.description_raw or ""
    if "工资" in desc or "利息" in desc or "分红" in desc or "奖金" in desc:
        return True
    if re.search(r'\b(salary|payroll|interest|dividend|bonus)\b', desc, re.IGNORECASE):
        return True
    return False


def run_deterministic_reconciliation(

    lines: List[NormalizedStatementLine],
    transactions: List[Dict[str, Any]],
    selected_account_id: UUID,
    account_currency: str,
    baseline_projected_balance: Decimal,
    authoritative_balance: Optional[Decimal] = None,
    is_credit_account: bool = False,
    installment_plans: Optional[List[Dict[str, Any]]] = None,
    installment_periods: Optional[Dict[UUID, List[Dict[str, Any]]]] = None,
    candidate_refund_expenses: Optional[List[Dict[str, Any]]] = None,
    existing_refund_totals: Optional[Dict[UUID, Decimal]] = None,
    household_accounts: Optional[List[Dict[str, Any]]] = None,
    household_movements: Optional[List[Dict[str, Any]]] = None,
    default_expense_category_id: Optional[UUID] = None,
    default_income_category_id: Optional[UUID] = None,
    fx_rate_to_cny: Optional[Decimal] = None
) -> ReconciliationResult:
    """
    Pure deterministic reconciliation pipeline execution:
    1. Description normalization
    2. Existing transaction matching
    3. Installment billed-period recognition
    4. Refund matching
    5. Transfer matching / counter-account search
    6. Missing ordinary expense/income/fee generation
    7. Candidate simulation & residual calculation
    8. Batch readiness determination & statistics computation
    """
    # 1. Deterministically sort and normalize lines to guarantee input permutation invariance
    sorted_lines = sorted(
        lines,
        key=lambda l: (
            l.effective_date or date.min,
            l.settlement_amount,
            l.description_raw or "",
            str(l.id)
        )
    )

    for line in sorted_lines:
        if not line.description_normalized:
            line.description_normalized = normalize_description(line.description_raw)

    # 2. Existing transaction matching
    matched_candidates, remaining_lines = match_statement_lines_to_transactions(
        lines=sorted_lines,
        transactions=transactions,
        selected_account_id=selected_account_id
    )

    all_candidates: List[CandidateProposal] = list(matched_candidates)
    unmatched_after_special: List[NormalizedStatementLine] = []

    # 3. Process each remaining line through specialized matchers
    for line in remaining_lines:
        processed = False

        # 3.1 Credit account: check installment recognition
        if is_credit_account and line.direction == "debit" and installment_plans:
            inst_cand = process_installment_line(
                line=line,
                selected_account_id=selected_account_id,
                installment_plans=installment_plans,
                installment_periods=installment_periods or {},
                default_expense_category_id=default_expense_category_id
            )
            if inst_cand:
                all_candidates.append(inst_cand)
                processed = True

        # 3.2 Refund check (credit line with refund semantics or line_type == 'refund')
        if not processed and _is_refund_line(line):
            if candidate_refund_expenses:
                ref_cand = process_refund_line(
                    line=line,
                    selected_account_id=selected_account_id,
                    candidate_expenses=candidate_refund_expenses,
                    existing_refund_totals=existing_refund_totals or {}
                )
                if ref_cand:
                    all_candidates.append(ref_cand)
                    processed = True

        # 3.3 Transfer check (line_type == 'transfer' or transfer keywords)
        if not processed and _is_transfer_line(line):
            trans_cand = process_transfer_line(
                line=line,
                selected_account_id=selected_account_id,
                household_accounts=household_accounts or [],
                household_movements=household_movements or []
            )
            if trans_cand:
                all_candidates.append(trans_cand)
                processed = True

        if not processed:
            unmatched_after_special.append(line)

    # 4. Process remaining unmatched lines into missing transactions
    for line in unmatched_after_special:
        line_date = line.effective_date

        # Invariant 1: No effective date -> automatic financial creation forbidden -> needs_review!
        if line_date is None:
            all_candidates.append(CandidateProposal(
                candidate_type="create_transaction",
                status="needs_review",
                statement_line_id=line.id,
                payload={
                    "line": {
                        "direction": line.direction,
                        "line_type": line.line_type,
                        "amount": str(line.settlement_amount),
                        "currency": line.settlement_currency,
                        "description": line.description_raw
                    }
                },
                reason_code="DATE_OUTSIDE_WINDOW",
                reason_detail="Statement line has no effective transaction/posting date; manual verification required"
            ))
            continue

        # Invariant 2: line_type == "unknown" -> needs_review TYPE_AMBIGUOUS (debit) or INCOME_TRANSFER_REFUND_AMBIGUOUS (credit)
        if line.line_type == "unknown":
            r_code = INCOME_TRANSFER_REFUND_AMBIGUOUS if line.direction == "credit" else TYPE_AMBIGUOUS
            r_detail = (
                "Statement credit could be income, refund, or internal transfer; manual confirmation required"
                if line.direction == "credit"
                else "Statement line type is unknown; cannot auto-create transaction"
            )
            all_candidates.append(CandidateProposal(
                candidate_type="create_transaction",
                status="needs_review",
                statement_line_id=line.id,
                payload={
                    "line": {
                        "direction": line.direction,
                        "line_type": line.line_type,
                        "amount": str(line.settlement_amount),
                        "currency": line.settlement_currency,
                        "occurred_on": line_date.isoformat(),
                        "description": line.description_raw
                    }
                },
                reason_code=r_code,
                reason_detail=r_detail
            ))
            continue

        if line.direction == "debit":
            # Debit: expense or fee
            if _is_fee_line(line):
                tx_type = "fee"
                cat_id = default_expense_category_id
                status = "accepted" if cat_id is not None else "needs_review"
                reason_code = None if cat_id is not None else "CATEGORY_REQUIRED"
                reason_detail = None if cat_id is not None else "Category resolution is required to commit this fee"
            elif line.line_type == "expense":
                tx_type = "expense"
                cat_id = default_expense_category_id
                status = "accepted" if cat_id is not None else "needs_review"
                reason_code = None if cat_id is not None else "CATEGORY_REQUIRED"
                reason_detail = None if cat_id is not None else "Category resolution is required to commit this expense"
            else:
                # Debit but not explicit expense/fee -> needs_review
                tx_type = "expense"
                cat_id = default_expense_category_id
                status = "needs_review"
                reason_code = TYPE_AMBIGUOUS
                reason_detail = "Statement debit line semantics are ambiguous; manual confirmation required"

            all_candidates.append(CandidateProposal(
                candidate_type="create_transaction",
                status=status,
                statement_line_id=line.id,
                payload={
                    "transaction": {
                        "transaction_type": tx_type,
                        "from_account_id": str(selected_account_id),
                        "amount": str(line.settlement_amount),
                        "currency": line.settlement_currency,
                        "occurred_on": line_date.isoformat(),
                        "merchant": line.merchant_hint or line.description_raw,
                        "category_id": str(cat_id) if cat_id else None
                    }
                },
                confidence=line.confidence or Decimal("0.90"),
                reason_code=reason_code,
                reason_detail=reason_detail
            ))

        elif line.direction == "credit":
            # Credit: cash_income or ambiguous credit
            if _is_income_line(line) or line.line_type == "income":
                cat_id = default_income_category_id
                status = "accepted" if cat_id is not None else "needs_review"
                reason_code = None if cat_id is not None else "CATEGORY_REQUIRED"
                reason_detail = None if cat_id is not None else "Category resolution is required to commit this income"
                all_candidates.append(CandidateProposal(
                    candidate_type="create_transaction",
                    status=status,
                    statement_line_id=line.id,
                    payload={
                        "transaction": {
                            "transaction_type": "cash_income",
                            "to_account_id": str(selected_account_id),
                            "amount": str(line.settlement_amount),
                            "currency": line.settlement_currency,
                            "occurred_on": line_date.isoformat(),
                            "merchant": line.merchant_hint or line.description_raw,
                            "category_id": str(cat_id) if cat_id else None
                        }
                    },
                    confidence=line.confidence or Decimal("0.90"),
                    reason_code=reason_code,
                    reason_detail=reason_detail
                ))
            else:
                # Unexplained credit line -> must NOT default to income -> needs_review!
                all_candidates.append(CandidateProposal(
                    candidate_type="create_transaction",
                    status="needs_review",
                    statement_line_id=line.id,
                    payload={
                        "line": {
                            "direction": "credit",
                            "amount": str(line.settlement_amount),
                            "currency": line.settlement_currency,
                            "occurred_on": line_date.isoformat(),
                            "description": line.description_raw
                        }
                    },
                    reason_code=INCOME_TRANSFER_REFUND_AMBIGUOUS,
                    reason_detail="Statement credit could be income, refund, or internal transfer; manual confirmation required"
                ))

        else:
            # Unknown direction
            all_candidates.append(CandidateProposal(
                candidate_type="create_transaction",
                status="needs_review",
                statement_line_id=line.id,
                payload={
                    "line": {
                        "direction": line.direction,
                        "amount": str(line.settlement_amount),
                        "currency": line.settlement_currency,
                        "occurred_on": line_date.isoformat(),
                        "description": line.description_raw
                    }
                },
                reason_code=TYPE_AMBIGUOUS,
                reason_detail="Statement line has unknown direction and cannot be created automatically"
            ))

    # 5. Evaluate residual after candidate simulation

    batch_status, residual, adj_cand = evaluate_residual_and_batch_readiness(
        baseline_projected_balance=baseline_projected_balance,
        authoritative_balance=authoritative_balance,
        candidates=all_candidates,
        account_id=selected_account_id,
        account_currency=account_currency,
        fx_rate_to_cny=fx_rate_to_cny
    )

    if adj_cand is not None:
        all_candidates.append(adj_cand)

    # 6. Calculate summary counts
    matched_count = sum(1 for c in all_candidates if c.candidate_type == "match" and c.status == "accepted")
    created_count = sum(1 for c in all_candidates if c.candidate_type in ("create_transaction", "create_transfer", "refund", "recognize_installment") and c.status == "accepted")
    pending_count = sum(1 for c in all_candidates if c.status == "needs_review")
    adjustment_amount = parse_decimal(adj_cand.payload["adjustment_amount"]) if (adj_cand and adj_cand.status == "accepted") else None

    return ReconciliationResult(
        batch_status=batch_status,
        candidates=all_candidates,
        matched_count=matched_count,
        created_count=created_count,
        pending_count=pending_count,
        residual_amount=residual,
        adjustment_amount=adjustment_amount,
        engine_version="v1.0.0"
    )
