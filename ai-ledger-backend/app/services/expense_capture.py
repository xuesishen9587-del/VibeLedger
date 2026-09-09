"""Simplified expense capture: short durable reservations, external extraction, atomic finalize."""
import hashlib
import json
import math
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from app.domain.capture_images import decode_image
from app.domain.capture_edits import ReviseRequestPayload
from pydantic import ValidationError
from app.domain.spending import fail, money, business_date, money_text
from app.repositories import spending as repo, simplified_schema as schema
from app.services import capture_receipts as receipts, spending_service as spending
from app.services.durable_commands import compute_command_hash
from app.services.spending_reports import prime_quote


def references(conn, household_id):
    accounts = schema.list_accounts(conn, household_id, status="active")
    aliases = repo.rows(conn, "SELECT account_id,alias_text FROM account_aliases WHERE household_id=%s AND status='active'", (household_id,))
    for account in accounts:
        account["aliases"] = [a["alias_text"] for a in aliases if a["account_id"] == account["id"]]
    return accounts, schema.list_categories(conn, household_id, category_type="expense", status="active")


def match(value, records):
    if not value:
        return None
    normalized = str(value).strip().casefold()
    matches = [r for r in records if normalized in [str(r["id"]).casefold(), r["name"].casefold(),
                                                    *(a.casefold() for a in r.get("aliases", []))]]
    return matches[0] if len(matches) == 1 else None


def confidence(draft, name):
    if name in draft.get("user_fields", []):
        return True
    value = draft.get("field_confidence", {}).get(name)
    return isinstance(value, (float, int)) and math.isfinite(value) and .85 <= value <= 1


def initial_draft(result, captured_at, household, accounts, categories):
    data = result.model_dump(mode="json", exclude={"raw_response"})
    account = match(data.get("from_account"), accounts)
    category = match(data.get("category"), categories)
    observed_date = data.get("occurred_on")
    date_source = "receipt"
    if not observed_date and data.get("date_evidence") == "current_payment":
        observed_date = captured_at.astimezone(ZoneInfo(household["timezone"])).date().isoformat()
        date_source = "capture_date"
    return {"occurred_on": observed_date, "merchant": (data.get("merchant") or "")[:240] or None,
        "original_amount": data.get("original_amount"), "original_currency": data.get("original_currency"),
        "from_account": {"id": str(account["id"]), "name": account["name"]} if account else None,
        "category": {"id": str(category["id"]), "name": category["name"]} if category else None,
        "payment_mode": data.get("payment_mode"), "total_periods": data.get("total_periods"),
        "remarks": None, "intent": data.get("intent", "unknown"), "date_source": date_source,
        "date_evidence": data.get("date_evidence", "uncertain"), "field_confidence": {
            k: v for k, v in (data.get("field_confidence") or {}).items()
            if k in ("amount", "currency", "date", "account", "category", "intent", "total_periods")
            and isinstance(v, (float, int)) and math.isfinite(v) and 0 <= v <= 1},
        "user_fields": [], "duplicate_ids": [], "action": None}


