"""Financial mutations called only while the durable receipt and household locks are held."""
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from app.domain.spending import fail, money, money_text, business_date, metadata_reasons
from app.repositories import spending as repo, simplified_schema as settings
from app.repositories import audit
from app.services.durable_commands import execute_durable_command, get_audit_actor_info

FX_FIELDS = ("reporting_amount", "reporting_currency", "reporting_fx_rate",
             "reporting_fx_as_of", "reporting_fx_source", "reporting_fx_locked_at")


def serialize(row):
    result = json.loads(json.dumps(row, default=str))
    result["original_amount"] = money_text(row["original_amount"], row["original_currency"])
    if row.get("reporting_amount") is not None:
        result["reporting_amount"] = money_text(row["reporting_amount"], row["reporting_currency"])
    result["review_reasons"] = metadata_reasons(row)
    return result


def record_audit(conn, actor, receipt_id, row, action, before=None, reason=None):
    kind, user, device = get_audit_actor_info(actor)
    audit.insert_audit_event(conn, household_id=actor.household_id, actor_type=kind,
        entity_type="transaction", entity_id=row["id"], action=action,
        actor_user_id=user, actor_device_id=device, source_request_id=receipt_id,
        before_data=serialize(before) if before else None, after_data=serialize(row), reason=reason)


def require_record(conn, household_id, transaction_id, expected_version=None):
    row = repo.get(conn, household_id, transaction_id)
    if not row:
        fail("TRANSACTION_NOT_FOUND", "Transaction not found.", 404)
    if expected_version is not None and row["row_version"] != expected_version:
        fail("ROW_VERSION_CONFLICT", "This transaction changed. Reload it.", 409)
    return row


def validate_record(conn, household_id, values, before=None):
    household = settings.get_household(conn, household_id)
    values["original_amount"] = money(values["original_amount"], values["original_currency"])
    values["occurred_on"] = business_date(values["occurred_on"], household)
    if values["transaction_type"] not in ("expense", "refund", "cash_income"):
        fail("INVALID_TRANSACTION", "Only expense, refund and cash income records are supported.")
    for key, getter in (("category_id", settings.get_category), ("account_id", settings.get_account)):
        ref_id = values.get(key)
        if ref_id is None:
            if key == "category_id":
                fail("INVALID_CATEGORY", "A category is required.")
            continue
        ref = getter(conn, household_id, ref_id)
        if not ref:
            fail("RESOURCE_NOT_FOUND", "Reference not found.", 404)
        unchanged = before and str(before.get(key)) == str(ref_id)
        if key == "category_id" and values.get("refund_of_transaction_id"):
            parent = require_record(conn, household_id, values["refund_of_transaction_id"])
            unchanged = unchanged or str(parent["category_id"]) == str(ref_id)
        if not unchanged and ref["status"] != "active":
            fail("INACTIVE_REFERENCE", "Select an active account or category.")
        if key == "category_id":
            required_type = "income" if values["transaction_type"] == "cash_income" else "expense"
            if ref["category_type"] != required_type:
                fail("CATEGORY_MISMATCH", "The category does not match the transaction type.")
    parent_id = values.get("refund_of_transaction_id")
    if parent_id:
        parent = require_record(conn, household_id, parent_id)
        if values["transaction_type"] != "refund" or parent["transaction_type"] != "expense" or parent["status"] != "committed" or str(values.get("id")) == str(parent_id):
            fail("INVALID_REFUND", "A refund must reference an active expense.")
        if values["original_currency"] != parent["original_currency"]:
            fail("INVALID_REFUND", "A linked refund must use the purchase currency.")
        total = repo.refund_total(conn, household_id, parent_id, values.get("id"))
        if total + values["original_amount"] > parent["original_amount"]:
            fail("REFUND_EXCEEDS_ORIGINAL", "Active refunds exceed the purchase amount.", 409)
    elif values["transaction_type"] == "refund" and not (values.get("remarks") or "").strip():
        fail("INVALID_REFUND", "An unlinked refund requires an explicit category and note.")
    if before and before["transaction_type"] == "expense":
        total = repo.refund_total(conn, household_id, before["id"])
        if total and (values["original_amount"] < total or values["original_currency"] != before["original_currency"]):
            fail("REFUND_EXCEEDS_ORIGINAL", "Correct linked refunds before changing this purchase.", 409)
    values["merchant_normalized"] = " ".join(values["merchant"].casefold().split()) if values.get("merchant") else None
    return household


def conversion(conn, household, values):
    """Only accepted dated cache reads; never perform network I/O under the write lock."""
    source, target = values["original_currency"], household["reporting_currency"]
    result = dict.fromkeys(FX_FIELDS)
    if source == target:
        quote = {"rate": Decimal(1), "rate_as_of": values["occurred_on"], "source": "identity"}
    else:
        matches = repo.rows(conn, "SELECT * FROM fx_quotes WHERE from_currency=%s AND to_currency=%s "
            "AND rate_as_of<=%s AND rate_as_of>=%s::date-7 ORDER BY rate_as_of DESC LIMIT 1",
            (source, target, values["occurred_on"], values["occurred_on"]))
        if not matches:
            return result
        quote = matches[0]
    rate = quote["rate"]
    if not rate.is_finite() or rate <= 0 or rate >= Decimal("1e12"):
        return result
    amount = (values["original_amount"] * rate).quantize(Decimal("1") if target == "JPY" else Decimal(".01"), rounding=ROUND_HALF_UP)
    if abs(amount) >= Decimal("1e14"):
        return result
    return dict(zip(FX_FIELDS, (amount, target, rate, quote["rate_as_of"], quote["source"], datetime.now(timezone.utc))))


