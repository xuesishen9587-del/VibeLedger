"""Read-only, cautious suggestions from a verified balance and *linked* spending records.

This never writes a balance observation. It intentionally declines a number if a
same-day date-only transaction could precede or follow the baseline snapshot, or
if a transaction is denominated in a different currency from the account.
Transfers, unlinked transactions, market moves and unrecorded bank activity are
not modeled, so even a ready estimate is not an account statement.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.spending import money_text
from app.repositories import simplified_schema as schema, spending as repo

SUPPORTED = {"cash", "savings", "credit"}
SIGNS = {"expense": -1, "refund": 1, "cash_income": 1}


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
    since = [row["as_of"].astimezone(zone).date() for row in snapshots if row["account_id"] in {a["id"] for a in accounts}]
    rows = repo.rows(conn, "SELECT account_id, occurred_on, original_amount, original_currency, transaction_type "
        "FROM transactions WHERE household_id=%s AND status='committed' AND account_id IS NOT NULL "
        "AND occurred_on BETWEEN %s AND %s ORDER BY account_id, occurred_on, id", (household_id, min(since), today)) if since else []
    linked = defaultdict(list)
    for row in rows:
        linked[row["account_id"]].append(row)
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
        account_rows = linked[account["id"]]
        same_day = [row for row in account_rows if row["occurred_on"] == baseline_date]
        later = [row for row in account_rows if row["occurred_on"] > baseline_date]
        incompatible = [row for row in later if row["original_currency"] != account["currency"]]
        item["same_day_count"] = len(same_day)
        item["currency_mismatch_count"] = len(incompatible)
        item["transaction_count"] = len(later)
        if same_day:
            item["status"] = "ambiguous_same_day"
        elif incompatible:
            item["status"] = "currency_mismatch"
        else:
            delta = sum((Decimal(row["original_amount"]) * SIGNS[row["transaction_type"]]
                        for row in later), Decimal(0))
            item["recorded_change"] = money_text(delta, account["currency"])
            item["estimated_balance"] = money_text(head["balance"] + delta, account["currency"])
            item["status"] = "ready"
        result.append(item)
    return {"as_of": now.isoformat(), "items": result}
