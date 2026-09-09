"""Monthly spending instructions; no payment, debt or ledger projection."""
import json
from datetime import date
from uuid import uuid4
from datetime import timedelta
from app.auth.context import SystemCommandActor
from psycopg2 import sql
from app.domain.spending import fail, money, money_text, local_today
from app.domain.spending_schedules import due_date, due_periods
from app.repositories import spending as repo, simplified_schema as settings, audit
from app.services import spending_service as spending
from app.services.durable_commands import execute_durable_command, get_audit_actor_info


def output(row):
    if isinstance(row, list):
        return [output(item) for item in row]
    if isinstance(row, dict) and row.get("currency"):
        row = dict(row)
        for field in ("amount", "amount_per_period"):
            if field in row:
                row[field] = money_text(row[field], row["currency"])
    return json.loads(json.dumps(row, default=str))


def get(conn, household_id, schedule_id, expected_version=None):
    rows = repo.rows(conn, "SELECT * FROM spending_schedules WHERE household_id=%s AND id=%s",
                     (household_id, schedule_id))
    if not rows:
        fail("SCHEDULE_NOT_FOUND", "Schedule not found.", 404)
    row = rows[0]
    if expected_version is not None and row["row_version"] != expected_version:
        fail("ROW_VERSION_CONFLICT", "This schedule changed. Reload it.", 409)
    return row


def insert(conn, table, values):
    assert table in ("spending_schedules", "schedule_occurrences")
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, values)), sql.SQL(",").join(sql.Placeholder() for _ in values))
    return repo.rows(conn, query, tuple(values.values()))[0]


def update(conn, table, household_id, identity, values):
    assert table in ("spending_schedules", "schedule_occurrences")
    query = sql.SQL("UPDATE {} SET {},row_version=row_version+1,updated_at=now() WHERE household_id=%s AND id=%s RETURNING *").format(
        sql.Identifier(table), sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in values))
    return repo.rows(conn, query, (*values.values(), household_id, identity))[0]


def history(conn, actor, rid, row, action, before=None, reason=None, entity="spending_schedule"):
    kind, user, device = get_audit_actor_info(actor)
    audit.insert_audit_event(conn, household_id=actor.household_id, actor_type=kind,
        entity_type=entity, entity_id=row["id"], action=action, actor_user_id=user,
        actor_device_id=device, source_request_id=rid, before_data=output(before) if before else None,
        after_data=output(row), reason=reason)


def terms(conn, household_id, data, before=None):
    row = dict(data)
    row["amount_per_period"] = money(row["amount_per_period"], row["currency"])
    if isinstance(row["start_month"], str):
        row["start_month"] = date.fromisoformat(row["start_month"])
    if row["start_month"].day != 1 or not 1 <= row["day_of_month"] <= 31:
        fail("INVALID_SCHEDULE", "Use the first of the start month and a monthly day from 1 to 31.")
    count = row.get("period_count")
    if (row["kind"] == "installment" and count is None) or (count is not None and not 1 <= count <= 1200):
        fail("INVALID_SCHEDULE", "Installments require 1 to 1200 periods.")
    if not row["name"].strip() or not row["merchant"].strip():
        fail("INVALID_SCHEDULE", "A schedule name and merchant are required.")
    if count:
        due_date(row["start_month"], row["day_of_month"], count)
    for field, getter in (("category_id", settings.get_category), ("account_id", settings.get_account)):
        identity = row.get(field)
        if identity is None and field == "account_id":
            continue
        reference = getter(conn, household_id, identity) if identity else None
        if not reference:
            fail("RESOURCE_NOT_FOUND", "Reference not found.", 404)
        unchanged = before and str(before.get(field)) == str(identity)
        if (reference["status"] != "active" and not unchanged) or (field == "category_id" and reference["category_type"] != "expense"):
            fail("INVALID_SCHEDULE", "Select an active expense category and payment account.")
    return row


