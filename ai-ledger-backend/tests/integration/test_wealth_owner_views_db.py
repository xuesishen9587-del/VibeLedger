"""Owner filtering over real household memberships, observations and FX quotes."""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4
from app.auth.context import AuthContext
from app.repositories import simplified_schema as schema, spending as repo
from app.services.balance_service import head_fields
from tests.integration import test_s2_spending_db as spending_setup
from tests.support.db_helper import BaseDbTestCase


class TestWealthOwnerViewsDb(BaseDbTestCase):
    def seed_test_data(self):
        spending_setup.TestS2SpendingDb.seed_test_data(self)
        self.second, self.empty, self.foreign = uuid4(), uuid4(), uuid4()
        for user in (self.second, self.empty, self.foreign):
            schema.create_user(self.conn, str(user), f"{user}@example.test", "Member", user_id=user)
        schema.add_household_member(self.conn, self.hh, self.second, "member")
        schema.add_household_member(self.conn, self.hh, self.empty, "member")
        foreign_hh = schema.create_household(self.conn, "Other family")["id"]
        schema.add_household_member(self.conn, foreign_hh, self.foreign, "owner")
        self.accounts = []
        self.add_account("Wallet", self.user, "100.10", risk="low")
        self.add_account("Debt", self.user, "-40.05", kind="credit")
        self.add_account("Savings", self.second, "200.20", risk="high")
        self.add_account("Surplus", self.second, "25.05", kind="credit")
        self.add_account("Unassigned", None, "30.00", currency="USD", risk="medium")
        schema.create_account(self.conn, foreign_hh, "Private account", "private", "cash", "CNY",
                              owner_user_id=self.foreign, opened_on=date(2026, 1, 1))
        repo.rows(self.conn, "INSERT INTO fx_quotes(from_currency,to_currency,rate_as_of,rate,source) "
                  "VALUES ('USD','CNY','2026-02-02',7.123456,'fixture') RETURNING rate")
        self.conn.commit()
        clock = patch("app.services.wealth_reports.datetime", wraps=datetime)
        clock.start().now.return_value = datetime(2026, 2, 3, tzinfo=timezone.utc)
        self.addCleanup(clock.stop)

    def add_account(self, name, owner, balance, *, kind="savings", currency="CNY", risk=None):
        account = schema.create_account(self.conn, self.hh, name, name, kind, currency,
            risk_level=risk, owner_user_id=owner, opened_on=date(2026, 1, 1))
        self.conn.commit()
        self.accounts.append(account)
        if balance is not None:
            heads = head_fields(self.conn, self.hh, account["id"])
            self.conn.commit()
            saved = self.client.post("/api/v1/balance-updates", headers={"Idempotency-Key": str(uuid4())},
                json={"observations": [{"account_id": str(account["id"]), "balance": balance, "currency": currency,
                    "as_of": "2026-02-02T12:00:00+08:00", "time_basis": "explicit", **heads}]})
            self.assertEqual(saved.status_code, 201, saved.text)
        return account

    def report(self, owner=None, **params):
        response = self.client.get("/api/v1/reports/wealth", params={**params, **({"owner": str(owner)} if owner is not None else {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_household_default_and_all_groups_reconcile_without_double_counting(self):
        family = self.report()
        groups = [self.report(owner) for owner in (self.user, self.second, "unassigned")]
        self.assertEqual(family["total_assets"], "539.05")
        self.assertEqual(family["total_liabilities"], "40.05")
        self.assertEqual(family["net_worth"], "499.00")
        for key in ("total_assets", "total_liabilities", "net_worth"):
            self.assertEqual(sum(Decimal(g[key]) for g in groups), Decimal(family[key]))
        self.assertEqual(len({a["account_id"] for g in groups for a in g["accounts"]}), 5)
        self.assertNotIn("Private account", str(family))

    def test_member_permissions_remain_shared_and_risk_uses_positive_assets(self):
        self.actor = AuthContext("browser", self.user, self.hh, "member")
        report = self.report(self.second)
        self.assertEqual(report["net_worth"], "225.25")
        buckets = {r["risk_level"]: r for r in report["risk_buckets"]}
        self.assertEqual(buckets["high"]["percentage"], "88.88")
        self.assertEqual(buckets["unclassified"]["percentage"], "11.12")
        self.assertEqual(self.report(self.user)["total_liabilities"], "40.05")

    def test_missing_balance_only_marks_its_owner_incomplete(self):
        missing = self.add_account("No observation", self.second, None)
        group = self.report(self.second)
        self.assertIsNone(group["net_worth"])
        self.assertEqual(group["known_net_worth"], "225.25")
        self.assertEqual(group["coverage"]["missing_account_ids"], [str(missing["id"])])
        self.assertTrue(self.report(self.user)["coverage"]["complete"])
        self.assertIsNone(self.report()["net_worth"])

    def test_missing_fx_keeps_native_balance_and_known_subtotal(self):
        repo.rows(self.conn, "DELETE FROM fx_quotes WHERE source='fixture' RETURNING rate")
        self.conn.commit()
        group = self.report("unassigned")
        self.assertEqual(group["accounts"][0]["balance"], "30.00")
        self.assertIsNone(group["accounts"][0]["converted_amount"])
        self.assertIsNone(group["net_worth"])
        self.assertEqual(group["known_net_worth"], "0.00")
        self.assertEqual(group["coverage"]["missing_fx_currencies"], ["USD"])
        self.assertEqual(self.report()["known_net_worth"], "285.30")
        self.assertTrue(self.report(self.user)["coverage"]["complete"])

    def test_empty_member_group_is_not_family_initialization(self):
        group = self.report(self.empty)
        self.assertEqual(group["accounts"], [])
        self.assertFalse(group["setup_required"])
        self.assertIsNone(group["net_worth"])
        self.assertFalse(group["coverage"]["complete"])
        self.assertTrue(all(r["percentage"] is None for r in group["risk_buckets"]))

    def test_empty_family_keeps_the_existing_initialization_contract(self):
        household = schema.create_household(self.conn, "Empty family")["id"]
        user = uuid4()
        schema.create_user(self.conn, str(user), f"{user}@example.test", "New member", user_id=user)
        schema.add_household_member(self.conn, household, user, "owner")
        self.conn.commit()
        self.actor = AuthContext("browser", user, household, "owner")
        report = self.report()
        self.assertTrue(report["setup_required"])
        self.assertEqual(report["accounts"], [])
        self.assertFalse(report["coverage"]["complete"])
        self.assertIsNone(report["net_worth"])

    def test_empty_unassigned_group_does_not_hide_the_family_accounts(self):
        repo.rows(self.conn, "UPDATE accounts SET owner_user_id=%s WHERE household_id=%s "
                  "AND owner_user_id IS NULL RETURNING id", (self.user, self.hh))
        self.conn.commit()
        self.assertEqual(self.report("unassigned")["accounts"], [])
        self.assertFalse(self.report("unassigned")["setup_required"])
        self.assertEqual(len(self.report()["accounts"]), 5)

    def test_future_opened_accounts_stay_outside_the_selected_view(self):
        # A future-opened account is outside the report even if it has this owner.
        schema.create_account(self.conn, self.hh, "Future account", "future", "cash", "CNY",
                              owner_user_id=self.user, opened_on=date(2026, 2, 4))
        self.conn.commit()
        report = self.report(self.user)
        self.assertEqual([a["name"] for a in report["accounts"]], [a["name"] for a in sorted(self.accounts[:2], key=lambda a: a["id"])])
        self.assertEqual(report["net_worth"], "60.05")

    def test_invalid_unknown_and_foreign_members_do_not_expose_membership(self):
        responses = [self.client.get("/api/v1/reports/wealth", params={"owner": owner})
                     for owner in ("invalid", str(uuid4()), str(self.foreign))]
        for response in responses:
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "INVALID_OWNER")
        self.assertEqual(responses[1].json(), responses[2].json())
        self.assertNotIn("Private account", responses[2].text)

    def test_disabled_member_remains_a_valid_ownership_view(self):
        repo.rows(self.conn, "UPDATE users SET status='disabled' WHERE id=%s RETURNING id", (self.second,))
        self.conn.commit()
        self.assertEqual(self.report(self.second)["net_worth"], "225.25")

    def test_current_owner_changes_do_not_write_or_filter_family_history(self):
        def history():
            response = self.client.get("/api/v1/reports/wealth-history", params={"from": "2026-02-01", "to": "2026-02-03"})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()
        before = history()
        audit_count = repo.rows(self.conn, "SELECT count(*) n FROM audit_events")[0]["n"]
        snapshots = repo.rows(self.conn, "SELECT * FROM account_snapshots ORDER BY id")
        repo.rows(self.conn, "UPDATE accounts SET owner_user_id=%s WHERE id=%s RETURNING id", (self.second, self.accounts[0]["id"]))
        self.conn.commit()
        self.assertEqual(self.report(self.user)["net_worth"], "-40.05")
        self.assertEqual(self.report(self.second)["net_worth"], "325.35")
        self.assertEqual(history(), before)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM audit_events")[0]["n"], audit_count)
        self.assertEqual(repo.rows(self.conn, "SELECT * FROM account_snapshots ORDER BY id"), snapshots)
