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
from app.repositories import audit as audit_repo
from tests.support.db_helper import BaseDbTestCase


class TestAuditApiDb(BaseDbTestCase):
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
            name="Audit Test Household",
            reporting_currency="CNY",
            status="active"
        )
        self.user_id = uuid4()
        self.auth_subject = "auth0|audit_user"
        users_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_subject,
            display_name="Audit User",
            email="audit@example.com",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user_id,
            role="owner"
        )
        self.browser_token = "valid.jwt.token"
        self.static_verifier.register_token(
            self.browser_token,
            {"sub": self.auth_subject, "exp": 9999999999}
        )
        self.conn.commit()

    def test_list_audit_events_filtering_and_history_endpoint(self):
        entity_id_1 = uuid4()
        entity_id_2 = uuid4()

        # Insert test audit events
        audit_repo.insert_audit_event(
            self.conn,
            household_id=self.household_id,
            actor_type="user",
            entity_type="transaction",
            entity_id=entity_id_1,
            action="create",
            actor_user_id=self.user_id,
            before_data=None,
            after_data={"merchant": "Test Store"}
        )
        audit_repo.insert_audit_event(
            self.conn,
            household_id=self.household_id,
            actor_type="user",
            entity_type="account",
            entity_id=entity_id_2,
            action="update",
            actor_user_id=self.user_id,
            before_data={"name": "Old"},
            after_data={"name": "New"},
            reason="Testing update"
        )
        self.conn.commit()

        headers = {"Authorization": f"Bearer {self.browser_token}"}

        # 1. Test /api/v1/audit-events
        resp = self.client.get("/api/v1/audit-events", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 2)

        # Filter by entity_type
        resp_tx = self.client.get("/api/v1/audit-events?entity_type=transaction", headers=headers)
        self.assertEqual(resp_tx.status_code, 200)
        self.assertEqual(len(resp_tx.json()["items"]), 1)
        self.assertEqual(resp_tx.json()["items"][0]["entity_type"], "transaction")

        # 2. Test canonical /api/v1/history
        resp_hist = self.client.get(
            f"/api/v1/history?entity_type=account&entity_id={entity_id_2}",
            headers=headers
        )
        self.assertEqual(resp_hist.status_code, 200)
        hist_data = resp_hist.json()
        self.assertIn("items", hist_data)
        self.assertEqual(len(hist_data["items"]), 1)
        item = hist_data["items"][0]
        self.assertEqual(item["entity_type"], "account")
        self.assertEqual(item["entity_id"], str(entity_id_2))
        self.assertEqual(item["action"], "update")
        self.assertEqual(item["actor_type"], "user")
        self.assertEqual(item["actor_user_id"], str(self.user_id))
        self.assertEqual(item["before_data"]["name"], "Old")
        self.assertEqual(item["after_data"]["name"], "New")
        self.assertEqual(item["reason"], "Testing update")

if __name__ == "__main__":
    unittest.main()