def expense_data(conn, schedule, occurrence):
    category = settings.get_category(conn, schedule["household_id"], occurrence["category_id"])
    uncertain = category["status"] != "active"
    if uncertain:
        category = settings.get_fallback_category(conn, schedule["household_id"], "expense")
    account = settings.get_account(conn, schedule["household_id"], occurrence["account_id"]) if occurrence.get("account_id") else None
    return {"transaction_type": "expense", "occurred_on": occurrence["due_on"],
        "original_amount": occurrence["amount"], "original_currency": occurrence["currency"],
        "category_id": category["id"], "account_id": account["id"] if account and account["status"] == "active" else None,
        "merchant": schedule["merchant"], "merchant_normalized": " ".join(schedule["merchant"].casefold().split()),
        "payment_mode": "installment" if schedule["kind"] == "installment" else "one_off"}, uncertain


def preview(conn, household_id, data):
    row = terms(conn, household_id, data)
    today = local_today(settings.get_household(conn, household_id))
    due = due_periods(row, today)
    periods = []
    for number, day in due:
        candidate = {"occurred_on": day, "original_amount": row["amount_per_period"],
            "original_currency": row["currency"], "account_id": row.get("account_id"),
            "merchant_normalized": " ".join(row["merchant"].casefold().split())}
        periods.append({"period_no": number, "due_on": str(day), "duplicate_ids":
                        [str(item["id"]) for item in repo.duplicate_candidates(conn, household_id, candidate)]})
    next_period = len(due) + 1
    return {"acknowledged_due_through": str(today), "due_periods": periods,
        "due_total": money_text(row["amount_per_period"] * len(due), row["currency"]), "currency": row["currency"],
        "upcoming_dates": [str(due_date(row["start_month"], row["day_of_month"], n))
                           for n in range(next_period, min(next_period+12, (row["period_count"] or next_period+11)+1))]}


def materialize_period(conn, actor, rid, schedule, number, day):
    existing = repo.rows(conn, "SELECT * FROM schedule_occurrences WHERE household_id=%s AND schedule_id=%s AND period_no=%s",
                        (actor.household_id, schedule["id"], number))
    if existing:
        return existing[0]
    occurrence = {"id": uuid4(), "household_id": actor.household_id, "schedule_id": schedule["id"],
        "period_no": number, "due_on": day, "amount": schedule["amount_per_period"],
        "currency": schedule["currency"], "category_id": schedule["category_id"], "account_id": schedule["account_id"],
        "status": "skipped" if schedule["status"] == "paused" else "needs_confirmation",
        "skip_reason": "Schedule paused" if schedule["status"] == "paused" else None}
    occurrence = insert(conn, "schedule_occurrences", occurrence)
    if schedule["status"] != "paused":
        data, uncertain = expense_data(conn, schedule, occurrence)
        if not repo.duplicate_candidates(conn, actor.household_id, data):
            spending.create_record(conn, actor, rid, data, source="scheduled", date_source="schedule",
                item_key=occurrence["id"], category_uncertain=uncertain, occurrence_id=occurrence["id"])
            occurrence = update(conn, "schedule_occurrences", actor.household_id, occurrence["id"], {"status": "recorded"})
    history(conn, actor, rid, occurrence, "skip_period" if occurrence["status"] == "skipped" else "create", entity="schedule_occurrence")
    return occurrence


def finish(conn, actor, rid, schedule):
    if schedule["period_count"]:
        count = repo.rows(conn, "SELECT count(*) AS n FROM schedule_occurrences WHERE household_id=%s AND schedule_id=%s "
                          "AND status IN ('recorded','skipped')", (actor.household_id, schedule["id"]))[0]["n"]
        if count == schedule["period_count"] and schedule["status"] not in ("completed", "cancelled"):
            changed = update(conn, "spending_schedules", actor.household_id, schedule["id"], {"status": "completed"})
            history(conn, actor, rid, changed, "update", schedule)


def catch_up(conn, actor, rid, schedule, today):
    if schedule["status"] in ("cancelled", "completed"):
        return
    for number, day in due_periods(schedule, today):
        materialize_period(conn, actor, rid, schedule, number, day)
    finish(conn, actor, rid, schedule)


