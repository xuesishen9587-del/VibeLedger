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
from app.repositories import devices as devices_repo
from app.repositories import ingestion as ingestion_repo
from app.repositories import reconciliation as reconciliation_repo
from tests.support.db_helper import BaseDbTestCase


class TestWorkQueueApiDb(BaseDbTestCase):
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
            name="Queue Test Household",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )
        self.user_id = uuid4()
        self.auth_subject = "auth0|queue_user"
        users_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_subject,
            display_name="Queue User",
            email="queue@example.com",
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
            name="Checking",
            institution="Bank",
            account_type="cash",
            currency="CNY",
            owner_user_id=self.user_id
        )
        self.browser_token = "queue.jwt.token"
        self.static_verifier.register_token(
            self.browser_token,
            {"sub": self.auth_subject, "exp": 9999999999}
        )
        self.conn.commit()

    def test_work_queue_returns_pending_items(self):
        # 1. Create a reconciliation batch in needs_review
        batch_id = uuid4()
        reconciliation_repo.create_reconciliation_batch(
            self.conn,
            batch_id=batch_id,
            household_id=self.household_id,
            account_id=self.account_id,
            batch_type="snapshot",
            currency="CNY",
            status="needs_review",
            residual_amount=300
        )
        self.conn.commit()

        headers = {"Authorization": f"Bearer {self.browser_token}"}
        resp = self.client.get("/api/v1/work-queue", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["work_type"], "reconciliation")
        self.assertEqual(data["items"][0]["status"], "needs_review")
