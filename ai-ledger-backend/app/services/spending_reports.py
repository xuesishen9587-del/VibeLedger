from collections import defaultdict
from datetime import date
from decimal import Decimal
from fastapi import HTTPException
from app.domain.spending import fail, money, money_text, business_date
from app.repositories import spending as repo, simplified_schema as settings
from app.services import spending_service as spending
from app.services.durable_commands import execute_durable_command


def prime_quote(conn, household_id, data, provider):
    household = settings.get_household(conn, household_id)
    source, target = data["original_currency"], household["reporting_currency"]
    try:
        money(data["original_amount"], source)
        day = business_date(data["occurred_on"], household)
    except HTTPException:
        return  # Authoritative validation happens inside the durable command.
    if source == target or provider is None:
        return
    cached = repo.rows(conn, "SELECT 1 FROM fx_quotes WHERE from_currency=%s AND to_currency=%s "
                      "AND rate_as_of BETWEEN %s::date-7 AND %s LIMIT 1", (source, target, day, day))
    conn.commit()  # Network call must not run in a database transaction.
    if cached:
        return
    quote = provider.fetch_quote(source, target, day)
    if quote:
        effective, rate = quote["rate_as_of"], quote["rate"]
        if not 0 <= (day - effective).days <= 7 or not rate.is_finite() or not 0 < rate < Decimal("1e12"):
            return
        with conn.cursor() as cur:
            cur.execute("INSERT INTO fx_quotes(from_currency,to_currency,rate_as_of,rate,source) "
                        "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (source, target, effective, rate, quote["source"]))
        conn.commit()


def refresh(conn, actor, key, data, provider):
    existing = settings.get_ingestion_request_by_key(conn, actor.household_id, actor.actor_scope, key)
    if not existing:
        # Refresh current wealth quotes without changing any observed balance.
        from app.domain.spending import local_today
        today = local_today(settings.get_household(conn, actor.household_id))
        currencies = repo.rows(conn, "SELECT DISTINCT currency FROM accounts WHERE household_id=%s AND status='active' ORDER BY currency LIMIT 5", (actor.household_id,))
        for item in currencies:
            prime_quote(conn, actor.household_id, {"original_amount": "1", "original_currency": item["currency"], "occurred_on": today}, provider)
        selected = repo.rows(conn, "SELECT * FROM transactions WHERE household_id=%s AND status='committed' "
            "AND reporting_amount IS NULL AND (%s::date IS NULL OR occurred_on>=%s) "
            "AND (%s::date IS NULL OR occurred_on<=%s) ORDER BY occurred_on,id LIMIT 200",
            (actor.household_id, data.get("from"), data.get("from"), data.get("to"), data.get("to")))
        # Bounded provider work per request; remaining currencies/dates can retry later.
        attempted = set()
        for row in selected:
            pair = (row["original_currency"], row["occurred_on"])
            if pair not in attempted and len(attempted) < 5:
                attempted.add(pair)
                prime_quote(conn, actor.household_id, row, provider)
    def mutate(c, rid):
        filled = 0
        household = settings.get_household(c, actor.household_id)
        rows = repo.rows(c, "SELECT * FROM transactions WHERE household_id=%s AND status='committed' "
            "AND reporting_amount IS NULL AND (%s::date IS NULL OR occurred_on>=%s) "
            "AND (%s::date IS NULL OR occurred_on<=%s) ORDER BY occurred_on,id LIMIT 200",
            (actor.household_id, data.get("from"), data.get("from"), data.get("to"), data.get("to")))
        for before in rows:
            values = spending.conversion(c, household, before)
            if values["reporting_amount"] is not None:
                row = repo.update(c, actor.household_id, before["id"], values)
                spending.record_audit(c, actor, rid, row, "fill_reporting_fx", before)
                filled += 1
        pending = repo.rows(c, "SELECT count(*) AS n FROM transactions WHERE household_id=%s "
                            "AND status='committed' AND reporting_amount IS NULL", (actor.household_id,))[0]["n"]
        return {"filled": filled, "pending": pending}, 200
    return execute_durable_command(conn, actor, key, "POST /api/v1/reports/refresh-fx", data, mutate)


def report(conn, household_id, start, end):
    from app.services.spending_schedules import freshness
    if start > end:
        fail("INVALID_DATE", "The date range is reversed.")
    records = repo.rows(conn, "SELECT * FROM transactions WHERE household_id=%s AND status='committed' "
                       "AND occurred_on BETWEEN %s AND %s", (household_id, start, end))
    household = settings.get_household(conn, household_id)
    def totals(rows, amount_key):
        values = {"gross_expenses": Decimal(0), "refunds": Decimal(0), "recorded_income": Decimal(0)}
        keys = {"expense": "gross_expenses", "refund": "refunds", "cash_income": "recorded_income"}
        missing = 0
        for row in rows:
            if row[amount_key] is None:
                missing += 1
            else:
                values[keys[row["transaction_type"]]] += row[amount_key]
        values["net_spending"] = values["gross_expenses"] - values["refunds"]
        currency = rows[0]["original_currency"] if amount_key == "original_amount" and rows else household["reporting_currency"]
        result = {"known_" + key: money_text(value, currency) for key, value in values.items()}
        result.update({key: None if missing else money_text(value, currency) for key, value in values.items()})
        result["missing_conversion_count"] = missing
        return result
    def groups(key, amount_key):
        buckets = defaultdict(list)
        for row in records:
            buckets[key(row)].append(row)
        return [{"key": name, **totals(rows, amount_key)} for name, rows in sorted(buckets.items())]
    return {"from": str(start), "to": str(end), "reporting_currency": household["reporting_currency"],
            "schedules_current_through": freshness(conn, household_id),
            **totals(records, "reporting_amount"),
            "native_currency_totals": groups(lambda r: r["original_currency"], "original_amount"),
            "category": groups(lambda r: str(r["category_id"]), "reporting_amount"),
            "merchant": groups(lambda r: r["merchant"] or "", "reporting_amount"),
            "month": groups(lambda r: r["occurred_on"].strftime("%Y-%m"), "reporting_amount")}
