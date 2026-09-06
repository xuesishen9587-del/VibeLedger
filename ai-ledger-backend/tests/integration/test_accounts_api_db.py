import unittest
from uuid import UUID, uuid4
import hashlib
from decimal import Decimal
from datetime import date, datetime, timezone
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.repositories import accounts as accounts_repo
from app.repositories import devices as devices_repo
from app.repositories import transactions as tx_repo
from app.repositories import audit as audit_repo
from app.repositories import categories as categories_repo

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

class TestAccountsApiDb(BaseDbTestCase):
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

        orig_post = cls.client.post
        orig_patch = cls.client.patch

        def _wrapped_post(url, *args, **kwargs):
            headers = dict(kwargs.get("headers") or {})
            if url.startswith("/api/v1/accounts") and "/aliases" not in url:
                if "Idempotency-Key" not in headers:
                    headers["Idempotency-Key"] = f"key-{uuid4().hex}"
            kwargs["headers"] = headers
            return orig_post(url, *args, **kwargs)

        def _wrapped_patch(url, *args, **kwargs):
            headers = dict(kwargs.get("headers") or {})
            if url.startswith("/api/v1/accounts") and "/aliases" not in url:
                if "Idempotency-Key" not in headers:
                    headers["Idempotency-Key"] = f"key-{uuid4().hex}"
            kwargs["headers"] = headers
            return orig_patch(url, *args, **kwargs)

        cls.client.post = _wrapped_post
        cls.client.patch = _wrapped_patch

    def seed_test_data(self):
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.raw_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_token.encode("utf-8")).digest()
        self.headers = {"Authorization": f"Bearer {self.raw_token}"}

        # Household B for isolation tests
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_test_{uuid4().hex}"
        self.token_b_hash = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_b = {"Authorization": f"Bearer {self.raw_token_b}"}

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # Setup Household A
                accounts_repo.create_household(conn, self.household_id, "Test Household A", reporting_currency="CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_user_a", "User A", "user_a@test.local")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "iPhone A", self.token_hash, household_id=self.household_id)

                # Setup Household B
                accounts_repo.create_household(conn, self.household_b_id, "Test Household B", reporting_currency="USD")
                accounts_repo.create_user(conn, self.user_b_id, "auth_user_b", "User B", "user_b@test.local")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "iPhone B", self.token_b_hash, household_id=self.household_b_id)
        finally:
            conn.close()

    def test_create_account_atomicity_and_audit(self):
        payload = {
            "name": "ICBC Salary",
            "balance_scope": "Operational Cash",
            "account_type": "cash",
            "currency": "CNY",
            "owner_user_id": str(self.user_id),
            "risk_level": "low"
        }
        res = self.client.post("/api/v1/accounts", json=payload, headers=self.headers)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["name"], "ICBC Salary")
        self.assertEqual(data["balance_scope"], "Operational Cash")
        self.assertEqual(data["account_type"], "cash")
        self.assertEqual(data["currency"], "CNY")
        self.assertEqual(data["risk_level"], "low")
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["row_version"], 0)

        account_id = UUID(data["id"])

        # Check DB row
        conn = get_connection(self.test_schema)
        try:
            acc = accounts_repo.get_account(conn, account_id, self.household_id)
            self.assertIsNotNone(acc)
            self.assertEqual(acc["name"], "ICBC Salary")
            self.assertEqual(acc["balance_scope"], "Operational Cash")

            # Verify audit event
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT action, entity_type, entity_id, actor_type, after_data
                    FROM audit_events
                    WHERE household_id = %s AND entity_id = %s;
                    """,
                    (self.household_id, account_id)
                )
                audit_row = cur.fetchone()
                self.assertIsNotNone(audit_row)
                self.assertEqual(audit_row[0], "create")
                self.assertEqual(audit_row[1], "account")
                self.assertEqual(audit_row[3], "device")
        finally:
            conn.close()

    def test_create_account_validations(self):
        # 1. Extra fields rejected (forbidden extra)
        res = self.client.post("/api/v1/accounts", json={
            "name": "Bad Extra",
            "balance_scope": "General",
            "account_type": "cash",
            "currency": "CNY",
            "institution": "Old Bank"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 422)

        # 2. Credit account cannot have risk_level
        res_credit_risk = self.client.post("/api/v1/accounts", json={
            "name": "Credit Bad Risk",
            "balance_scope": "Personal Credit",
            "account_type": "credit",
            "currency": "CNY",
            "risk_level": "medium"
        }, headers=self.headers)
        self.assertEqual(res_credit_risk.status_code, 422)
        self.assertEqual(res_credit_risk.json()["error"]["code"], "LINKED_ACCOUNT_INVALID")

        # 3. Owner user ID not in household rejected
        foreign_user_id = uuid4()
        res = self.client.post("/api/v1/accounts", json={
            "name": "Foreign User Acc",
            "balance_scope": "General",
            "account_type": "cash",
            "currency": "CNY",
            "owner_user_id": str(foreign_user_id)
        }, headers=self.headers)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "USER_NOT_IN_HOUSEHOLD")

        # 4. Duplicate active name in same household rejected
        res_ok = self.client.post("/api/v1/accounts", json={
            "name": "CMB Visa",
            "balance_scope": "Personal Credit",
            "account_type": "credit",
            "currency": "USD"
        }, headers=self.headers)
        self.assertEqual(res_ok.status_code, 201)

        res_dup = self.client.post("/api/v1/accounts", json={
            "name": "cmb visa", # case insensitive duplicate
            "balance_scope": "Personal Credit",
            "account_type": "credit",
            "currency": "USD"
        }, headers=self.headers)
        self.assertEqual(res_dup.status_code, 422)
        self.assertEqual(res_dup.json()["error"]["code"], "ACCOUNT_NAME_CONFLICT")

    def test_list_accounts_with_filters(self):
        # Create cash and credit accounts
        res1 = self.client.post("/api/v1/accounts", json={
            "name": "Savings A",
            "balance_scope": "Emergency Fund",
            "account_type": "savings",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res1.status_code, 201)

        res2 = self.client.post("/api/v1/accounts", json={
            "name": "Credit A",
            "balance_scope": "Personal Credit",
            "account_type": "credit",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res2.status_code, 201)

        # Query all
        res_all = self.client.get("/api/v1/accounts", headers=self.headers)
        self.assertEqual(res_all.status_code, 200)
        self.assertIsNone(res_all.json().get("next_cursor"))
        items = res_all.json()["items"]
        self.assertGreaterEqual(len(items), 2)

        # Filter by account_type=credit
        res_credit = self.client.get("/api/v1/accounts?account_type=credit", headers=self.headers)
        self.assertEqual(res_credit.status_code, 200)
        self.assertIsNone(res_credit.json().get("next_cursor"))
        c_items = res_credit.json()["items"]
        for it in c_items:
            self.assertEqual(it["account_type"], "credit")

    def test_patch_account_optimistic_concurrency_and_audit(self):
        # Create account
        res = self.client.post("/api/v1/accounts", json={
            "name": "DBS Multi",
            "balance_scope": "Operational",
            "account_type": "cash",
            "currency": "SGD"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        acc_id = data["id"]
        row_version = data["row_version"]

        # Patch with stale expected_version -> 409 Conflict
        res_stale = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "name": "DBS Renamed",
            "expected_version": row_version + 5
        }, headers=self.headers)
        self.assertEqual(res_stale.status_code, 409)
        self.assertEqual(res_stale.json()["error"]["code"], "ROW_VERSION_CONFLICT")

        # Patch with correct expected_version -> 200 OK
        res_patch = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "name": "DBS Main SGD",
            "balance_scope": "Primary Cash",
            "expected_version": row_version
        }, headers=self.headers)
        self.assertEqual(res_patch.status_code, 200)
        p_data = res_patch.json()
        self.assertEqual(p_data["name"], "DBS Main SGD")
        self.assertEqual(p_data["balance_scope"], "Primary Cash")
        self.assertEqual(p_data["row_version"], row_version + 1)

    def test_patch_account_immutability_rules(self):
        res = self.client.post("/api/v1/accounts", json={
            "name": "Immutable Test Acc",
            "balance_scope": "General",
            "account_type": "cash",
            "currency": "USD"
        }, headers=self.headers)
        acc_id = UUID(res.json()["id"])
        row_version = res.json()["row_version"]

        # 1. Before financial history, currency CAN be updated
        res_curr_ok = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "currency": "EUR",
            "expected_version": row_version
        }, headers=self.headers)
        self.assertEqual(res_curr_ok.status_code, 200)
        self.assertEqual(res_curr_ok.json()["currency"], "EUR")
        self.assertEqual(res_curr_ok.json()["row_version"], row_version + 1)
        row_version = res_curr_ok.json()["row_version"]

        # Confirm DB has EUR
        conn = get_connection(self.test_schema)
        try:
            acc_db = accounts_repo.get_account(conn, acc_id, self.household_id)
            self.assertEqual(acc_db["currency"], "EUR")
        finally:
            conn.close()

        # 2. Record a transaction on this account
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # Create category and ingestion request
                cat_id = uuid4()
                categories_repo.create_category(conn, household_id=self.household_id, name="Salary Cat", category_type="income", category_id=cat_id)
                req_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ingestion_requests (
                            id, household_id, user_id, device_id, actor_scope, idempotency_key, request_kind, operation, request_hash, status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """, (req_id, self.household_id, self.user_id, self.device_id, "device:1", "req_imm_12345", "command", "tx", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "processing"))

                tx_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO transactions (
                            id, household_id, transaction_type, occurred_on, account_id, category_id,
                            original_amount, original_currency, date_source, source, status,
                            created_by_user_id, source_request_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """, (tx_id, self.household_id, "cash_income", date(2026, 8, 1), acc_id, cat_id, Decimal("100.00"), "EUR", "manual", "shortcut", "committed", self.user_id, req_id))
        finally:
            conn.close()

        # 3. Try to change currency after transaction exists -> 422 CURRENCY_IMMUTABLE
        res_curr = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "currency": "GBP",
            "expected_version": row_version
        }, headers=self.headers)
        self.assertEqual(res_curr.status_code, 422)
        self.assertEqual(res_curr.json()["error"]["code"], "CURRENCY_IMMUTABLE")

        # 4. Try to change account_type after transaction exists -> 422 ACCOUNT_TYPE_IMMUTABLE
        res_type = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "account_type": "credit",
            "expected_version": row_version
        }, headers=self.headers)
        self.assertEqual(res_type.status_code, 422)
        self.assertEqual(res_type.json()["error"]["code"], "ACCOUNT_TYPE_IMMUTABLE")

    def test_patch_account_explicit_null_fields(self):
        # 1. Create account with owner_user_id and risk_level populated
        res_acc = self.client.post("/api/v1/accounts", json={
            "name": "Savings With Owner",
            "balance_scope": "Savings",
            "account_type": "savings",
            "currency": "USD",
            "owner_user_id": str(self.user_id),
            "risk_level": "very_low"
        }, headers=self.headers)
        self.assertEqual(res_acc.status_code, 201)
        data = res_acc.json()
        acc_id = data["id"]
        row_v = data["row_version"]
        self.assertEqual(data["owner_user_id"], str(self.user_id))
        self.assertEqual(data["risk_level"], "very_low")

        # 2. Explicitly clear owner_user_id and risk_level using null
        res_clear = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "owner_user_id": None,
            "risk_level": None,
            "expected_version": row_v
        }, headers=self.headers)
        self.assertEqual(res_clear.status_code, 200)
        c_data = res_clear.json()
        self.assertIsNone(c_data["owner_user_id"])
        self.assertIsNone(c_data["risk_level"])

        # Confirm DB has NULL
        conn = get_connection(self.test_schema)
        try:
            acc_db = accounts_repo.get_account(conn, UUID(acc_id), self.household_id)
            self.assertIsNone(acc_db["owner_user_id"])
            self.assertIsNone(acc_db["risk_level"])
        finally:
            conn.close()

    def test_patch_account_missing_expected_version_rejected(self):
        res = self.client.post("/api/v1/accounts", json={
            "name": "Account Missing Version",
            "balance_scope": "Testing",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=self.headers)
        acc_id = res.json()["id"]

        # Missing expected_version -> 422 Unprocessable Entity
        res_patch = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "name": "Account Renamed"
        }, headers=self.headers)
        self.assertEqual(res_patch.status_code, 422)

    def test_patch_account_opened_on_after_financial_observations_rejected(self):
        # 1. Create account with opened_on 2026-01-01
        res = self.client.post("/api/v1/accounts", json={
            "name": "Account For Lifetime Check",
            "balance_scope": "Testing",
            "account_type": "cash",
            "currency": "CNY",
            "opened_on": "2026-01-01"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 201)
        acc_id = UUID(res.json()["id"])
        row_v = res.json()["row_version"]

        # 2. Insert snapshot on 2026-02-01
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                req_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ingestion_requests (
                            id, household_id, user_id, device_id, actor_scope, idempotency_key, request_kind, operation, request_hash, status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """, (req_id, self.household_id, self.user_id, self.device_id, "device:1", "req_snap_lifetime", "command", "snap", "0" * 64, "processing"))
                    cur.execute("""
                        INSERT INTO account_snapshots (
                            id, household_id, account_id, as_of, time_basis, balance, currency, source, status, created_by_user_id, source_request_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """, (uuid4(), self.household_id, acc_id, datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc), "explicit", Decimal("100.00"), "CNY", "manual", "active", self.user_id, req_id))
        finally:
            conn.close()

        # 3. Attempt to move opened_on to 2026-03-01 (after snapshot date 2026-02-01) -> 400 Bad Request
        res_bad = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "opened_on": "2026-03-01",
            "expected_version": row_v
        }, headers=self.headers)
        self.assertEqual(res_bad.status_code, 400)
        self.assertIn("observations before new opened_on", res_bad.json()["detail"])

        # 4. Moving opened_on to 2026-01-15 (still before 2026-02-01 snapshot) -> 200 OK
        res_ok = self.client.patch(f"/api/v1/accounts/{acc_id}", json={
            "opened_on": "2026-01-15",
            "expected_version": row_v
        }, headers=self.headers)
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.json()["opened_on"], "2026-01-15")

    def test_account_lifecycle_close_reopen_cancel(self):
        # 1. Create active account
        res = self.client.post("/api/v1/accounts", json={
            "name": "Account For Lifecycle",
            "balance_scope": "Testing",
            "account_type": "cash",
            "currency": "CNY",
            "opened_on": "2026-01-01"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 201)
        acc_id = UUID(res.json()["id"])
        row_v = res.json()["row_version"]

        # Attempt to close without snapshot -> 400 Bad Request
        res_bad_close = self.client.post(f"/api/v1/accounts/{acc_id}/close", json={
            "expected_version": row_v,
            "closed_on": "2026-06-01"
        }, headers=self.headers)
        self.assertEqual(res_bad_close.status_code, 422)

        # Create non-zero snapshot
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                req_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ingestion_requests (
                            id, household_id, user_id, device_id, actor_scope,
                            idempotency_key, request_kind, operation, request_hash, status, committed_at
                        ) VALUES (%s, %s, %s, %s, 'device:test', %s, 'command', 'snapshot', %s, 'committed', now());
                    """, (req_id, self.household_id, self.user_id, self.device_id, f"key_{uuid4().hex[:16]}", '0'*64))

                    bad_snap_id = uuid4()
                    cur.execute("""
                        INSERT INTO account_snapshots (
                            id, household_id, account_id, as_of, time_basis, balance, currency, source, status, created_by_user_id, source_request_id
                        ) VALUES (%s, %s, %s, '2026-06-01 10:00:00+00', 'explicit', 50.00, 'CNY', 'manual', 'active', %s, %s);
                    """, (bad_snap_id, self.household_id, acc_id, self.user_id, req_id))
        finally:
            conn.close()

        # Attempt to close with non-zero snapshot -> 400 Bad Request
        res_nonzero_close = self.client.post(f"/api/v1/accounts/{acc_id}/close", json={
            "expected_version": row_v,
            "closing_snapshot_id": str(bad_snap_id),
            "closed_on": "2026-06-01"
        }, headers=self.headers)
        self.assertEqual(res_nonzero_close.status_code, 400)
        self.assertIn("explicit zero", res_nonzero_close.json()["detail"])

        # Create explicit zero snapshot
        zero_snap_id = uuid4()
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                req_zero_id = uuid4()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ingestion_requests (
                            id, household_id, user_id, device_id, actor_scope,
                            idempotency_key, request_kind, operation, request_hash, status, committed_at
                        ) VALUES (%s, %s, %s, %s, 'device:test', %s, 'command', 'snapshot', %s, 'committed', now());
                    """, (req_zero_id, self.household_id, self.user_id, self.device_id, f"key_{uuid4().hex[:16]}", '0'*64))

                    # Mark previous snapshot voided so it doesn't count as later active
                    cur.execute("UPDATE account_snapshots SET status = 'voided', voided_at = now(), void_reason = 'closed' WHERE id = %s;", (bad_snap_id,))
                    cur.execute("""
                        INSERT INTO account_snapshots (
                            id, household_id, account_id, as_of, time_basis, balance, currency, source, status, created_by_user_id, source_request_id
                        ) VALUES (%s, %s, %s, '2026-06-01 12:00:00+00', 'explicit', 0.000000, 'CNY', 'manual', 'active', %s, %s);
                    """, (zero_snap_id, self.household_id, acc_id, self.user_id, req_zero_id))
        finally:
            conn.close()

        # Close succeeds with explicit zero snapshot
        res_close = self.client.post(f"/api/v1/accounts/{acc_id}/close", json={
            "expected_version": row_v,
            "closing_snapshot_id": str(zero_snap_id),
            "closed_on": "2026-06-01"
        }, headers=self.headers)
        self.assertEqual(res_close.status_code, 200)
        self.assertEqual(res_close.json()["status"], "closed")
        self.assertEqual(res_close.json()["closed_on"], "2026-06-01")
        row_v = res_close.json()["row_version"]

        # Reopen account
        res_reopen = self.client.post(f"/api/v1/accounts/{acc_id}/reopen", json={
            "expected_version": row_v,
            "reason": "Accidental close"
        }, headers=self.headers)
        self.assertEqual(res_reopen.status_code, 200)
        self.assertEqual(res_reopen.json()["status"], "active")
        self.assertIsNone(res_reopen.json()["closed_on"])
        row_v = res_reopen.json()["row_version"]

        # Attempt to cancel account with history -> 400 Bad Request
        res_bad_cancel = self.client.post(f"/api/v1/accounts/{acc_id}/cancel", json={
            "expected_version": row_v,
            "reason": "Not needed"
        }, headers=self.headers)
        self.assertEqual(res_bad_cancel.status_code, 400)
        self.assertIn("financial history", res_bad_cancel.json()["detail"])

        # Cancel truly unused account -> 200 OK
        res_unused = self.client.post("/api/v1/accounts", json={
            "name": "Truly Unused Account",
            "balance_scope": "Testing",
            "account_type": "cash",
            "currency": "CNY",
            "opened_on": "2026-01-01"
        }, headers=self.headers)
        self.assertEqual(res_unused.status_code, 201)
        unused_id = UUID(res_unused.json()["id"])
        unused_v = res_unused.json()["row_version"]

        res_cancel = self.client.post(f"/api/v1/accounts/{unused_id}/cancel", json={
            "expected_version": unused_v,
            "reason": "Not needed"
        }, headers=self.headers)
        self.assertEqual(res_cancel.status_code, 200)
        self.assertEqual(res_cancel.json()["status"], "cancelled")

    def test_account_aliases_crud_and_patch(self):
        res_acc1 = self.client.post("/api/v1/accounts", json={
            "name": "ICBC Card 1",
            "balance_scope": "Personal Credit",
            "account_type": "credit",
            "currency": "CNY"
        }, headers=self.headers)
        acc1_id = res_acc1.json()["id"]

        # 1. Create alias on acc1
        res_a1 = self.client.post(f"/api/v1/accounts/{acc1_id}/aliases", json={"alias": "工行"}, headers=self.headers)
        self.assertEqual(res_a1.status_code, 201)
        alias1_id = res_a1.json()["id"]
        self.assertEqual(res_a1.json()["alias"], "工行")
        self.assertEqual(res_a1.json()["status"], "active")

        # 2. Duplicate alias on same account -> 422 conflict
        res_dup = self.client.post(f"/api/v1/accounts/{acc1_id}/aliases", json={"alias": "工行"}, headers=self.headers)
        self.assertEqual(res_dup.status_code, 422)
        self.assertEqual(res_dup.json()["error"]["code"], "ACCOUNT_ALIAS_CONFLICT")

        # 3. List aliases for acc1
        res_list = self.client.get(f"/api/v1/accounts/{acc1_id}/aliases", headers=self.headers)
        self.assertEqual(res_list.status_code, 200)
        self.assertIsNone(res_list.json().get("next_cursor"))
        self.assertEqual(len(res_list.json()["items"]), 1)
        self.assertEqual(res_list.json()["items"][0]["alias"], "工行")

        # 4. Patch alias
        res_patch_alias = self.client.patch(f"/api/v1/accounts/{acc1_id}/aliases/{alias1_id}", json={
            "alias": "工行白金卡",
            "expected_version": 0
        }, headers=self.headers)
        self.assertEqual(res_patch_alias.status_code, 200)
        self.assertEqual(res_patch_alias.json()["alias"], "工行白金卡")
        self.assertEqual(res_patch_alias.json()["row_version"], 1)

        # Missing expected_version on alias patch -> 422 Unprocessable Entity
        res_alias_no_ver = self.client.patch(f"/api/v1/accounts/{acc1_id}/aliases/{alias1_id}", json={
            "alias": "工行钻石卡"
        }, headers=self.headers)
        self.assertEqual(res_alias_no_ver.status_code, 422)

        # 5. Deactivate alias on acc1 via canonical PATCH
        res_del = self.client.patch(f"/api/v1/accounts/{acc1_id}/aliases/{alias1_id}", json={
            "status": "inactive",
            "expected_version": 1
        }, headers=self.headers)
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(res_del.json()["status"], "inactive")

    def test_cross_household_isolation(self):
        # Create account in Household A
        res_a = self.client.post("/api/v1/accounts", json={
            "name": "Household A Secret Acc",
            "balance_scope": "Private",
            "account_type": "cash",
            "currency": "CNY"
        }, headers=self.headers)
        acc_a_id = res_a.json()["id"]

        # Household B device attempts to patch Household A's account -> 404 Not Found
        res_hack = self.client.patch(f"/api/v1/accounts/{acc_a_id}", json={
            "name": "Hacked Name",
            "expected_version": 0
        }, headers=self.headers_b)
        self.assertEqual(res_hack.status_code, 404)
        self.assertEqual(res_hack.json()["error"]["code"], "ACCOUNT_NOT_FOUND")

        # Household B device attempts to close Household A's account -> 404 Not Found
        res_close = self.client.post(f"/api/v1/accounts/{acc_a_id}/close", json={
            "expected_version": 0,
            "closing_snapshot_id": str(uuid4()),
            "closed_on": "2026-06-01"
        }, headers=self.headers_b)
        self.assertEqual(res_close.status_code, 404)

        # Household B device attempts to read aliases -> 404 Not Found
        res_alias = self.client.get(f"/api/v1/accounts/{acc_a_id}/aliases", headers=self.headers_b)
        self.assertEqual(res_alias.status_code, 404)

        # Repository-level isolation checks
        conn = get_connection(self.test_schema)
        try:
            # Account lookup with Household B must return None
            self.assertIsNone(accounts_repo.get_account(conn, UUID(acc_a_id), self.household_b_id))
            # Account update with Household B must return None
            self.assertIsNone(accounts_repo.update_account(conn, UUID(acc_a_id), self.household_b_id, name="CrossHH"))
            # List aliases with Household B must return empty
            self.assertEqual(accounts_repo.list_account_aliases(conn, UUID(acc_a_id), self.household_b_id), [])

            # Create alias in Household A
            al_id = uuid4()
            accounts_repo.create_account_alias(
                conn, al_id, UUID(acc_a_id), "Secret Alias", "secret alias", status="active", household_id=self.household_id
            )
            conn.commit()
            # Alias lookup with Household B must return None
            self.assertIsNone(accounts_repo.get_account_alias(conn, alias_id=al_id, household_id=self.household_b_id, account_id=UUID(acc_a_id)))
            # Alias check exists with Household B must return False
            self.assertFalse(accounts_repo.check_account_alias_exists(conn, UUID(acc_a_id), "secret alias", household_id=self.household_b_id))
            # Alias deactivation with Household B must return None
            self.assertIsNone(accounts_repo.deactivate_account_alias(conn, al_id, UUID(acc_a_id), self.household_b_id))

            # History queries across households must return None
            self.assertIsNone(audit_repo.get_entity_history(conn, self.household_b_id, "account", UUID(acc_a_id)))
            self.assertIsNone(audit_repo.get_entity_history(conn, self.household_b_id, "account_alias", al_id))
        finally:
            conn.close()

        # History API across households returns 404
        res_hist = self.client.get(f"/api/v1/history?entity_type=account&entity_id={acc_a_id}", headers=self.headers_b)
        self.assertEqual(res_hist.status_code, 404)

    def test_list_accounts_legacy_inactive_status_rejected(self):
        resp = self.client.get("/api/v1/accounts?status=inactive", headers=self.headers)
        self.assertEqual(resp.status_code, 422)

if __name__ == "__main__":
    unittest.main()
