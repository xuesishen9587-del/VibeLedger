import unittest
from uuid import UUID, uuid4
from datetime import date
import hashlib
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo
from app.repositories import devices as devices_repo

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

class TestCategoriesApiDb(BaseDbTestCase):
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

        # Household B for cross-household isolation tests
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
                accounts_repo.create_user(conn, self.user_id, "auth_cat_a", "User A", "user_a@cat.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "Device A", self.token_hash)

                accounts_repo.create_household(conn, self.household_b_id, "Household B", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_b_id, "auth_cat_b", "User B", "user_b@cat.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "Device B", self.token_b_hash)


        finally:
            conn.close()

    def test_category_crud_and_validations(self):
        # 1. Create category
        res = self.client.post("/api/v1/categories", json={
            "name": "Food & Dining",
            "type": "expense"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 201)
        cat_data = res.json()
        cat_id = cat_data["id"]
        self.assertEqual(cat_data["name"], "Food & Dining")
        self.assertEqual(cat_data["type"], "expense")
        self.assertEqual(cat_data["status"], "active")

        # 2. Duplicate active name in same household & type rejected -> 422
        res_dup = self.client.post("/api/v1/categories", json={
            "name": "food & dining", # case insensitive duplicate
            "type": "expense"
        }, headers=self.headers)
        self.assertEqual(res_dup.status_code, 422)
        self.assertEqual(res_dup.json()["error"]["code"], "CATEGORY_NAME_CONFLICT")

        # 3. Same name with DIFFERENT type (income) -> allowed
        res_inc = self.client.post("/api/v1/categories", json={
            "name": "Food & Dining",
            "type": "income"
        }, headers=self.headers)
        self.assertEqual(res_inc.status_code, 201)

        # 4. List categories with filters
        res_list = self.client.get("/api/v1/categories?type=expense", headers=self.headers)
        self.assertEqual(res_list.status_code, 200)
        items = res_list.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Food & Dining")

        # 5. Patch rename
        res_patch = self.client.patch(f"/api/v1/categories/{cat_id}", json={
            "name": "Groceries & Dining"
        }, headers=self.headers)
        self.assertEqual(res_patch.status_code, 200)
        self.assertEqual(res_patch.json()["name"], "Groceries & Dining")

        # 6. Deactivate category
        res_deact = self.client.post(f"/api/v1/categories/{cat_id}/deactivate", headers=self.headers)
        self.assertEqual(res_deact.status_code, 200)
        self.assertEqual(res_deact.json()["status"], "inactive")

        # Confirm filtered list shows it as inactive
        res_act_list = self.client.get("/api/v1/categories?status=active", headers=self.headers)
        act_ids = [c["id"] for c in res_act_list.json()["items"]]
        self.assertNotIn(cat_id, act_ids)

        res_inact_list = self.client.get("/api/v1/categories?status=inactive", headers=self.headers)
        inact_ids = [c["id"] for c in res_inact_list.json()["items"]]
        self.assertIn(cat_id, inact_ids)

    def test_cross_household_category_isolation(self):
        # Create category in Household A
        res_a = self.client.post("/api/v1/categories", json={
            "name": "Household A Only",
            "type": "expense"
        }, headers=self.headers)
        cat_a_id = res_a.json()["id"]

        # Household B device attempts to patch Household A's category -> 404
        res_patch = self.client.patch(f"/api/v1/categories/{cat_a_id}", json={
            "name": "Hacked Category"
        }, headers=self.headers_b)
        self.assertEqual(res_patch.status_code, 404)
        self.assertEqual(res_patch.json()["error"]["code"], "CATEGORY_NOT_FOUND")

        # Household B device attempts to deactivate Household A's category -> 404
        res_deact = self.client.post(f"/api/v1/categories/{cat_a_id}/deactivate", headers=self.headers_b)
        self.assertEqual(res_deact.status_code, 404)

    def test_deactivated_category_historical_transaction_and_expense_rejection(self):
        # 1. Create account & category
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                acc_id = uuid4()
                accounts_repo.create_account(conn, acc_id, self.household_id, "Checking", "cash", "CNY")
                cat_id = uuid4()
                categories_repo.create_category(conn, cat_id, self.household_id, "Old Category", "expense")

                # Create historical transaction referencing this category
                tx_id = uuid4()
                from decimal import Decimal
                from app.repositories import transactions as tx_repo
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("150.00"),
                    original_currency="CNY",
                    from_amount=Decimal("150.00"),
                    from_currency="CNY",
                    from_account_id=acc_id,
                    category_id=cat_id,
                    status="committed"
                )
        finally:
            conn.close()

        # 2. Deactivate category
        res_deact = self.client.post(f"/api/v1/categories/{cat_id}/deactivate", headers=self.headers)
        self.assertEqual(res_deact.status_code, 200)
        self.assertEqual(res_deact.json()["status"], "inactive")

        # 3. Historical transaction remains readable and preserves category
        res_tx = self.client.get(f"/api/v1/transactions/{tx_id}", headers=self.headers)
        self.assertEqual(res_tx.status_code, 200)
        tx_data = res_tx.json()
        self.assertEqual(tx_data["category"]["id"], str(cat_id))
        self.assertEqual(tx_data["category"]["name"], "Old Category")

        # 4. Inactive category cannot be used for new Expense workflow confirmation
        # Create an ingestion request with draft referencing the deactivated category
        req_id = uuid4()
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                from app.repositories import ingestion as ingestion_repo
                ingestion_repo.create_ingestion_request(
                    conn=conn,
                    request_id=req_id,
                    device_id=self.device_id,
                    idempotency_key=f"test_inact_cat_{uuid4().hex}",
                    request_kind="expense",
                    request_hash=b"dummy_hash_01234567890123456789012345678901",
                    status="needs_confirmation",
                    draft_payload={
                        "occurred_on": "2026-08-20",
                        "merchant": "Test Merchant",
                        "original_amount": "100.00",
                        "original_currency": "CNY",
                        "from_account": {"id": str(acc_id), "name": "Checking"},
                        "category": {"id": str(cat_id), "name": "Old Category"},
                        "payment_mode": "one_off"
                    }

                )
        finally:
            conn.close()


        # Confirming with inactive category returns 422
        res_confirm = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers=self.headers
        )
        self.assertEqual(res_confirm.status_code, 422)
        self.assertEqual(res_confirm.json()["error"]["code"], "CATEGORY_NOT_FOUND")

if __name__ == "__main__":
    unittest.main()


