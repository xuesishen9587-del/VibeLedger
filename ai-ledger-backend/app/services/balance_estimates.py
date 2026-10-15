"""Read-only, cautious suggestions from a verified balance and *linked* spending records.

This never writes a balance observation. It intentionally declines a number if a
same-day date-only transaction could precede or follow the baseline snapshot, or
if a transaction is denominated in a different currency from the account.
Transfers, unlinked transactions, market moves and unrecorded bank activity are
not modeled, so even a ready estimate is not an account statement.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.domain.spending import money_text
from app.repositories import simplified_schema as schema, spending as repo

SUPPORTED = {"cash", "savings", "credit"}


def report(conn, household_id):
    household = schema.get_household(conn, household_id)
    now = datetime.now(timezone.utc)
    zone = ZoneInfo(household["timezone"])
    today = now.astimezone(zone).date()
    accounts = repo.rows(conn, "SELECT id, name, account_type, currency FROM accounts "
        "WHERE household_id=%s AND status='active' AND opened_on<=%s "
        "AND (closed_on IS NULL OR closed_on>=%s) ORDER BY id", (household_id, today, today))
    snapshots = repo.rows(conn, "SELECT DISTINCT ON (account_id) account_id, id, as_of, balance, currency "
        "FROM account_snapshots WHERE household_id=%s AND status='active' AND as_of<=%s "
        "ORDER BY account_id, as_of DESC", (household_id, now))
    heads = {row["account_id"]: row for row in snapshots}
    eligible = [a for a in accounts if a["account_type"] in SUPPORTED and a["id"] in heads]
    # Aggregate in PostgreSQL, using each account's own baseline. Do not fetch the
    # family's full transaction history into application memory on every read.
    changes = repo.rows(conn, """
        WITH baselines AS (
            SELECT * FROM unnest(%s::uuid[], %s::date[], %s::text[])
                AS b(account_id, baseline_date, currency)
        )
        SELECT b.account_id,
            count(t.id) FILTER (WHERE t.occurred_on=b.baseline_date) AS same_day_count,
            count(t.id) FILTER (WHERE t.occurred_on>b.baseline_date) AS transaction_count,
            count(t.id) FILTER (WHERE t.occurred_on>b.baseline_date
                AND t.original_currency<>b.currency) AS currency_mismatch_count,
            COALESCE(sum(CASE WHEN t.occurred_on>b.baseline_date AND t.original_currency=b.currency
                THEN CASE t.transaction_type WHEN 'expense' THEN -t.original_amount
                    ELSE t.original_amount END ELSE 0 END), 0) AS recorded_change
        FROM baselines b LEFT JOIN transactions t
            ON t.household_id=%s AND t.account_id=b.account_id AND t.status='committed'
            AND t.transaction_type IN ('expense', 'refund', 'cash_income')
            AND t.occurred_on BETWEEN b.baseline_date AND %s
        GROUP BY b.account_id
        """, ([a["id"] for a in eligible],
              [heads[a["id"]]["as_of"].astimezone(zone).date() for a in eligible],
              [a["currency"] for a in eligible], household_id, today)) if eligible else []
    linked = {row["account_id"]: row for row in changes}
    result = []
    for account in accounts:
        head = heads.get(account["id"])
        item = {"account_id": str(account["id"]), "currency": account["currency"],
                "status": "no_snapshot", "snapshot_id": str(head["id"]) if head else None,
                "baseline_date": None, "baseline_balance": None,
                "recorded_change": None, "estimated_balance": None,
                "transaction_count": 0, "same_day_count": 0, "currency_mismatch_count": 0}
        if not head:
            result.append(item)
            continue
        baseline_date = head["as_of"].astimezone(zone).date()
        item["baseline_date"] = baseline_date.isoformat()
        item["baseline_balance"] = money_text(head["balance"], account["currency"])
        if account["account_type"] not in SUPPORTED:
            item["status"] = "unsupported_account"
            result.append(item)
            continue
        change = linked[account["id"]]
        for field in ("same_day_count", "currency_mismatch_count", "transaction_count"):
            item[field] = change[field]
        if item["same_day_count"]:
            item["status"] = "ambiguous_same_day"
        elif item["currency_mismatch_count"]:
            item["status"] = "currency_mismatch"
        else:
            delta = change["recorded_change"]
            item["recorded_change"] = money_text(delta, account["currency"])
            item["estimated_balance"] = money_text(head["balance"] + delta, account["currency"])
            item["status"] = "ready"
        result.append(item)
    return {"as_of": now.isoformat(), "items": result}
