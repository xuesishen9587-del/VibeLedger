"""Read-only balance recommendations from linked, same-currency committed transactions."""
from datetime import date
from uuid import uuid4

from app.repositories import simplified_schema as schema, spending as repo
from app.services.balance_service import head_fields
from tests.integration import test_s2_spending_db as spending_setup
from tests.support.db_helper import BaseDbTestCase


class TestBalanceEstimatesDb(BaseDbTestCase):
    def seed_test_data(self):
        spending_setup.TestS2SpendingDb.seed_test_data(self)
        self.bank = schema.create_account(self.conn, self.hh, "Bank", "savings", "savings", "CNY",
                                          opened_on=date(2026, 1, 1))
        self.credit = schema.create_account(self.conn, self.hh, "Credit", "debt", "credit", "CNY",
                                            opened_on=date(2026, 1, 1))
        self.investment = schema.create_account(self.conn, self.hh, "Investments", "holdings", "investment", "CNY",
                                                opened_on=date(2026, 1, 1))
        self.unknown = schema.create_account(self.conn, self.hh, "No starting balance", "cash", "cash", "CNY",
                                             opened_on=date(2026, 1, 1))
        self.income_category = schema.create_category(self.conn, self.hh, "Income", "income")["id"]
        self.conn.commit()

    def baseline(self, account, balance="100.00"):
        payload = {"observations": [{"account_id": str(account["id"]), "balance": balance,
                   "currency": "CNY", "as_of": "2026-02-02T12:00:00+08:00",
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
