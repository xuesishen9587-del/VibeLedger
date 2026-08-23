from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from uuid import UUID
from datetime import date

from app.domain.reconciliation.models import (
    NormalizedStatementLine,
    CandidateProposal,
    COUNTER_ACCOUNT_UNRESOLVED,
    CROSS_CURRENCY_MISSING_LEG
)
from app.domain.money import parse_decimal, quantize_money


def process_transfer_line(
    line: NormalizedStatementLine,
    selected_account_id: UUID,
    household_accounts: List[Dict[str, Any]],
    household_movements: List[Dict[str, Any]]
) -> CandidateProposal:
    """
    Evaluates transfer matching & counter-account resolution for a transfer statement line:
    - Same-currency two-leg matching from explicit trustworthy counter-leg evidence
    - Cross-currency two-leg matching (requires both explicit real legs)
    - Rejects committed ordinary movements (e.g. cash_income) as fake transfer legs
    - Ambiguity detection (multiple counter-accounts -> needs_review)
    - Missing counterparty / missing cross-currency leg -> needs_review
    """
    if line.direction not in ("debit", "credit"):
        return CandidateProposal(
            candidate_type="create_transfer",
            status="needs_review",
            statement_line_id=line.id,
            payload={
                "line": {
                    "direction": line.direction,
                    "amount": str(line.settlement_amount),
                    "currency": line.settlement_currency,
                    "occurred_on": line.effective_date.isoformat() if line.effective_date else None
                }
            },
            reason_code="TYPE_AMBIGUOUS",
            reason_detail="Transfer statement line has unknown direction and cannot be auto-created"
        )

    sel_acc_str = str(selected_account_id)
    line_date = line.effective_date
    line_curr = line.settlement_currency
    line_amount = quantize_money(line.settlement_amount, line_curr)

    # Search household movements for trustworthy opposite counter-statement leg
    matching_counter_legs = []

    for mov in (household_movements or []):
        # Must be trustworthy uncommitted statement counter-leg evidence
        is_trustworthy_counter_leg = (
            mov.get("is_counter_statement_leg") is True
            or mov.get("evidence_type") in ("statement_counter_leg", "uncommitted_transfer_leg")
        )
        if not is_trustworthy_counter_leg:
            # Committed transactions (cash_income, expense, etc.) must NEVER be treated as a free transfer leg
            continue

        mov_acc_id = str(mov.get("account_id"))
        if mov_acc_id == sel_acc_str:
            continue  # Must be a different account in the household

        mov_date = mov.get("occurred_on")
        if isinstance(mov_date, str):
            mov_date = date.fromisoformat(mov_date)
        
        if line_date and mov_date and abs((line_date - mov_date).days) > 5:
            continue

        mov_dir = mov.get("direction")  # 'debit' or 'credit'
        if line.direction == "debit" and mov_dir != "credit":
            continue
        if line.direction == "credit" and mov_dir != "debit":
            continue

        mov_curr = mov.get("currency")
        if not mov_curr:
            continue
        mov_amount = quantize_money(parse_decimal(mov.get("amount", "0")), mov_curr)

        # Same currency match
        if mov_curr == line_curr and mov_amount == line_amount:
            matching_counter_legs.append({
                "account_id": UUID(mov_acc_id),
                "amount": mov_amount,
                "currency": mov_curr,
                "occurred_on": mov_date,
                "is_cross_currency": False,
                "raw_movement": mov
            })
        # Cross currency match candidate (both explicit real amounts must exist)
        elif mov_curr != line_curr and mov_amount > Decimal("0.00") and line_amount > Decimal("0.00"):
            matching_counter_legs.append({
                "account_id": UUID(mov_acc_id),
                "amount": mov_amount,
                "currency": mov_curr,
                "occurred_on": mov_date,
                "is_cross_currency": True,
                "raw_movement": mov
            })

    # Case 1: Multiple counter-accounts matched -> Ambiguity!
    if len(matching_counter_legs) > 1:
        return CandidateProposal(
            candidate_type="create_transfer",
            status="needs_review",
            statement_line_id=line.id,
            payload={
                "line": {
                    "direction": line.direction,
                    "amount": str(line_amount),
                    "currency": line_curr,
                    "occurred_on": line_date.isoformat() if line_date else None
                },
                "candidate_counter_accounts": [str(leg["account_id"]) for leg in matching_counter_legs]
            },
            reason_code=COUNTER_ACCOUNT_UNRESOLVED,
            reason_detail="Multiple plausible counter-accounts found for this transfer"
        )

    # Case 2: Exactly one matching counter-account leg
    if len(matching_counter_legs) == 1:
        leg = matching_counter_legs[0]
        if leg["is_cross_currency"]:
            # Cross-currency transfer
            if line.direction == "debit":
                from_acc, from_amt, from_cur = selected_account_id, line_amount, line_curr
                to_acc, to_amt, to_cur = leg["account_id"], leg["amount"], leg["currency"]
            else:
                from_acc, from_amt, from_cur = leg["account_id"], leg["amount"], leg["currency"]
                to_acc, to_amt, to_cur = selected_account_id, line_amount, line_curr

            effective_fx = (from_amt / to_amt).quantize(Decimal("0.000000000001"))
            return CandidateProposal(
                candidate_type="create_transfer",
                status="accepted",
                statement_line_id=line.id,
                payload={
                    "transfer": {
                        "from_account_id": str(from_acc),
                        "from_amount": str(from_amt),
                        "from_currency": from_cur,
                        "to_account_id": str(to_acc),
                        "to_amount": str(to_amt),
                        "to_currency": to_cur,
                        "effective_fx_rate": str(effective_fx),
                        "occurred_on": line_date.isoformat() if line_date else None
                    }
                }
            )
        else:
            # Same currency transfer
            if line.direction == "debit":
                from_acc = selected_account_id
                to_acc = leg["account_id"]
            else:
                from_acc = leg["account_id"]
                to_acc = selected_account_id

            return CandidateProposal(
                candidate_type="create_transfer",
                status="accepted",
                statement_line_id=line.id,
                payload={
                    "transfer": {
                        "from_account_id": str(from_acc),
                        "from_amount": str(line_amount),
                        "from_currency": line_curr,
                        "to_account_id": str(to_acc),
                        "to_amount": str(line_amount),
                        "to_currency": line_curr,
                        "effective_fx_rate": "1.000000",
                        "occurred_on": line_date.isoformat() if line_date else None
                    }
                }
            )

    # Case 3: No counter-account found
    if line.original_currency and line.original_currency != line_curr:
        return CandidateProposal(
            candidate_type="create_transfer",
            status="needs_review",
            statement_line_id=line.id,
            payload={
                "line": {
                    "direction": line.direction,
                    "amount": str(line_amount),
                    "currency": line_curr,
                    "original_amount": str(line.original_amount) if line.original_amount else None,
                    "original_currency": line.original_currency,
                    "occurred_on": line_date.isoformat() if line_date else None
                }
            },
            reason_code=CROSS_CURRENCY_MISSING_LEG,
            reason_detail="Cross-currency transfer requires both explicit account legs; cannot fabricate missing leg"
        )

    return CandidateProposal(
        candidate_type="create_transfer",
        status="needs_review",
        statement_line_id=line.id,
        payload={
            "line": {
                "direction": line.direction,
                "amount": str(line_amount),
                "currency": line_curr,
                "occurred_on": line_date.isoformat() if line_date else None
            }
        },
        reason_code=COUNTER_ACCOUNT_UNRESOLVED,
        reason_detail="No unique counter-account found for this transfer"
    )