def prepare_quotes(conn, actor, key, schedules, provider):
    """Bounded preparation before command/source/household locks; missing FX stays explicit."""
    if provider is None or settings.get_ingestion_request_by_key(conn, actor.household_id, actor.actor_scope, key):
        return
    from app.services.spending_reports import prime_quote
    today = local_today(settings.get_household(conn, actor.household_id))
    attempts = set()
    for schedule in schedules:
        if schedule.get("status", "active") != "active":
            continue
        if not schedule.get("start_month") or not schedule.get("day_of_month"):
            continue
        existing = {r["period_no"] for r in repo.rows(conn,
            "SELECT period_no FROM schedule_occurrences WHERE household_id=%s AND schedule_id=%s",
            (actor.household_id, schedule["id"]))} if schedule.get("id") else set()
        for number, day in due_periods(schedule, today):
            if number in existing:
                continue
            pair = (schedule["currency"], day)
            if pair in attempts:
                continue
            if len(attempts) == 5:
                return
            attempts.add(pair)
            prime_quote(conn, actor.household_id, {"original_amount": schedule["amount_per_period"], "original_currency": schedule["currency"], "occurred_on": day}, provider)


def create(conn, actor, key, data, provider=None):
    prepare_quotes(conn, actor, key, [data], provider)
    def mutate(c, rid):
        today = local_today(settings.get_household(c, actor.household_id))
        if data["acknowledged_due_through"] != str(today):
            fail("SCHEDULE_PREVIEW_STALE", "Refresh the dates and acknowledge due spending before Save.", 409)
        values = terms(c, actor.household_id, {k: v for k, v in data.items() if k not in
                       ("acknowledged_due_through", "replaces_transaction_id", "expected_transaction_version", "source_draft_request_id", "expected_draft_version")})
        source = None
        if data.get("source_draft_request_id"):
            source = settings.get_ingestion_request(c, actor.household_id, data["source_draft_request_id"])
            if source["row_version"] != data["expected_draft_version"]:
                fail("DRAFT_CHANGED", "The source draft changed. Reload it.", 409)
            if source["status"] != "needs_confirmation" or source["draft_payload"].get("payment_mode") != "installment":
                fail("INVALID_REQUEST_STATE", "An uncommitted installment draft is required.", 409)
        replaced = data.get("replaces_transaction_id")
        if replaced:
            prior = spending.require_record(c, actor.household_id, replaced, data["expected_transaction_version"])
            if values["kind"] != "installment" or prior["transaction_type"] != "expense" or prior["schedule_occurrence_id"] or prior["original_currency"] != values["currency"] or prior["original_amount"] != values["amount_per_period"] * values["period_count"]:
                fail("INVALID_SCHEDULE", "The upfront expense must match the full installment total and currency.")
            spending.void_record(c, actor, rid, replaced, {"expected_version": data["expected_transaction_version"], "delete_reason": "Converted to monthly spending schedule"})
        values.update(id=uuid4(), household_id=actor.household_id, created_by_user_id=actor.user_id,
                      created_by_device_id=actor.device_id, source_request_id=rid)
        schedule = insert(c, "spending_schedules", values)
        history(c, actor, rid, schedule, "create")
        catch_up(c, actor, rid, schedule, today)
        if source:
            settings.finalize_ingestion_request(c, actor.household_id, source["id"], source["row_version"],
                "rejected", {"status": "rejected", "request_id": str(source["id"]), "schedule_id": str(schedule["id"]),
                             "display_summary": "Recorded as a monthly spending schedule."}, 200)
        return output(get(c, actor.household_id, schedule["id"])), 201
    sources = [data["source_draft_request_id"]] if data.get("source_draft_request_id") else []
    return execute_durable_command(conn, actor, key, "POST /api/v1/spending-schedules", data, mutate,
                                   source_expense_request_ids=sources)


