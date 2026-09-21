"""Native interval reports and explicit flow assertions on the simplified schema."""
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from app.domain.investment_gains import interval, flow_amount
from app.domain.spending import fail, money_text
from app.repositories import spending as repo, simplified_schema as schema
from app.services.balance_service import history, output
from app.services.durable_commands import execute_durable_command


def report(conn, household_id, account_id=None, start=None, end=None):
    if start and end and start > end:
        fail("INVALID_DATE_RANGE", "Use an ordered date range.")
    household = schema.get_household(conn, household_id)
    tz = ZoneInfo(household["timezone"])
    if end and end > datetime.now(tz).date():
        fail("INVALID_DATE_RANGE", "Future investment gains are unavailable.")
    accounts = repo.rows(conn, "SELECT * FROM accounts WHERE household_id=%s AND account_type='investment' "
        "AND status<>'cancelled' AND (%s::uuid IS NULL OR id=%s) ORDER BY id", (household_id, account_id, account_id))
    if account_id and not accounts:
        fail("ACCOUNT_NOT_FOUND", "Investment account not found.", 404)
    snapshots = repo.rows(conn, "SELECT s.*,COALESCE((SELECT max(ev.id) FROM audit_events ev WHERE ev.household_id=s.household_id "
        "AND ev.entity_type='account_snapshot' AND ev.entity_id=s.id AND ev.action IN ('create','replace')),0) AS observation_revision "
        "FROM account_snapshots s JOIN accounts a ON a.id=s.account_id AND a.household_id=s.household_id "
        "WHERE s.household_id=%s AND a.account_type='investment' ORDER BY s.account_id,s.as_of", (household_id,))
    inputs = repo.rows(conn, "SELECT i.*,s.as_of AS opening_as_of,e.as_of AS closing_as_of,"
        "COALESCE((SELECT max(ev.id) FROM audit_events ev WHERE ev.household_id=i.household_id AND ev.entity_type='investment_period_input' "
        "AND ev.entity_id=i.id AND ev.action IN ('create','update','confirm_flows')),0) AS confirmation_revision FROM investment_period_inputs i "
        "JOIN account_snapshots s ON s.id=i.opening_snapshot_id AND s.household_id=i.household_id "
        "JOIN account_snapshots e ON e.id=i.closing_snapshot_id AND e.household_id=i.household_id WHERE i.household_id=%s", (household_id,))
    grouped, assertions = defaultdict(list), defaultdict(list)
    for row in snapshots:
        if row.get("status","active")=="active":
            grouped[row["account_id"]].append(row)
    for row in inputs:
        # A later split cannot resurrect an earlier completeness assertion merely
        # because the inserted observation is subsequently voided. Explicit PUT
        # reconfirms the restored pair. Audit IDs order writes under the shared
        # household lock; transaction-start timestamps do not order waiting writers.
        row["pair_invalidated"]=any(s["account_id"]==row["account_id"]
            and row["opening_as_of"]<s["as_of"]<row["closing_as_of"]
            and s.get("observation_revision",0)>row.get("confirmation_revision",0)
            for s in snapshots)
        assertions[row["account_id"]].append(row)
    items, gaps, unavailable, excluded, historical, first_observations = [], [], [], [], [], []
    for account in accounts:
        if (start and account["closed_on"] and account["closed_on"] < start) or (end and account["opened_on"] > end):
            continue
        rows = grouped[account["id"]]
        if rows and (not start or rows[0]["as_of"].astimezone(tz).date()>=start) and (not end or rows[0]["as_of"].astimezone(tz).date()<=end):
            first_observations.append({"account_id":str(account["id"]),"account_name":account["name"],"currency":account["currency"],
                "snapshot_id":str(rows[0]["id"]),"as_of":rows[0]["as_of"].isoformat(),"gain":None,"gain_status":"unavailable","reason":"FIRST_OBSERVATION"})
        pairs = {(a["id"], b["id"]) for a,b in zip(rows,rows[1:])}
        saved = {(r["opening_snapshot_id"],r["closing_snapshot_id"]): r for r in assertions[account["id"]]}
        invalid = [r for key,r in saved.items() if key not in pairs or r["pair_invalidated"]]
        historical.extend(output(r) for r in invalid)
        if len(rows) < 2:
            unavailable.append({"account_id": str(account["id"]), "account_name": account["name"], "currency": account["currency"],
                "gain": None, "gain_status": "unavailable", "reason": "FIRST_OBSERVATION" if rows else "MISSING_OBSERVATIONS"})
        lower = datetime.combine(max(start,account["opened_on"]),time.min,tz) if start else None
        upper_day = min(end,account["closed_on"]) if end and account["closed_on"] else end
        upper = datetime.combine(upper_day,time.max,tz) if upper_day else None
        if not rows:
            gaps.append({"account_id":str(account["id"]),"reason":"MISSING_OBSERVATIONS"})
        else:
            if lower and rows[0]["as_of"] > lower:
                gaps.append({"account_id":str(account["id"]),"reason":"BEFORE_FIRST_OBSERVATION","from":lower.isoformat(),"to":min(rows[0]["as_of"],upper).isoformat() if upper else rows[0]["as_of"].isoformat()})
            if upper and rows[-1]["as_of"] < upper:
                gaps.append({"account_id":str(account["id"]),"reason":"AFTER_LAST_OBSERVATION","from":max(rows[-1]["as_of"],lower).isoformat() if lower else rows[-1]["as_of"].isoformat(),"to":upper.isoformat()})
        for opening,closing in zip(rows,rows[1:]):
            pair = (opening["id"],closing["id"])
            changed = any(r["opening_as_of"] < closing["as_of"] and r["closing_as_of"] > opening["as_of"] for r in invalid)
            item = interval(account,opening,closing,saved.get(pair),household["investment_review_change_ratio"],changed)
            first,last = opening["as_of"].astimezone(tz).date(), closing["as_of"].astimezone(tz).date()
            if (start and first < start) or (end and last > end):
                if not (start and last < start) and not (end and first > end):
                    excluded.append(item)
                continue
            items.append(item)
    totals = []
    for currency in sorted({a["currency"] for a in accounts}):
        included = [r for r in items if r["currency"]==currency]
        confirmed = sum((Decimal(r["gain"]) for r in included if r["gain_status"]=="user_confirmed"),Decimal(0))
        estimated = sum((Decimal(r["gain"]) for r in included if r["gain_status"]=="estimated"),Decimal(0))
        count = sum(r["gain_status"]=="estimated" for r in included)
        totals.append({"currency":currency,"confirmed_gain_subtotal":money_text(confirmed,currency),
            "estimated_gain_subtotal":money_text(estimated,currency),
            "combined_gain":money_text(confirmed+estimated,currency) if included else None,
            "gain_status":"estimated" if count else "user_confirmed" if included else "unavailable",
            "estimated_interval_count":count,"confirmed_interval_count":len(included)-count})
    return {"from":str(start) if start else None,"to":str(end) if end else None,"items":items,
        "unavailable":unavailable,"first_observations":first_observations,"excluded_boundary_intervals":excluded,"historical_inputs":historical,
        "native_currency_totals":totals,"coverage":{"complete":bool(items) and not gaps and not unavailable and not excluded,"gaps":gaps},
        "basis":"whole_observation_intervals_native_currency","investment_review_change_ratio":str(household["investment_review_change_ratio"])}


