from decimal import Decimal
from typing import Optional, Dict, Any, Set, Tuple
from datetime import date
import unicodedata
import re

from app.domain.reconciliation.models import (
    NormalizedStatementLine,
    MatchScore,
    MERCHANT_STRONG_SIMILARITY,
    MERCHANT_MEDIUM_SIMILARITY,
    MERCHANT_WEAK_SIMILARITY,
    SETTLEMENT_DEVIATION_SUSPICIOUS,
    AMOUNT_CONFLICT,
    ORIGINAL_AMOUNT_CONFLICT,
    DATE_OUTSIDE_WINDOW
)
from app.domain.reconciliation.normalizer import normalize_description
from app.domain.money import parse_decimal, quantize_money


def extract_trigrams(text: str) -> Set[str]:
    """
    Extracts pg_trgm compatible character trigrams with two leading spaces and one trailing space.
    """
    if not text:
        return set()
    padded = f"  {text} "
    trigrams = set()
    for i in range(len(padded) - 2):
        trigrams.add(padded[i:i + 3])
    return trigrams


def trigram_similarity(str1: Optional[str], str2: Optional[str]) -> Decimal:
    """
    Computes deterministic Jaccard trigram similarity matching PostgreSQL pg_trgm.
    similarity = |trigrams(s1) & trigrams(s2)| / |trigrams(s1) | trigrams(s2)|
    Returns Decimal between 0.00 and 1.00.
    """
    s1 = normalize_description(str1) if str1 else ""
    s2 = normalize_description(str2) if str2 else ""
    if not s1 or not s2:
        return Decimal("0.00")
    if s1 == s2:
        return Decimal("1.00")

    tri1 = extract_trigrams(s1)
    tri2 = extract_trigrams(s2)
    if not tri1 and not tri2:
        return Decimal("0.00")
    
    intersection = len(tri1 & tri2)
    union = len(tri1 | tri2)
    if union == 0:
        return Decimal("0.00")
    
    sim = Decimal(intersection) / Decimal(union)
    return sim.quantize(Decimal("0.0001"))


