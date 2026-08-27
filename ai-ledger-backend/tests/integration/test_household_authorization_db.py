import unittest
from uuid import uuid4
from datetime import date
from fastapi.testclient import TestClient

from app.db import get_connection
from app.main import create_app
from app.api.deps import get_db_connection
from app.auth.browser_verifier import set_browser_verifier, StaticBrowserAuthVerifier
from app.repositories import users as users_repo
from app.repositories import household_members as members_repo
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo
from tests.support.db_helper import BaseDbTestCase


class TestHouseholdAuthorizationDb(BaseDbTestCase):
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
        cls.static_verifier = StaticBrowserAuthVerifier()
        set_browser_verifier(cls.static_verifier)

    @classmethod
    def tearDownClass(cls):
        set_browser_verifier(None)
        super().tearDownClass()

    def seed_test_data(self):
        # 1. Household A
        self.household_a_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_a_id,
            name="Household A",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )
        self.user_a_id = uuid4()
        self.auth_sub_a = "auth0|user_a"
        users_repo.create_user(
            self.conn,
            user_id=self.user_a_id,
            auth_subject=self.auth_sub_a,
            display_name="User Alpha",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_a_id,
            user_id=self.user_a_id,
            role="owner"
        )
        self.account_a_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=self.account_a_id,
            household_id=self.household_a_id,
            name="Alpha Checking",
            account_type="cash",
            currency="CNY"
        )
        self.category_a_id = uuid4()
        categories_repo.create_category(
            self.conn,
            category_id=self.category_a_id,
            household_id=self.household_a_id,
            name="Alpha Dining",
            category_type="expense"
        )

        # 2. Household B
        self.household_b_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_b_id,
            name="Household B",
            reporting_currency="USD",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )
        self.user_b_id = uuid4()
        self.auth_sub_b = "auth0|user_b"
        users_repo.create_user(
            self.conn,
            user_id=self.user_b_id,
            auth_subject=self.auth_sub_b,
            display_name="User Beta",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_b_id,
            user_id=self.user_b_id,
            role="owner"
        )
        self.account_b_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=self.account_b_id,
            household_id=self.household_b_id,
            name="Beta Checking",
            account_type="cash",
            currency="USD"
        )
        self.category_b_id = uuid4()
        categories_repo.create_category(
            self.conn,
            category_id=self.category_b_id,
            household_id=self.household_b_id,
            name="Beta Groceries",
            category_type="expense"
        )

        # Tokens
        self.jwt_user_a = "valid.jwt.usera"
        self.static_verifier.register_token(self.jwt_user_a, {"sub": self.auth_sub_a})

        self.jwt_user_b = "valid.jwt.userb"
        self.static_verifier.register_token(self.jwt_user_b, {"sub": self.auth_sub_b})

        self.conn.commit()

    def test_account_list_isolation(self):
        # User A should only see Account A
        res_a = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.jwt_user_a}"}
        )
        self.assertEqual(res_a.status_code, 200)
        items_a = res_a.json()["items"]
        acc_ids_a = [item["id"] for item in items_a]
        self.assertIn(str(self.account_a_id), acc_ids_a)
        self.assertNotIn(str(self.account_b_id), acc_ids_a)

        # User B should only see Account B
        res_b = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.jwt_user_b}"}
        )
        self.assertEqual(res_b.status_code, 200)
        items_b = res_b.json()["items"]
        acc_ids_b = [item["id"] for item in items_b]
        self.assertIn(str(self.account_b_id), acc_ids_b)
        self.assertNotIn(str(self.account_a_id), acc_ids_b)

    def test_cross_household_account_patch_returns_404(self):
        # User A attempts to modify User B's account
        res = self.client.patch(
            f"/api/v1/accounts/{self.account_b_id}",
            headers={"Authorization": f"Bearer {self.jwt_user_a}"},
            json={
                "name": "Hacked Account Name",
                "row_version": 0
            }
        )
        self.assertEqual(res.status_code, 404)

    def test_cross_household_category_patch_returns_404(self):
        # User A attempts to modify User B's category
        res = self.client.patch(
            f"/api/v1/categories/{self.category_b_id}",
            headers={"Authorization": f"Bearer {self.jwt_user_a}"},
            json={"name": "Hacked Category"}
        )
        self.assertEqual(res.status_code, 404)

    def test_cross_household_transaction_read_returns_404(self):
        random_tx_id = uuid4()
        res = self.client.get(
            f"/api/v1/transactions/{random_tx_id}",
            headers={"Authorization": f"Bearer {self.jwt_user_a}"}
        )
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
