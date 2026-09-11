"""One observation write path for manual, screenshot and statement balances."""
import json
from datetime import datetime, timezone, time
from uuid import uuid4
from zoneinfo import ZoneInfo
from psycopg2 import sql
from fastapi import HTTPException
from app.domain.balances import signed_money, instant
from app.domain.spending import fail, money_text
from app.repositories import spending as repo, simplified_schema as schema, audit
from app.services.durable_commands import execute_durable_command, get_audit_actor_info


def output(row):
    result = json.loads(json.dumps(row, default=str))
    if row and isinstance(row, dict) and "balance" in row:
        result["balance"] = money_text(row["balance"], row["currency"])
    return result


def latest(conn, household_id, account_id):
    rows = repo.rows(conn, "SELECT * FROM account_snapshots WHERE household_id=%s AND account_id=%s AND status='active' ORDER BY as_of DESC LIMIT 1", (household_id, account_id))
    return rows[0] if rows else None


def head_fields(conn, household_id, account_id):
    account = schema.get_account(conn, household_id, account_id)
    if not account:
        fail("ACCOUNT_NOT_FOUND", "Account not found.", 404)
    head = latest(conn, household_id, account_id)
    return {"expected_account_version": account["row_version"],
            "expected_latest_snapshot_id": str(head["id"]) if head else None}


def validate(conn, household_id, data, replacing=None):
    account = schema.get_account(conn, household_id, data["account_id"])
    if not account:
        fail("ACCOUNT_NOT_FOUND", "Account not found.", 404)
    head = latest(conn, household_id, account["id"])
    if str(data["expected_latest_snapshot_id"]) != str(head["id"] if head else None) or data["expected_account_version"] != account["row_version"]:
        raise HTTPException(409, {"error": {"code": "BALANCE_CHANGED", "message": "Account or latest observation changed. Reload before saving.",
            "retryable": False, "details": {"account_id": str(account["id"]), "account_version": account["row_version"], "latest_snapshot": output(head)}}})
    value, timestamp = signed_money(data["balance"], data["currency"]), instant(data["as_of"])
    household = schema.get_household(conn, household_id)
    day = timestamp.astimezone(ZoneInfo(household["timezone"])).date()
    if account["status"] == "cancelled" or day < account["opened_on"] or (account["closed_on"] and day > account["closed_on"]):
        fail("ACCOUNT_LIFETIME", "Observation must lie within the account lifetime.")
    if timestamp > datetime.now(timezone.utc):
        fail("INVALID_DATE", "Future balances cannot be recorded.")
    if account["currency"] != data["currency"]:
        fail("CURRENCY_MISMATCH", "Balance currency must match the account.")
    if data["time_basis"] == "date_only" and timestamp != datetime.combine(day, time.max, ZoneInfo(household["timezone"])).astimezone(timezone.utc):
        fail("INVALID_DATE", "Historical date-only observations use the household date's end, not invented intraday ordering.")
    if account["status"] == "closed" and head and timestamp > head["as_of"] and value != 0:
        fail("ACCOUNT_CLOSURE_CONFLICT", "A new observation cannot replace the closing zero with a nonzero balance. Reopen first.", 409)
    conflicts = repo.rows(conn, "SELECT * FROM account_snapshots WHERE household_id=%s AND account_id=%s AND status='active' AND (%s::uuid IS NULL OR id<>%s)", (household_id, account["id"], replacing, replacing))
    for row in conflicts:
        if row["as_of"] == timestamp:
            fail("SNAPSHOT_TIME_CONFLICT", "An active observation exists at this time. Correct it explicitly.", 409)
        same_day = row["as_of"].astimezone(ZoneInfo(household["timezone"])).date() == day
        if same_day and (row["time_basis"] == "date_only" or data["time_basis"] == "date_only"):
            fail("SNAPSHOT_TIME_CONFLICT", "Date-only evidence cannot establish ordering within this day. Correct or exclude the conflicting observation.", 409)
    return account, value, timestamp


def history(conn, actor, rid, row, action, before=None, reason=None, entity="account_snapshot"):
    kind, user, device = get_audit_actor_info(actor)
    audit.insert_audit_event(conn, household_id=actor.household_id, actor_type=kind, actor_user_id=user,
        actor_device_id=device, source_request_id=rid, entity_type=entity, entity_id=row["id"],
        action=action, before_data=output(before) if before else None, after_data=output(row), reason=reason)


