"""Read-only balance recommendations from linked, same-currency committed transactions."""
from datetime import date, datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from app.api.deps import get_auth_context
from app.auth.context import AuthContext
from app.repositories import simplified_schema as schema, spending as repo
from app.services import balance_estimates
from app.services.balance_service import head_fields
from tests.integration import test_s2_spending_db as spending_setup
from tests.support.db_helper import BaseDbTestCase


class TestBalanceEstimatesDb(BaseDbTestCase):
    def seed_test_data(self):
        spending_setup.TestS2SpendingDb.seed_test_data(self)
        self.bank = schema.create_account(self.conn, self.hh, "Bank", "savings", "savings", "CNY",
                                          opened_on=date(2026, 1, 1), owner_user_id=self.user)
        self.credit = schema.create_account(self.conn, self.hh, "Credit", "debt", "credit", "CNY",
                                            opened_on=date(2026, 1, 1))
        self.investment = schema.create_account(self.conn, self.hh, "Investments", "holdings", "investment", "CNY",
                                                opened_on=date(2026, 1, 1))
        self.unknown = schema.create_account(self.conn, self.hh, "No starting balance", "cash", "cash", "CNY",
                                             opened_on=date(2026, 1, 1))
        self.income_category = schema.create_category(self.conn, self.hh, "Income", "income")["id"]
        self.conn.commit()

    def baseline(self, account, balance="100.00", when="2026-02-02T12:00:00+08:00"):
        payload = {"observations": [{"account_id": str(account["id"]), "balance": balance,
                   "currency": account["currency"], "as_of": when,
                   "time_basis": "explicit", **head_fields(self.conn, self.hh, account["id"])}]}
        result = self.client.post("/api/v1/balance-updates", json=payload,
                                  headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["snapshots"][0]["id"]

    def transaction(self, account, amount, when, kind="expense", currency="CNY"):
        payload = {"transaction_type": kind, "occurred_on": when, "original_amount": amount,
                   "original_currency": currency, "category_id": str(self.income_category if kind == "cash_income" else self.category),
                   "account_id": str(account["id"]) if account else None, "merchant": "Fixture",
                   **({"remarks": "Refund"} if kind == "refund" else {})}
        result = self.client.post("/api/v1/transactions", json=payload,
                                  headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def items(self):
        response = self.client.get("/api/v1/reports/balance-estimates")
        self.assertEqual(response.status_code, 200, response.text)
        return {r["account_id"]: r for r in response.json()["items"]}

    def test_100_minus_10_minus_5_recommends_85_without_creating_balance(self):
        baseline_id = self.baseline(self.bank)
        self.transaction(self.bank, "10.00", "2026-02-03")
        self.transaction(self.bank, "5.00", "2026-02-04")
        self.transaction(None, "100.00", "2026-02-05")
        audit = repo.rows(self.conn, "SELECT count(*) n FROM audit_events")[0]["n"]
        observations = repo.rows(self.conn, "SELECT count(*) n FROM account_snapshots")[0]["n"]
        row = self.items()[str(self.bank["id"])]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["snapshot_id"], baseline_id)
        self.assertEqual(row["baseline_balance"], "100.00")
        self.assertEqual(row["transaction_count"], 2)
        self.assertEqual(row["recorded_change"], "-15.00")
        self.assertEqual(row["estimated_balance"], "85.00")
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM audit_events")[0]["n"], audit)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM account_snapshots")[0]["n"], observations)
        self.assertEqual(self.client.get("/api/v1/reports/wealth").json()["known_net_worth"], "100.00")

    def test_refund_and_cash_income_restore_balance_but_unlinked_transactions_do_not(self):
        self.baseline(self.bank)
        self.transaction(self.bank, "10.00", "2026-02-03")
        self.transaction(self.bank, "2.00", "2026-02-04", "refund")
        self.transaction(self.bank, "3.00", "2026-02-05", "cash_income")
        self.transaction(None, "40.00", "2026-02-06")
        row = self.items()[str(self.bank["id"])]
        self.assertEqual(row["recorded_change"], "-5.00")
        self.assertEqual(row["estimated_balance"], "95.00")
        self.assertEqual(row["transaction_count"], 3)

    def test_credit_balance_is_signed_and_no_cash_account_snapshots_remain_unavailable(self):
        self.baseline(self.credit, "-100.00")
        self.transaction(self.credit, "10.00", "2026-02-03")
        self.transaction(self.credit, "5.00", "2026-02-04", "refund")
        rows = self.items()
        self.assertEqual(rows[str(self.credit["id"])]["estimated_balance"], "-105.00")
        self.assertEqual(rows[str(self.unknown["id"])]["status"], "no_snapshot")
        self.assertIsNone(rows[str(self.unknown["id"])]["estimated_balance"])

    def test_snapshot_day_transaction_cannot_be_ordered_against_snapshot(self):
        self.baseline(self.bank)
        self.transaction(self.bank, "10.00", "2026-02-02")
        self.transaction(self.bank, "5.00", "2026-02-03")
        row = self.items()[str(self.bank["id"])]
        self.assertEqual(row["status"], "ambiguous_same_day")
        self.assertEqual(row["same_day_count"], 1)
        self.assertIsNone(row["estimated_balance"])
        self.assertIsNone(row["recorded_change"])

    def test_cross_currency_is_not_silently_subtracted(self):
        self.baseline(self.bank)
        self.transaction(self.bank, "5.00", "2026-02-03", currency="USD")
        row = self.items()[str(self.bank["id"])]
        self.assertEqual(row["status"], "currency_mismatch")
        self.assertEqual(row["currency_mismatch_count"], 1)
        self.assertIsNone(row["estimated_balance"])

    def test_investment_cannot_be_inferred_from_cash_spending(self):
        self.baseline(self.investment)
        self.transaction(self.investment, "10.00", "2026-02-03")
        row = self.items()[str(self.investment["id"])]
        self.assertEqual(row["status"], "unsupported_account")
        self.assertIsNone(row["estimated_balance"])

    def test_negative_cash_and_credit_surplus_keep_their_signs(self):
        self.baseline(self.bank, "0.00")
        self.baseline(self.credit, "20.00")
        self.transaction(self.bank, "15.00", "2026-02-03")
        self.transaction(self.credit, "10.00", "2026-02-03")
        self.transaction(self.credit, "2.00", "2026-02-04", "refund")
        self.transaction(self.credit, "3.00", "2026-02-05", "cash_income")
        rows = self.items()
        self.assertEqual(rows[str(self.bank["id"])]["estimated_balance"], "-15.00")
        self.assertEqual(rows[str(self.credit["id"])]["estimated_balance"], "15.00")

    def test_zero_minor_unit_currency_needs_no_fx_and_no_transactions_is_unchanged(self):
        yen = schema.create_account(self.conn, self.hh, "Yen", "yen", "credit", "JPY",
                                    opened_on=date(2026, 1, 1))
        self.conn.commit()
        self.baseline(yen, "0")
        row = self.items()[str(yen["id"])]
        self.assertEqual(row["estimated_balance"], "0")
        self.assertEqual(row["recorded_change"], "0")
        self.assertEqual(row["transaction_count"], 0)

    def test_voided_older_and_other_account_transactions_do_not_change_estimate(self):
        self.baseline(self.bank)
        self.transaction(self.bank, "40.00", "2026-02-01")
        voided = self.transaction(self.bank, "20.00", "2026-02-03")
        response = self.client.post(f"/api/v1/transactions/{voided['id']}/void",
            json={"expected_version": 0, "delete_reason": "Wrong entry"},
            headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        self.transaction(self.credit, "30.00", "2026-02-03")
        self.transaction(self.bank, "10.00", "2026-02-04")
        row = self.items()[str(self.bank["id"])]
        self.assertEqual(row["estimated_balance"], "90.00")
        self.assertEqual(row["transaction_count"], 1)

    def test_latest_correction_replaces_baseline_and_stale_recommendation_cannot_be_saved(self):
        old = self.baseline(self.bank)
        self.transaction(self.bank, "10.00", "2026-02-03")
        before = self.items()[str(self.bank["id"])]
        corrected = self.client.post(f"/api/v1/snapshots/{old}/correct", json={
            "expected_version": 0, **head_fields(self.conn, self.hh, self.bank["id"]),
            "reason": "Bank amount corrected", "balance": "200.00", "currency": "CNY",
            "as_of": "2026-02-02T12:00:00+08:00", "time_basis": "explicit"},
            headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(corrected.status_code, 200, corrected.text)
        row = self.items()[str(self.bank["id"])]
        self.assertEqual(row["snapshot_id"], corrected.json()["id"])
        self.assertEqual(row["estimated_balance"], "190.00")
        rejected = self.client.post("/api/v1/balance-updates", json={"observations": [{
            "account_id": str(self.bank["id"]), "balance": before["estimated_balance"],
            "currency": "CNY", "as_of": "2026-02-04T12:00:00+08:00", "time_basis": "explicit",
            "expected_account_version": 0, "expected_latest_snapshot_id": old}]},
            headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(rejected.status_code, 409, rejected.text)

    def test_voided_latest_snapshot_falls_back_to_previous_valid_baseline(self):
        old = self.baseline(self.bank)
        latest = self.baseline(self.bank, "120.00", when="2026-02-04T12:00:00+08:00")
        self.transaction(self.bank, "10.00", "2026-02-03")
        self.assertEqual(self.items()[str(self.bank["id"])]["estimated_balance"], "120.00")
        response = self.client.post(f"/api/v1/snapshots/{latest}/void", json={
            "expected_version": 0, **head_fields(self.conn, self.hh, self.bank["id"]),
            "reason": "Wrong observation"}, headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        row = self.items()[str(self.bank["id"])]
        self.assertEqual(row["snapshot_id"], old)
        self.assertEqual(row["estimated_balance"], "90.00")

    def test_household_local_day_is_used_for_baseline_ambiguity_and_today_cutoff(self):
        self.baseline(self.bank, when="2026-02-02T20:00:00Z")  # Feb 3 in household timezone
        self.transaction(self.bank, "10.00", "2026-02-03")
        self.transaction(self.bank, "5.00", "2026-02-04")
        with patch.object(balance_estimates, "datetime") as clock:
            clock.now.return_value = datetime(2026, 2, 3, 16, 30, tzinfo=timezone.utc)
            row = self.items()[str(self.bank["id"])]
        self.assertEqual(row["baseline_date"], "2026-02-03")
        self.assertEqual(row["same_day_count"], 1)
        self.assertEqual(row["transaction_count"], 1)  # Feb 4, although UTC is still Feb 3
        self.assertEqual(row["status"], "ambiguous_same_day")

    def test_member_sees_shared_accounts_but_cannot_choose_a_foreign_household(self):
        member = schema.create_user(self.conn, str(uuid4()), "member@example.test", "Member")
        schema.add_household_member(self.conn, self.hh, member["id"], "member")
        foreign = schema.create_household(self.conn, "Foreign")
        foreign_account = schema.create_account(self.conn, foreign["id"], "Foreign cash", "cash", "cash", "CNY",
                                                opened_on=date(2026, 1, 1))
        self.conn.commit()
        self.baseline(self.bank)
        owner = self.actor
        self.actor = AuthContext("browser", member["id"], self.hh, "member")
        self.assertEqual(self.items()[str(self.bank["id"])]["estimated_balance"], "100.00")
        response = self.client.get("/api/v1/reports/balance-estimates", params={"household_id": str(foreign["id"])})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(str(foreign_account["id"]), {r["account_id"] for r in response.json()["items"]})
        self.actor = AuthContext("browser", owner.user_id, foreign["id"], "owner")
        self.assertEqual(set(self.items()), {str(foreign_account["id"])})

    def test_no_accounts_or_snapshots_returns_safe_empty_values(self):
        self.assertTrue(all(r["estimated_balance"] is None for r in self.items().values()))
        empty = schema.create_household(self.conn, "Empty")
        self.conn.commit()
        self.actor = AuthContext("browser", self.user, empty["id"], "owner")
        self.assertEqual(self.items(), {})

    def test_unauthenticated_request_is_rejected(self):
        del self.client.app.dependency_overrides[get_auth_context]
        response = self.client.get("/api/v1/reports/balance-estimates")
        self.assertEqual(response.status_code, 401, response.text)

    def test_query_count_and_result_size_are_bounded_by_accounts_not_transactions(self):
        self.baseline(self.bank)
        self.baseline(self.credit, "-100.00", when="2026-02-04T12:00:00+08:00")
        for _ in range(15):
            self.transaction(self.bank, "1.00", "2026-02-03")
        self.transaction(self.credit, "2.00", "2026-02-03")  # before its own baseline
        real_rows = repo.rows
        queries = []
        def observed(conn, sql, params=()):
            rows = real_rows(conn, sql, params)
            queries.append((sql, len(rows)))
            return rows
        with patch.object(balance_estimates.repo, "rows", side_effect=observed):
            result = balance_estimates.report(self.conn, self.hh)
        self.assertEqual(len(queries), 3)
        self.assertTrue(all(sql.lstrip().startswith(("SELECT", "WITH")) for sql, _ in queries))
        self.assertLessEqual(queries[-1][1], 2)
        rows = {r["account_id"]: r for r in result["items"]}
        self.assertEqual(rows[str(self.bank["id"])]["estimated_balance"], "85.00")
        self.assertEqual(rows[str(self.credit["id"])]["estimated_balance"], "-100.00")
