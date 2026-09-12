import threading
import unittest
from datetime import date
import hashlib
from typing import Any, Dict
from uuid import UUID, uuid4
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo
from app.repositories import devices as devices_repo
from app.repositories import audit as audit_repo
import app.repositories.simplified_schema as schema_repo
from app.auth.browser_verifier import StaticBrowserAuthVerifier, set_browser_verifier

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase


class TestAliasCategoryCommandReceiptsDb(BaseDbTestCase):
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
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.raw_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_token.encode("utf-8")).digest()
        self.headers = {"Authorization": f"Bearer {self.raw_token}"}

        self.auth_sub = f"auth0|test_user_{uuid4().hex[:8]}"
        self.jwt_token = f"valid.jwt.{uuid4().hex}"
        self.static_verifier.register_token(self.jwt_token, {"sub": self.auth_sub})
        self.browser_headers = {"Authorization": f"Bearer {self.jwt_token}"}

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
                accounts_repo.create_household(conn, self.household_id, "Household A", reporting_currency="CNY")
                accounts_repo.create_user(conn, self.user_id, self.auth_sub, "User A", "user_a@test.local")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "Device A", self.token_hash, household_id=self.household_id)

                accounts_repo.create_household(conn, self.household_b_id, "Household B", reporting_currency="CNY")
                accounts_repo.create_user(conn, self.user_b_id, f"sub_b_{uuid4().hex[:8]}", "User B", "user_b@test.local")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "Device B", self.token_b_hash, household_id=self.household_b_id)

                # Seed an account in household A
                self.account_id = uuid4()
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.account_id,
                    household_id=self.household_id,
                    name="Checking Account",
                    balance_scope="asset",
                    account_type="cash",
                    currency="CNY",
                    opened_on=date.today(),
                    status="active",
                )
        finally:
            conn.close()

    def _get_receipts(self, household_id: UUID) -> list[Dict[str, Any]]:
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, household_id, user_id, device_id, actor_scope,
                           idempotency_key, operation, request_hash, status,
                           response_http_status, failure_code, committed_at, row_version
                    FROM ingestion_requests
                    WHERE household_id = %s;
                    """,
                    (str(household_id),),
                )
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def _get_audits(self, household_id: UUID, entity_type: str, entity_id: UUID) -> list[Dict[str, Any]]:
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, household_id, actor_type, entity_type, entity_id,
                           action, actor_user_id, actor_device_id, source_request_id
                    FROM audit_events
                    WHERE household_id = %s AND entity_type = %s AND entity_id = %s
                    ORDER BY id ASC;
                    """,
                    (str(household_id), entity_type, str(entity_id)),
                )
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def test_01_header_contract_missing_or_invalid_key_rejected(self):
        # 1. POST alias missing key -> 422
        res = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "Alias Test"},
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 422)

        # 2. PATCH alias short key (<8 chars) -> 422
        dummy_alias_id = uuid4()
        res = self.client.patch(
            f"/api/v1/accounts/{self.account_id}/aliases/{dummy_alias_id}",
            json={"expected_version": 0, "alias": "ShortKey"},
            headers={**self.headers, "Idempotency-Key": "short"},
        )
        self.assertEqual(res.status_code, 422)

        # 3. POST category missing key -> 422
        res = self.client.post(
            "/api/v1/categories",
            json={"name": "Groceries", "type": "expense"},
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 422)

        # 4. PATCH category long key (>200 chars) -> 422
        dummy_cat_id = uuid4()
        res = self.client.patch(
            f"/api/v1/categories/{dummy_cat_id}",
            json={"expected_version": 0, "name": "LongKey"},
            headers={**self.headers, "Idempotency-Key": "k" * 201},
        )
        self.assertEqual(res.status_code, 422)

        # Ingestion requests must be completely empty (0 receipts created)
        receipts = self._get_receipts(self.household_id)
        self.assertEqual(len(receipts), 0)

    def test_02_successful_alias_crud_and_receipt_contract(self):
        key_create = f"key-alias-create-{uuid4().hex}"
        # 1. Create Alias
        res_create = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "招商工资卡"},
            headers={**self.headers, "Idempotency-Key": key_create},
        )
        self.assertEqual(res_create.status_code, 201)
        alias_data = res_create.json()
        alias_id = UUID(alias_data["id"])
        self.assertEqual(alias_data["alias"], "招商工资卡")
        self.assertEqual(alias_data["status"], "active")
        self.assertEqual(alias_data["row_version"], 0)

        # Verify receipt
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_create]
        self.assertEqual(len(receipts), 1)
        rec = receipts[0]
        self.assertEqual(rec["status"], "committed")
        self.assertEqual(rec["response_http_status"], 201)
        self.assertIsNotNone(rec["committed_at"])
        self.assertEqual(rec["actor_scope"], f"device:{self.device_id}")

        # Verify audit event links source_request_id to receipt id
        audits = self._get_audits(self.household_id, "account_alias", alias_id)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["action"], "create")
        self.assertEqual(audits[0]["source_request_id"], rec["id"])

        # 2. Replay create alias with same key -> byte-identical replay, 0 extra audits
        res_replay = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "招商工资卡"},
            headers={**self.headers, "Idempotency-Key": key_create},
        )
        self.assertEqual(res_replay.status_code, 201)
        self.assertEqual(res_replay.json(), alias_data)
        self.assertEqual(len(self._get_audits(self.household_id, "account_alias", alias_id)), 1)

        # 3. Patch Alias
        key_patch = f"key-alias-patch-{uuid4().hex}"
        res_patch = self.client.patch(
            f"/api/v1/accounts/{self.account_id}/aliases/{alias_id}",
            json={"expected_version": 0, "alias": "招商主卡"},
            headers={**self.headers, "Idempotency-Key": key_patch},
        )
        self.assertEqual(res_patch.status_code, 200)
        patched_data = res_patch.json()
        self.assertEqual(patched_data["alias"], "招商主卡")
        self.assertEqual(patched_data["row_version"], 1)

        # Verify patch receipt and audit
        rec_patch = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_patch][0]
        self.assertEqual(rec_patch["status"], "committed")
        self.assertEqual(rec_patch["response_http_status"], 200)

        audits_patch = self._get_audits(self.household_id, "account_alias", alias_id)
        self.assertEqual(len(audits_patch), 2)
        self.assertEqual(audits_patch[1]["action"], "update")
        self.assertEqual(audits_patch[1]["source_request_id"], rec_patch["id"])

        # 4. Replay patch alias even with stale expected_version=0 -> success replay
        res_patch_replay = self.client.patch(
            f"/api/v1/accounts/{self.account_id}/aliases/{alias_id}",
            json={"expected_version": 0, "alias": "招商主卡"},
            headers={**self.headers, "Idempotency-Key": key_patch},
        )
        self.assertEqual(res_patch_replay.status_code, 200)
        self.assertEqual(res_patch_replay.json(), patched_data)
        self.assertEqual(len(self._get_audits(self.household_id, "account_alias", alias_id)), 2)

    def test_03_successful_category_crud_and_receipt_contract(self):
        key_create = f"key-cat-create-{uuid4().hex}"
        # 1. Create Category
        res_create = self.client.post(
            "/api/v1/categories",
            json={"name": "Dining Out", "type": "expense", "description": "Restaurant meals"},
            headers={**self.headers, "Idempotency-Key": key_create},
        )
        self.assertEqual(res_create.status_code, 201)
        cat_data = res_create.json()
        cat_id = UUID(cat_data["id"])
        self.assertEqual(cat_data["name"], "Dining Out")
        self.assertEqual(cat_data["type"], "expense")
        self.assertEqual(cat_data["description"], "Restaurant meals")
        self.assertEqual(cat_data["row_version"], 0)

        # Verify receipt and audit
        rec = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_create][0]
        self.assertEqual(rec["status"], "committed")
        self.assertEqual(rec["response_http_status"], 201)
        self.assertIsNotNone(rec["committed_at"])

        audits = self._get_audits(self.household_id, "category", cat_id)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["action"], "create")
        self.assertEqual(audits[0]["source_request_id"], rec["id"])

        # 2. Replay create category -> success replay
        res_replay = self.client.post(
            "/api/v1/categories",
            json={"name": "Dining Out", "type": "expense", "description": "Restaurant meals"},
            headers={**self.headers, "Idempotency-Key": key_create},
        )
        self.assertEqual(res_replay.status_code, 201)
        self.assertEqual(res_replay.json(), cat_data)
        self.assertEqual(len(self._get_audits(self.household_id, "category", cat_id)), 1)

        # 3. Patch Category
        key_patch = f"key-cat-patch-{uuid4().hex}"
        res_patch = self.client.patch(
            f"/api/v1/categories/{cat_id}",
            json={"expected_version": 0, "name": "Food & Dining"},
            headers={**self.headers, "Idempotency-Key": key_patch},
        )
        self.assertEqual(res_patch.status_code, 200)
        patched_data = res_patch.json()
        self.assertEqual(patched_data["name"], "Food & Dining")
        self.assertEqual(patched_data["row_version"], 1)

        rec_patch = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_patch][0]
        self.assertEqual(rec_patch["status"], "committed")

        audits_patch = self._get_audits(self.household_id, "category", cat_id)
        self.assertEqual(len(audits_patch), 2)
        self.assertEqual(audits_patch[1]["action"], "update")
        self.assertEqual(audits_patch[1]["source_request_id"], rec_patch["id"])

        # 4. Replay patch category with stale version -> success replay
        res_patch_replay = self.client.patch(
            f"/api/v1/categories/{cat_id}",
            json={"expected_version": 0, "name": "Food & Dining"},
            headers={**self.headers, "Idempotency-Key": key_patch},
        )
        self.assertEqual(res_patch_replay.status_code, 200)
        self.assertEqual(res_patch_replay.json(), patched_data)
        self.assertEqual(len(self._get_audits(self.household_id, "category", cat_id)), 2)

    def test_04_idempotency_key_reuse_conflict_rejected(self):
        key = f"key-reuse-{uuid4().hex}"

        # 1. Alias Create with key
        res1 = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "Card A"},
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(res1.status_code, 201)

        # 2. Reuse key with changed body -> 409
        res_diff_body = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "Card B"},
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(res_diff_body.status_code, 409)
        self.assertEqual(res_diff_body.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

        # 3. Reuse key with different target account path -> 409
        other_acc_id = uuid4()
        res_diff_path = self.client.post(
            f"/api/v1/accounts/{other_acc_id}/aliases",
            json={"alias": "Card A"},
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(res_diff_path.status_code, 409)
        self.assertEqual(res_diff_path.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

        # 4. Reuse key on category endpoint -> 409
        res_diff_op = self.client.post(
            "/api/v1/categories",
            json={"name": "Card A", "type": "expense"},
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(res_diff_op.status_code, 409)
        self.assertEqual(res_diff_op.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

    def test_05_patch_field_presence_command_identity_distinction(self):
        # Create category with description
        key_init = f"key-cat-init-{uuid4().hex}"
        res_init = self.client.post(
            "/api/v1/categories",
            json={"name": "Travel", "type": "expense", "description": "Flights & hotels"},
            headers={**self.headers, "Idempotency-Key": key_init},
        )
        self.assertEqual(res_init.status_code, 201)
        cat_id = res_init.json()["id"]

        key_test = f"key-field-presence-{uuid4().hex}"

        # 1. PATCH with omitted description
        res_omitted = self.client.patch(
            f"/api/v1/categories/{cat_id}",
            json={"expected_version": 0, "name": "Travel & Holidays"},
            headers={**self.headers, "Idempotency-Key": key_test},
        )
        self.assertEqual(res_omitted.status_code, 200)
        # Description should still be Flights & hotels
        self.assertEqual(res_omitted.json()["description"], "Flights & hotels")
        self.assertEqual(res_omitted.json()["name"], "Travel & Holidays")

        # 2. Reusing same key with explicit description: null -> 409 IDEMPOTENCY_KEY_REUSE
        res_reuse = self.client.patch(
            f"/api/v1/categories/{cat_id}",
            json={"expected_version": 0, "name": "Travel & Holidays", "description": None},
            headers={**self.headers, "Idempotency-Key": key_test},
        )
        self.assertEqual(res_reuse.status_code, 409)
        self.assertEqual(res_reuse.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

        # 3. Executing explicit null description with a new key clears description
        key_clear = f"key-clear-desc-{uuid4().hex}"
        res_clear = self.client.patch(
            f"/api/v1/categories/{cat_id}",
            json={"expected_version": 1, "description": None},
            headers={**self.headers, "Idempotency-Key": key_clear},
        )
        self.assertEqual(res_clear.status_code, 200)
        self.assertIsNone(res_clear.json()["description"])

    def test_06_deterministic_alias_conflict_rejection_and_replay_parity(self):
        # Create first alias
        key_a1 = f"key-a1-{uuid4().hex}"
        res1 = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "Existing Alias"},
            headers={**self.headers, "Idempotency-Key": key_a1},
        )
        self.assertEqual(res1.status_code, 201)

        # Attempt to create duplicate alias -> 422 ACCOUNT_ALIAS_CONFLICT
        key_dup = f"key-dup-alias-{uuid4().hex}"
        res_dup = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "existing alias"},  # case-insensitive duplicate
            headers={**self.headers, "Idempotency-Key": key_dup},
        )
        self.assertEqual(res_dup.status_code, 422)
        err_json = res_dup.json()
        self.assertEqual(err_json["error"]["code"], "ACCOUNT_ALIAS_CONFLICT")

        # Verify receipt is terminal rejected with exact status and payload
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_dup]
        self.assertEqual(len(receipts), 1)
        rec = receipts[0]
        self.assertEqual(rec["status"], "rejected")
        self.assertEqual(rec["response_http_status"], 422)
        self.assertEqual(rec["failure_code"], "ACCOUNT_ALIAS_CONFLICT")
        self.assertIsNone(rec["committed_at"])

        # Exact replay of rejected command returns exact same 422 and JSON body
        res_replay = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "existing alias"},
            headers={**self.headers, "Idempotency-Key": key_dup},
        )
        self.assertEqual(res_replay.status_code, 422)
        self.assertEqual(res_replay.json(), err_json)

    def test_07_deterministic_fallback_category_rejection_and_replay_parity(self):
        # Seed fallback category
        fb_id = uuid4()
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                categories_repo.create_category(
                    conn=conn,
                    category_id=fb_id,
                    household_id=self.household_id,
                    name="Uncategorized Expense",
                    category_type="expense",
                    is_fallback=True,
                )
        finally:
            conn.close()

        # Attempt to deactivate fallback category via PATCH / status="inactive" -> 400 Bad Request
        key_fb = f"key-fb-deact-{uuid4().hex}"
        res_fb = self.client.patch(
            f"/api/v1/categories/{fb_id}",
            json={"expected_version": 0, "status": "inactive"},
            headers={**self.headers, "Idempotency-Key": key_fb},
        )
        self.assertEqual(res_fb.status_code, 400)
        fb_err_json = res_fb.json()
        self.assertEqual(fb_err_json["error"]["code"], "BAD_REQUEST")
        self.assertIn("Fallback category cannot be archived", fb_err_json["detail"])

        # Check receipt
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_fb]
        self.assertEqual(len(receipts), 1)
        rec = receipts[0]
        self.assertEqual(rec["status"], "rejected")
        self.assertEqual(rec["response_http_status"], 400)
        self.assertEqual(rec["failure_code"], "BAD_REQUEST")

        # Replay rejected fallback deactivation -> exact 400 and byte-identical JSON
        res_fb_replay = self.client.patch(
            f"/api/v1/categories/{fb_id}",
            json={"expected_version": 0, "status": "inactive"},
            headers={**self.headers, "Idempotency-Key": key_fb},
        )
        self.assertEqual(res_fb_replay.status_code, 400)
        self.assertEqual(res_fb_replay.json(), fb_err_json)

    def test_08_actor_isolation_device_and_browser(self):
        key = f"key-isolation-{uuid4().hex}"

        # 1. Device actor executes POST category
        res_dev = self.client.post(
            "/api/v1/categories",
            json={"name": "Device Category", "type": "expense"},
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(res_dev.status_code, 201)

        # 2. Browser actor executes same key and same payload in same household
        # Different actor_scope ("user:..." vs "device:...") -> should execute as independent command
        # Note: category name "Device Category" already exists so this should reject with 422 CATEGORY_NAME_CONFLICT
        res_browser = self.client.post(
            "/api/v1/categories",
            json={"name": "Device Category", "type": "expense"},
            headers={**self.browser_headers, "Idempotency-Key": key},
        )
        self.assertEqual(res_browser.status_code, 422)
        self.assertEqual(res_browser.json()["error"]["code"], "CATEGORY_NAME_CONFLICT")

        # Receipts table should have 2 receipts for this key: 1 committed (device), 1 rejected (user)
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
        self.assertEqual(len(receipts), 2)
        scopes = {r["actor_scope"]: r["status"] for r in receipts}
        self.assertEqual(scopes.get(f"device:{self.device_id}"), "committed")
        self.assertEqual(scopes.get(f"user:{self.user_id}"), "rejected")

    def test_09_atomic_rollback_on_unexpected_failure(self):
        # Trigger an unexpected exception inside mutation by mocking / patching
        from unittest.mock import patch
        key_crash = f"key-crash-{uuid4().hex}"

        with patch("app.repositories.categories.create_category", side_effect=RuntimeError("Simulated DB connection drop")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/api/v1/categories",
                    json={"name": "Crash Category", "type": "expense"},
                    headers={**self.headers, "Idempotency-Key": key_crash},
                )

        # Ensure zero partial mutation, zero audit event, and zero committed receipt
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_crash]
        self.assertEqual(len(receipts), 0)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM categories WHERE name = 'Crash Category';")
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_10_concurrent_same_key_execution(self):
        barrier = threading.Barrier(2)
        key = f"key-race-{uuid4().hex}"
        results = []

        def worker():
            app = create_app()
            client = TestClient(app)

            def _get_db():
                conn = get_connection(self.test_schema)
                try:
                    yield conn
                finally:
                    if not conn.closed:
                        conn.close()

            app.dependency_overrides[get_db_connection] = _get_db
            barrier.wait()
            res = client.post(
                "/api/v1/categories",
                json={"name": "Race Category", "type": "expense"},
                headers={**self.headers, "Idempotency-Key": key},
            )
            results.append(res)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        # Both must return 201 Created and identical JSON
        for r in results:
            self.assertEqual(r.status_code, 201)
        self.assertEqual(results[0].json(), results[1].json())

        # Exactly 1 receipt in committed status
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "committed")

        # Exactly 1 category record in DB
        cat_id = UUID(results[0].json()["id"])
        audits = self._get_audits(self.household_id, "category", cat_id)
        self.assertEqual(len(audits), 1)

    def test_11_race_safe_unique_constraint_alias_conflict(self):
        from unittest.mock import patch
        key = f"key-alias-integrity-{uuid4().hex}"

        # Seed an existing alias in DB
        key_init = f"key-alias-init-{uuid4().hex}"
        self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "Unique Card"},
            headers={**self.headers, "Idempotency-Key": key_init},
        )

        # Mock check_account_alias_exists to return False, simulating a race where pre-check passed
        # but DB unique constraint uq_account_aliases_active triggers on INSERT
        with patch("app.repositories.accounts.check_account_alias_exists", return_value=False):
            res = self.client.post(
                f"/api/v1/accounts/{self.account_id}/aliases",
                json={"alias": "Unique Card"},
                headers={**self.headers, "Idempotency-Key": key},
            )
            self.assertEqual(res.status_code, 422)
            self.assertEqual(res.json()["error"]["code"], "ACCOUNT_ALIAS_CONFLICT")
            err_json = res.json()

        # Verify terminal rejected receipt
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "rejected")
        self.assertEqual(receipts[0]["response_http_status"], 422)
        self.assertEqual(receipts[0]["failure_code"], "ACCOUNT_ALIAS_CONFLICT")

        # Verify exact retry replay of rejection
        res_replay = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "Unique Card"},
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(res_replay.status_code, 422)
        self.assertEqual(res_replay.json(), err_json)

    def test_12_race_safe_unique_constraint_category_conflict(self):
        from unittest.mock import patch
        key = f"key-cat-integrity-{uuid4().hex}"

        # Seed an existing category in DB
        key_init = f"key-cat-init-{uuid4().hex}"
        self.client.post(
            "/api/v1/categories",
            json={"name": "Unique Expense", "type": "expense"},
            headers={**self.headers, "Idempotency-Key": key_init},
        )

        # Mock check_category_name_exists to return False, simulating a race where pre-check passed
        # but DB unique constraint uq_categories_active_name triggers on INSERT
        with patch("app.repositories.categories.check_category_name_exists", return_value=False):
            res = self.client.post(
                "/api/v1/categories",
                json={"name": "Unique Expense", "type": "expense"},
                headers={**self.headers, "Idempotency-Key": key},
            )
            self.assertEqual(res.status_code, 422)
            self.assertEqual(res.json()["error"]["code"], "CATEGORY_NAME_CONFLICT")
            err_json = res.json()

        # Verify terminal rejected receipt
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "rejected")
        self.assertEqual(receipts[0]["response_http_status"], 422)
        self.assertEqual(receipts[0]["failure_code"], "CATEGORY_NAME_CONFLICT")

        # Verify exact retry replay of rejection
        res_replay = self.client.post(
            "/api/v1/categories",
            json={"name": "Unique Expense", "type": "expense"},
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(res_replay.status_code, 422)
        self.assertEqual(res_replay.json(), err_json)

    def test_13_non_target_integrity_error_in_alias_mutation_is_not_translated(self):
        from unittest.mock import patch
        import psycopg2
        key_nontarget = f"key-alias-nontarget-{uuid4().hex}"

        # Trigger real non-target IntegrityError (e.g. check constraint chk_account_aliases_status)
        def _fail_with_nontarget_constraint(*args, **kwargs):
            conn = kwargs.get("conn") or (args[0] if args else None)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO account_aliases (household_id, account_id, alias_text, normalized_alias, status) "
                    "VALUES (%s, %s, %s, %s, %s);",
                    (str(self.household_id), str(self.account_id), "BadStatus", "badstatus", "invalid_status")
                )

        with patch("app.repositories.accounts.create_account_alias", side_effect=_fail_with_nontarget_constraint):
            with self.assertRaises(psycopg2.IntegrityError) as ctx:
                self.client.post(
                    f"/api/v1/accounts/{self.account_id}/aliases",
                    json={"alias": "NonTarget"},
                    headers={**self.headers, "Idempotency-Key": key_nontarget},
                )
            self.assertEqual(getattr(ctx.exception.diag, "constraint_name", None), "chk_account_aliases_status")

        # Verify unexpected failure rolled back: NO receipt, NO mutation, NO audit
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_nontarget]
        self.assertEqual(len(receipts), 0)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM account_aliases WHERE alias_text = 'NonTarget';")
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_14_non_target_integrity_error_in_category_mutation_is_not_translated(self):
        from unittest.mock import patch
        import psycopg2
        key_nontarget = f"key-cat-nontarget-{uuid4().hex}"

        # Trigger real non-target IntegrityError (e.g. check constraint chk_categories_status)
        def _fail_with_nontarget_constraint(*args, **kwargs):
            conn = kwargs.get("conn") or (args[0] if args else None)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO categories (id, household_id, name, category_type, status, is_fallback) "
                    "VALUES (%s, %s, %s, %s, %s, %s);",
                    (str(uuid4()), str(self.household_id), "BadStatusCat", "expense", "invalid_status", False)
                )

        with patch("app.repositories.categories.create_category", side_effect=_fail_with_nontarget_constraint):
            with self.assertRaises(psycopg2.IntegrityError) as ctx:
                self.client.post(
                    "/api/v1/categories",
                    json={"name": "NonTargetCat", "type": "expense"},
                    headers={**self.headers, "Idempotency-Key": key_nontarget},
                )
            self.assertEqual(getattr(ctx.exception.diag, "constraint_name", None), "chk_categories_status")

        # Verify unexpected failure rolled back: NO receipt, NO mutation, NO audit
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_nontarget]
        self.assertEqual(len(receipts), 0)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM categories WHERE name = 'NonTargetCat';")
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_15_alias_atomic_rollback_on_unexpected_failure(self):
        # Trigger unexpected exception after alias command execution begins
        from unittest.mock import patch
        key_crash = f"key-alias-crash-{uuid4().hex}"

        with patch("app.repositories.accounts.create_account_alias", side_effect=RuntimeError("Simulated alias failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"/api/v1/accounts/{self.account_id}/aliases",
                    json={"alias": "Crash Alias"},
                    headers={**self.headers, "Idempotency-Key": key_crash},
                )

        # Ensure zero partial mutation, zero audit event, and zero surviving receipt
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key_crash]
        self.assertEqual(len(receipts), 0)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM account_aliases WHERE alias_text = 'Crash Alias';")
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_16_concurrent_same_key_alias_execution(self):
        barrier = threading.Barrier(2)
        key = f"key-race-alias-{uuid4().hex}"
        results = []

        def worker():
            app = create_app()
            client = TestClient(app)

            def _get_db():
                conn = get_connection(self.test_schema)
                try:
                    yield conn
                finally:
                    if not conn.closed:
                        conn.close()

            app.dependency_overrides[get_db_connection] = _get_db
            barrier.wait()
            res = client.post(
                f"/api/v1/accounts/{self.account_id}/aliases",
                json={"alias": "Race Alias"},
                headers={**self.headers, "Idempotency-Key": key},
            )
            results.append(res)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        # Both must return 201 Created and identical JSON
        for r in results:
            self.assertEqual(r.status_code, 201)
        self.assertEqual(results[0].json(), results[1].json())

        # Exactly 1 receipt in committed status
        receipts = [r for r in self._get_receipts(self.household_id) if r["idempotency_key"] == key]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "committed")

        # Exactly 1 alias record in DB
        alias_id = UUID(results[0].json()["id"])
        audits = self._get_audits(self.household_id, "account_alias", alias_id)
        self.assertEqual(len(audits), 1)

    def test_17_patch_alias_and_category_target_id_reuse_counterexamples(self):
        # 1. Seed alias and category
        res_alias = self.client.post(
            f"/api/v1/accounts/{self.account_id}/aliases",
            json={"alias": "Original Alias"},
            headers={**self.headers, "Idempotency-Key": f"key-seed-alias-{uuid4().hex}"},
        )
        alias_1_id = res_alias.json()["id"]

        res_cat = self.client.post(
            "/api/v1/categories",
            json={"name": "Original Category", "type": "expense"},
            headers={**self.headers, "Idempotency-Key": f"key-seed-cat-{uuid4().hex}"},
        )
        cat_1_id = res_cat.json()["id"]

        # 2. PATCH alias counterexample: same actor + same key + different alias_id -> 409 IDEMPOTENCY_KEY_REUSE
        key_alias_patch = f"key-patch-alias-{uuid4().hex}"
        res_patch_1 = self.client.patch(
            f"/api/v1/accounts/{self.account_id}/aliases/{alias_1_id}",
            json={"expected_version": 0, "alias": "Renamed Alias"},
            headers={**self.headers, "Idempotency-Key": key_alias_patch},
        )
        self.assertEqual(res_patch_1.status_code, 200)

        # Different alias_id (even non-existent random UUID) under the same key -> 409
        diff_alias_id = uuid4()
        res_patch_diff_alias = self.client.patch(
            f"/api/v1/accounts/{self.account_id}/aliases/{diff_alias_id}",
            json={"expected_version": 0, "alias": "Renamed Alias"},
            headers={**self.headers, "Idempotency-Key": key_alias_patch},
        )
        self.assertEqual(res_patch_diff_alias.status_code, 409)
        self.assertEqual(res_patch_diff_alias.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

        # 3. PATCH category counterexample: same actor + same key + different category_id -> 409 IDEMPOTENCY_KEY_REUSE
        key_cat_patch = f"key-patch-cat-{uuid4().hex}"
        res_cat_1 = self.client.patch(
            f"/api/v1/categories/{cat_1_id}",
            json={"expected_version": 0, "name": "Renamed Category"},
            headers={**self.headers, "Idempotency-Key": key_cat_patch},
        )
        self.assertEqual(res_cat_1.status_code, 200)

        # Different category_id (even non-existent random UUID) under the same key -> 409
        diff_cat_id = uuid4()
        res_patch_diff_cat = self.client.patch(
            f"/api/v1/categories/{diff_cat_id}",
            json={"expected_version": 0, "name": "Renamed Category"},
            headers={**self.headers, "Idempotency-Key": key_cat_patch},
        )
        self.assertEqual(res_patch_diff_cat.status_code, 409)
        self.assertEqual(res_patch_diff_cat.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")


if __name__ == "__main__":
    unittest.main()