def freshness(conn, household_id):
    today = local_today(settings.get_household(conn, household_id))
    through = today
    schedules = repo.rows(conn, "SELECT * FROM spending_schedules WHERE household_id=%s AND status IN ('active','paused')", (household_id,))
    for schedule in schedules:
        materialized = {r["period_no"] for r in repo.rows(conn, "SELECT period_no FROM schedule_occurrences WHERE household_id=%s AND schedule_id=%s", (household_id, schedule["id"]))}
        for number, day in due_periods(schedule, today):
            if number not in materialized:
                through = min(through, day - timedelta(days=1))
                break
    return str(through)


def run_due(factory, provider=None):
    """Trusted internal daily entry point. HTTP/OIDC wiring is a separate S5 concern."""
    from app.services.capture_receipts import session
    from app.services.spending_reports import prime_quote
    class PeriodNoLongerDue(Exception):
        pass
    with session(factory) as conn:
        households = repo.rows(conn, "SELECT id FROM households WHERE status='active' ORDER BY id")
    processed = 0
    for household in households:
        hh = household["id"]
        with session(factory) as conn:
            today = local_today(settings.get_household(conn, hh))
            schedules = repo.rows(conn, "SELECT * FROM spending_schedules WHERE household_id=%s AND status IN ('active','paused') ORDER BY id", (hh,))
        for schedule in schedules:
            for number, day in due_periods(schedule, today):
                actor = SystemCommandActor(hh, schedule["created_by_user_id"])
                key = f"schedule:{schedule['id']}:{number}"
                with session(factory) as conn:
                    # Check existing identity before any optional network work.
                    prior = repo.rows(conn, "SELECT id FROM schedule_occurrences WHERE household_id=%s AND schedule_id=%s AND period_no=%s", (hh, schedule["id"], number))
                    if prior:
                        continue
                    if provider and schedule["status"] == "active":
                        prime_quote(conn, hh, {"original_amount": schedule["amount_per_period"], "original_currency": schedule["currency"], "occurred_on": day}, provider)
                    def mutate(c, rid):
                        current = get(c, hh, schedule["id"])
                        current_household = settings.get_household(c, hh)
                        if current_household["status"] != "active":
                            raise PeriodNoLongerDue()
                        actual_today = local_today(current_household)
                        eligible = dict(due_periods(current, actual_today))
                        if current["status"] not in ("active", "paused") or number not in eligible:
                            raise PeriodNoLongerDue()
                        occurrence = materialize_period(c, actor, rid, current, number, eligible[number])
                        finish(c, actor, rid, current)
                        return output(occurrence), 200
                    try:
                        execute_durable_command(conn, actor, key, "POST /internal/spending-schedules/period",
                            {"schedule_id": str(schedule["id"]), "period_no": number}, mutate)
                    except PeriodNoLongerDue:
                        continue  # No terminal receipt may block this period if it becomes due later.
                    processed += 1
    return {"processed": processed}


def change(conn, actor, key, identity, action, data, provider=None):
    if provider:
        candidates = repo.rows(conn, "SELECT * FROM spending_schedules WHERE household_id=%s AND id=%s", (actor.household_id, identity))
        prepare_quotes(conn, actor, key, candidates, provider)
    def mutate(c, rid):
        schedule = get(c, actor.household_id, identity, data["expected_version"])
        if schedule["status"] in ("cancelled", "completed"):
            fail("INVALID_REQUEST_STATE", "This schedule has ended.", 409)
        today = local_today(settings.get_household(c, actor.household_id))
        catch_up(c, actor, rid, schedule, today)
        before = get(c, actor.household_id, identity)
        if before["status"] == "completed":
            return output(before), 200
        if action == "patch":
            changes = {k: v for k, v in data.items() if k not in ("expected_version", "reason")}
            combined = terms(c, actor.household_id, {**before, **changes}, before)
            occurrences = repo.rows(c, "SELECT COALESCE(max(period_no),0) AS n FROM schedule_occurrences WHERE household_id=%s AND schedule_id=%s", (actor.household_id, identity))[0]["n"]
            if occurrences:
                for field in ("start_month", "day_of_month", "currency", "kind", "merchant"):
                    if combined[field] != before[field]:
                        fail("INVALID_SCHEDULE", "Calendar, currency, kind and merchant are fixed after a period materializes.")
            if combined["period_count"] is not None and combined["period_count"] < occurrences:
                fail("INVALID_SCHEDULE", "Period count cannot remove an existing period.")
            changes = {k: combined[k] for k in changes}
            audit_action = "update"
        else:
            required = "paused" if action == "resume" else "active" if action == "pause" else before["status"]
            if before["status"] != required:
                fail("INVALID_REQUEST_STATE", "The schedule is not in the required state.", 409)
            changes = {"status": {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]}
            audit_action = action + "_schedule"
            if action == "cancel":
                pending = repo.rows(c, "SELECT * FROM schedule_occurrences WHERE household_id=%s AND schedule_id=%s AND status='needs_confirmation'", (actor.household_id, identity))
                for occurrence in pending:
                    skipped = update(c, "schedule_occurrences", actor.household_id, occurrence["id"], {"status": "skipped", "skip_reason": data.get("reason") or "Schedule cancelled"})
                    history(c, actor, rid, skipped, "skip_period", occurrence, entity="schedule_occurrence")
        changed = update(c, "spending_schedules", actor.household_id, identity, changes)
        history(c, actor, rid, changed, audit_action, before, data.get("reason"))
        finish(c, actor, rid, changed)
        return output(get(c, actor.household_id, identity)), 200
    operation = f"PATCH /api/v1/spending-schedules/{identity}" if action == "patch" else f"POST /api/v1/spending-schedules/{identity}/{action}"
    return execute_durable_command(conn, actor, key, operation, data, mutate)


