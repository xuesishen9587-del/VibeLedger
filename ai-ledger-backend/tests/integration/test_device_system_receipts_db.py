import hashlib
import unittest
from datetime import date
from typing import Any, Dict, List
from uuid import UUID, uuid4
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.auth.context import AuthContext, SystemCommandActor
from app.auth.browser_verifier import StaticBrowserAuthVerifier, set_browser_verifier
from app.domain.transactions import IdempotencyKeyReuseError
from app.repositories import accounts as accounts_repo
from app.repositories import devices as devices_repo
from app.repositories import audit as audit_repo
from app.repositories import users as users_repo
from app.repositories import household_members as members_repo
import app.repositories.simplified_schema as schema_repo
from app.services.durable_commands import (
    execute_durable_command,
    get_audit_actor_info,
)

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase


class TestDeviceSystemReceiptsDb(BaseDbTestCase):
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
        # Household A
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_1_id = uuid4()
        self.device_2_id = uuid4()

        self.raw_token_1 = f"vbl_dev1_{uuid4().hex}"
        self.token_hash_1 = hashlib.sha256(self.raw_token_1.encode("utf-8")).digest()
        self.headers_dev1 = {"Authorization": f"Bearer {self.raw_token_1}"}

        self.raw_token_2 = f"vbl_dev2_{uuid4().hex}"
        self.token_hash_2 = hashlib.sha256(self.raw_token_2.encode("utf-8")).digest()
        self.headers_dev2 = {"Authorization": f"Bearer {self.raw_token_2}"}

        self.auth_sub = f"auth0|test_user_{uuid4().hex[:8]}"
        self.jwt_token = f"valid.jwt.{uuid4().hex}"
        self.static_verifier.register_token(self.jwt_token, {"sub": self.auth_sub})
        self.browser_headers = {"Authorization": f"Bearer {self.jwt_token}"}

        # Household B (Foreign)
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_devb_{uuid4().hex}"
        self.token_hash_b = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_devb = {"Authorization": f"Bearer {self.raw_token_b}"}

        self.auth_sub_b = f"auth0|test_user_b_{uuid4().hex[:8]}"
        self.jwt_token_b = f"valid.jwt.b.{uuid4().hex}"
        self.static_verifier.register_token(self.jwt_token_b, {"sub": self.auth_sub_b})
        self.browser_headers_b = {"Authorization": f"Bearer {self.jwt_token_b}"}

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_household(conn, self.household_id, "Household Alpha", reporting_currency="USD")
                accounts_repo.create_user(conn, self.user_id, self.auth_sub, "User Alpha", "alpha@example.com")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_1_id, self.user_id, "iPhone Dev 1", self.token_hash_1, household_id=self.household_id)
                devices_repo.create_device(conn, self.device_2_id, self.user_id, "Mac Dev 2", self.token_hash_2, household_id=self.household_id)

                accounts_repo.create_household(conn, self.household_b_id, "Household Beta", reporting_currency="USD")
                accounts_repo.create_user(conn, self.user_b_id, self.auth_sub_b, "User Beta", "beta@example.com")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "Foreign Dev B", self.token_hash_b, household_id=self.household_b_id)
        finally:
            conn.close()

    def _get_receipts(self, household_id: UUID) -> List[Dict[str, Any]]:
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, actor_scope, idempotency_key, operation, request_hash, status,
                           response_http_status, failure_code, user_id, device_id, committed_at
                    FROM ingestion_requests
                    WHERE household_id = %s
                    ORDER BY created_at ASC;
                    """,
                    (str(household_id),),
                )
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def _get_audits(self, household_id: UUID, entity_type: str, entity_id: UUID) -> List[Dict[str, Any]]:
        conn = get_connection(self.test_schema)
        try:
            return audit_repo.list_audit_events_for_entity(conn, entity_type, entity_id, household_id)
        finally:
            conn.close()

    # --- 1. Device Revoke Header Contract & Replay ---

    def test_01_revoke_header_contract_missing_or_invalid_key_rejected(self):
        # 1. Missing header -> 422
        res = self.client.post(
            f"/api/v1/devices/{self.device_2_id}/revoke",
            headers=self.browser_headers,
        )
        self.assertEqual(res.status_code, 422)

        # 2. Key too short (<8 chars) -> 422
        res_short = self.client.post(
            f"/api/v1/devices/{self.device_2_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": "short"},
        )
        self.assertEqual(res_short.status_code, 422)

        # 3. Key too long (>200 chars) -> 422
        res_long = self.client.post(
            f"/api/v1/devices/{self.device_2_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": "k" * 201},
        )
        self.assertEqual(res_long.status_code, 422)

        # Zero receipts and zero state changes
        receipts = self._get_receipts(self.household_id)
        self.assertEqual(len(receipts), 0)

        conn = get_connection(self.test_schema)
        try:
            dev = devices_repo.get_device_by_id(conn, self.device_2_id)
            self.assertEqual(dev["status"], "active")
        finally:
            conn.close()

    def test_02_successful_device_revoke_and_exact_replay_parity(self):
        key = f"key-revoke-dev2-{uuid4().hex}"

        # 1. First execution: browser user revokes device 2
        res = self.client.post(
            f"/api/v1/devices/{self.device_2_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": key},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("device", body)
        self.assertEqual(body["device"]["status"], "revoked")
        self.assertEqual(body["device"]["device_id"], str(self.device_2_id))
        self.assertIsNotNone(body["device"]["revoked_at"])

        # Exactly 1 receipt in committed status
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "committed")
        self.assertEqual(receipts[0]["response_http_status"], 200)
        self.assertEqual(receipts[0]["actor_scope"], f"user:{self.user_id}")
        self.assertEqual(receipts[0]["operation"], f"POST /api/v1/devices/{self.device_2_id}/revoke")
        receipt_id = receipts[0]["id"]

        # Exactly 1 audit event with source_request_id = receipt_id
        audits = self._get_audits(self.household_id, "device", self.device_2_id)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["action"], "update")
        self.assertEqual(audits[0]["actor_type"], "user")
        self.assertEqual(audits[0]["source_request_id"], receipt_id)

        # 2. Exact retry with same actor + same key + same device
        res_replay = self.client.post(
            f"/api/v1/devices/{self.device_2_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": key},
        )
        self.assertEqual(res_replay.status_code, 200)
        self.assertEqual(res_replay.json(), body)

        # Still exactly 1 audit event in DB (no duplicate state change or audit)
        audits_after = self._get_audits(self.household_id, "device", self.device_2_id)
        self.assertEqual(len(audits_after), 1)

    def test_03_device_revoke_target_id_reuse_conflict_rejected(self):
        key = f"key-reuse-target-{uuid4().hex}"

        # 1. Revoke device 2 under key
        res1 = self.client.post(
            f"/api/v1/devices/{self.device_2_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": key},
        )
        self.assertEqual(res1.status_code, 200)

        # 2. Reusing same key for a different device_id (even nonexistent UUID) -> 409 IDEMPOTENCY_KEY_REUSE
        diff_device_id = uuid4()
        res2 = self.client.post(
            f"/api/v1/devices/{diff_device_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": key},
        )
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

    def test_04_deterministic_rejection_foreign_and_nonexistent_device(self):
        # 1. Foreign device (device belonging to Household B)
        key_foreign = f"key-foreign-dev-{uuid4().hex}"
        res_foreign = self.client.post(
            f"/api/v1/devices/{self.device_b_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": key_foreign},
        )
        self.assertEqual(res_foreign.status_code, 404)
        self.assertEqual(res_foreign.json()["error"]["code"], "DEVICE_NOT_FOUND")
        err_json_foreign = res_foreign.json()

        # Terminal rejected receipt in Household A
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_foreign]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "rejected")
        self.assertEqual(receipts[0]["response_http_status"], 404)
        self.assertEqual(receipts[0]["failure_code"], "DEVICE_NOT_FOUND")

        # Foreign device in Household B remains active and unaffected
        conn = get_connection(self.test_schema)
        try:
            dev_b = devices_repo.get_device_by_id(conn, self.device_b_id)
            self.assertEqual(dev_b["status"], "active")
        finally:
            conn.close()

        # Replay foreign device rejection
        res_foreign_replay = self.client.post(
            f"/api/v1/devices/{self.device_b_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": key_foreign},
        )
        self.assertEqual(res_foreign_replay.status_code, 404)
        self.assertEqual(res_foreign_replay.json(), err_json_foreign)

        # 2. Nonexistent device
        key_nonexistent = f"key-nonexist-dev-{uuid4().hex}"
        random_dev_id = uuid4()
        res_nonexist = self.client.post(
            f"/api/v1/devices/{random_dev_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": key_nonexistent},
        )
        self.assertEqual(res_nonexist.status_code, 404)
        self.assertEqual(res_nonexist.json()["error"]["code"], "DEVICE_NOT_FOUND")
        err_json_nonexist = res_nonexist.json()

        receipts_ne = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_nonexistent]
        self.assertEqual(len(receipts_ne), 1)
        self.assertEqual(receipts_ne[0]["status"], "rejected")
        self.assertEqual(receipts_ne[0]["response_http_status"], 404)

        # Replay nonexistent device rejection
        res_ne_replay = self.client.post(
            f"/api/v1/devices/{random_dev_id}/revoke",
            headers={**self.browser_headers, "Idempotency-Key": key_nonexistent},
        )
        self.assertEqual(res_ne_replay.status_code, 404)
        self.assertEqual(res_ne_replay.json(), err_json_nonexist)

    def test_05_unexpected_failure_rollback_preserves_zero_receipt_or_state(self):
        from unittest.mock import patch
        key_crash = f"key-crash-revoke-{uuid4().hex}"

        with patch("app.repositories.devices.revoke_device", side_effect=RuntimeError("Simulated unexpected crash")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"/api/v1/devices/{self.device_2_id}/revoke",
                    headers={**self.browser_headers, "Idempotency-Key": key_crash},
                )

        # Zero surviving receipts
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_crash]
        self.assertEqual(len(receipts), 0)

        # Zero audit events
        audits = self._get_audits(self.household_id, "device", self.device_2_id)
        self.assertEqual(len(audits), 0)

        # Device remains active
        conn = get_connection(self.test_schema)
        try:
            dev = devices_repo.get_device_by_id(conn, self.device_2_id)
            self.assertEqual(dev["status"], "active")
        finally:
            conn.close()

    def test_06_device_self_revoke_authentication_precedence(self):
        # Device 1 revokes itself
        key = f"key-self-revoke-{uuid4().hex}"
        res = self.client.post(
            f"/api/v1/devices/{self.device_1_id}/revoke",
            headers={**self.headers_dev1, "Idempotency-Key": key},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["device"]["status"], "revoked")

        # Now device 1 is revoked in DB; subsequent requests with device 1 token
        # MUST fail authentication with 401 DEVICE_REVOKED before receipt lookup
        res_subsequent = self.client.post(
            f"/api/v1/devices/{self.device_1_id}/revoke",
            headers={**self.headers_dev1, "Idempotency-Key": key},
        )
        self.assertEqual(res_subsequent.status_code, 401)
        self.assertEqual(res_subsequent.json()["error"]["code"], "DEVICE_REVOKED")

    # --- 2. Device Provisioning Secret Exception ---

    def test_07_device_provisioning_one_time_secret_exception(self):
        # Provision new device
        res = self.client.post(
            "/api/v1/devices",
            headers=self.browser_headers,
            json={"device_name": "One-Time Secret Phone", "platform": "ios"},
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        raw_token = data["token"]
        new_dev_id = UUID(data["device"]["device_id"])

        self.assertTrue(len(raw_token) >= 32)

        # Proves 0 command receipts created in ingestion_requests
        receipts = [r for r in self._get_receipts(self.household_id) if r["operation"] == "POST /api/v1/devices"]
        self.assertEqual(len(receipts), 0)

        # Proves audit event has create action and after_data does NOT contain token or token_hash
        audits = self._get_audits(self.household_id, "device", new_dev_id)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["action"], "create")
        after_data = audits[0].get("after_data") or {}
        self.assertNotIn("token", after_data)
        self.assertNotIn("token_hash", after_data)

    # --- 3. Internal-System Actor Scope Infrastructure ---

    def test_08_internal_system_actor_receipt_infrastructure_and_replay(self):
        actor = SystemCommandActor(household_id=self.household_id, user_id=self.user_id)
        self.assertEqual(actor.actor_scope, f"system:{self.household_id}")
        self.assertIsNone(actor.device_id)
        self.assertTrue(actor.is_system)
        self.assertFalse(actor.is_device)
        self.assertFalse(actor.is_browser)

        # Verify audit actor mapping: actor_type is strictly "system", zero user/device attribution
        audit_actor_type, audit_user_id, audit_device_id = get_audit_actor_info(actor)
        self.assertEqual(audit_actor_type, "system")
        self.assertIsNone(audit_user_id)
        self.assertIsNone(audit_device_id)

        key = f"key-system-command-{uuid4().hex}"
        op = "POST /api/v1/internal/test-action"
        body = {"parameter": "system_value"}

        conn = get_connection(self.test_schema)
        try:
            def _mutate(c: Any, rid: UUID):
                # Write an audit event from system actor
                audit_repo.insert_audit_event(
                    conn=c,
                    household_id=actor.household_id,
                    actor_type=audit_actor_type,
                    actor_user_id=audit_user_id,
                    actor_device_id=audit_device_id,
                    source_request_id=rid,
                    entity_type="device",
                    entity_id=self.device_1_id,
                    action="update",
                    after_data={"system_modified": True},
                )
                return {"result": "success"}, 200

            res_payload, res_status = execute_durable_command(
                conn=conn,
                auth_context=actor,
                idempotency_key=key,
                operation=op,
                body=body,
                mutation_fn=_mutate,
            )
            self.assertEqual(res_status, 200)
            self.assertEqual(res_payload, {"result": "success"})

            # Verify receipt in DB
            receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["actor_scope"], f"system:{self.household_id}")
            self.assertIsNone(receipts[0]["device_id"])
            self.assertEqual(receipts[0]["user_id"], self.user_id)
            self.assertEqual(receipts[0]["status"], "committed")
            self.assertEqual(receipts[0]["response_http_status"], 200)
            receipt_id = receipts[0]["id"]

            # Verify audit event in DB
            audits = [a for a in self._get_audits(self.household_id, "device", self.device_1_id) if a["source_request_id"] == receipt_id]
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]["actor_type"], "system")
            self.assertIsNone(audits[0]["actor_user_id"])
            self.assertIsNone(audits[0]["actor_device_id"])

            # Verify exact replay by system actor
            replay_payload, replay_status = execute_durable_command(
                conn=conn,
                auth_context=actor,
                idempotency_key=key,
                operation=op,
                body=body,
                mutation_fn=_mutate,
            )
            self.assertEqual(replay_status, 200)
            self.assertEqual(replay_payload, {"result": "success"})
        finally:
            conn.close()

    def test_09_internal_system_actor_changed_command_conflict(self):
        actor = SystemCommandActor(household_id=self.household_id, user_id=self.user_id)
        key = f"key-system-conflict-{uuid4().hex}"
        op = "POST /api/v1/internal/action-a"
        body_a = {"task": "a"}

        conn = get_connection(self.test_schema)
        try:
            execute_durable_command(
                conn=conn,
                auth_context=actor,
                idempotency_key=key,
                operation=op,
                body=body_a,
                mutation_fn=lambda c, rid: ({"done": "a"}, 200),
            )

            # Same system actor + same key + changed operation or body -> 409 IdempotencyKeyReuseError
            body_b = {"task": "b"}
            with self.assertRaises(IdempotencyKeyReuseError):
                execute_durable_command(
                    conn=conn,
                    auth_context=actor,
                    idempotency_key=key,
                    operation=op,
                    body=body_b,
                    mutation_fn=lambda c, rid: ({"done": "b"}, 200),
                )
        finally:
            conn.close()

    def test_10_textual_key_independence_system_and_user_actor(self):
        key = f"shared-key-{uuid4().hex}"
        sys_actor = SystemCommandActor(household_id=self.household_id, user_id=self.user_id)
        user_actor = AuthContext(
            auth_mode="browser",
            user_id=self.user_id,
            household_id=self.household_id,
            household_role="owner",
        )

        conn = get_connection(self.test_schema)
        try:
            # 1. System actor executes under key
            execute_durable_command(
                conn=conn,
                auth_context=sys_actor,
                idempotency_key=key,
                operation="POST /api/v1/action",
                body={"val": 1},
                mutation_fn=lambda c, rid: ({"sys": True}, 200),
            )

            # 2. Browser user actor executes under EXACT SAME key
            execute_durable_command(
                conn=conn,
                auth_context=user_actor,
                idempotency_key=key,
                operation="POST /api/v1/action",
                body={"val": 1},
                mutation_fn=lambda c, rid: ({"user": True}, 200),
            )

            # Verify two independent receipts in DB under same household and same key
            receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
            self.assertEqual(len(receipts), 2)
            scopes = {r["actor_scope"] for r in receipts}
            self.assertEqual(scopes, {f"system:{self.household_id}", f"user:{self.user_id}"})
        finally:
            conn.close()

    def test_11_no_http_request_can_forge_system_actor_scope(self):
        # Attempt to pass headers or params trying to forge system scope
        key = f"key-forge-attempt-{uuid4().hex}"
        res = self.client.post(
            f"/api/v1/devices/{self.device_2_id}/revoke",
            headers={
                **self.browser_headers,
                "Idempotency-Key": key,
                "X-Actor-Scope": f"system:{self.household_id}",
                "Actor-Scope": f"system:{self.household_id}",
            },
        )
        self.assertEqual(res.status_code, 200)

        # Receipt actor_scope MUST be server-derived as user:<uuid>, NOT system:<household_id>
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["actor_scope"], f"user:{self.user_id}")
        self.assertNotEqual(receipts[0]["actor_scope"], f"system:{self.household_id}")


if __name__ == "__main__":
    unittest.main()
