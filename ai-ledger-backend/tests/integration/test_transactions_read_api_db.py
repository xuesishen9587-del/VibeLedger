import unittest
from uuid import UUID, uuid4
import hashlib
from decimal import Decimal
from datetime import date, datetime, timezone
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo
from app.repositories import transactions as tx_repo
from app.repositories import devices as devices_repo

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

class TestTransactionsReadApiDb(BaseDbTestCase):
    @classmethod
    def cls_setup(cls):
        cls.app = create_app()
        cls.client = TestClient(cls.app)

        def _get_db():
            conn = get_connection(cls.test_schema)
            try:
                yield conn
            finally:
                if not conn.closed:
                    conn.close()
        cls.app.dependency_overrides[get_db_connection] = _get_db

    def seed_test_data(self):
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.raw_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_token.encode("utf-8")).digest()
        self.headers = {"Authorization": f"Bearer {self.raw_token}"}

        # Household B
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_test_{uuid4().hex}"
        self.token_b_hash = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_b = {"Authorization": f"Bearer {self.raw_token_b}"}

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_household(conn, self.household_id, "Household A", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_tx_a", "User A", "user_a@tx.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "Device A", self.token_hash)

                accounts_repo.create_household(conn, self.household_b_id, "Household B", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_b_id, "auth_tx_b", "User B", "user_b@tx.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "Device B", self.token_b_hash)



                # Seed accounts
                self.account_cash_id = uuid4()
                accounts_repo.create_account(
                    conn, self.account_cash_id, self.household_id, "ICBC Debit", "cash", "CNY"
                )
                self.account_credit_id = uuid4()
                accounts_repo.create_account(
                    conn, self.account_credit_id, self.household_id, "CMB Visa", "credit", "USD", billing_day=5, due_day=25
                )

                # Seed categories
                self.cat_food_id = uuid4()
                categories_repo.create_category(conn, self.cat_food_id, self.household_id, "Food", "expense")
                self.cat_salary_id = uuid4()
                categories_repo.create_category(conn, self.cat_salary_id, self.household_id, "Salary", "income")

                # Seed 4 Transactions
                self.tx1_id = uuid4() # expense on Aug 10
                tx_repo.create_transaction(
                    conn, self.tx1_id, self.household_id, "expense", date(2026, 8, 10),
                    original_amount=Decimal("268.00"), original_currency="CNY",
                    from_amount=Decimal("268.00"), from_currency="CNY",
                    from_account_id=self.account_cash_id, category_id=self.cat_food_id,
                    merchant="JD.com", status="committed", verification_status="statement_confirmed",
                    reporting_amount=Decimal("268.00"), reporting_currency="CNY"
                )

                self.tx2_id = uuid4() # USD expense on Aug 15
                tx_repo.create_transaction(
                    conn, self.tx2_id, self.household_id, "expense", date(2026, 8, 15),
                    original_amount=Decimal("50.00"), original_currency="USD",
                    from_amount=Decimal("50.00"), from_currency="USD",
                    from_account_id=self.account_credit_id, category_id=self.cat_food_id,
                    merchant="Amazon US", status="committed", verification_status="unverified",
                    effective_fx_rate=Decimal("7.200000000000"),
                    reporting_amount=Decimal("360.00"), reporting_currency="CNY"
                )

                self.tx3_id = uuid4() # salary income on Aug 20
                tx_repo.create_transaction(
                    conn, self.tx3_id, self.household_id, "cash_income", date(2026, 8, 20),
                    original_amount=Decimal("20000.00"), original_currency="CNY",
                    to_amount=Decimal("20000.00"), to_currency="CNY",
                    to_account_id=self.account_cash_id, category_id=self.cat_salary_id,
                    merchant="Employer", status="committed", verification_status="statement_confirmed",
                    reporting_amount=Decimal("20000.00"), reporting_currency="CNY"
                )

                self.tx4_id = uuid4() # transfer on Aug 25
                tx_repo.create_transaction(
                    conn, self.tx4_id, self.household_id, "transfer", date(2026, 8, 25),
                    original_amount=Decimal("1000.00"), original_currency="CNY",
                    from_amount=Decimal("1000.00"), from_currency="CNY",
                    to_amount=Decimal("1000.00"), to_currency="CNY",
                    from_account_id=self.account_cash_id, to_account_id=self.account_credit_id,
                    status="committed", verification_status="unverified"
                )

                # Link refund to tx1
                self.tx_refund_id = uuid4()
                tx_repo.create_transaction(
                    conn, self.tx_refund_id, self.household_id, "refund", date(2026, 8, 12),
                    original_amount=Decimal("50.00"), original_currency="CNY",
                    to_amount=Decimal("50.00"), to_currency="CNY",
                    to_account_id=self.account_cash_id, category_id=self.cat_food_id,
                    status="committed"
                )
                tx_repo.create_transaction_link(
                    conn, uuid4(), self.tx_refund_id, self.tx1_id, "refund_of"
                )
        finally:
            conn.close()

    def test_list_transactions_filters(self):
        # 1. Filter by date range (Aug 14 to Aug 21) -> should return tx2 and tx3
        res = self.client.get("/api/v1/transactions?from=2026-08-14&to=2026-08-21", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        tx_ids = [it["id"] for it in items]
        self.assertEqual(len(tx_ids), 2)
        self.assertIn(str(self.tx2_id), tx_ids)
        self.assertIn(str(self.tx3_id), tx_ids)

        # 2. Filter by account_id (matching from or to) -> account_credit_id is in tx2 and tx4
        res_acc = self.client.get(f"/api/v1/transactions?account_id={self.account_credit_id}", headers=self.headers)
        self.assertEqual(res_acc.status_code, 200)
        items_acc = res_acc.json()["items"]
        tx_ids_acc = [it["id"] for it in items_acc]
        self.assertEqual(len(tx_ids_acc), 2)
        self.assertIn(str(self.tx2_id), tx_ids_acc)
        self.assertIn(str(self.tx4_id), tx_ids_acc)

        # 3. Filter by transaction_type=expense -> tx1 and tx2
        res_type = self.client.get("/api/v1/transactions?transaction_type=expense", headers=self.headers)
        self.assertEqual(res_type.status_code, 200)
        items_type = res_type.json()["items"]
        self.assertEqual(len(items_type), 2)
        for it in items_type:
            self.assertEqual(it["transaction_type"], "expense")

        # 4. Filter by currency=USD -> tx2
        res_curr = self.client.get("/api/v1/transactions?currency=USD", headers=self.headers)
        self.assertEqual(res_curr.status_code, 200)
        items_curr = res_curr.json()["items"]
        self.assertEqual(len(items_curr), 1)
        self.assertEqual(items_curr[0]["id"], str(self.tx2_id))
        self.assertEqual(items_curr[0]["original_currency"], "USD")
        self.assertEqual(items_curr[0]["original_amount"], "50.00")
        self.assertEqual(items_curr[0]["effective_fx_rate"], "7.200000000000")

    def test_deterministic_cursor_pagination(self):
        # Page 1 with limit=2
        res1 = self.client.get("/api/v1/transactions?limit=2", headers=self.headers)
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        items1 = data1["items"]
        next_cursor = data1["next_cursor"]
        self.assertEqual(len(items1), 2)
        self.assertIsNotNone(next_cursor)

        # Page 2 using next_cursor
        res2 = self.client.get(f"/api/v1/transactions?limit=2&cursor={next_cursor}", headers=self.headers)
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        items2 = data2["items"]
        self.assertEqual(len(items2), 2)

        # Ensure no overlap between page 1 and page 2
        p1_ids = {it["id"] for it in items1}
        p2_ids = {it["id"] for it in items2}
        self.assertEqual(len(p1_ids.intersection(p2_ids)), 0)

    def test_get_transaction_details_and_links(self):
        res = self.client.get(f"/api/v1/transactions/{self.tx1_id}", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["id"], str(self.tx1_id))
        self.assertEqual(data["merchant"], "JD.com")
        self.assertEqual(data["from_account"]["name"], "ICBC Debit")
        self.assertEqual(data["category"]["name"], "Food")
        self.assertIn("links", data)
        self.assertEqual(len(data["links"]), 1)
        self.assertEqual(data["links"][0]["relation_type"], "refund_of")

    def test_cross_household_transaction_isolation(self):
        # Household B device attempts to read Household A transaction -> 404
        res = self.client.get(f"/api/v1/transactions/{self.tx1_id}", headers=self.headers_b)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "TRANSACTION_NOT_FOUND")

    def test_invalid_cursor_returns_canonical_422(self):
        # Malformed garbage cursor -> 422 INVALID_REQUEST canonical error envelope, not 500
        res = self.client.get("/api/v1/transactions?cursor=garbage_cursor_not_base64", headers=self.headers)
        self.assertEqual(res.status_code, 422)
        err = res.json()["error"]
        self.assertEqual(err["code"], "INVALID_REQUEST")
        self.assertEqual(err["retryable"], False)

if __name__ == "__main__":
    unittest.main()

