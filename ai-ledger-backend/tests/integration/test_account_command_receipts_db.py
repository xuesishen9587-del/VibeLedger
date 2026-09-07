import unittest
from uuid import UUID, uuid4
import hashlib
from decimal import Decimal
from datetime import date, datetime, timezone
import threading
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.repositories import accounts as accounts_repo
from app.repositories import devices as devices_repo
from app.repositories import audit as audit_repo
from app.repositories import categories as categories_repo
import app.repositories.simplified_schema as schema_repo
from app.services.account_commands import compute_command_hash
from app.api.routes.accounts import CreateAccountRequest
from app.auth.browser_verifier import StaticBrowserAuthVerifier, set_browser_verifier

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase


class TestAccountCommandReceiptsDb(BaseDbTestCase):
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
        schema_repo.create_household(
            self.conn,
            household_id=self.household_id,
            name="Receipt Test Household",
            reporting_currency="CNY"
        )
        self.user_id = uuid4()
        self.auth_sub = f"auth0|test_user_{uuid4().hex[:8]}"
        schema_repo.create_user(
            self.conn,
            user_id=self.user_id,
            auth_subject=self.auth_sub,
            email=f"{self.auth_sub}@example.com",
            display_name="Receipt User"
        )
        schema_repo.add_household_member(
            self.conn,
            household_id=self.household_id,
            user_id=self.user_id,
            role="owner"
        )

        # Device credentials
        self.device_id = uuid4()
        self.raw_device_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_device_token.encode("utf-8")).digest()
        devices_repo.create_device(
            self.conn,
            self.device_id,
            self.user_id,
            "Test Device",
            self.token_hash,
            platform="ios",
            household_id=self.household_id
        )
        self.device_headers = {"Authorization": f"Bearer {self.raw_device_token}"}

        # Browser JWT credentials
        self.jwt_token = f"valid.jwt.{uuid4().hex}"
        self.static_verifier.register_token(self.jwt_token, {"sub": self.auth_sub})
        self.browser_headers = {"Authorization": f"Bearer {self.jwt_token}"}

        self.conn.commit()

    def test_01_header_contract_missing_or_invalid_key_rejected(self):
        """
        Requirements 1-3:
        For each of the five account mutation endpoints:
        - Missing Idempotency-Key is rejected (422)
        - Invalid key length (<8 or >200) is rejected (422)
        - Zero mutations occur and zero ingestion_requests are created
        """
        # Create an account first for the non-create endpoints
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Existing Account",
            account_type="cash",
            currency="CNY",
            opened_on=date(2026, 1, 1)
        )
        self.conn.commit()

        endpoints = [
            ("POST", "/api/v1/accounts", {
                "name": "New Acc",
                "balance_scope": "Scope",
                "account_type": "cash",
                "currency": "CNY"
            }),
            ("PATCH", f"/api/v1/accounts/{acc_id}", {
                "name": "Patched Acc",
                "expected_version": 0
            }),
            ("POST", f"/api/v1/accounts/{acc_id}/close", {
                "expected_version": 0,
                "closed_on": "2026-06-01",
                "closing_snapshot_id": str(uuid4())
            }),
            ("POST", f"/api/v1/accounts/{acc_id}/reopen", {
                "expected_version": 0
            }),
            ("POST", f"/api/v1/accounts/{acc_id}/cancel", {
                "expected_version": 0
            }),
        ]

        for method, url, payload in endpoints:
            # 1. Missing header
            if method == "POST":
                res = self.client.post(url, json=payload, headers=self.device_headers)
            else:
                res = self.client.patch(url, json=payload, headers=self.device_headers)
            self.assertEqual(res.status_code, 422, f"Expected 422 for missing key on {method} {url}")

            # 2. Key too short (< 8 chars)
            h_short = {**self.device_headers, "Idempotency-Key": "short"}
            if method == "POST":
                res = self.client.post(url, json=payload, headers=h_short)
            else:
                res = self.client.patch(url, json=payload, headers=h_short)
            self.assertEqual(res.status_code, 422, f"Expected 422 for short key on {method} {url}")

            # 3. Key too long (> 200 chars)
            h_long = {**self.device_headers, "Idempotency-Key": "k" * 201}
            if method == "POST":
                res = self.client.post(url, json=payload, headers=h_long)
            else:
                res = self.client.patch(url, json=payload, headers=h_long)
            self.assertEqual(res.status_code, 422, f"Expected 422 for long key on {method} {url}")

        # Assert no ingestion_requests were inserted
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingestion_requests WHERE household_id = %s;", (str(self.household_id),))
            cnt = cur.fetchone()[0]
            self.assertEqual(cnt, 0, "No receipts should be created for rejected header requests")

    def test_02_successful_account_create_replay_and_receipt_contract(self):
        """
        Requirements 4-11:
        - First create succeeds (201)
        - Exactly one committed command receipt exists with correct metadata
        - Exact retry returns same HTTP status and body
        - Exactly one account row and one audit event exist
        """
        key = "idemp-key-create-001"
        payload = {
            "name": "Standard Cash",
            "balance_scope": "Primary Cash",
            "account_type": "cash",
            "currency": "CNY"
        }
        headers = {**self.device_headers, "Idempotency-Key": key}

        # 1. First execution
        res1 = self.client.post("/api/v1/accounts", json=payload, headers=headers)
        self.assertEqual(res1.status_code, 201)
        data1 = res1.json()
        self.assertEqual(data1["name"], "Standard Cash")
        acc_id = data1["id"]

        # 2. Verify command receipt
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, household_id, actor_scope, idempotency_key, request_kind, operation,
                       request_hash, status, response_http_status, committed_at, response_payload
                FROM ingestion_requests
                WHERE household_id = %s AND idempotency_key = %s;
                """,
                (str(self.household_id), key)
            )
            receipt = cur.fetchone()
            self.assertIsNotNone(receipt)
            receipt_id = receipt[0]
            self.assertEqual(receipt[2], f"device:{self.device_id}")
            self.assertEqual(receipt[3], key)
            self.assertEqual(receipt[4], "command")
            self.assertEqual(receipt[5], "POST /api/v1/accounts")
            expected_hash = compute_command_hash("POST /api/v1/accounts", CreateAccountRequest(**payload).model_dump(mode="json"))
            self.assertEqual(receipt[6], expected_hash)
            self.assertEqual(receipt[7], "committed")
            self.assertEqual(receipt[8], 201)
            self.assertIsNotNone(receipt[9], "committed_at must be set")
            self.assertEqual(receipt[10]["id"], acc_id)

        # 3. Exact retry with same key and body
        res2 = self.client.post("/api/v1/accounts", json=payload, headers=headers)
        self.assertEqual(res2.status_code, 201)
        data2 = res2.json()
        self.assertEqual(data1, data2)

        # 4. Verify no duplicates in database
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM accounts WHERE household_id = %s AND name = 'Standard Cash';", (str(self.household_id),))
            self.assertEqual(cur.fetchone()[0], 1)

            cur.execute("SELECT COUNT(*) FROM audit_events WHERE household_id = %s AND entity_id = %s;", (str(self.household_id), acc_id))
            self.assertEqual(cur.fetchone()[0], 1)

            cur.execute("SELECT source_request_id FROM audit_events WHERE household_id = %s AND entity_id = %s;", (str(self.household_id), acc_id))
            audit_source_id = str(cur.fetchone()[0])
            self.assertEqual(audit_source_id, str(receipt_id))

    def test_03_replay_before_state_checks_patch_with_stale_expected_version(self):
        """
        Requirements 12 & 14:
        - Successful PATCH followed by identical retry with now-stale expected_version replays success
        - Replay does not increment row_version and does not duplicate audit
        """
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Version Test Acc",
            account_type="cash",
            currency="CNY"
        )
        self.conn.commit()

        key = "idemp-key-patch-stale-001"
        payload = {
            "name": "Version Test Renamed",
            "expected_version": 0
        }
        headers = {**self.device_headers, "Idempotency-Key": key}

        # First call: succeeds from version 0 -> 1
        res1 = self.client.patch(f"/api/v1/accounts/{acc_id}", json=payload, headers=headers)
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["row_version"], 1)

        # Second call: identical retry using original expected_version=0
        # If state check happened first, it would 409 ROW_VERSION_CONFLICT.
        # But receipt replay must precede state check!
        res2 = self.client.patch(f"/api/v1/accounts/{acc_id}", json=payload, headers=headers)
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res1.json(), res2.json())

        # Verify row_version remains 1, audit events count is exactly 1 update
        acc = accounts_repo.get_account(self.conn, acc_id, self.household_id)
        self.assertEqual(acc["row_version"], 1)

        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM audit_events WHERE household_id = %s AND entity_id = %s AND action = 'update';", (str(self.household_id), str(acc_id)))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_04_replay_before_state_checks_close_with_stale_version(self):
        """
        Requirements 13 & 14:
        - Successful close followed by identical retry with now-stale expected_version replays success
        """
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Close Test Acc",
            account_type="cash",
            currency="CNY",
            opened_on=date(2026, 1, 1)
        )
        # Create explicit zero closing snapshot
        snap_id = uuid4()
        req_id = uuid4()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_requests (
                    id, household_id, user_id, device_id, actor_scope,
                    idempotency_key, request_kind, operation, request_hash, status, committed_at
                ) VALUES (%s, %s, %s, %s, 'device:test', %s, 'command', 'snapshot', %s, 'committed', now());
                """,
                (str(req_id), str(self.household_id), str(self.user_id), str(self.device_id), f"key_{uuid4().hex[:16]}", '0'*64)
            )
            cur.execute(
                """
                INSERT INTO account_snapshots (
                    id, household_id, account_id, as_of, time_basis, balance,
                    currency, source, status, created_by_user_id, source_request_id
                ) VALUES (
                    %s, %s, %s, '2026-06-01 12:00:00+00', 'explicit', 0.00,
                    'CNY', 'manual', 'active', %s, %s
                );
                """,
                (str(snap_id), str(self.household_id), str(acc_id), str(self.user_id), str(req_id))
            )
        self.conn.commit()

        key = "idemp-key-close-stale-001"
        payload = {
            "expected_version": 0,
            "closed_on": "2026-06-01",
            "closing_snapshot_id": str(snap_id)
        }
        headers = {**self.device_headers, "Idempotency-Key": key}

        # First close
        res1 = self.client.post(f"/api/v1/accounts/{acc_id}/close", json=payload, headers=headers)
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["status"], "closed")

        # Stale retry with expected_version=0
        res2 = self.client.post(f"/api/v1/accounts/{acc_id}/close", json=payload, headers=headers)
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res1.json(), res2.json())

        acc = accounts_repo.get_account(self.conn, acc_id, self.household_id)
        self.assertEqual(acc["status"], "closed")
        self.assertEqual(acc["row_version"], 1)

    def test_05_key_reuse_conflict_changed_body(self):
        """
        Requirement 15:
        Same actor + same key + changed body -> 409 IDEMPOTENCY_KEY_REUSE
        """
        key = "idemp-key-reuse-body-001"
        headers = {**self.device_headers, "Idempotency-Key": key}

        res1 = self.client.post("/api/v1/accounts", json={
            "name": "First Name Acc",
            "balance_scope": "Scope A",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=headers)
        self.assertEqual(res1.status_code, 201)

        # Same key, changed body
        res2 = self.client.post("/api/v1/accounts", json={
            "name": "Second Name Acc",
            "balance_scope": "Scope B",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=headers)
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

        # Second account was NOT created
        exists = accounts_repo.check_account_name_exists(self.conn, self.household_id, "Second Name Acc")
        self.assertFalse(exists)

    def test_06_key_reuse_conflict_different_account_target(self):
        """
        Requirement 16:
        Same actor + same key + different account target -> 409 IDEMPOTENCY_KEY_REUSE
        """
        acc_a = uuid4()
        accounts_repo.create_account(self.conn, account_id=acc_a, household_id=self.household_id, name="Acc Target A", account_type="cash", currency="CNY")
        acc_b = uuid4()
        accounts_repo.create_account(self.conn, account_id=acc_b, household_id=self.household_id, name="Acc Target B", account_type="cash", currency="CNY")
        self.conn.commit()

        key = "idemp-key-reuse-target-001"
        headers = {**self.device_headers, "Idempotency-Key": key}
        patch_body = {"name": "Renamed Target", "expected_version": 0}

        res1 = self.client.patch(f"/api/v1/accounts/{acc_a}", json=patch_body, headers=headers)
        self.assertEqual(res1.status_code, 200)

        # Same key used on different account resource
        res2 = self.client.patch(f"/api/v1/accounts/{acc_b}", json=patch_body, headers=headers)
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

        # Acc B untouched
        b_row = accounts_repo.get_account(self.conn, acc_b, self.household_id)
        self.assertEqual(b_row["name"], "Acc Target B")

    def test_07_key_reuse_conflict_different_operation(self):
        """
        Requirement 17 & 18:
        Same actor + same key + different account operation -> 409 IDEMPOTENCY_KEY_REUSE
        """
        key = "idemp-key-reuse-op-001"
        headers = {**self.device_headers, "Idempotency-Key": key}

        res1 = self.client.post("/api/v1/accounts", json={
            "name": "Op Test Acc",
            "balance_scope": "Scope",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=headers)
        self.assertEqual(res1.status_code, 201)
        acc_id = res1.json()["id"]

        # Same key used for reopen
        res2 = self.client.post(f"/api/v1/accounts/{acc_id}/reopen", json={"expected_version": 0}, headers=headers)
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

    def test_08_actor_isolation_device_and_browser(self):
        """
        Requirements 19-21:
        - Browser and device may independently reuse the same textual key without collision
        - Receipts have different actor_scope (device:<uuid> vs user:<uuid>)
        - Client cannot forge system:* or other actor scopes
        """
        key = "shared-textual-key-12345"

        # 1. Device creates account with key
        dev_headers = {**self.device_headers, "Idempotency-Key": key}
        res_dev = self.client.post("/api/v1/accounts", json={
            "name": "Device Created Acc",
            "balance_scope": "Dev",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=dev_headers)
        self.assertEqual(res_dev.status_code, 201)

        # 2. Browser user creates account with EXACT SAME textual key
        browser_headers = {**self.browser_headers, "Idempotency-Key": key}
        res_browser = self.client.post("/api/v1/accounts", json={
            "name": "Browser Created Acc",
            "balance_scope": "Browser",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=browser_headers)
        self.assertEqual(res_browser.status_code, 201)

        # 3. Check receipts in DB
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor_scope, status FROM ingestion_requests
                WHERE household_id = %s AND idempotency_key = %s
                ORDER BY actor_scope;
                """,
                (str(self.household_id), key)
            )
            rows = cur.fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], f"device:{self.device_id}")
            self.assertEqual(rows[1][0], f"user:{self.user_id}")

    def test_09_rejected_result_replay(self):
        """
        Requirements 22-24:
        - Deterministic rejected command stores terminal 'rejected' receipt
        - Identical retry returns the stored rejection
        - Changed body under the same key conflicts (409) instead of replacing it
        """
        # Create initial account
        acc1 = uuid4()
        accounts_repo.create_account(self.conn, account_id=acc1, household_id=self.household_id, name="Unique Active Name", account_type="cash", currency="CNY")
        self.conn.commit()

        key = "idemp-key-rejected-001"
        headers = {**self.device_headers, "Idempotency-Key": key}
        payload = {
            "name": "Unique Active Name", # Duplicate active name -> deterministic rejection
            "balance_scope": "Scope",
            "account_type": "cash",
            "currency": "CNY"
        }

        # 1. First execution fails with 422 ACCOUNT_NAME_CONFLICT
        res1 = self.client.post("/api/v1/accounts", json=payload, headers=headers)
        self.assertEqual(res1.status_code, 422)
        self.assertEqual(res1.json()["error"]["code"], "ACCOUNT_NAME_CONFLICT")

        # Check receipt in DB has status='rejected'
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, response_http_status, failure_code, committed_at
                FROM ingestion_requests
                WHERE household_id = %s AND idempotency_key = %s;
                """,
                (str(self.household_id), key)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "rejected")
            self.assertEqual(row[1], 422)
            self.assertEqual(row[2], "ACCOUNT_NAME_CONFLICT")
            self.assertIsNone(row[3]) # committed_at must be NULL

        # 2. Identical retry returns stored rejection
        res2 = self.client.post("/api/v1/accounts", json=payload, headers=headers)
        self.assertEqual(res2.status_code, 422)
        self.assertEqual(res2.json(), res1.json())

        # 3. Changed body under same key -> 409 IDEMPOTENCY_KEY_REUSE
        res3 = self.client.post("/api/v1/accounts", json={
            "name": "Completely Different Name",
            "balance_scope": "Scope",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=headers)
        self.assertEqual(res3.status_code, 409)
        self.assertEqual(res3.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

    def test_10_atomic_rollback_on_unexpected_failure(self):
        """
        Requirements 25-29:
        - Inject failure during mutation transaction
        - After rollback: no partial account, no audit event, no committed receipt survives
        - Clean retry with same key can execute normally
        """
        key = "idemp-key-rollback-001"
        headers = {**self.device_headers, "Idempotency-Key": key}
        payload = {
            "name": "Rollback Target Acc",
            "balance_scope": "Scope",
            "account_type": "cash",
            "currency": "CNY"
        }

        # Monkeypatch audit_repo.insert_audit_event to simulate sudden crash during transaction
        orig_insert = audit_repo.insert_audit_event
        def _failing_insert(*args, **kwargs):
            raise RuntimeError("Injected database/transaction crash")

        audit_repo.insert_audit_event = _failing_insert
        try:
            with self.assertRaises(Exception):
                self.client.post("/api/v1/accounts", json=payload, headers=headers)
        finally:
            audit_repo.insert_audit_event = orig_insert

        # Verify DB state: no account, no audit, no receipt survives
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM accounts WHERE household_id = %s AND name = 'Rollback Target Acc';", (str(self.household_id),))
            self.assertEqual(cur.fetchone()[0], 0)

            cur.execute("SELECT COUNT(*) FROM audit_events WHERE household_id = %s AND after_data->>'name' = 'Rollback Target Acc';", (str(self.household_id),))
            self.assertEqual(cur.fetchone()[0], 0)

            cur.execute("SELECT COUNT(*) FROM ingestion_requests WHERE household_id = %s AND idempotency_key = %s;", (str(self.household_id), key))
            self.assertEqual(cur.fetchone()[0], 0, "Uncommitted receipt must not survive rollback")

        # Clean retry with same key executes normally
        res_clean = self.client.post("/api/v1/accounts", json=payload, headers=headers)
        self.assertEqual(res_clean.status_code, 201)
        self.assertEqual(res_clean.json()["name"], "Rollback Target Acc")

    def test_11_concurrent_same_key_execution(self):
        """
        Requirements 30-35:
        - Two concurrent threads execute the same valid command using threading.Barrier
        - Exactly one mutation commits
        - Exactly one audit event commits
        - Exactly one command receipt exists
        - Both callers resolve to the same stored logical result
        """
        key = f"concurrent-key-{uuid4().hex}"
        payload = {
            "name": f"Concurrent Acc {uuid4().hex[:6]}",
            "balance_scope": "Scope",
            "account_type": "cash",
            "currency": "CNY"
        }
        headers = {**self.device_headers, "Idempotency-Key": key}

        barrier = threading.Barrier(2)
        results = []

        def worker():
            client = TestClient(self.app)
            barrier.wait()
            res = client.post("/api/v1/accounts", json=payload, headers=headers)
            results.append((res.status_code, res.json()))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        # Both must receive 201 Created with identical response body
        self.assertEqual(results[0][0], 201)
        self.assertEqual(results[1][0], 201)
        self.assertEqual(results[0][1], results[1][1])

        acc_id = results[0][1]["id"]

        # Exactly 1 account, 1 audit event, 1 command receipt
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM accounts WHERE household_id = %s AND id = %s;", (str(self.household_id), acc_id))
            self.assertEqual(cur.fetchone()[0], 1)

            cur.execute("SELECT COUNT(*) FROM audit_events WHERE household_id = %s AND entity_id = %s;", (str(self.household_id), acc_id))
            self.assertEqual(cur.fetchone()[0], 1)

            cur.execute("SELECT COUNT(*) FROM ingestion_requests WHERE household_id = %s AND idempotency_key = %s;", (str(self.household_id), key))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_12_patch_field_presence_command_identity_conflicts(self):
        """
        Requirements for Defect 1:
        - Omitted risk_level vs explicit risk_level: null under same actor/key conflicts (409 IDEMPOTENCY_KEY_REUSE)
        - Omitted owner_user_id vs explicit owner_user_id: null under same actor/key conflicts (409 IDEMPOTENCY_KEY_REUSE)
        - Neither changed command is silently replayed
        - Exact retry of truly identical PATCH still replays successfully
        - Stale expected_version exact replay continues to work
        """
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Patch Field Pres Acc",
            account_type="investment",
            currency="CNY",
            owner_user_id=self.user_id,
            risk_level="medium",
            opened_on=date(2026, 1, 1)
        )
        self.conn.commit()

        # --- Case A: risk_level omitted vs explicit null ---
        key_a = f"idemp-key-patch-risk-{uuid4().hex[:8]}"
        headers_a = {**self.device_headers, "Idempotency-Key": key_a}

        # 1. First execution: omit risk_level
        payload_a_omitted = {
            "name": "Renamed Without Risk",
            "expected_version": 0
        }
        res_a1 = self.client.patch(f"/api/v1/accounts/{acc_id}", json=payload_a_omitted, headers=headers_a)
        self.assertEqual(res_a1.status_code, 200)
        self.assertEqual(res_a1.json()["name"], "Renamed Without Risk")
        self.assertEqual(res_a1.json()["risk_level"], "medium")
        self.assertEqual(res_a1.json()["row_version"], 1)

        # 2. Exact retry of the truly identical PATCH (even with now-stale expected_version=0) replays successfully
        res_a_replay = self.client.patch(f"/api/v1/accounts/{acc_id}", json=payload_a_omitted, headers=headers_a)
        self.assertEqual(res_a_replay.status_code, 200)
        self.assertEqual(res_a_replay.json(), res_a1.json())

        # 3. Same key with explicit risk_level: null conflicts with IDEMPOTENCY_KEY_REUSE
        payload_a_null = {
            "name": "Renamed Without Risk",
            "risk_level": None,
            "expected_version": 0
        }
        res_a_conflict = self.client.patch(f"/api/v1/accounts/{acc_id}", json=payload_a_null, headers=headers_a)
        self.assertEqual(res_a_conflict.status_code, 409)
        self.assertEqual(res_a_conflict.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

        # Verify DB state: risk_level is still "medium", not cleared to null
        acc = accounts_repo.get_account(self.conn, acc_id, self.household_id)
        self.assertEqual(acc["risk_level"], "medium")
        self.assertEqual(acc["row_version"], 1)

        # --- Case B: owner_user_id omitted vs explicit null ---
        key_b = f"idemp-key-patch-owner-{uuid4().hex[:8]}"
        headers_b = {**self.device_headers, "Idempotency-Key": key_b}

        # 1. First execution: omit owner_user_id
        payload_b_omitted = {
            "name": "Renamed Without Owner",
            "expected_version": 1
        }
        res_b1 = self.client.patch(f"/api/v1/accounts/{acc_id}", json=payload_b_omitted, headers=headers_b)
        self.assertEqual(res_b1.status_code, 200)
        self.assertEqual(res_b1.json()["name"], "Renamed Without Owner")
        self.assertEqual(res_b1.json()["owner_user_id"], str(self.user_id))
        self.assertEqual(res_b1.json()["row_version"], 2)

        # 2. Exact retry of the truly identical PATCH replays successfully
        res_b_replay = self.client.patch(f"/api/v1/accounts/{acc_id}", json=payload_b_omitted, headers=headers_b)
        self.assertEqual(res_b_replay.status_code, 200)
        self.assertEqual(res_b_replay.json(), res_b1.json())

        # 3. Same key with explicit owner_user_id: null conflicts with IDEMPOTENCY_KEY_REUSE
        payload_b_null = {
            "name": "Renamed Without Owner",
            "owner_user_id": None,
            "expected_version": 1
        }
        res_b_conflict = self.client.patch(f"/api/v1/accounts/{acc_id}", json=payload_b_null, headers=headers_b)
        self.assertEqual(res_b_conflict.status_code, 409)
        self.assertEqual(res_b_conflict.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

        # Verify DB state: owner_user_id is still self.user_id, not cleared
        acc = accounts_repo.get_account(self.conn, acc_id, self.household_id)
        self.assertEqual(acc["owner_user_id"], self.user_id)
        self.assertEqual(acc["row_version"], 2)

    def test_13_deterministic_400_rejection_and_replay_byte_identical(self):
        """
        Requirements for Defect 2:
        - Actual Stage 2A HTTP 400 rejection path (invalid closing snapshot)
        - First request returns deterministic 400 with BAD_REQUEST payload
        - Receipt in DB has terminal status='rejected', response_http_status=400, response_payload matching first JSON body
        - Identical retry returns exactly the same status and JSON body
        - No account mutation occurs
        - No audit event for nonexistent change occurs
        - Same key with changed command returns IDEMPOTENCY_KEY_REUSE (409)
        """
        acc_id = uuid4()
        accounts_repo.create_account(
            self.conn,
            account_id=acc_id,
            household_id=self.household_id,
            name="Close Reject Target Acc",
            account_type="cash",
            currency="CNY",
            opened_on=date(2026, 1, 1)
        )
        self.conn.commit()

        key = f"idemp-key-close-reject-{uuid4().hex[:8]}"
        headers = {**self.device_headers, "Idempotency-Key": key}
        fake_snap_id = uuid4()
        payload = {
            "expected_version": 0,
            "closed_on": "2026-06-01",
            "closing_snapshot_id": str(fake_snap_id)
        }

        # 1. First execution fails with deterministic 400
        res1 = self.client.post(f"/api/v1/accounts/{acc_id}/close", json=payload, headers=headers)
        self.assertEqual(res1.status_code, 400)
        body1 = res1.json()
        self.assertEqual(body1["error"]["code"], "BAD_REQUEST")
        self.assertEqual(body1["error"]["retryable"], False)
        self.assertIn(f"Closing snapshot {fake_snap_id} not found", body1["error"]["message"])
        self.assertEqual(body1["detail"], body1["error"]["message"])

        # 2. Receipt is terminal 'rejected' and stores exact status and payload
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, response_http_status, failure_code, committed_at, response_payload
                FROM ingestion_requests
                WHERE household_id = %s AND idempotency_key = %s;
                """,
                (str(self.household_id), key)
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "rejected")
            self.assertEqual(row[1], 400)
            self.assertEqual(row[2], "BAD_REQUEST")
            self.assertIsNone(row[3]) # committed_at must be NULL
            self.assertEqual(row[4], body1) # saved response_payload exactly equals first JSON body

        # 3. Identical retry returns exactly the same status and JSON body
        res2 = self.client.post(f"/api/v1/accounts/{acc_id}/close", json=payload, headers=headers)
        self.assertEqual(res2.status_code, 400)
        self.assertEqual(res2.json(), body1)

        # 4. Verify no account mutation occurred
        acc = accounts_repo.get_account(self.conn, acc_id, self.household_id)
        self.assertEqual(acc["status"], "active")
        self.assertIsNone(acc["closed_on"])
        self.assertEqual(acc["row_version"], 0)

        # 5. Verify no audit event for nonexistent change occurred
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM audit_events WHERE household_id = %s AND entity_id = %s AND action = 'close';",
                (str(self.household_id), str(acc_id))
            )
            self.assertEqual(cur.fetchone()[0], 0)

        # 6. Same key with changed command returns IDEMPOTENCY_KEY_REUSE (409)
        changed_payload = {
            "expected_version": 0,
            "closed_on": "2026-06-02",
            "closing_snapshot_id": str(fake_snap_id)
        }
        res3 = self.client.post(f"/api/v1/accounts/{acc_id}/close", json=changed_payload, headers=headers)
        self.assertEqual(res3.status_code, 409)
        self.assertEqual(res3.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")
