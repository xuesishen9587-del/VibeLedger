from typing import Dict, Any, List, Optional
from uuid import UUID


def classify_candidates(candidates: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Classifies candidates according to canonical persisted lifecycle:
    - actionable: 'proposed', 'needs_review'
    - resolved: 'accepted', 'applied'
    - rejected: 'rejected'
    """
    actionable = []
    resolved = []
    rejected = []

    for c in candidates:
        st = c.get("status")
        if st in ("proposed", "needs_review"):
            actionable.append(c)
        elif st in ("accepted", "applied"):
            resolved.append(c)
        elif st == "rejected":
            rejected.append(c)
        else:
            actionable.append(c)

    return {
        "actionable": actionable,
        "resolved": resolved,
        "rejected": rejected
    }


def format_candidate_options(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts deterministic review options for ambiguous candidate matching.
    """
    options = candidate.get("options")
    if options and isinstance(options, list):
        return options
    payload = candidate.get("payload")
    if payload and isinstance(payload, dict) and "options" in payload:
        return payload["options"]
    return []


def is_ambiguous_match_candidate(candidate: Dict[str, Any]) -> bool:
    """
    Determines if candidate requires explicit target transaction selection.
    """
    if candidate.get("reason_code") == "MULTIPLE_TRANSACTION_MATCHES":
        return True
    options = format_candidate_options(candidate)
    return len(options) > 1


def is_category_required_candidate(candidate: Dict[str, Any]) -> bool:
    """
    Determines if candidate requires category selection before accept.
    """
    if candidate.get("reason_code") == "CATEGORY_REQUIRED":
        return True
    payload = candidate.get("payload", {})
    tx_data = payload.get("transaction", payload) if isinstance(payload, dict) else {}
    return bool(tx_data.get("transaction_type") in ("expense", "fee") and not tx_data.get("category_id"))


def build_category_patch_payload(candidate: Dict[str, Any], category_id: str) -> Dict[str, Any]:
    """
    Constructs a structured patch payload for updating missing candidate category.
    """
    payload = candidate.get("payload", {})
    if isinstance(payload, dict) and "transaction" in payload:
        updated = dict(payload)
        updated["transaction"] = dict(payload["transaction"])
        updated["transaction"]["category_id"] = str(category_id)
        return updated
    elif isinstance(payload, dict):
        updated = dict(payload)
        updated["category_id"] = str(category_id)
        return {"transaction": updated}
    return {"transaction": {"category_id": str(category_id)}}


def is_batch_ready_to_commit(preview: Dict[str, Any]) -> bool:
    """
    Checks if reconciliation batch has no remaining actionable candidates.
    """
    candidates = preview.get("candidates", [])
    classified = classify_candidates(candidates)
    return len(classified["actionable"]) == 0