def materialize(conn, actor, key, provider=None):
    if provider:
        candidates = repo.rows(conn, "SELECT * FROM spending_schedules WHERE household_id=%s AND status='active' ORDER BY id", (actor.household_id,))
        prepare_quotes(conn, actor, key, candidates, provider)
    def mutate(c, rid):
        today = local_today(settings.get_household(c, actor.household_id))
        schedules = repo.rows(c, "SELECT * FROM spending_schedules WHERE household_id=%s AND status IN ('active','paused') ORDER BY id", (actor.household_id,))
        for schedule in schedules:
            catch_up(c, actor, rid, schedule, today)
        counts = repo.rows(c, "SELECT status,count(*) AS count FROM schedule_occurrences WHERE household_id=%s GROUP BY status", (actor.household_id,))
        return {"schedules_current_through": str(today), "occurrence_counts": counts}, 200
    return execute_durable_command(conn, actor, key, "POST /api/v1/spending-schedules/materialize", {}, mutate)


def resolve(conn, actor, key, identity, data, provider=None):
    if provider and data["action"] == "record_separately" and not settings.get_ingestion_request_by_key(conn, actor.household_id, actor.actor_scope, key):
        from app.services.spending_reports import prime_quote
        rows = repo.rows(conn, "SELECT * FROM schedule_occurrences WHERE household_id=%s AND id=%s AND status='needs_confirmation'",
                         (actor.household_id, identity))
        if rows:
            prime_quote(conn, actor.household_id, {"original_amount": rows[0]["amount"],
                "original_currency": rows[0]["currency"], "occurred_on": rows[0]["due_on"]}, provider)
    def mutate(c, rid):
        matches = repo.rows(c, "SELECT * FROM schedule_occurrences WHERE household_id=%s AND id=%s", (actor.household_id, identity))
        if not matches:
            fail("OCCURRENCE_NOT_FOUND", "Period not found.", 404)
        before = matches[0]
        if before["row_version"] != data["expected_version"]:
            fail("ROW_VERSION_CONFLICT", "This period changed. Reload it.", 409)
        if before["status"] != "needs_confirmation":
            fail("INVALID_REQUEST_STATE", "This period is already resolved.", 409)
        schedule = get(c, actor.household_id, before["schedule_id"])
        if data["action"] == "skip":
            changes = {"status": "skipped", "skip_reason": data.get("reason") or "User skipped"}
        else:
            if data["action"] == "link_existing":
                row = spending.require_record(c, actor.household_id, data["transaction_id"], data["expected_transaction_version"])
                if row["status"] != "committed" or row["transaction_type"] != "expense" or row["schedule_occurrence_id"] or row["original_amount"] != before["amount"] or row["original_currency"] != before["currency"] or row["occurred_on"] != before["due_on"]:
                    fail("SCHEDULE_OCCURRENCE_CONFLICT", "Choose an unbound expense matching this period's date, amount and currency.", 409)
                linked = repo.update(c, actor.household_id, row["id"], {"schedule_occurrence_id": identity})
                spending.record_audit(c, actor, rid, linked, "update", row, "Linked to schedule period")
            else:
                fields, uncertain = expense_data(c, schedule, before)
                spending.create_record(c, actor, rid, fields, source="scheduled", date_source="schedule",
                    item_key=identity, category_uncertain=uncertain, occurrence_id=identity)
            changes = {"status": "recorded"}
        row = update(c, "schedule_occurrences", actor.household_id, identity, changes)
        history(c, actor, rid, row, "skip_period" if row["status"] == "skipped" else "update", before, entity="schedule_occurrence")
        finish(c, actor, rid, schedule)
        return output(row), 200
    return execute_durable_command(conn, actor, key, f"POST /api/v1/schedule-occurrences/{identity}/resolve", data, mutate)


