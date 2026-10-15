"""Deterministic current-ownership views retain the household wealth rules."""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import TestCase
from unittest.mock import MagicMock, patch
from uuid import uuid4
from fastapi.testclient import TestClient
from app.api.deps import get_auth_context, get_db_connection
from app.auth.context import AuthContext
from app.main import create_app
from app.services import wealth_reports


class TestWealthOwnerViews(TestCase):
    def setUp(self):
        self.hh, self.first, self.second, self.empty = (uuid4() for _ in range(4))
        self.at = datetime(2026, 2, 3, tzinfo=timezone.utc)
        self.accounts, self.snapshots = [], []
        self.add_account("Wallet", self.first, "100.10", risk="low")
        self.add_account("Debt", self.first, "-40.05", kind="credit")
        self.add_account("Savings", self.second, "200.20", risk="high")
        self.add_account("Surplus", self.second, "25.05", kind="credit")
        self.add_account("Unassigned", None, "30.00", currency="USD", risk="medium")
        self.quotes = [{"rate": Decimal("7.123456"), "rate_as_of": date(2026, 2, 2)}]
        self.conn = MagicMock()
        self.household = {"timezone": "Asia/Singapore", "reporting_currency": "CNY"}
        self.members = [{"user_id": user} for user in (self.first, self.second, self.empty)]
        self.start_patch("schema.get_household", return_value=self.household)
        self.start_patch("schema.list_household_members", side_effect=lambda *args: self.members)
        self.query = self.start_patch("repo.rows", side_effect=self.rows)
        clock = self.start_patch("datetime", wraps=datetime)
        clock.now.return_value = self.at
        app = create_app()
        app.dependency_overrides[get_db_connection] = lambda: self.conn
        app.dependency_overrides[get_auth_context] = lambda: AuthContext("browser", self.first, self.hh, "member")
        self.client = TestClient(app)

    def start_patch(self, name, **kwargs):
        mocked = patch("app.services.wealth_reports." + name, **kwargs)
        self.addCleanup(mocked.stop)
        return mocked.start()

    def add_account(self, name, owner, amount, *, kind="savings", currency="CNY", risk=None):
        account = {"id": uuid4(), "name": name, "owner_user_id": owner, "account_type": kind,
            "currency": currency, "risk_level": risk, "balance_scope": name, "row_version": 0}
        self.accounts.append(account)
        if amount is not None:
            self.snapshots.append({"id": uuid4(), "account_id": account["id"], "balance": Decimal(amount),
                "currency": currency, "as_of": datetime(2026, 2, 2, tzinfo=timezone.utc), "time_basis": "explicit"})
        return account

    def rows(self, conn, sql, params):
        if "FROM accounts " in sql:
            self.assertEqual(params[0], self.hh)
            return self.accounts
        if "FROM account_snapshots " in sql:
            self.assertEqual(params[0], self.hh)
            return self.snapshots
        if "FROM fx_quotes " in sql:
            return self.quotes
        raise AssertionError(sql)

    def report(self, owner=None, **params):
        response = self.client.get("/api/v1/reports/wealth", params={**params, **({"owner": str(owner)} if owner is not None else {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_default_household_keeps_totals_and_positive_asset_risk_basis(self):
        report = self.report()
        self.assertEqual(report["total_assets"], "539.05")
        self.assertEqual(report["total_liabilities"], "40.05")
        self.assertEqual(report["net_worth"], "499.00")
        self.assertEqual(len(report["accounts"]), 5)
        self.assertTrue(report["coverage"]["complete"])
        self.assertFalse(report["setup_required"])
        self.assertEqual({r["risk_level"]: r["percentage"] for r in report["risk_buckets"]},
                         {"very_low": "0.00", "low": "18.57", "medium": "39.64", "high": "37.14", "unclassified": "4.65"})

    def test_members_keep_shared_access_but_recompute_totals_and_risk(self):
        first, second = self.report(self.first), self.report(self.second)
        self.assertEqual(first["net_worth"], "60.05")
        self.assertEqual([a["name"] for a in first["accounts"]], ["Wallet", "Debt"])
        self.assertEqual(second["total_assets"], "225.25")
        self.assertEqual(second["net_worth"], "225.25")
        self.assertEqual({r["risk_level"]: r["percentage"] for r in second["risk_buckets"] if r["amount"] != "0.00"},
                         {"high": "88.88", "unclassified": "11.12"})

    def test_unassigned_participates_and_conversion_rounds_half_up(self):
        report = self.report("unassigned")
        self.assertEqual([a["name"] for a in report["accounts"]], ["Unassigned"])
        self.assertEqual(report["accounts"][0]["balance"], "30.00")
        self.assertEqual(report["net_worth"], "213.70")
        self.quotes[0]["rate"] = Decimal("7.1235")
        self.assertEqual(self.report("unassigned")["net_worth"], "213.71")

    def test_missing_balance_affects_only_its_group(self):
        missing = self.add_account("Not observed", self.second, None)
        report = self.report(self.second)
        self.assertIsNone(report["total_assets"])
        self.assertIsNone(report["net_worth"])
        self.assertEqual(report["known_assets"], "225.25")
        self.assertEqual(report["coverage"]["missing_account_ids"], [str(missing["id"])])
        self.assertIsNone(report["accounts"][-1]["balance"])
        self.assertTrue(self.report(self.first)["coverage"]["complete"])

    def test_missing_fx_preserves_native_amount_and_never_assumes_zero_or_one(self):
        self.quotes.clear()
        report = self.report("unassigned")
        self.assertEqual(report["known_assets"], "0.00")
        self.assertIsNone(report["total_assets"])
        self.assertIsNone(report["net_worth"])
        self.assertEqual(report["accounts"][0]["balance"], "30.00")
        self.assertIsNone(report["accounts"][0]["converted_amount"])
        self.assertEqual(report["coverage"]["missing_fx_currencies"], ["USD"])
        self.assertTrue(all(r["percentage"] is None for r in report["risk_buckets"]))
        self.assertTrue(self.report(self.first)["coverage"]["complete"])

    def test_empty_group_is_not_an_uninitialized_family_or_verified_zero(self):
        for owner in (self.empty, "unassigned"):
            if owner == "unassigned":
                self.accounts[-1]["owner_user_id"] = self.first
            report = self.report(owner)
            self.assertEqual(report["accounts"], [])
            self.assertFalse(report["setup_required"])
            self.assertFalse(report["coverage"]["complete"])
            self.assertEqual(report["coverage"]["missing_account_ids"], [])
            self.assertIsNone(report["net_worth"])
            self.assertTrue(all(r["percentage"] is None for r in report["risk_buckets"]))
        self.accounts.clear()
        self.assertTrue(self.report()["setup_required"])

    def test_invalid_and_nonmember_uuid_have_safe_equivalent_errors(self):
        for owner in ("not-a-uuid", "", str(uuid4()), str(uuid4())):
            response = self.client.get("/api/v1/reports/wealth", params={"owner": owner})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "INVALID_OWNER")

    def test_zero_and_debt_only_have_no_positive_asset_percentages(self):
        self.snapshots[0]["balance"] = Decimal("0.00")
        report = self.report(self.first)
        self.assertEqual(report["net_worth"], "-40.05")
        self.assertEqual(report["total_assets"], "0.00")
        self.assertTrue(all(r["percentage"] is None for r in report["risk_buckets"]))

    def test_stale_observations_remain_included_and_history_keeps_fx_cutoff(self):
        self.snapshots[0]["as_of"] = datetime(2025, 10, 1, tzinfo=timezone.utc)
        report = self.report(self.first)
        self.assertEqual(report["net_worth"], "60.05")
        self.assertEqual(report["coverage"]["stale_account_ids"], [str(self.accounts[0]["id"])])
        self.assertTrue(report["accounts"][0]["very_stale"])
        self.quotes[0]["rate_as_of"] = date(2026, 1, 1)
        self.assertEqual(self.report("unassigned")["net_worth"], "213.70")
        historical = self.report("unassigned", as_of="2026-02-02")
        self.assertIsNone(historical["net_worth"])
        self.assertEqual(historical["risk_buckets"], [])