def validate_draft(conn, actor, draft, request_row, *, confirming=False):
    draft = json.loads(json.dumps(draft, default=str))
    accounts, categories = references(conn, actor.household_id)
    fallback = next((c for c in categories if c["is_fallback"]), None)
    if not fallback:
        fail("CATEGORY_CONFIGURATION", "An active Other category is required.", 409)
    warnings, blocked = [], False
    def warning(code, message, blocking=True):
        nonlocal blocked
        warnings.append({"code": code, "message": message})
        blocked |= blocking
    if draft.get("intent") != "expense":
        warning("EXPENSE_INTENT_UNCERTAIN", "Confirm that this is an expense by correcting its intent.")
    elif not confirming and not confidence(draft, "intent"):
        warning("EXPENSE_INTENT_UNCERTAIN", "Review whether this is an expense.")
    household = schema.get_household(conn, actor.household_id)
    for name, key, validator in (("amount", "original_amount", lambda: money(draft.get("original_amount"), draft.get("original_currency"))),
                                  ("date", "occurred_on", lambda: business_date(draft.get("occurred_on"), household))):
        try:
            validator()
        except HTTPException:
            warning("INVALID_" + name.upper(), "Correct the expense " + name + ".")
        else:
            if not confirming and not confidence(draft, name):
                warning("UNCERTAIN_" + name.upper(), "Review the expense " + name + ".")
    if not confirming and not confidence(draft, "currency"):
        warning("UNCERTAIN_CURRENCY", "Review the original currency.")
    account = match((draft.get("from_account") or {}).get("id"), accounts)
    if not confidence(draft, "account"):
        account = None
    category = match((draft.get("category") or {}).get("id"), categories)
    uncertain = not category or not confidence(draft, "category")
    if uncertain:
        category = fallback
        warning("CATEGORY_UNCERTAIN", "Saved under Other; correct the category later.", False)
    if not account:
        warning("MISSING_ACCOUNT", "Payment account unknown; correct it later.", False)
    draft["from_account"] = {"id": str(account["id"]), "name": account["name"]} if account else None
    draft["category"] = {"id": str(category["id"]), "name": category["name"]}
    draft["category_uncertain"] = uncertain
    if draft.get("payment_mode") not in ("one_off", "installment"):
        warning("PAYMENT_MODE_REQUIRED", "Choose one-off or installment spending.")
    if draft.get("payment_mode") == "installment" and draft.get("action") not in ("record_full_purchase", "use_schedule_period"):
        warning("INSTALLMENT_ACTION_REQUIRED", "Create a schedule in Dashboard, select a due period, or explicitly record the full purchase.")
    fields = {"transaction_type": "expense", "occurred_on": draft.get("occurred_on"),
        "original_amount": draft.get("original_amount"), "original_currency": draft.get("original_currency"),
        "category_id": str(category["id"]), "account_id": str(account["id"]) if account else None,
        "merchant": draft.get("merchant"), "merchant_normalized": " ".join(draft["merchant"].casefold().split()) if draft.get("merchant") else None,
        "remarks": draft.get("remarks"), "payment_mode": draft.get("payment_mode")}
    # Only valid facts can participate in a duplicate lookup.
    try:
        money(fields["original_amount"], fields["original_currency"])
        business_date(fields["occurred_on"], household)
        candidates = repo.duplicate_candidates(conn, actor.household_id, fields)
    except HTTPException:
        candidates = []
    if request_row.get("image_sha256"):
        candidates += repo.rows(conn, "SELECT t.id FROM transactions t JOIN ingestion_requests r "
            "ON r.id=t.source_request_id AND r.household_id=t.household_id WHERE t.household_id=%s "
            "AND t.status='committed' AND r.image_sha256=%s", (actor.household_id, request_row["image_sha256"]))
    ids = sorted({str(c["id"]) for c in candidates})
    if ids:
        warning("POSSIBLE_DUPLICATE", "Review matching expenses. Confirming records a separate purchase.",
                not confirming or bool(set(ids) - set(draft.get("duplicate_ids", []))))
    draft["duplicate_ids"], draft["warnings"] = ids, warnings
    return draft, fields, blocked


def finalize(conn, actor, row, proposed, confirming=False):
    draft, fields, blocked = validate_draft(conn, actor, proposed, row, confirming=confirming)
    if blocked:
        return receipts.response(receipts.draft(conn, actor, row, draft))
    if draft.get("action") == "use_schedule_period":
        from app.services.spending_schedules import bind_capture_period
        transaction = bind_capture_period(conn, actor, row["id"], draft, fields)
    else:
        transaction = spending.create_record(conn, actor, row["id"], fields, source="shortcut",
            date_source=draft["date_source"], category_uncertain=draft["category_uncertain"])
    saved = spending.serialize(transaction)
    message = f"{saved['original_amount']} {saved['original_currency']} · {saved.get('merchant') or 'Expense'}\n{saved['occurred_on']}"
    result = {"status": "committed", "request_id": str(row["id"]), "transaction_id": saved["id"],
        "payment_mode": transaction["payment_mode"], "display_summary": message,
        "row_version": saved["row_version"], "original_amount": saved["original_amount"],
        "original_currency": saved["original_currency"], "warnings": draft["warnings"]}
    if saved["reporting_amount"] is None:
        result["warnings"].append({"code": "REPORTING_FX_UNAVAILABLE", "message": "Original amount saved; reporting conversion is pending."})
    return receipts.response(receipts.terminal(conn, actor, row, "committed", result))