def bind_capture_period(conn, actor, receipt_id, draft, fields):
    """Bind explicit capture/import intent under its outer receipt and household lock."""
    if any(draft.get(key) is None for key in ("schedule_id", "period_no", "expected_schedule_version")):
        fail("INVALID_SCHEDULE", "Select a schedule, period and current version.")
    schedule = get(conn, actor.household_id, draft["schedule_id"], draft["expected_schedule_version"])
    number = draft["period_no"]
    if schedule["period_count"] is not None and number > schedule["period_count"]:
        fail("INVALID_SCHEDULE", "The period is outside this schedule.")
    day = due_date(schedule["start_month"], schedule["day_of_month"], number)
    if day > local_today(settings.get_household(conn, actor.household_id)):
        fail("INVALID_SCHEDULE", "Future periods cannot be recorded early.")
    matches = repo.rows(conn, "SELECT * FROM schedule_occurrences WHERE household_id=%s AND schedule_id=%s AND period_no=%s",
                        (actor.household_id, schedule["id"], number))
    occurrence = matches[0] if matches else None
    expected_amount = occurrence["amount"] if occurrence else schedule["amount_per_period"]
    if money(fields["original_amount"], fields["original_currency"]) != expected_amount or fields["original_currency"] != schedule["currency"] or str(fields["occurred_on"]) != str(day):
        fail("SCHEDULE_OCCURRENCE_CONFLICT", "The captured amount, currency and date must match the selected period.", 409)
    if occurrence and occurrence["status"] == "recorded":
        transaction = repo.rows(conn, "SELECT * FROM transactions WHERE household_id=%s AND schedule_occurrence_id=%s",
                               (actor.household_id, occurrence["id"]))[0]
        if transaction["status"] != "committed":
            fail("SCHEDULE_OCCURRENCE_CONFLICT", "This period was voided and cannot be regenerated.", 409)
        return transaction
    if schedule["status"] != "active" or (occurrence and occurrence["status"] == "skipped"):
        fail("INVALID_REQUEST_STATE", "This schedule period is not available.", 409)
    if not occurrence:
        occurrence = insert(conn, "schedule_occurrences", {"id": uuid4(), "household_id": actor.household_id,
            "schedule_id": schedule["id"], "period_no": number, "due_on": day, "amount": expected_amount,
            "currency": schedule["currency"], "category_id": schedule["category_id"], "account_id": schedule["account_id"],
            "status": "needs_confirmation"})
    transaction = spending.create_record(conn, actor, receipt_id, fields, source="shortcut",
        date_source=draft["date_source"], category_uncertain=draft["category_uncertain"], occurrence_id=occurrence["id"])
    changed = update(conn, "schedule_occurrences", actor.household_id, occurrence["id"], {"status": "recorded"})
    history(conn, actor, receipt_id, changed, "update", occurrence, entity="schedule_occurrence")
    finish(conn, actor, receipt_id, schedule)
    return transaction
