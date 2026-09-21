"""Last reported wealth: no spending projection, native amounts survive missing FX."""
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from app.domain.balances import instant
from app.domain.spending import fail, MINOR_UNITS, money_text
from app.repositories import spending as repo, simplified_schema as schema


def report_time(value, household):
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(household["timezone"])
    if value is None:
        return now, True
    if isinstance(value, str) and len(value) == 10:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            fail("INVALID_DATE", "Use a date or timestamp with timezone.")
        timestamp = datetime.combine(day, time.max, tz).astimezone(timezone.utc)
        if day == now.astimezone(tz).date():
            timestamp = now
    else:
        timestamp = instant(value)
    if timestamp > now:
        fail("INVALID_DATE", "Future wealth is not a reported balance.")
    return timestamp, False


def report(conn, household_id, as_of=None, *, historical_risk=False):
    household = schema.get_household(conn, household_id)
    timestamp, current = report_time(as_of, household)
    tz = ZoneInfo(household["timezone"])
    day = timestamp.astimezone(tz).date()
    currency = household["reporting_currency"]
    accounts = repo.rows(conn, "SELECT * FROM accounts WHERE household_id=%s AND status<>'cancelled' "
        "AND opened_on<=%s AND (closed_on IS NULL OR closed_on>=%s) ORDER BY id", (household_id, day, day))
    rows = repo.rows(conn, "SELECT DISTINCT ON (account_id) * FROM account_snapshots WHERE household_id=%s "
        "AND status='active' AND as_of<=%s ORDER BY account_id,as_of DESC", (household_id, timestamp))
    by_account = {r["account_id"]: r for r in rows}
    assets, debts = Decimal(0), Decimal(0)
    buckets = {key: Decimal(0) for key in ("very_low", "low", "medium", "high", "unclassified")}
    missing, missing_fx, stale, stale_fx, times, results = [], set(), [], set(), [], []
    for account in accounts:
        row = by_account.get(account["id"])
        item = {"account_id": str(account["id"]), "name": account["name"], "account_type": account["account_type"],
            "currency": account["currency"], "balance_scope": account["balance_scope"], "account_version": account["row_version"],
            "snapshot_id": None, "as_of": None, "balance": None, "converted_amount": None, "fx_as_of": None,
            "age_days": None, "needs_update": False, "very_stale": False, "time_basis": None}
        if not row:
            missing.append(str(account["id"]))
            results.append(item)
            continue
        age = max(0, (day - row["as_of"].astimezone(tz).date()).days)
        times.append(row["as_of"])
        if age > 30:
            stale.append(str(account["id"]))
        item.update(snapshot_id=str(row["id"]), as_of=row["as_of"].isoformat(), balance=money_text(row["balance"], row["currency"]),
                    age_days=age, needs_update=age>30, very_stale=age>90, time_basis=row["time_basis"])
        rate, rate_day = Decimal(1), day
        if row["currency"] != currency:
            quotes = repo.rows(conn, "SELECT * FROM fx_quotes WHERE from_currency=%s AND to_currency=%s "
                "AND rate_as_of<=%s ORDER BY rate_as_of DESC LIMIT 1", (row["currency"], currency, day))
            quote = quotes[0] if quotes else None
            if not quote or (not current and (day - quote["rate_as_of"]).days > 7):
                missing_fx.add(row["currency"])
                results.append(item)
                continue
            rate, rate_day = quote["rate"], quote["rate_as_of"]
            if (day - rate_day).days > 7:
                stale_fx.add(row["currency"])
        converted = (row["balance"] * rate).quantize(Decimal(10) ** -MINOR_UNITS[currency], rounding=ROUND_HALF_UP)
        item.update(converted_amount=money_text(converted, currency), fx_as_of=str(rate_day), fx_age_days=(day-rate_day).days,
                    credit_surplus=account["account_type"] == "credit" and row["balance"] > 0)
        results.append(item)
        if converted > 0:
            assets += converted
            risk = account["risk_level"] if account["account_type"] != "credit" else None
            buckets[risk or "unclassified"] += converted
        else:
            debts -= converted
    complete = bool(accounts) and not missing and not missing_fx
    totals = {"assets": assets, "liabilities": debts, "net_worth": assets-debts}
    result = {"as_of": timestamp.isoformat(), "reporting_currency": currency, "accounts": results,
        "setup_required": not accounts, "risk_buckets": [] if historical_risk else [
            {"risk_level": key, "amount": money_text(value, currency),
             "percentage": str((value/assets*100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if assets else None}
            for key, value in buckets.items()],
        "risk_basis": "not_available_historically" if historical_risk else "current_account_metadata",
        "coverage": {"complete": complete, "missing_account_ids": missing, "missing_fx_currencies": sorted(missing_fx),
            "stale_account_ids": stale, "stale_fx_currencies": sorted(stale_fx),
            "oldest_observation_at": min(times).isoformat() if times else None,
            "newest_observation_at": max(times).isoformat() if times else None}}
    for name, value in totals.items():
        result["known_"+name] = money_text(value, currency)
        result[name if name=="net_worth" else "total_"+name] = money_text(value, currency) if complete else None
    return result


def history(conn, household_id, start, end):
    if start>end or (end-start).days>3660:
        fail("INVALID_DATE_RANGE", "Use an ordered date range of at most ten years.")
    household = schema.get_household(conn, household_id)
    tz = ZoneInfo(household["timezone"])
    lower = datetime.combine(start, time.min, tz)
    upper, _ = report_time(str(end), household)
    events = {lower, upper}
    rows = repo.rows(conn, "SELECT as_of FROM account_snapshots WHERE household_id=%s AND status='active' "
        "AND as_of BETWEEN %s AND %s", (household_id, lower, upper))
    events.update(r["as_of"] for r in rows)
    for account in schema.list_accounts(conn, household_id):
        for day in (account["opened_on"], account["closed_on"]+timedelta(days=1) if account["closed_on"] else None):
            if day and start<=day<=end:
                point = datetime.combine(day, time.min, tz)
                if point<=upper:
                    events.add(point)
    # Historical conversion can change without a new balance observation. Include
    # quote publication/expiry and observation-age boundaries so chart segments
    # cannot imply complete or fresh data across a known coverage change.
    quotes = repo.rows(conn, "SELECT DISTINCT rate_as_of FROM fx_quotes WHERE to_currency=%s "
        "AND from_currency IN (SELECT currency FROM accounts WHERE household_id=%s AND status<>'cancelled') "
        "AND rate_as_of BETWEEN %s AND %s", (household["reporting_currency"], household_id, start-timedelta(days=7), end))
    boundaries = [day for quote in quotes for day in (quote["rate_as_of"],quote["rate_as_of"]+timedelta(days=8))]
    observations = repo.rows(conn, "SELECT as_of FROM account_snapshots WHERE household_id=%s AND status='active' "
        "AND as_of BETWEEN %s AND %s", (household_id, lower-timedelta(days=31), upper))
    boundaries.extend(row["as_of"].astimezone(tz).date()+timedelta(days=31) for row in observations)
    for day in boundaries:
        point = datetime.combine(day, time.min, tz)
        if lower<=point<=upper:
            events.add(point)
    return {"from": str(start), "to": str(end), "basis": "last_reported_observations",
            "points": [report(conn, household_id, t.isoformat(), historical_risk=True) for t in sorted(events)]}