def dependency_failure(factory, actor, identity):
    with receipts.session(factory) as conn:
        current = receipts.get(conn, actor, identity, lock=True)
        if current["status"] != "processing":
            return receipts.response(current)
        code = "CAPTURE_DEPENDENCY_UNAVAILABLE"
        result = {"error": {"code": code, "message": "Capture failed. Recover using this request key.",
            "retryable": True, "details": {"request_id": str(identity)}}}
        return receipts.response(receipts.terminal(conn, actor, current, "failed", result, 503, code))


def process(factory, actor, payload, model, fx_provider=None):
    started = time.monotonic()
    raw, mime = decode_image(payload["image"])
    captured_at = payload["captured_at"]
    if captured_at > datetime.now(timezone.utc):
        fail("INVALID_CAPTURE_DATE", "Capture timestamp cannot be in the future.")
    digest = hashlib.sha256(raw).hexdigest()
    request_hash = compute_command_hash("POST /api/v1/expenses", {"image_sha256": digest, "mime_type": mime,
        "captured_at": captured_at.astimezone(timezone.utc).isoformat(), "client_version": payload.get("client_version"), "note": payload.get("note")})
    with receipts.session(factory) as conn:
        row, inserted = receipts.reserve(conn, actor, payload["idempotency_key"], request_hash, digest, captured_at, payload.get("client_version"))
        if not inserted:
            return receipts.response(row)
        accounts, categories = references(conn, actor.household_id)
        household = schema.get_household(conn, actor.household_id)
    try:
        # No database connection is alive here. The SDK has a 40s timeout and no retry.
        result = model.extract_expense(raw, mime, payload.get("note"), accounts, categories, captured_at=captured_at)
        proposed = initial_draft(result, captured_at, household, accounts, categories)
        if time.monotonic() - started >= 45:
            raise TimeoutError()
    except Exception:
        return dependency_failure(factory, actor, row["id"])
    finally:
        raw = None
    if fx_provider and proposed.get("original_currency") and proposed.get("original_amount"):
        with receipts.session(factory) as conn:
            prime_quote(conn, actor.household_id, proposed, fx_provider)
    if time.monotonic() - started >= 45:
        return dependency_failure(factory, actor, row["id"])
    with receipts.session(factory) as conn:
        current = receipts.get(conn, actor, row["id"], lock=True)
        if current["status"] != "processing":
            return receipts.response(current)
        schema.acquire_household_finance_lock(conn, actor.household_id)
        receipts.authorize(conn, actor)
        if proposed["intent"] in ("transfer", "repayment", "failed", "refund") and confidence(proposed, "intent"):
            return receipts.response(receipts.rejected(conn, actor, current, "This is not a spending purchase. Use Dashboard for refunds."))
        return finalize(conn, actor, current, proposed)


def confirm(factory, actor, identity, expected_version=None, fx_provider=None):
    with receipts.session(factory) as conn:
        row = receipts.get(conn, actor, identity)
        if row["status"] in receipts.TERMINAL:
            return receipts.response(row)
        receipts.version(row, actor, expected_version)
        if row["status"] != "needs_confirmation" or row["request_kind"] != "expense":
            fail("INVALID_REQUEST_STATE", "An expense draft is required.", 409)
        observed_version = row["row_version"]
        if fx_provider:
            prime_quote(conn, actor.household_id, row["draft_payload"], fx_provider)
    with receipts.session(factory) as conn:
        row = receipts.get(conn, actor, identity, lock=True)
        if row["status"] in receipts.TERMINAL:
            return receipts.response(row)
        receipts.version(row, actor, observed_version)
        schema.acquire_household_finance_lock(conn, actor.household_id)
        receipts.authorize(conn, actor)
        return finalize(conn, actor, row, row["draft_payload"], confirming=True)


