import unittest
import hashlib
from uuid import uuid4
from datetime import date
from fastapi.testclient import TestClient
import jwt

from app.db import get_connection
from app.main import create_app
from app.api.deps import get_db_connection
from app.auth.browser_verifier import set_browser_verifier, StaticBrowserAuthVerifier
from app.repositories import users as users_repo
from app.repositories import household_members as members_repo
from app.repositories import devices as devices_repo
from tests.support.db_helper import BaseDbTestCase


class TestAuthPhase10Db(BaseDbTestCase):
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
        # 1. Household 1 (active)
        self.household_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_id,
            name="Household One",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )

        # 2. Active User 1
        self.user_id = uuid4()
        self.auth_subject = "auth0|user_1"
        users_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_subject,
            display_name="User One",
            email="user1@example.com",
            default_currency="CNY",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user_id,
            role="owner"
        )

        # 3. Active Device for User 1
        self.raw_device_token = "device_secret_token_1234567890_abcdef"
        self.token_hash = hashlib.sha256(self.raw_device_token.encode("utf-8")).digest()
        self.device_id = uuid4()
        devices_repo.create_device(
            self.conn,
            device_id=self.device_id,
            user_id=self.user_id,
            device_name="iPhone 15 Pro",
            token_hash=self.token_hash,
            platform="ios_shortcuts",
            status="active"
        )

        # Register JWT token in static verifier (3-segment header.payload.signature)
        self.browser_jwt_token = "valid.jwt.user1"
        self.static_verifier.register_token(
            self.browser_jwt_token,
            {"sub": self.auth_subject, "email": "user1@example.com"}
        )
        self.conn.commit()

    def test_device_auth_success(self):
        # Calling an authenticated endpoint (e.g. GET /api/v1/accounts)
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("items", res.json())

    def test_browser_jwt_auth_success(self):
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("items", res.json())

    def test_missing_auth_header_fails_401(self):
        res = self.client.get("/api/v1/accounts")
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")
        self.assertFalse(data["error"]["retryable"])

    def test_empty_bearer_token_fails_401(self):
        res = self.client.get("/api/v1/accounts", headers={"Authorization": "Bearer   "})
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    def test_invalid_device_token_fails_401(self):
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": "Bearer invalid_nonexistent_token_string"}
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    def test_revoked_device_token_fails_401(self):
        # Revoke device in DB
        devices_repo.revoke_device(self.conn, self.device_id)
        self.conn.commit()

        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "DEVICE_REVOKED")

    def test_disabled_user_fails_403(self):
        # Disable user
        with self.conn.cursor() as cur:
            cur.execute("UPDATE users SET status = 'disabled' WHERE id = %s;", (self.user_id,))
        self.conn.commit()

        # Test device auth with disabled user
        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "USER_DISABLED")

        # Test browser JWT auth with disabled user
        res_jwt = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.browser_jwt_token}"}
        )
        self.assertEqual(res_jwt.status_code, 403)
        self.assertEqual(res_jwt.json()["error"]["code"], "USER_DISABLED")

    def test_user_zero_household_memberships_fails_403(self):
        # Remove membership
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM household_members WHERE user_id = %s;", (self.user_id,))
        self.conn.commit()

        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "USER_NOT_IN_HOUSEHOLD")

    def test_user_multiple_household_memberships_fails_closed_403(self):
        # Create second active household and add user
        hid2 = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=hid2,
            name="Household Two",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=hid2,
            user_id=self.user_id,
            role="member"
        )
        self.conn.commit()

        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {self.raw_device_token}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "AMBIGUOUS_HOUSEHOLD_MEMBERSHIP")

    def test_jwt_unregistered_sub_fails_401(self):
        token = "valid.jwt.unknown.user"
        self.static_verifier.register_token(token, {"sub": "auth0|unknown_unregistered_sub"})

        res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "UNAUTHORIZED")


if __name__ == "__main__":
    unittest.main()
