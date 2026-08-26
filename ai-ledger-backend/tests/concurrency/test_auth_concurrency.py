import unittest
import hashlib
import concurrent.futures
from uuid import uuid4
from datetime import date
from fastapi.testclient import TestClient

from app.db import get_connection
from app.main import create_app
from app.api.deps import get_db_connection
from app.auth.browser_verifier import set_browser_verifier, StaticBrowserAuthVerifier
from app.repositories import users as users_repo
from app.repositories import household_members as members_repo
from app.repositories import devices as devices_repo
from tests.support.db_helper import BaseDbTestCase


class TestAuthConcurrency(BaseDbTestCase):
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
        self.household_id = uuid4()
        members_repo.create_household(
            self.conn,
            household_id=self.household_id,
            name="Concurrency Household",
            reporting_currency="CNY",
            ledger_start_date=date(2026, 1, 1),
            status="active"
        )
        self.user_id = uuid4()
        self.auth_subject = "auth0|concurrent_user"
        users_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_subject,
            display_name="Concurrent User",
            status="active"
        )
        members_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user_id,
            role="owner"
        )

        self.jwt_token = "valid.jwt.concurrency"
        self.static_verifier.register_token(self.jwt_token, {"sub": self.auth_subject})

        self.raw_device_token = "device_concurrency_token_12345678"
        token_hash = hashlib.sha256(self.raw_device_token.encode("utf-8")).digest()
        self.device_id = uuid4()
        devices_repo.create_device(
            self.conn,
            device_id=self.device_id,
            user_id=self.user_id,
            device_name="Concurrency Device",
            token_hash=token_hash,
            platform="ios_shortcuts",
            status="active"
        )
        self.conn.commit()

    def test_concurrent_device_provisioning(self):
        num_workers = 8

        def _provision(i):
            return self.client.post(
                "/api/v1/devices",
                headers={"Authorization": f"Bearer {self.jwt_token}"},
                json={"device_name": f"Concurrent Device {i}", "platform": "ios"}
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_provision, i) for i in range(num_workers)]
            responses = [f.result() for f in futures]

        for r in responses:
            self.assertEqual(r.status_code, 201)

        tokens = [r.json()["token"] for r in responses]
        device_ids = [r.json()["device"]["device_id"] for r in responses]

        # Ensure all tokens and device IDs are distinct
        self.assertEqual(len(set(tokens)), num_workers)
        self.assertEqual(len(set(device_ids)), num_workers)

    def test_concurrent_device_authentication(self):
        num_requests = 16

        def _auth():
            return self.client.get(
                "/api/v1/accounts",
                headers={"Authorization": f"Bearer {self.raw_device_token}"}
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_auth) for _ in range(num_requests)]
            responses = [f.result() for f in futures]

        for r in responses:
            self.assertEqual(r.status_code, 200)

    def test_concurrent_revocation_and_auth(self):
        # 1. Provision a new device
        res = self.client.post(
            "/api/v1/devices",
            headers={"Authorization": f"Bearer {self.jwt_token}"},
            json={"device_name": "Device To Revoke Concurrently", "platform": "ios"}
        )
        self.assertEqual(res.status_code, 201)
        dev_id = res.json()["device"]["device_id"]
        token = res.json()["token"]

        # 2. Revoke in one call
        revoke_res = self.client.post(
            f"/api/v1/devices/{dev_id}/revoke",
            headers={"Authorization": f"Bearer {self.jwt_token}"}
        )
        self.assertEqual(revoke_res.status_code, 200)

        # 3. Multiple concurrent authentications using the revoked token must all fail 401
        def _attempt_auth():
            return self.client.get(
                "/api/v1/accounts",
                headers={"Authorization": f"Bearer {token}"}
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_attempt_auth) for _ in range(8)]
            responses = [f.result() for f in futures]

        for r in responses:
            self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
