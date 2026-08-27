import unittest
from uuid import uuid4
from decimal import Decimal
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
from app.repositories import transactions as transactions_repo
import app.services.ledger_service as ledger_service
from tests.support.db_helper import BaseDbTestCase


class TestTransactionCorrectionsApiDb(BaseDbTestCase):
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

    def setUp(self):
        super().setUp()
        self.household_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_id,
            name="Correction Test Household",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )
        self.user_id = uuid4()
        self.auth_subject = "auth0|corr_user"
        users_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_subject,
            display_name="Corr User",
            email="corr@example.com",
            default_currency="CNY",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user_id,
            role="owner"
        )
        self.account_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=self.account_id,
            household_id=self.household_id,
            name="Wallet",
            institution="Cash",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id
        )
        self.account_jpy_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=self.account_jpy_id,
            household_id=self.household_id,
            name="JPY Cash",
            institution="Cash",
            account_type="cash",
            currency="JPY",
            owner_user_id=self.user_id
        )
        self.category_id = uuid4()
        categories_repo.create_category(
            self.conn,
            category_id=self.category_id,
            household_id=self.household_id,
            name="Dining",
            category_type="expense"
        )
        self.inactive_category_id = uuid4()
        categories_repo.create_category(
            self.conn,
            category_id=self.inactive_category_id,
            household_id=self.household_id,
            name="Old Cat",
            category_type="expense",
            status="inactive"
        )
        self.income_category_id = uuid4()
        categories_repo.create_category(
            self.conn,
            category_id=self.income_category_id,
            household_id=self.household_id,
            name="Salary",
            category_type="income"
        )
        self.browser_token = "corr.jwt.token"
        self.static_verifier.register_token(
            self.browser_token,
            {"sub": self.auth_subject, "exp": 9999999999}
        )
        self.conn.commit()

    def test_void_transaction_workflow_and_concurrency(self):
        # 1. Record an expense
        tx = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_id,
            amount=Decimal("150.00"),
            currency="CNY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 1),
            merchant="Restaurant",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        tx_id = tx["id"]
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # 2. Missing expected_version -> 422
        resp_no_ver = self.client.post(
            f"/api/v1/transactions/{tx_id}/void",
            headers=headers,
            json={"delete_reason": "No version"}
        )
        self.assertEqual(resp_no_ver.status_code, 422)

        # 3. Wrong expected_version -> 409
        resp_wrong_ver = self.client.post(
            f"/api/v1/transactions/{tx_id}/void",
            headers=headers,
            json={"expected_version": 999, "delete_reason": "Wrong version"}
        )
        self.assertEqual(resp_wrong_ver.status_code, 409)

        # 4. Correct expected_version -> 200
        resp = self.client.post(
            f"/api/v1/transactions/{tx_id}/void",
            headers=headers,
            json={"expected_version": 0, "delete_reason": "Wrong entry"}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "voided")
        self.assertTrue(data["account_balance_restored"])

        # 5. Check balance restored
        state = accounts_repo.get_account_state(self.conn, self.account_id)
        self.assertEqual(state["ledger_balance"], Decimal("0.000000"))

        # 6. Second void on same transaction -> 409
        resp_double = self.client.post(
            f"/api/v1/transactions/{tx_id}/void",
            headers=headers,
            json={"expected_version": 1, "delete_reason": "Double void"}
        )
        self.assertEqual(resp_double.status_code, 409)

    def test_correction_preview_and_commit_workflow(self):
        tx = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_id,
            amount=Decimal("100.00"),
            currency="CNY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 1),
            merchant="Store A",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        tx_id = tx["id"]
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # Preview correction
        preview_resp = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/preview",
            headers=headers,
            json={"merchant": "Store B", "from_amount": "120.00"}
        )
        self.assertEqual(preview_resp.status_code, 200)
        prev_data = preview_resp.json()
        self.assertEqual(prev_data["expected_version"], 0)
        self.assertEqual(len(prev_data["account_state_deltas"]), 1)
        self.assertEqual(prev_data["account_state_deltas"][0]["delta"], "-20.00")

        # Commit correction with optimistic concurrency
        commit_resp = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={
                "expected_version": 0,
                "changes": {"merchant": "Store B", "from_amount": "120.00"},
                "reason": "Corrected receipt total"
            }
        )
        self.assertEqual(commit_resp.status_code, 200)
        updated = commit_resp.json()
        self.assertEqual(updated["merchant"], "Store B")
        self.assertEqual(updated["from_amount"], "120.00")
        self.assertEqual(updated["row_version"], 1)

        # Attempt commit with old expected_version -> 409 Conflict
        conflict_resp = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={
                "expected_version": 0,
                "changes": {"merchant": "Store C"},
                "reason": "Stale edit"
            }
        )
        self.assertEqual(conflict_resp.status_code, 409)
        self.assertEqual(conflict_resp.json()["error"]["code"], "ROW_VERSION_CONFLICT")

    def test_correction_validation_and_domain_hardening(self):
        tx = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_id,
            amount=Decimal("100.00"),
            currency="CNY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 1),
            merchant="Store A",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        tx_id = tx["id"]
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # 1. Negative amount -> 422
        resp_neg = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "-50.00"}}
        )
        self.assertEqual(resp_neg.status_code, 422)

        # 2. Zero amount -> 422
        resp_zero = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "0.00"}}
        )
        self.assertEqual(resp_zero.status_code, 422)

        # 3. Unsupported extra field -> 422
        resp_extra = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"arbitrary_sql": "DROP TABLE transactions"}}
        )
        self.assertEqual(resp_extra.status_code, 422)

        # 4. Inactive category -> 422
        resp_inactive_cat = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"category_id": str(self.inactive_category_id)}}
        )
        self.assertEqual(resp_inactive_cat.status_code, 422)

        # 5. Income category on expense transaction -> 422
        resp_cat_mismatch = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"category_id": str(self.income_category_id)}}
        )
        self.assertEqual(resp_cat_mismatch.status_code, 422)

    def test_jpy_minor_unit_precision_validation(self):
        tx_jpy = ledger_service.record_expense(
            self.conn,
            household_id=self.household_id,
            from_account_id=self.account_jpy_id,
            amount=Decimal("1000"),
            currency="JPY",
            category_id=self.category_id,
            occurred_on=date(2026, 8, 1),
            merchant="Tokyo Store",
            created_by_user_id=self.user_id
        )
        self.conn.commit()
        tx_id = tx_jpy["id"]
        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # JPY cannot have decimal fractions (.50) -> 422
        resp_jpy_frac = self.client.post(
            f"/api/v1/transactions/{tx_id}/corrections/commit",
            headers=headers,
            json={"expected_version": 0, "changes": {"from_amount": "1000.50"}}
        )
        self.assertEqual(resp_jpy_frac.status_code, 422)
