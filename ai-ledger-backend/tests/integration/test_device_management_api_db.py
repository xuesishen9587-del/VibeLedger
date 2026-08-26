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


class TestDeviceManagementApiDb(BaseDbTestCase):
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
        # 1. Household 1
        self.household_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_id,
            name="Household One",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )

        # 2. User 1
        self.user1_id = uuid4()
        self.auth_subject1 = "auth0|user_1"
        users_repo.create_user(
            self.conn,
            user_id=self.user1_id,
            auth_subject=self.auth_subject1,
            display_name="User One",
            email="user1@example.com",
            default_currency="CNY",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user1_id,
            role="owner"
        )

        # 3. User 2 (in same household)
        self.user2_id = uuid4()
        self.auth_subject2 = "auth0|user_2"
        users_repo.create_user(
            self.conn,
            user_id=self.user2_id,
            auth_subject=self.auth_subject2,
            display_name="User Two",
            email="user2@example.com",
            default_currency="CNY",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user2_id,
            role="member"
        )

        # Register JWT tokens for both users
        self.jwt_user1 = "valid.jwt.user1"
        self.static_verifier.register_token(
            self.jwt_user1,
            {"sub": self.auth_subject1, "email": "user1@example.com"}
        )

        self.jwt_user2 = "valid.jwt.user2"
        self.static_verifier.register_token(
            self.jwt_user2,
            {"sub": self.auth_subject2, "email": "user2@example.com"}
        )

        self.conn.commit()

    def test_provision_device_and_authenticate(self):
        # 1. Provision new device for User 1
        res = self.client.post(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.jwt_user1}"},
            json={
                "device_name": "Work iPhone",
                "platform": "ios_shortcuts",
                "client_version": "v1.0.0"
            }
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertIn("device", data)
        self.assertIn("token", data)

        device_info = data["device"]
        raw_token = data["token"]
        self.assertEqual(device_info["device_name"], "Work iPhone")
        self.assertEqual(device_info["status"], "active")
        self.assertEqual(device_info["user_id"], str(self.user1_id))
        self.assertNotIn("token_hash", device_info)
        self.assertTrue(len(raw_token) >= 32)

        # 2. Immediately use newly provisioned device token on financial endpoint
        acc_res = self.client.get(
            "/api/v1/accounts",
            headers={"Authorization": f"Bearer {raw_token}"}
        )
        self.assertEqual(acc_res.status_code, 200)

        # 3. Check audit log
        events = audit_repo.list_audit_events_for_entity(
            self.conn,
            entity_type="device",
            entity_id=device_info["device_id"]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "create")
        self.assertEqual(events[0]["household_id"], self.household_id)

    def test_list_devices_isolation_and_redaction(self):
        # Provision a device for user 1
        res1 = self.client.post(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.jwt_user1}"},
            json={"device_name": "User 1 iPad", "platform": "ipad_os"}
        )
        self.assertEqual(res1.status_code, 201)
        dev1_id = res1.json()["device"]["device_id"]

        # Provision a device for user 2
        res2 = self.client.post(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.jwt_user2}"},
            json={"device_name": "User 2 Watch", "platform": "watch_os"}
        )
        self.assertEqual(res2.status_code, 201)
        dev2_id = res2.json()["device"]["device_id"]

        # User 1 listing devices: sees only dev1
        list1 = self.client.get(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.jwt_user1}"}
        )
        self.assertEqual(list1.status_code, 200)
        items1 = list1.json()["items"]
        dev_ids1 = [item["device_id"] for item in items1]
        self.assertIn(dev1_id, dev_ids1)
        self.assertNotIn(dev2_id, dev_ids1)

        # User 2 listing devices: sees only dev2
        list2 = self.client.get(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.jwt_user2}"}
        )
        self.assertEqual(list2.status_code, 200)
        items2 = list2.json()["items"]
        dev_ids2 = [item["device_id"] for item in items2]
        self.assertIn(dev2_id, dev_ids2)
        self.assertNotIn(dev1_id, dev_ids2)

        # Verify credentials redaction
        for item in items1:
            self.assertNotIn("token_hash", item)
            self.assertNotIn("token", item)

    def test_revoke_device_lifecycle(self):
        # 1. Provision device
        res = self.client.post(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.jwt_user1}"},
            json={"device_name": "Temporary Mac", "platform": "macos"}
        )
        self.assertEqual(res.status_code, 201)
        device_id = res.json()["device"]["device_id"]
        raw_token = res.json()["token"]

        # Verify active
        self.assertEqual(
            self.client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {raw_token}"}).status_code,
            200
        )

        # 2. Revoke device
        revoke_res = self.client.post(
            f"/api/v1/devices/{device_id}/revoke",
            headers={"Authorization": f"Bearer {self.jwt_user1}"}
        )
        self.assertEqual(revoke_res.status_code, 200)
        revoked_info = revoke_res.json()["device"]
        self.assertEqual(revoked_info["status"], "revoked")
        self.assertIsNotNone(revoked_info["revoked_at"])

        # 3. Subsequent authentication immediately fails
        self.assertEqual(
            self.client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {raw_token}"}).status_code,
            401
        )

        # 4. Check audit log has revoke event
        events = audit_repo.list_audit_events_for_entity(
            self.conn,
            entity_type="device",
            entity_id=device_id
        )
        actions = [e["action"] for e in events]
        self.assertIn("soft_delete", actions)

    def test_revoke_cross_user_device_returns_404(self):
        # User 1 creates a device
        res = self.client.post(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.jwt_user1}"},
            json={"device_name": "Secret Phone", "platform": "ios"}
        )
        device_id = res.json()["device"]["device_id"]

        # User 2 attempts to revoke User 1's device -> must return 404 (isolation)
        res_revoke = self.client.post(
            f"/api/v1/devices/{device_id}/revoke",
            headers={"Authorization": f"Bearer {self.jwt_user2}"}
        )
        self.assertEqual(res_revoke.status_code, 404)
        self.assertEqual(res_revoke.json()["error"]["code"], "DEVICE_NOT_FOUND")

    def test_revoke_nonexistent_device_returns_404(self):
        random_id = uuid4()
        res = self.client.post(
            f"/api/v1/devices/{random_id}/revoke",
            headers={"Authorization": f"Bearer {self.jwt_user1}"}
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "DEVICE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
