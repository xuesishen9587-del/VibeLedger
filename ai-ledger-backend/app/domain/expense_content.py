"""Optional descriptive metadata. Never participates in financial identity or gating."""
import re
from typing import Annotated
from pydantic import BeforeValidator
from app.domain.spending import fail

SOURCES = {"manual", "screenshot", "user_note", "statement", "schedule"}

# Bad optional model metadata must not discard otherwise valid financial facts.
# Structured human edits use strict API limits instead.
ExtractedContent = Annotated[str | None, BeforeValidator(
    lambda value: value if isinstance(value, str) and len(value) <= 160 else None)]
ContentEvidence = Annotated[str | None, BeforeValidator(
    lambda value: value if isinstance(value, str) and len(value) <= 400 else None)]


def content_fields(value, source="manual"):
    value = " ".join(value.split()) if isinstance(value, str) else None
    if value and len(value) > 160:
        fail("INVALID_EXPENSE_CONTENT", "Expense content must be at most 160 characters.")
    return {"expense_content": value or None,
            "expense_content_source": source if value and source in SOURCES else None}


def extracted_content(data, *, source=None, note=None):
    """Only accept independently confident, evidenced descriptions; old providers remain valid."""
    origin = source or data.get("expense_content_source")
    evidence = (data.get("expense_content_evidence") or "").strip()
    confidence = data.get("expense_content_confidence") or 0
    if (origin not in {"screenshot", "user_note", "statement"} or not evidence
            or confidence < .85 or (origin == "user_note" and (not note or evidence not in note))):
        return content_fields(None)
    value = (data.get("expense_content") or "").strip()
    # Order identifiers are not a useful description, even when copied faithfully.
    value = re.sub(r"(?:订单[号码]*[：:\s-]*)?[A-Za-z]*\d{8,}[A-Za-z\d]*", "", value)
    value = re.sub(r"[-·\s]*(?:美团App|京东App|淘宝App)[-·\s]*$", "", value, flags=re.I)
    value = re.sub(r"[-·\s]+$", "", value).strip()
    if value in {"京东平台商户", "平台商户", "京东", "美团", "淘宝", "拼多多", "微信支付", "支付宝",
                 "消费", "购物", "商品", "付款", "支付"} or len(value) > 160:
        value = None
    # An item count is accepted only when the evidence actually states that count.
    count = re.search(r"(?:等|共|合计)\s*(\d+)\s*件", value or "")
    if count and not re.search(rf"{re.escape(count[1])}\s*件", evidence):
        value = re.sub(r"(?:等|共|合计)\s*\d+\s*件(?:商品)?", "", value).strip(" 、，")
    return content_fields(value, origin)


def command_payload(payload):
    """Retain historical defaults/hashes when an older client omits the new field."""
    data = payload.model_dump(mode="json")
    if "expense_content" not in payload.model_fields_set:
        data.pop("expense_content", None)
    return data