def compute_match_score(
    line: NormalizedStatementLine,
    tx: Dict[str, Any],
    selected_account_id: Any
) -> MatchScore:
    """
    Computes exact frozen match score components for a candidate pair:
    - Amount evidence: up to 40
    - Date proximity: up to 20
    - Merchant/description similarity: up to 20
    - Type compatibility: up to 10
    - Extra evidence: up to 10
    Total capped at 100.
    """
    score = MatchScore()
    
    # --- 1. Date check and proximity score ---
    effective_date = line.effective_date
    tx_occurred_on = tx.get("occurred_on")
    if isinstance(tx_occurred_on, str):
        tx_occurred_on = date.fromisoformat(tx_occurred_on)
    
    if effective_date is None or tx_occurred_on is None:
        score.is_blocked = True
        score.block_reason = DATE_OUTSIDE_WINDOW
        return score

    date_diff = abs((effective_date - tx_occurred_on).days)
    score.date_diff_days = date_diff

    if date_diff > 5:
        score.date_score = 0
        score.is_blocked = True
        score.block_reason = DATE_OUTSIDE_WINDOW
    elif date_diff == 0:
        score.date_score = 20
    elif date_diff == 1:
        score.date_score = 18
    elif date_diff == 2:
        score.date_score = 16
    elif date_diff == 3:
        score.date_score = 12
    elif date_diff == 4:
        score.date_score = 8
    elif date_diff == 5:
        score.date_score = 5

    # --- 2. Amount Evidence Score & Contradiction Checks ---
    # Determine transaction selected-account amount & currency
    tx_selected_amount: Optional[Decimal] = None
    tx_selected_currency: Optional[str] = None
    tx_account_leg_status = tx.get("account_leg_status", "authoritative")

    def _extract_acc(v):
        if not v:
            return None
        if isinstance(v, dict):
            return str(v.get("id")) if v.get("id") else None
        return str(v)

    from_acc = _extract_acc(tx.get("from_account_id") or tx.get("from_account"))
    to_acc = _extract_acc(tx.get("to_account_id") or tx.get("to_account"))
    sel_acc = str(selected_account_id)

    if line.direction == "debit":
        if from_acc == sel_acc:

            if tx.get("from_amount") is not None:
                tx_selected_amount = parse_decimal(tx["from_amount"])
                tx_selected_currency = tx.get("from_currency")
            elif tx.get("original_amount") is not None:
                tx_selected_amount = parse_decimal(tx["original_amount"])
                tx_selected_currency = tx.get("original_currency")
    elif line.direction == "credit":
        if to_acc == sel_acc:
            if tx.get("to_amount") is not None:
                tx_selected_amount = parse_decimal(tx["to_amount"])
                tx_selected_currency = tx.get("to_currency")
            elif tx.get("original_amount") is not None:
                tx_selected_amount = parse_decimal(tx["original_amount"])
                tx_selected_currency = tx.get("original_currency")
    else:  # unknown direction
        if from_acc == sel_acc and tx.get("from_amount") is not None:
            tx_selected_amount = parse_decimal(tx["from_amount"])
            tx_selected_currency = tx.get("from_currency")
        elif to_acc == sel_acc and tx.get("to_amount") is not None:
            tx_selected_amount = parse_decimal(tx["to_amount"])
            tx_selected_currency = tx.get("to_currency")

    # Check original amount agreement / contradiction
    orig_amount_match = False
    if line.original_amount is not None and tx.get("original_amount") is not None:
        tx_orig_amount = parse_decimal(tx["original_amount"])
        tx_orig_curr = tx.get("original_currency")
        if line.original_currency == tx_orig_curr:
            if line.original_amount == tx_orig_amount:
                orig_amount_match = True
            else:
                score.is_blocked = True
                score.block_reason = ORIGINAL_AMOUNT_CONFLICT
                return score

    # Check settlement amount
    settlement_matched = False
    if tx_selected_amount is not None and tx_selected_currency == line.settlement_currency:
        if tx_account_leg_status == "estimated":
            # Estimated settlement: compute deviation percentage
            diff = abs(line.settlement_amount - tx_selected_amount)
            deviation = diff / tx_selected_amount if tx_selected_amount > 0 else Decimal("1.0")
            if deviation <= Decimal("0.05"):
                score.amount_score = 35
                settlement_matched = True
            elif deviation <= Decimal("0.10"):
                score.amount_score = 25
                settlement_matched = True
            elif deviation <= Decimal("0.20"):
                score.amount_score = 10
                settlement_matched = True
            else:
                score.amount_score = 0
                score.is_blocked = True
                score.block_reason = SETTLEMENT_DEVIATION_SUSPICIOUS
                return score
        else:
            # Authoritative settlement
            if line.settlement_amount == tx_selected_amount:
                score.amount_score = 40
                settlement_matched = True
            else:
                # Authoritative amount conflict
                score.amount_score = 0
                score.is_blocked = True
                score.block_reason = AMOUNT_CONFLICT
                return score
    elif orig_amount_match and not settlement_matched:
        score.amount_score = 35

    # --- 3. Merchant / Description Similarity Score ---
    line_desc = line.merchant_hint or line.description_normalized or line.description_raw
    tx_desc = tx.get("merchant_normalized") or tx.get("merchant") or tx.get("remarks")
    similarity = trigram_similarity(line_desc, tx_desc)
    score.merchant_similarity = similarity

    if similarity >= MERCHANT_STRONG_SIMILARITY:
        score.merchant_score = 20
    elif similarity >= MERCHANT_MEDIUM_SIMILARITY:
        score.merchant_score = 15
    elif similarity >= MERCHANT_WEAK_SIMILARITY:
        score.merchant_score = 8
    else:
        score.merchant_score = 0

    # --- 4. Type Compatibility Score ---
    line_type = line.line_type
    tx_type = tx.get("transaction_type")
    if line_type != "unknown" and tx_type is not None:
        if line_type == tx_type:
            score.type_score = 10
        elif line_type in ("expense", "fee") and tx_type in ("expense", "fee"):
            score.type_score = 10
        elif line_type == "transfer" and tx_type == "transfer":
            score.type_score = 10
        elif line_type == "refund" and tx_type == "refund":
            score.type_score = 10

    # --- 5. Extra Evidence Score ---
    # If both settlement and original currency independently match
    if settlement_matched and orig_amount_match:
        score.extra_score = 10

    # Compute Total Score (capped at 100)
    score.total_score = min(
        100,
        score.amount_score + score.date_score + score.merchant_score + score.type_score + score.extra_score
    )

    return score