def create_record(conn, actor, rid, data, source="manual", replacing=None):
    account, value, timestamp = validate(conn, actor.household_id, data, replacing)
    fields = {"id": uuid4(), "household_id": actor.household_id, "account_id": account["id"],
        "balance": value, "currency": data["currency"], "as_of": timestamp, "time_basis": data["time_basis"],
        "source": source, "source_request_id": rid, "created_by_user_id": actor.user_id,
        "created_by_device_id": actor.device_id, "notes": data.get("notes"), "replaces_snapshot_id": replacing}
    query = sql.SQL("INSERT INTO account_snapshots ({}) VALUES ({}) RETURNING *").format(
        sql.SQL(",").join(map(sql.Identifier, fields)), sql.SQL(",").join(sql.Placeholder() for _ in fields))
    row = repo.rows(conn, query, tuple(fields.values()))[0]
    history(conn, actor, rid, row, "create" if not replacing else "replace")
    return row


def save_rows(conn, actor, rid, observations, source="manual"):
    ids = [str(r["account_id"]) for r in observations]
    if not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids):
        fail("INVALID_BALANCE_ROWS", "Select between 1 and 50 distinct accounts.")
    # Validate the complete set before the first mutation; caller also owns the transaction.
    for data in observations:
        validate(conn, actor.household_id, data)
    return [output(create_record(conn, actor, rid, data, source)) for data in observations]


def save(conn, actor, key, data):
    def mutate(c, rid):
        rows = save_rows(c, actor, rid, data["observations"])
        return {"status": "committed", "request_id": str(rid), "snapshots": rows,
                "display_summary": f"Saved {len(rows)} balance observations."}, 201
    return execute_durable_command(conn, actor, key, "POST /api/v1/balance-updates", data, mutate)


def change(conn, actor, key, identity, data, correcting=False):
    def mutate(c, rid):
        rows = repo.rows(c, "SELECT * FROM account_snapshots WHERE household_id=%s AND id=%s", (actor.household_id, identity))
        if not rows:
            fail("SNAPSHOT_NOT_FOUND", "Observation not found.", 404)
        before = rows[0]
        if before["row_version"] != data["expected_version"] or before["status"] != "active":
            fail("ROW_VERSION_CONFLICT", "Observation changed. Reload it.", 409)
        if not data["reason"].strip():
            fail("INVALID_REASON", "Explain the correction or void.")
        account = schema.get_account(c, actor.household_id, before["account_id"])
        fields = {**before, **data, "account_id": before["account_id"]}
        # Check head/configuration before optional reopening. It is part of the same Save.
        guard = {**before, **{k: data[k] for k in ("expected_latest_snapshot_id", "expected_account_version")}}
        validate(c, actor.household_id, guard, identity)
        if data.get("reopen_account"):
            if account["status"] != "closed":
                fail("INVALID_REQUEST_STATE", "Only closed accounts can be reopened.", 409)
            changed = repo.rows(c, "UPDATE accounts SET status='active',closed_on=NULL,row_version=row_version+1,updated_at=now() WHERE household_id=%s AND id=%s RETURNING *", (actor.household_id, account["id"]))[0]
            history(c, actor, rid, changed, "reopen", account, data["reason"], "account")
            fields["expected_account_version"] = changed["row_version"]
        elif account["status"] == "closed":
            remaining = repo.rows(c, "SELECT * FROM account_snapshots WHERE household_id=%s AND account_id=%s AND status='active' AND id<>%s ORDER BY as_of DESC", (actor.household_id, account["id"], identity))
            if correcting:
                remaining.append({"as_of": instant(data["as_of"]), "balance": signed_money(data["balance"], data["currency"])})
            closing = max(remaining, key=lambda r: r["as_of"]) if remaining else None
            tz = ZoneInfo(schema.get_household(c, actor.household_id)["timezone"])
            if not closing or closing["balance"] != 0 or closing["as_of"].astimezone(tz).date() != account["closed_on"]:
                fail("ACCOUNT_CLOSURE_CONFLICT", "Preserve a zero closing observation or explicitly reopen the account.", 409)
        if correcting:
            validate(c, actor.household_id, fields, identity)
        voided = repo.rows(c, "UPDATE account_snapshots SET status='voided',voided_at=now(),voided_by_user_id=%s,void_reason=%s,row_version=row_version+1,updated_at=now() WHERE household_id=%s AND id=%s RETURNING *", (actor.user_id, data["reason"], actor.household_id, identity))[0]
        history(c, actor, rid, voided, "void", before, data["reason"])
        if correcting:
            # Voiding intentionally changes the head; guards above checked the reviewed state.
            fields.update(head_fields(c, actor.household_id, before["account_id"]))
            row = create_record(c, actor, rid, fields, "manual", identity)
            return output(row), 200
        return output(voided), 200
    operation = f"POST /api/v1/snapshots/{identity}/" + ("correct" if correcting else "void")
    return execute_durable_command(conn, actor, key, operation, data, mutate)
