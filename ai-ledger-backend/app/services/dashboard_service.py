from typing import Optional, Dict, Any, List
from uuid import UUID
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timezone
from collections import defaultdict

from app.domain.money import quantize_money, parse_decimal, validate_currency_code
from app.domain.transactions import FxRateUnavailableError, HouseholdMismatchError
from app.repositories.accounts import list_accounts, get_household
from app.services.reference_fx_service import ReferenceFxService

def get_overview(
    conn,
    household_id: UUID,
    as_of_dt: Optional[datetime] = None,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Computes household balance sheet overview, total assets, total liabilities,
    net worth, data freshness metrics, and authoritative asset allocation in reporting currency.
    """
    household = get_household(conn, household_id)
    if not household:
        raise HouseholdMismatchError(f"Household {household_id} not found.")

    reporting_currency = household["reporting_currency"]
    as_of = as_of_dt or datetime.now(timezone.utc)
    fx = fx_service or ReferenceFxService()

    active_accounts = list_accounts(conn, household_id, status='active')

    total_assets = Decimal("0")
    total_liabilities = Decimal("0")

    confirmed_30d_count = 0
    confirmed_90d_count = 0
    total_active_accounts = len(active_accounts)

    allocation_dict = {
        "cash": Decimal("0"),
        "savings": Decimal("0"),
        "investment": Decimal("0"),
        "credit": Decimal("0")
    }

    for acc in active_accounts:
        curr = acc["currency"]
        bal = parse_decimal(acc["ledger_balance"])
        acc_type = acc["account_type"]
        last_snap = acc["last_authoritative_snapshot_at"]

        # 1. Freshness attribution
        if last_snap is not None:
            age_days = (as_of.date() - last_snap.date()).days
            if age_days < 0:
                age_days = 0
            if age_days <= 30:
                confirmed_30d_count += 1
            if age_days <= 90:
                confirmed_90d_count += 1

        # 2. Currency conversion
        if curr == reporting_currency:
            converted_bal = quantize_money(bal, reporting_currency)
        else:
            rate = fx.get_rate(curr, reporting_currency, as_of=as_of.date())
            if rate is None or rate <= 0:
                raise FxRateUnavailableError(f"Reference FX rate unavailable for {curr} -> {reporting_currency}")
            converted_bal = quantize_money(bal * rate, reporting_currency)

        # 3. Asset & liability classification
        if acc_type == "credit":
            if converted_bal < 0:
                total_liabilities += abs(converted_bal)
            elif converted_bal > 0:
                total_assets += converted_bal
                allocation_dict["credit"] += converted_bal
        else:
            if converted_bal >= 0:
                total_assets += converted_bal
                if acc_type in allocation_dict:
                    allocation_dict[acc_type] += converted_bal
            else:
                total_liabilities += abs(converted_bal)

    net_worth = total_assets - total_liabilities

    if total_active_accounts > 0:
        ratio_30d = (Decimal(confirmed_30d_count) / Decimal(total_active_accounts)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        ratio_90d = (Decimal(confirmed_90d_count) / Decimal(total_active_accounts)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    else:
        ratio_30d = Decimal("1.0000")
        ratio_90d = Decimal("1.0000")

    asset_allocation = [
        {
            "account_type": "cash",
            "account_type_label": "活期资产",
            "amount": f"{quantize_money(allocation_dict['cash'], reporting_currency):.2f}",
            "currency": reporting_currency
        },
        {
            "account_type": "savings",
            "account_type_label": "储蓄资产",
            "amount": f"{quantize_money(allocation_dict['savings'], reporting_currency):.2f}",
            "currency": reporting_currency
        },
        {
            "account_type": "investment",
            "account_type_label": "投资资产",
            "amount": f"{quantize_money(allocation_dict['investment'], reporting_currency):.2f}",
            "currency": reporting_currency
        },
        {
            "account_type": "credit",
            "account_type_label": "信用溢缴款",
            "amount": f"{quantize_money(allocation_dict['credit'], reporting_currency):.2f}",
            "currency": reporting_currency
        }
    ]

    return {
        "as_of": as_of.isoformat(),
        "reporting_currency": reporting_currency,
        "total_assets": f"{quantize_money(total_assets, reporting_currency):.2f}",
        "total_liabilities": f"{quantize_money(total_liabilities, reporting_currency):.2f}",
        "net_worth": f"{quantize_money(net_worth, reporting_currency):.2f}",
        "asset_allocation": asset_allocation,
        "data_freshness": {
            "confirmed_within_30d_ratio": f"{ratio_30d:.4f}",
            "confirmed_within_90d_ratio": f"{ratio_90d:.4f}"
        }
    }

def get_cash_flow(
    conn,
    household_id: UUID,
    from_date: date,
    to_date: date,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Computes household cash flow summary and category breakdown over the given period.
    Household Expense = ordinary expense + fee - refunds.
    Net Cash Flow = cash_income - Household Expense.
    Excludes: transfer, opening_balance, reconciliation_adjustment, investment_pnl.
    """
    household = get_household(conn, household_id)
    if not household:
        raise HouseholdMismatchError(f"Household {household_id} not found.")

    reporting_currency = household["reporting_currency"]
    fx = fx_service or ReferenceFxService()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.transaction_type, t.occurred_on,
                   t.from_amount, t.from_currency,
                   t.to_amount, t.to_currency,
                   t.original_amount, t.original_currency,
                   t.reporting_amount, t.reporting_currency,
                   COALESCE(t.category_id, orig_t.category_id) AS category_id,
                   COALESCE(c.name, orig_c.name) AS category_name
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN transaction_links tl ON tl.target_transaction_id = t.id AND tl.relation_type = 'refund'
            LEFT JOIN transactions orig_t ON orig_t.id = tl.source_transaction_id
            LEFT JOIN categories orig_c ON orig_c.id = orig_t.category_id
            WHERE t.household_id = %s
              AND t.occurred_on >= %s
              AND t.occurred_on <= %s
              AND t.status = 'committed'
              AND t.deleted_at IS NULL;
            """,
            (household_id, from_date, to_date)
        )
        rows = cur.fetchall()

    cash_income_total = Decimal("0")
    ordinary_expense_total = Decimal("0")
    fee_total = Decimal("0")
    refund_total = Decimal("0")
    category_totals = defaultdict(Decimal)

    for r in rows:
        tx_type = r[1]
        tx_date = r[2]
        from_amt, from_curr = r[3], r[4]
        to_amt, to_curr = r[5], r[6]
        orig_amt, orig_curr = r[7], r[8]
        rep_amt, rep_curr = r[9], r[10]
        cat_id, cat_name = r[11], r[12]

        # Strictly exclude transfers, opening balances, adjustments
        if tx_type in ("transfer", "opening_balance", "reconciliation_adjustment"):
            continue

        if tx_type == "cash_income":
            raw_amt = to_amt or orig_amt
            raw_curr = to_curr or orig_curr
        elif tx_type in ("expense", "fee"):
            raw_amt = from_amt or orig_amt
            raw_curr = from_curr or orig_curr
        elif tx_type == "refund":
            raw_amt = to_amt or orig_amt
            raw_curr = to_curr or orig_curr
        else:
            continue

        if raw_amt is None:
            continue
        raw_amt = parse_decimal(raw_amt)

        # Convert to reporting currency
        if rep_amt is not None and rep_curr == reporting_currency:
            converted = parse_decimal(rep_amt)
        elif raw_curr == reporting_currency:
            converted = raw_amt
        else:
            rate = fx.get_rate(raw_curr, reporting_currency, as_of=tx_date)
            if rate is None or rate <= 0:
                raise FxRateUnavailableError(f"Reference FX rate unavailable for {raw_curr} -> {reporting_currency}")
            converted = quantize_money(raw_amt * rate, reporting_currency)

        if tx_type == "cash_income":
            cash_income_total += converted
        elif tx_type == "expense":
            ordinary_expense_total += converted
            category_totals[(cat_id, cat_name)] += converted
        elif tx_type == "fee":
            fee_total += converted
            category_totals[(cat_id, cat_name)] += converted
        elif tx_type == "refund":
            refund_total += converted
            category_totals[(cat_id, cat_name)] -= converted

    # Household Expense = ordinary expense + fee - refund (net expense)
    household_expense = ordinary_expense_total + fee_total - refund_total
    net_cash_flow = cash_income_total - household_expense

    expense_by_category = [
        {
            "category_id": str(cid) if cid else None,
            "category_name": cname or "未分类",
            "amount": f"{quantize_money(amt, reporting_currency):.2f}",
            "currency": reporting_currency
        }
        for (cid, cname), amt in sorted(category_totals.items(), key=lambda x: x[1], reverse=True)
    ]

    return {
        "cash_income": f"{quantize_money(cash_income_total, reporting_currency):.2f}",
        "expense": f"{quantize_money(household_expense, reporting_currency):.2f}",
        "refund": f"{quantize_money(refund_total, reporting_currency):.2f}",
        "net_cash_flow": f"{quantize_money(net_cash_flow, reporting_currency):.2f}",
        "expense_by_category": expense_by_category,
        "reporting_currency": reporting_currency
    }

def get_investments_summary(
    conn,
    household_id: UUID,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Reads and aggregates confirmed investment P&L for the household and computes total valuation.
    Investment P&L remains strictly separate from cash income.
    Converts multi-currency items to the household reporting currency using Decimal FX.
    """
    household = get_household(conn, household_id)
    if not household:
        raise HouseholdMismatchError(f"Household {household_id} not found.")

    reporting_currency = household["reporting_currency"]
    fx = fx_service or ReferenceFxService()

    # 1. Authoritative Total Valuation across active investment accounts
    inv_accounts = list_accounts(conn, household_id, status='active', account_type='investment')
    total_valuation = Decimal("0")
    as_of_date = to_date or date.today()

    for acc in inv_accounts:
        bal = parse_decimal(acc["ledger_balance"])
        curr = acc["currency"]
        if curr == reporting_currency:
            conv = bal
        else:
            rate = fx.get_rate(curr, reporting_currency, as_of=as_of_date)
            if rate is None or rate <= 0:
                raise FxRateUnavailableError(f"Reference FX rate unavailable for {curr} -> {reporting_currency}")
            conv = quantize_money(bal * rate, reporting_currency)
        total_valuation += conv

    # 2. Confirmed P&L Periods
    query = """
        SELECT id, account_id, period_start, period_end,
               contributions_amount, withdrawals_amount, pnl_amount,
               currency, status, calculation_version, created_at
        FROM investment_pnl_periods
        WHERE household_id = %(household_id)s
          AND status = 'confirmed'
    """
    params: Dict[str, Any] = {"household_id": household_id}

    if from_date is not None:
        query += " AND period_end >= %(from_date)s"
        params["from_date"] = from_date
    if to_date is not None:
        query += " AND period_start <= %(to_date)s"
        params["to_date"] = to_date

    query += " ORDER BY period_end DESC;"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    total_pnl = Decimal("0")
    items = []
    for r in rows:
        pnl = parse_decimal(r[6])
        curr = r[7]
        period_end_date = r[3]

        if curr == reporting_currency:
            converted_pnl = pnl
        else:
            rate = fx.get_rate(curr, reporting_currency, as_of=period_end_date)
            if rate is None or rate <= 0:
                raise FxRateUnavailableError(f"Reference FX rate unavailable for {curr} -> {reporting_currency}")
            converted_pnl = quantize_money(pnl * rate, reporting_currency)

        total_pnl += converted_pnl

        items.append({
            "id": str(r[0]),
            "account_id": str(r[1]),
            "period_start": r[2].isoformat(),
            "period_end": r[3].isoformat(),
            "contributions_amount": f"{quantize_money(r[4], r[7]):.2f}",
            "withdrawals_amount": f"{quantize_money(r[5], r[7]):.2f}",
            "pnl_amount": f"{quantize_money(r[6], r[7]):.2f}",
            "currency": r[7],
            "status": r[8]
        })

    return {
        "reporting_currency": reporting_currency,
        "total_valuation": f"{quantize_money(total_valuation, reporting_currency):.2f}",
        "total_pnl": f"{quantize_money(total_pnl, reporting_currency):.2f}",
        "items": items
    }


def get_account_freshness(
    conn,
    household_id: UUID,
    as_of_dt: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Returns account snapshot freshness status and age in days.
    Freshness classifications:
      <= 30 days: 'fresh'
      31..90 days: 'stale'
      > 90 days or no snapshot: 'expired'
    """
    household = get_household(conn, household_id)
    if not household:
        raise HouseholdMismatchError(f"Household {household_id} not found.")

    as_of = as_of_dt or datetime.now(timezone.utc)
    active_accounts = list_accounts(conn, household_id, status='active')

    items = []
    for acc in active_accounts:
        snap_at = acc["last_authoritative_snapshot_at"]
        if snap_at is None:
            age_days = None
            freshness = "expired"
        else:
            delta_days = (as_of.date() - snap_at.date()).days
            age_days = max(0, delta_days)
            if age_days <= 30:
                freshness = "fresh"
            elif age_days <= 90:
                freshness = "stale"
            else:
                freshness = "expired"

        items.append({
            "account_id": str(acc["id"]),
            "account_name": acc["name"],
            "last_authoritative_snapshot_at": snap_at.isoformat() if snap_at else None,
            "age_days": age_days,
            "freshness": freshness
        })

    return {"items": items}