def create_record(conn, actor, receipt_id, data, *, source="dashboard_manual", date_source="manual", item_key="single", category_uncertain=False, occurrence_id=None):
    values = {key: data.get(key) for key in ("transaction_type", "occurred_on", "original_amount",
        "original_currency", "category_id", "account_id", "merchant", "remarks", "refund_of_transaction_id")}
    if values.get("refund_of_transaction_id"):
        parent = require_record(conn, actor.household_id, values["refund_of_transaction_id"])
        values["category_id"] = parent["category_id"]
    values.update(id=uuid4(), household_id=actor.household_id, source=source, date_source=date_source,
        created_by_user_id=actor.user_id, created_by_device_id=actor.device_id,
        source_request_id=receipt_id, source_item_key=str(item_key), category_uncertain=category_uncertain,
        account_review_acknowledged=data.get("account_review_acknowledged", False),
        schedule_occurrence_id=occurrence_id,
        payment_mode=data.get("payment_mode", "one_off") if values["transaction_type"] == "expense" else None)
    household = validate_record(conn, actor.household_id, values)
    values.update(conversion(conn, household, values))
    row = repo.insert(conn, values)
    record_audit(conn, actor, receipt_id, row, "create")
    return row


def patch_record(conn, actor, receipt_id, transaction_id, data):
    before = require_record(conn, actor.household_id, transaction_id, data["expected_version"])
    if before["status"] != "committed":
        fail("INVALID_REQUEST_STATE", "A voided record cannot be edited.", 409)
    changes = {key: val for key, val in data.items() if key not in ("expected_version", "reason")}
    if any(changes.get(key, True) is None for key in ("occurred_on", "original_amount", "original_currency", "category_id", "account_review_acknowledged")):
        fail("INVALID_REQUEST", "Required fields cannot be cleared.")
    if "category_id" in changes:
        changes["category_uncertain"] = False
    if "account_id" in changes:
        changes["account_review_acknowledged"] = changes.get("account_review_acknowledged", False)
    values = {**before, **changes}
    household = validate_record(conn, actor.household_id, values, before)
    changes["merchant_normalized"] = values["merchant_normalized"]
    if "original_amount" in changes:
        changes["original_amount"] = values["original_amount"]
    if "occurred_on" in changes:
        changes.update(occurred_on=values["occurred_on"], date_source="manual")
    if any(key in changes and values[key] != before[key] for key in ("occurred_on", "original_amount", "original_currency")):
        changes.update(conversion(conn, household, values))
    row = repo.update(conn, actor.household_id, transaction_id, changes)
    record_audit(conn, actor, receipt_id, row, "update", before, data.get("reason"))
    return row


def void_record(conn, actor, receipt_id, transaction_id, data):
    before = require_record(conn, actor.household_id, transaction_id, data["expected_version"])
    if before["status"] != "committed":
        fail("INVALID_REQUEST_STATE", "The transaction is already voided.", 409)
    if repo.refund_total(conn, actor.household_id, transaction_id):
        fail("REFUND_EXCEEDS_ORIGINAL", "Resolve linked refunds before voiding the purchase.", 409)
    if not data["delete_reason"].strip():
        fail("INVALID_REQUEST", "A void reason is required.")
    row = repo.update(conn, actor.household_id, transaction_id,
        {"status": "voided", "deleted_at": datetime.now(timezone.utc),
         "deleted_by_user_id": actor.user_id, "delete_reason": data["delete_reason"].strip()})
    record_audit(conn, actor, receipt_id, row, "void", before, data["delete_reason"])
    return row


def command(conn, actor, key, method, data, transaction_id=None, provider=None):
    from app.services.spending_reports import prime_quote
    existing = settings.get_ingestion_request_by_key(conn, actor.household_id, actor.actor_scope, key)
    if provider and not existing:
        if method == "create":
            prime_quote(conn, actor.household_id, data, provider)
        elif method == "patch" and any(k in data for k in ("original_amount", "original_currency", "occurred_on")):
            before = repo.get(conn, actor.household_id, transaction_id)
            if before and before["row_version"] == data["expected_version"]:
                prime_quote(conn, actor.household_id, {**before, **data}, provider)
    path = "/api/v1/transactions" + (f"/{transaction_id}" if transaction_id else "")
    if method == "void":
        path += "/void"
    operation = f"{'POST' if method in ('create', 'void') else 'PATCH'} {path}"
    def mutate(c, rid):
        if method == "create":
            row = create_record(c, actor, rid, data)
        elif method == "patch":
            row = patch_record(c, actor, rid, transaction_id, data)
        else:
            row = void_record(c, actor, rid, transaction_id, data)
        return serialize(row), 201 if method == "create" else 200
    return execute_durable_command(conn, actor, key, operation, data, mutate)