def put(conn, actor, key, data):
    def mutate(c,rid):
        rows = repo.rows(c,"SELECT * FROM account_snapshots WHERE household_id=%s AND id IN (%s,%s) AND status='active'",
            (actor.household_id,data["opening_snapshot_id"],data["closing_snapshot_id"]))
        by_id = {str(r["id"]):r for r in rows}
        if len(rows)!=2:
            fail("SNAPSHOT_NOT_FOUND","Active interval observations not found.",404)
        opening,closing = by_id[data["opening_snapshot_id"]],by_id[data["closing_snapshot_id"]]
        account = schema.get_account(c,actor.household_id,opening["account_id"])
        if account["account_type"]!="investment" or opening["account_id"]!=closing["account_id"] or opening["currency"]!=closing["currency"] or opening["as_of"]>=closing["as_of"]:
            fail("INVALID_INVESTMENT_PAIR","Use ordered observations of one investment account.")
        between = repo.rows(c,"SELECT id FROM account_snapshots WHERE household_id=%s AND account_id=%s AND status='active' AND as_of>%s AND as_of<%s",
            (actor.household_id,account["id"],opening["as_of"],closing["as_of"]))
        if between:
            fail("INVESTMENT_PAIR_CHANGED","The interval changed. Reload its consecutive observations.",409)
        additions,withdrawals = (flow_amount(data[k],account["currency"]) for k in ("contributions_amount","withdrawals_amount"))
        existing = repo.rows(c,"SELECT * FROM investment_period_inputs WHERE household_id=%s AND opening_snapshot_id=%s AND closing_snapshot_id=%s",
            (actor.household_id,opening["id"],closing["id"]))
        before = existing[0] if existing else None
        if data["expected_version"] != (before["row_version"] if before else None):
            fail("ROW_VERSION_CONFLICT","Flow inputs changed. Reload before saving.",409)
        if before:
            row = repo.rows(c,"UPDATE investment_period_inputs SET contributions_amount=%s,withdrawals_amount=%s,notes=%s,"
                "confirmed_by_user_id=%s,confirmed_at=now(),status='active',voided_at=NULL,voided_by_user_id=NULL,void_reason=NULL,"
                "row_version=row_version+1,updated_at=now() WHERE household_id=%s AND id=%s RETURNING *",
                (additions,withdrawals,data.get("notes"),actor.user_id,actor.household_id,before["id"]))[0]
        else:
            row = repo.rows(c,"INSERT INTO investment_period_inputs (id,household_id,account_id,opening_snapshot_id,closing_snapshot_id,"
                "contributions_amount,withdrawals_amount,created_by_user_id,confirmed_by_user_id,notes,source_request_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (uuid4(),actor.household_id,account["id"],opening["id"],closing["id"],additions,withdrawals,actor.user_id,actor.user_id,data.get("notes"),rid))[0]
        history(c,actor,rid,row,"update" if before else "create",before,entity="investment_period_input")
        return output(row),200 if before else 201
    return execute_durable_command(conn,actor,key,"PUT /api/v1/investment-period-inputs",data,mutate)


def void(conn,actor,key,identity,data):
    def mutate(c,rid):
        rows=repo.rows(c,"SELECT * FROM investment_period_inputs WHERE household_id=%s AND id=%s",(actor.household_id,identity))
        if not rows:
            fail("INVESTMENT_INPUT_NOT_FOUND","Flow input not found.",404)
        before=rows[0]
        if before["status"]!="active" or before["row_version"]!=data["expected_version"]:
            fail("ROW_VERSION_CONFLICT","Flow inputs changed. Reload before saving.",409)
        if not data["reason"].strip():
            fail("INVALID_REASON","Explain why the flow confirmation is withdrawn.")
        row=repo.rows(c,"UPDATE investment_period_inputs SET status='voided',voided_at=now(),voided_by_user_id=%s,void_reason=%s,"
            "row_version=row_version+1,updated_at=now() WHERE household_id=%s AND id=%s RETURNING *",
            (actor.user_id,data["reason"],actor.household_id,identity))[0]
        history(c,actor,rid,row,"void",before,data["reason"],entity="investment_period_input")
        return output(row),200
    return execute_durable_command(conn,actor,key,f"POST /api/v1/investment-period-inputs/{identity}/void",data,mutate)


def update_settings(conn,actor,key,data):
    def mutate(c,rid):
        try:
            ratio=Decimal(data["investment_review_change_ratio"])
            if not ratio.is_finite() or not 0<ratio<=1 or ratio!=ratio.quantize(Decimal("0.0001")):
                raise ValueError()
        except (InvalidOperation,ValueError):
            fail("INVALID_REVIEW_THRESHOLD","Use a ratio above zero and at most one, with up to four decimal places.")
        before=schema.get_household(c,actor.household_id)
        if before["row_version"]!=data["expected_version"]:
            fail("ROW_VERSION_CONFLICT","Household settings changed. Reload them.",409)
        row=schema.update_household_settings(c,actor.household_id,data["expected_version"],investment_review_change_ratio=ratio)
        history(c,actor,rid,row,"update",before,entity="household")
        return output(row),200
    return execute_durable_command(conn,actor,key,"PATCH /api/v1/household-settings",data,mutate)


def review_page(result,cursor,limit):
    items=sorted((r for r in result["items"] if r["needs_review"]),key=lambda r:r["id"])
    total=len(items)
    if cursor:
        try:
            first,last=cursor.split(":")
            if str(UUID(first))+":"+str(UUID(last))!=cursor:
                raise ValueError()
        except ValueError:
            fail("INVALID_CURSOR","Invalid investment review cursor.")
        items=[r for r in items if r["id"]>cursor]
    return {"items":items[:limit],"next_cursor":items[limit-1]["id"] if len(items)>limit else None},total
