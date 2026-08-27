import unittest
from unittest.mock import MagicMock
from decimal import Decimal
from uuid import uuid4
from datetime import date, datetime, timezone, timedelta

from app.services.dashboard_service import (
    get_overview,
    get_cash_flow,
    get_investments_summary,
    get_account_freshness
)
from app.services.reference_fx_service import ReferenceFxService
from app.domain.transactions import FxRateUnavailableError

class TestDashboardServiceUnit(unittest.TestCase):
    def setUp(self):
        self.household_id = uuid4()
        self.now_utc = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)
        self.mock_fx = ReferenceFxService(fixed_rates={
            ("USD", "CNY"): Decimal("7.20"),
            ("EUR", "CNY"): Decimal("7.80")
        })

    def test_investments_summary_multi_currency_fx_conversion(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Mock get_household
        mock_cur.fetchone.return_value = (
            self.household_id, "Test Household", "CNY", "active", 0, datetime.now(), datetime.now()
        )

        # Mock investment_pnl_periods rows:
        # Row 1: 100.00 CNY
        # Row 2: 100.00 USD
        mock_cur.fetchall.return_value = [
            (
                uuid4(), uuid4(), date(2026, 8, 1), date(2026, 8, 30),
                Decimal("0.00"), Decimal("0.00"), Decimal("100.00"),
                "CNY", "confirmed", 1, datetime.now()
            ),
            (
                uuid4(), uuid4(), date(2026, 8, 1), date(2026, 8, 30),
                Decimal("0.00"), Decimal("0.00"), Decimal("100.00"),
                "USD", "confirmed", 1, datetime.now()
            )
        ]

        with unittest.mock.patch("app.services.dashboard_service.list_accounts") as mock_list_accs:
            mock_list_accs.return_value = [
                {
                    "id": uuid4(),
                    "name": "Invest CNY",
                    "account_type": "investment",
                    "currency": "CNY",
                    "ledger_balance": Decimal("50000.00")
                }
            ]

            summary = get_investments_summary(
                conn=mock_conn,
                household_id=self.household_id,
                from_date=date(2026, 8, 1),
                to_date=date(2026, 8, 31),
                fx_service=self.mock_fx
            )

        # 100.00 CNY + 100.00 USD * 7.20 = 820.00 CNY
        self.assertEqual(summary["reporting_currency"], "CNY")
        self.assertEqual(summary["total_pnl"], "820.00")
        self.assertEqual(len(summary["items"]), 2)

        items_by_curr = {it["currency"]: it for it in summary["items"]}
        self.assertEqual(items_by_curr["CNY"]["pnl_amount"], "100.00")
        self.assertEqual(items_by_curr["USD"]["pnl_amount"], "100.00")

    def test_overview_balance_sheet_and_freshness(self):

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Mock get_household
        mock_cur.fetchone.return_value = (
            self.household_id, "Test Household", "CNY", "active", 0, datetime.now(), datetime.now()
        )

        # Mock list_accounts
        cash_id = uuid4()
        cc_debt_id = uuid4()
        cc_overpay_id = uuid4()
        usd_savings_id = uuid4()

        # Mock list_accounts return
        with unittest.mock.patch("app.services.dashboard_service.list_accounts") as mock_list_accounts:
            mock_list_accounts.return_value = [
                {
                    "id": cash_id,
                    "name": "ICBC Debit",
                    "account_type": "cash",
                    "currency": "CNY",
                    "ledger_balance": Decimal("10000.00"),
                    "last_authoritative_snapshot_at": self.now_utc - timedelta(days=5)
                },
                {
                    "id": cc_debt_id,
                    "name": "CMB Credit",
                    "account_type": "credit",
                    "currency": "CNY",
                    "ledger_balance": Decimal("-3000.00"),
                    "last_authoritative_snapshot_at": self.now_utc - timedelta(days=40)
                },
                {
                    "id": cc_overpay_id,
                    "name": "BOC Credit Overpaid",
                    "account_type": "credit",
                    "currency": "CNY",
                    "ledger_balance": Decimal("500.00"),
                    "last_authoritative_snapshot_at": self.now_utc - timedelta(days=100)
                },
                {
                    "id": usd_savings_id,
                    "name": "Chase USD",
                    "account_type": "savings",
                    "currency": "USD",
                    "ledger_balance": Decimal("1000.00"), # 1000 USD * 7.20 = 7200 CNY
                    "last_authoritative_snapshot_at": None
                }
            ]

            overview = get_overview(
                conn=mock_conn,
                household_id=self.household_id,
                as_of_dt=self.now_utc,
                fx_service=self.mock_fx
            )

            # Assets: 10000 (CNY cash) + 500 (CC overpay) + 7200 (USD savings converted) = 17700.00
            # Liabilities: 3000 (CMB credit debt) = 3000.00
            # Net Worth: 17700 - 3000 = 14700.00
            self.assertEqual(overview["reporting_currency"], "CNY")
            self.assertEqual(overview["total_assets"], "17700.00")
            self.assertEqual(overview["total_liabilities"], "3000.00")
            self.assertEqual(overview["net_worth"], "14700.00")

            # Freshness:
            # Total accounts = 4
            # <= 30d: ICBC (5d) -> 1/4 = 0.2500
            # <= 90d: ICBC (5d), CMB (40d) -> 2/4 = 0.5000
            self.assertEqual(overview["data_freshness"]["confirmed_within_30d_ratio"], "0.2500")
            self.assertEqual(overview["data_freshness"]["confirmed_within_90d_ratio"], "0.5000")

    def test_cash_flow_formula_and_exclusions(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Mock get_household
        mock_cur.fetchone.return_value = (
            self.household_id, "Test Household", "CNY", "active", 0, datetime.now(), datetime.now()
        )

        from_date = date(2026, 8, 1)
        to_date = date(2026, 8, 31)

        # Frozen deterministic case:
        # ordinary expense = 1000 CNY
        # fee              =   20 CNY
        # refund           =  100 CNY
        # cash_income      = 5000 CNY
        # Excluded transactions: transfer, opening_balance, reconciliation_adjustment
        cat_shop_id = uuid4()
        mock_cur.fetchall.return_value = [
            (uuid4(), "expense", date(2026, 8, 5), Decimal("1000.00"), "CNY", None, None, Decimal("1000.00"), "CNY", Decimal("1000.00"), "CNY", cat_shop_id, "Shopping"),
            (uuid4(), "fee", date(2026, 8, 6), Decimal("20.00"), "CNY", None, None, Decimal("20.00"), "CNY", Decimal("20.00"), "CNY", None, None),
            (uuid4(), "refund", date(2026, 8, 10), None, None, Decimal("100.00"), "CNY", Decimal("100.00"), "CNY", Decimal("100.00"), "CNY", cat_shop_id, "Shopping"),
            (uuid4(), "cash_income", date(2026, 8, 15), None, None, Decimal("5000.00"), "CNY", Decimal("5000.00"), "CNY", Decimal("5000.00"), "CNY", None, None),
            (uuid4(), "transfer", date(2026, 8, 18), Decimal("2000.00"), "CNY", Decimal("2000.00"), "CNY", Decimal("2000.00"), "CNY", None, None, None, None),
            (uuid4(), "opening_balance", date(2026, 8, 1), None, None, Decimal("10000.00"), "CNY", Decimal("10000.00"), "CNY", None, None, None, None),
            (uuid4(), "reconciliation_adjustment", date(2026, 8, 20), Decimal("50.00"), "CNY", None, None, Decimal("50.00"), "CNY", None, None, None, None)
        ]

        cf = get_cash_flow(
            conn=mock_conn,
            household_id=self.household_id,
            from_date=from_date,
            to_date=to_date,
            fx_service=self.mock_fx
        )

        # Expected:
        # cash_income = 5000.00
        # expense = 1000 + 20 - 100 = 920.00
        # refund = 100.00
        # net_cash_flow = 5000 - 920 = 4080.00
        self.assertEqual(cf["cash_income"], "5000.00")
        self.assertEqual(cf["expense"], "920.00")
        self.assertEqual(cf["refund"], "100.00")
        self.assertEqual(cf["net_cash_flow"], "4080.00")
        self.assertEqual(cf["reporting_currency"], "CNY")

    def test_account_freshness_classification(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        mock_cur.fetchone.return_value = (
            self.household_id, "Test Household", "CNY", "active", 0, datetime.now(), datetime.now()
        )

        with unittest.mock.patch("app.services.dashboard_service.list_accounts") as mock_list_accounts:
            mock_list_accounts.return_value = [
                {
                    "id": uuid4(),
                    "name": "Fresh Acc",
                    "last_authoritative_snapshot_at": self.now_utc - timedelta(days=2)
                },
                {
                    "id": uuid4(),
                    "name": "Stale Acc",
                    "last_authoritative_snapshot_at": self.now_utc - timedelta(days=35)
                },
                {
                    "id": uuid4(),
                    "name": "Expired Acc",
                    "last_authoritative_snapshot_at": self.now_utc - timedelta(days=120)
                },
                {
                    "id": uuid4(),
                    "name": "Uninitialized Acc",
                    "last_authoritative_snapshot_at": None
                }
            ]

            freshness = get_account_freshness(
                conn=mock_conn,
                household_id=self.household_id,
                as_of_dt=self.now_utc
            )

            items = {item["account_name"]: item for item in freshness["items"]}
            self.assertEqual(items["Fresh Acc"]["freshness"], "fresh")
            self.assertEqual(items["Fresh Acc"]["age_days"], 2)

            self.assertEqual(items["Stale Acc"]["freshness"], "stale")
            self.assertEqual(items["Stale Acc"]["age_days"], 35)

            self.assertEqual(items["Expired Acc"]["freshness"], "expired")
            self.assertEqual(items["Expired Acc"]["age_days"], 120)

            self.assertEqual(items["Uninitialized Acc"]["freshness"], "expired")
            self.assertIsNone(items["Uninitialized Acc"]["age_days"])

if __name__ == "__main__":
    unittest.main()