def revise(factory, actor, identity, changes, model=None):
    expected = changes.get("expected_version")
    note = changes.get("correction_note")
    with receipts.session(factory) as conn:
        row = receipts.get(conn, actor, identity)
        if row["status"] != "needs_confirmation" or row["request_kind"] != "expense":
            fail("INVALID_REQUEST_STATE", "An expense draft is required.", 409)
        if actor.is_browser or expected is not None:
            receipts.version(row, actor, expected)
        observed_version = row["row_version"]
        accounts, categories = references(conn, actor.household_id)
    patch = {k: v for k, v in changes.items() if k not in ("expected_version", "correction_note")}
    if note:
        if patch:
            fail("INVALID_REQUEST", "Use structured edits or a correction note, not both.")
        try:
            result = model.revise_expense_draft(row["draft_payload"], note, accounts, categories)
            patch = result.model_dump(mode="json", exclude_none=True, exclude={"raw_response"})
        except Exception:
            fail("CAPTURE_DEPENDENCY_UNAVAILABLE", "Draft revision unavailable; the saved draft is unchanged.", 503)
        for source, target, refs in (("from_account", "from_account_id", accounts), ("category", "category_id", categories)):
            if source in patch:
                selected = match(patch.pop(source), refs)
                patch[target] = str(selected["id"]) if selected else None
        try:
            # Model output crosses the same bounds as structured user edits.
            patch = ReviseRequestPayload.model_validate(patch).model_dump(mode="json", exclude_unset=True)
        except ValidationError:
            fail("CAPTURE_DEPENDENCY_UNAVAILABLE", "Draft revision was invalid; the saved draft is unchanged.", 503)
    with receipts.session(factory) as conn:
        current = receipts.get(conn, actor, identity, lock=True)
        receipts.version(current, actor, observed_version)
        if current["status"] != "needs_confirmation":
            fail("INVALID_REQUEST_STATE", "The draft is no longer pending.", 409)
        schema.acquire_household_finance_lock(conn, actor.household_id)
        receipts.authorize(conn, actor)
        draft = dict(current["draft_payload"])
        user_fields = set(draft.get("user_fields", []))
        for key, value in patch.items():
            if key in ("from_account_id", "category_id"):
                getter = schema.get_account if key == "from_account_id" else schema.get_category
                reference = getter(conn, actor.household_id, value) if value else None
                if value and (not reference or reference["status"] != "active"):
                    fail("RESOURCE_NOT_FOUND", "Active reference not found.", 404)
                if key == "category_id" and reference and reference["category_type"] != "expense":
                    fail("CATEGORY_MISMATCH", "Select an expense category.")
                draft["from_account" if key == "from_account_id" else "category"] = {"id": str(reference["id"]), "name": reference["name"]} if reference else None
                user_fields.add("account" if key == "from_account_id" else "category")
            else:
                draft[key] = value
                user_fields.add({"original_amount": "amount", "original_currency": "currency", "occurred_on": "date"}.get(key, key))
            if key == "occurred_on":
                draft["date_source"] = "manual"
        draft["user_fields"] = sorted(user_fields)
        draft, _, _ = validate_draft(conn, actor, draft, current)
        return receipts.response(receipts.draft(conn, actor, current, draft))


def cancel(factory, actor, *, key=None, identity=None, expected_version=None):
    with receipts.session(factory) as conn:
        if key:
            row, _ = receipts.reserve(conn, actor, key, cancel=True)
        else:
            row = receipts.get(conn, actor, identity, lock=True)
        if row["status"] in receipts.TERMINAL:
            return receipts.response(row)
        if not key:
            receipts.version(row, actor, expected_version)
        return receipts.response(receipts.rejected(conn, actor, row))
