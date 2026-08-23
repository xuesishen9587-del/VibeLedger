from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from uuid import UUID
from datetime import date

from app.domain.reconciliation.models import (
    NormalizedStatementLine,
    CandidateProposal,
    ScoredCandidate,
    AUTO_MATCH_SCORE,
    AUTO_MATCH_MARGIN,
    NO_MATCH,
    LOW_MATCH_SCORE,
    MULTIPLE_TRANSACTION_MATCHES,
    TRANSACTION_ALREADY_CLAIMED,
    AMOUNT_CONFLICT,
    ORIGINAL_AMOUNT_CONFLICT,
    DATE_OUTSIDE_WINDOW,
    AUTHORITATIVE_DATA_CONFLICT,
    SETTLEMENT_DEVIATION_SUSPICIOUS
)
from app.domain.reconciliation.scoring import compute_match_score



def match_statement_lines_to_transactions(
    lines: List[NormalizedStatementLine],
    transactions: List[Dict[str, Any]],
    selected_account_id: UUID
) -> Tuple[List[CandidateProposal], List[NormalizedStatementLine]]:
    """
    Deterministic matching engine for statement lines against candidate transactions.
    Returns:
    - candidates: List[CandidateProposal] for lines that matched (accepted or needs_review)
    - unmatched_lines: List[NormalizedStatementLine] that found no viable match
    """
    sel_acc = str(selected_account_id)
    
    def _extract_acc(v):
        if not v:
            return None
        if isinstance(v, dict):
            return str(v.get("id")) if v.get("id") else None
        return str(v)

    # 1. Pre-filter committed, non-deleted transactions for the selected account
    eligible_txs = []
    for tx in transactions:
        if tx.get("status") != "committed" or tx.get("deleted_at") is not None:
            continue
        from_acc = _extract_acc(tx.get("from_account_id") or tx.get("from_account"))
        to_acc = _extract_acc(tx.get("to_account_id") or tx.get("to_account"))
        if from_acc == sel_acc or to_acc == sel_acc:
            eligible_txs.append(tx)

    # 2. Score all valid candidate pairs for each line
    # Map: line.id -> List[ScoredCandidate]
    line_candidates: Dict[UUID, List[ScoredCandidate]] = {}
    line_conflicts: Dict[UUID, List[ScoredCandidate]] = {}
    
    for line in lines:
        scored_for_line: List[ScoredCandidate] = []
        conflicts_for_line: List[ScoredCandidate] = []

        for tx in eligible_txs:
            from_acc = _extract_acc(tx.get("from_account_id") or tx.get("from_account"))
            to_acc = _extract_acc(tx.get("to_account_id") or tx.get("to_account"))
            
            # Direction gate
            if line.direction == "debit" and from_acc != sel_acc:
                continue
            if line.direction == "credit" and to_acc != sel_acc:
                continue
            if line.direction == "unknown" and from_acc != sel_acc and to_acc != sel_acc:
                continue

            score = compute_match_score(line, tx, selected_account_id)
            if score.is_blocked:
                # Check for material same-event conflict evidence
                is_material = False
                if score.block_reason in (AMOUNT_CONFLICT, ORIGINAL_AMOUNT_CONFLICT):
                    if score.merchant_similarity >= Decimal("0.40") or (score.date_diff_days is not None and score.date_diff_days <= 5):
                        is_material = True
                elif score.block_reason == SETTLEMENT_DEVIATION_SUSPICIOUS:
                    is_material = True
                elif score.block_reason == AUTHORITATIVE_DATA_CONFLICT:
                    if score.merchant_similarity >= Decimal("0.40") or (score.date_diff_days is not None and score.date_diff_days <= 5):
                        is_material = True
                elif score.block_reason == DATE_OUTSIDE_WINDOW:
                    if score.merchant_similarity >= Decimal("0.80") and score.amount_score >= 35:
                        is_material = True

                if is_material:
                    conflicts_for_line.append(ScoredCandidate(statement_line=line, transaction=tx, score=score))
                continue

            if score.date_diff_days is not None and score.date_diff_days > 5:
                continue
            
            scored_for_line.append(ScoredCandidate(statement_line=line, transaction=tx, score=score))

        # Deterministic sorting: total_score DESC, date_diff ASC, tx_id ASC
        scored_for_line.sort(
            key=lambda sc: (
                -sc.score.total_score,
                sc.score.date_diff_days if sc.score.date_diff_days is not None else 999,
                str(sc.transaction.get("id"))
            )
        )
        conflicts_for_line.sort(
            key=lambda sc: (
                -sc.score.merchant_similarity,
                sc.score.date_diff_days if sc.score.date_diff_days is not None else 999,
                str(sc.transaction.get("id"))
            )
        )
        line_candidates[line.id] = scored_for_line
        line_conflicts[line.id] = conflicts_for_line

    # 3. Find transaction -> List of lines mapping to verify mutual-best uniqueness
    # Map: tx_id -> List[(line_id, ScoredCandidate)]
    tx_to_lines: Dict[UUID, List[Tuple[UUID, ScoredCandidate]]] = {}
    for line_id, sc_list in line_candidates.items():
        for sc in sc_list:
            tx_id = sc.transaction["id"]
            if tx_id not in tx_to_lines:
                tx_to_lines[tx_id] = []
            tx_to_lines[tx_id].append((line_id, sc))

    # Sort each tx's candidate lines: score DESC, date_diff ASC, line_id ASC
    for tx_id, pairs in tx_to_lines.items():
        pairs.sort(
            key=lambda p: (
                -p[1].score.total_score,
                p[1].score.date_diff_days if p[1].score.date_diff_days is not None else 999,
                str(p[0])
            )
        )

    # 4. Make deterministic match decisions
    match_candidates: List[CandidateProposal] = []
    unmatched_lines: List[NormalizedStatementLine] = []

    for line in lines:
        candidates = line_candidates.get(line.id, [])
        conflicts = line_conflicts.get(line.id, [])

        if not candidates:
            if conflicts:
                # Material conflict exists: record needs_review candidate and DO NOT treat line as unmatched
                best_conf = conflicts[0]
                conf_score = best_conf.score
                conf_tx_id = best_conf.transaction["id"]
                match_candidates.append(CandidateProposal(
                    candidate_type="match",
                    status="needs_review",
                    statement_line_id=line.id,
                    target_transaction_id=conf_tx_id,
                    payload={
                        "evidence": {
                            "block_reason": conf_score.block_reason,
                            "date_diff_days": conf_score.date_diff_days,
                            "merchant_similarity": str(conf_score.merchant_similarity)
                        },
                        "matched_transaction": {
                            "id": str(conf_tx_id),
                            "occurred_on": best_conf.transaction.get("occurred_on").isoformat() if isinstance(best_conf.transaction.get("occurred_on"), date) else str(best_conf.transaction.get("occurred_on")),
                            "amount": str(best_conf.transaction.get("from_amount") or best_conf.transaction.get("to_amount") or best_conf.transaction.get("original_amount")),
                            "currency": str(best_conf.transaction.get("from_currency") or best_conf.transaction.get("to_currency") or best_conf.transaction.get("original_currency")),
                            "merchant": best_conf.transaction.get("merchant")
                        }
                    },
                    confidence=Decimal("0.50"),
                    reason_code=conf_score.block_reason,
                    reason_detail=f"Material conflict with candidate transaction: {conf_score.block_reason}"
                ))
            else:
                unmatched_lines.append(line)
            continue


        best_cand = candidates[0]
        best_score = best_cand.score.total_score
        second_best_score = candidates[1].score.total_score if len(candidates) > 1 else 0
        best_tx_id = best_cand.transaction["id"]

        # Check if line direction is unknown
        if line.direction == "unknown":
            match_candidates.append(CandidateProposal(
                candidate_type="match",
                status="needs_review",
                statement_line_id=line.id,
                target_transaction_id=best_tx_id,
                payload={
                    "evidence": {
                        "score_breakdown": {
                            "amount": best_cand.score.amount_score,
                            "date": best_cand.score.date_score,
                            "merchant": best_cand.score.merchant_score,
                            "type": best_cand.score.type_score,
                            "extra": best_cand.score.extra_score,
                            "total": best_cand.score.total_score
                        },
                        "reason": "Unknown line direction"
                    }
                },
                confidence=Decimal(best_score) / Decimal("100.00"),
                reason_code="TYPE_AMBIGUOUS",
                reason_detail="Statement line has unknown direction and requires manual verification"
            ))
            continue

        # Check score threshold
        if best_score < AUTO_MATCH_SCORE:
            match_candidates.append(CandidateProposal(
                candidate_type="match",
                status="needs_review",
                statement_line_id=line.id,
                target_transaction_id=best_tx_id,
                payload={
                    "evidence": {
                        "score_breakdown": {
                            "amount": best_cand.score.amount_score,
                            "date": best_cand.score.date_score,
                            "merchant": best_cand.score.merchant_score,
                            "type": best_cand.score.type_score,
                            "extra": best_cand.score.extra_score,
                            "total": best_cand.score.total_score
                        }
                    }
                },
                confidence=Decimal(best_score) / Decimal("100.00"),
                reason_code=LOW_MATCH_SCORE,
                reason_detail=f"Best match score {best_score} is below automatic threshold {AUTO_MATCH_SCORE}"
            ))
            continue

        # Check score margin
        if (best_score - second_best_score) < AUTO_MATCH_MARGIN:
            match_candidates.append(CandidateProposal(
                candidate_type="match",
                status="needs_review",
                statement_line_id=line.id,
                target_transaction_id=best_tx_id,
                payload={
                    "evidence": {
                        "score_breakdown": {
                            "amount": best_cand.score.amount_score,
                            "date": best_cand.score.date_score,
                            "merchant": best_cand.score.merchant_score,
                            "type": best_cand.score.type_score,
                            "extra": best_cand.score.extra_score,
                            "total": best_cand.score.total_score
                        },
                        "second_best_score": second_best_score
                    }
                },
                confidence=Decimal(best_score) / Decimal("100.00"),
                reason_code=MULTIPLE_TRANSACTION_MATCHES,
                reason_detail=f"Match margin ({best_score} - {second_best_score} = {best_score - second_best_score}) is below required margin {AUTO_MATCH_MARGIN}"
            ))
            continue

        # Check mutual-best condition: is this line the unique best line for best_cand.transaction?
        tx_pairs = tx_to_lines.get(best_tx_id, [])
        is_tx_unique_best = False
        if tx_pairs and tx_pairs[0][0] == line.id:
            # If there's a second line for this tx, check margin / strict best
            if len(tx_pairs) > 1:
                tx_second_score = tx_pairs[1][1].score.total_score
                if best_score > tx_second_score:
                    is_tx_unique_best = True
            else:
                is_tx_unique_best = True

        if not is_tx_unique_best:
            match_candidates.append(CandidateProposal(
                candidate_type="match",
                status="needs_review",
                statement_line_id=line.id,
                target_transaction_id=best_tx_id,
                payload={
                    "evidence": {
                        "score_breakdown": {
                            "amount": best_cand.score.amount_score,
                            "date": best_cand.score.date_score,
                            "merchant": best_cand.score.merchant_score,
                            "type": best_cand.score.type_score,
                            "extra": best_cand.score.extra_score,
                            "total": best_cand.score.total_score
                        },
                        "conflict": "Multiple statement lines claim the same transaction"
                    }
                },
                confidence=Decimal(best_score) / Decimal("100.00"),
                reason_code=MULTIPLE_TRANSACTION_MATCHES,
                reason_detail="Multiple statement lines compete for the same candidate transaction"
            ))
            continue

        # All criteria satisfied: AUTO-MATCH ACCEPTED!
        payload = {
            "evidence": {
                "score_breakdown": {
                    "amount": best_cand.score.amount_score,
                    "date": best_cand.score.date_score,
                    "merchant": best_cand.score.merchant_score,
                    "type": best_cand.score.type_score,
                    "extra": best_cand.score.extra_score,
                    "total": best_cand.score.total_score
                }
            },
            "matched_transaction": {
                "id": str(best_tx_id),
                "occurred_on": best_cand.transaction.get("occurred_on").isoformat() if isinstance(best_cand.transaction.get("occurred_on"), date) else str(best_cand.transaction.get("occurred_on")),
                "amount": str(best_cand.transaction.get("from_amount") or best_cand.transaction.get("to_amount") or best_cand.transaction.get("original_amount")),
                "currency": str(best_cand.transaction.get("from_currency") or best_cand.transaction.get("to_currency") or best_cand.transaction.get("original_currency")),
                "merchant": best_cand.transaction.get("merchant")
            }
        }
        
        # If this was an estimated foreign card settlement, include settlement patch details in payload
        if best_cand.transaction.get("account_leg_status") == "estimated":
            patch = {
                "actual_settlement_amount": str(line.settlement_amount),
                "settlement_amount": str(line.settlement_amount),
                "estimated_amount": str(best_cand.transaction.get("from_amount") or best_cand.transaction.get("to_amount") or best_cand.transaction.get("original_amount")),
                "settlement_currency": line.settlement_currency,
                "posted_on": line.posted_on.isoformat() if line.posted_on else None
            }
            payload["settlement_patch"] = patch
            payload["evidence"]["settlement_patch"] = patch

        match_candidates.append(CandidateProposal(
            candidate_type="match",
            status="accepted",
            statement_line_id=line.id,
            target_transaction_id=best_tx_id,
            payload=payload,
            confidence=Decimal(best_score) / Decimal("100.00")
        ))


    return match_candidates, unmatched_lines
