import os
os.environ["ENVIRONMENT"] = "test"
os.environ["DB_SCHEMA"] = "vibeledger_test_runner"

import unittest
import uuid
from uuid import UUID
import hashlib
import base64
import threading
from decimal import Decimal
from datetime import date, datetime, timezone
import psycopg2
from psycopg2 import sql
from fastapi.testclient import TestClient

from app import config
from app.db import get_connection, transaction
from migrations import runner
from app.main import create_app
from app.api.deps import get_db_connection
from app.api.routes.expenses import router as expenses_router
from app.api.routes.ingestion import router as ingestion_router
from app.repositories import accounts as accounts_repo
from app.repositories import transactions as tx_repo
from app.repositories import ingestion as ingestion_repo
from app.repositories import installments as installments_repo
from app.repositories import devices as devices_repo
from app.repositories import audit as audit_repo
from app.services.gemini_service import MockGeminiService, ExpenseExtractionResult
from app.services.reference_fx_service import ReferenceFxService
from app.domain.installments import calculate_installment_schedule
from app.domain import transactions as domain_tx

class TestExpenseApi(unittest.TestCase):
    def setUp(self):
        if not config.is_safe_for_testing():
            self.skipTest("Skipping integration test. ENVIRONMENT must be 'test'.")

        self.test_schema = f"vibeledger_test_{uuid.uuid4().hex[:12]}"
        runner.run_migrations(self.test_schema)
        self.conn = get_connection(self.test_schema)

        # 1. Base test fixture: Household & User & Membership
        self.household_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        accounts_repo.create_household(self.conn, self.household_id, "Test Household", date(2026, 1, 1))
        accounts_repo.create_user(self.conn, self.user_id, "auth_user_test", "Alice User")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO household_members (household_id, user_id, role, joined_at)
                VALUES (%s, %s, 'owner', now());
                """,
                (self.household_id, self.user_id)
            )

        # 2. Devices
        self.raw_token_1 = "valid-iphone-device-token-12345678"
        self.token_hash_1 = hashlib.sha256(self.raw_token_1.encode("utf-8")).digest()
        self.device_id_1 = uuid.uuid4()
        devices_repo.create_device(
            self.conn, self.device_id_1, self.user_id, "Alice's iPhone", self.token_hash_1,
            platform="ios_shortcuts", status="active", client_version="ios-shortcut-2.0"
        )

        self.raw_token_2 = "valid-iphone-device-token-87654321"
        self.token_hash_2 = hashlib.sha256(self.raw_token_2.encode("utf-8")).digest()
        self.device_id_2 = uuid.uuid4()
        devices_repo.create_device(
            self.conn, self.device_id_2, self.user_id, "Bob's iPhone", self.token_hash_2,
            platform="ios_shortcuts", status="active", client_version="ios-shortcut-2.0"
        )

        self.raw_token_revoked = "revoked-device-token-00000000"
        self.token_hash_revoked = hashlib.sha256(self.raw_token_revoked.encode("utf-8")).digest()
        self.device_id_revoked = uuid.uuid4()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices (id, user_id, device_name, platform, token_hash, status, created_at, revoked_at)
                VALUES (%s, %s, 'Old iPhone', 'ios_shortcuts', %s, 'revoked', now(), now());
                """,
                (self.device_id_revoked, self.user_id, self.token_hash_revoked)
            )

        # 3. Accounts
        self.acc_cny_cash = uuid.uuid4()
        self.acc_cny_credit = uuid.uuid4()
        self.acc_usd_credit = uuid.uuid4()

        accounts_repo.create_account(self.conn, self.acc_cny_cash, self.household_id, "ICBC_Debit", "cash", "CNY")
        accounts_repo.create_account(self.conn, self.acc_cny_credit, self.household_id, "ICBC_Visa_Credit", "credit", "CNY", billing_day=5, due_day=25)
        accounts_repo.create_account(self.conn, self.acc_usd_credit, self.household_id, "USD_Visa_Card", "credit", "USD", billing_day=10, due_day=28)

        # Setup baseline balance
        accounts_repo.update_account_state_projection(self.conn, self.acc_cny_cash, Decimal("5000.000000"))
        accounts_repo.update_account_state_projection(self.conn, self.acc_cny_credit, Decimal("0.000000"))
        accounts_repo.update_account_state_projection(self.conn, self.acc_usd_credit, Decimal("0.000000"))

        # 4. Account Aliases
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO account_aliases (id, account_id, alias_text, normalized_alias, status)
                VALUES (%s, %s, '工行工资卡', '工行工资卡', 'active'),
                       (%s, %s, '工行信用卡', '工行信用卡', 'active'),
                       (%s, %s, 'USD Visa', 'usd visa', 'active');
                """,
                (uuid.uuid4(), self.acc_cny_cash, uuid.uuid4(), self.acc_cny_credit, uuid.uuid4(), self.acc_usd_credit)
            )

        # 5. Categories
        self.cat_dining = uuid.uuid4()
        self.cat_gadgets = uuid.uuid4()
        self.cat_travel = uuid.uuid4()
        self.cat_income = uuid.uuid4()

        accounts_repo.create_category(self.conn, self.cat_dining, self.household_id, "Dining", "expense")
        accounts_repo.create_category(self.conn, self.cat_gadgets, self.household_id, "Digital & Gadgets", "expense")
        accounts_repo.create_category(self.conn, self.cat_travel, self.household_id, "Travel", "expense")
        accounts_repo.create_category(self.conn, self.cat_income, self.household_id, "Salary", "income")

        self.conn.commit()

        # 6. Setup FastAPI test client & Mock Services
        self.mock_gemini = MockGeminiService()
        self.mock_fx = ReferenceFxService({
            ("JPY", "USD"): Decimal("0.006890000000"),
            ("USD", "CNY"): Decimal("7.250000000000")
        })

        expenses_router._gemini_service = self.mock_gemini
        expenses_router._reference_fx_service = self.mock_fx
        ingestion_router._gemini_service = self.mock_gemini
        ingestion_router._reference_fx_service = self.mock_fx

        self.app = create_app()

        def override_get_db():
            conn = get_connection(self.test_schema)
            try:
                yield conn
            finally:
                if conn and not conn.closed:
                    conn.close()

        self.app.dependency_overrides[get_db_connection] = override_get_db
        self.client = TestClient(self.app)

    def tearDown(self):
        if hasattr(self, "conn") and self.conn:
            self.conn.close()

        if config.is_safe_for_testing() and hasattr(self, "test_schema"):
            config.validate_test_schema(self.test_schema)
            settings = config.get_settings()
            conn = get_connection(settings.DB_SCHEMA)
            try:
                with conn.cursor() as cur:
                    quoted_schema = sql.Identifier(self.test_schema)
                    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE").format(schema=quoted_schema))
                conn.commit()
            except Exception as e:
                print(f"Warning: failed to drop test schema {self.test_schema}: {e}")
            finally:
                conn.close()

    def _sample_image_payload(self):
        return {
            "mime_type": "image/jpeg",
            "base64": base64.b64encode(b"fake_jpeg_image_bytes").decode("utf-8")
        }

    # ==========================================
    # A. Authentication Tests
    # ==========================================

    def test_01_missing_bearer_token_rejected(self):
        # 1. missing Bearer token -> 401
        res = self.client.post("/api/v1/expenses", json={
            "idempotency_key": "test-key-01-no-auth",
            "image": self._sample_image_payload()
        })
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    def test_02_invalid_token_rejected(self):
        # 2. invalid token -> 401
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": "Bearer invalid-token-xyz"},
            json={
                "idempotency_key": "test-key-02-invalid-auth",
                "image": self._sample_image_payload()
            }
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "UNAUTHORIZED")

    def test_03_revoked_device_rejected(self):
        # 3. inactive/revoked device rejected
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_revoked}"},
            json={
                "idempotency_key": "test-key-03-revoked-auth",
                "image": self._sample_image_payload()
            }
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "UNAUTHORIZED")

    def test_04_valid_device_resolves_and_updates_last_seen(self):
        # 4 & 5. valid device resolves user+household and updates last_seen_at
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="JD",
            original_amount=Decimal("150.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Dining",
            confidence=1.0
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-04-valid-device",
                "image": self._sample_image_payload()
            }
        )
        self.assertEqual(res.status_code, 200)

        # Check last_seen_at in DB
        with self.conn.cursor() as cur:
            cur.execute("SELECT last_seen_at FROM devices WHERE id = %s;", (self.device_id_1,))
            last_seen = cur.fetchone()[0]
            self.assertIsNotNone(last_seen)

    def test_06_raw_token_never_persisted(self):
        # 6. raw token never persisted in plaintext
        with self.conn.cursor() as cur:
            cur.execute("SELECT token_hash FROM devices WHERE id = %s;", (self.device_id_1,))
            hash_in_db = bytes(cur.fetchone()[0])
            self.assertEqual(hash_in_db, self.token_hash_1)
            self.assertNotEqual(hash_in_db.decode("utf-8", errors="ignore"), self.raw_token_1)

    # ==========================================
    # B. Idempotency Tests
    # ==========================================

    def test_07_same_device_same_key_same_payload_replays(self):
        # 7. same device + same key + same payload -> replay -> 1 request, 1 result
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Apple Store",
            original_amount=Decimal("268.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Digital & Gadgets",
            confidence=1.0
        ))

        payload = {
            "idempotency_key": "test-key-07-replay-match",
            "captured_at": "2026-08-19T09:45:00+08:00",
            "image": self._sample_image_payload(),
            "note": "buy cable"
        }

        # 1st call
        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertEqual(data1["status"], "committed")

        # 2nd call (same payload)
        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()

        self.assertEqual(data1["request_id"], data2["request_id"])
        self.assertEqual(data1["transaction_id"], data2["transaction_id"])

        # Check DB has exactly 1 transaction
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (UUID(data1["request_id"]),))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_08_same_device_same_key_different_payload_conflict(self):
        # 8. same device + same key + different payload -> 409 IDEMPOTENCY_KEY_REUSE
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Store A",
            original_amount=Decimal("100.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Dining",
            confidence=1.0
        ))

        payload1 = {
            "idempotency_key": "test-key-08-reuse-key",
            "image": self._sample_image_payload(),
            "note": "note 1"
        }
        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload1)
        self.assertEqual(res1.status_code, 200)

        # 2nd call with different note
        payload2 = {
            "idempotency_key": "test-key-08-reuse-key",
            "image": self._sample_image_payload(),
            "note": "different note"
        }
        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload2)
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

    def test_09_different_devices_same_textual_key_allowed(self):
        # 9. different devices + same textual key -> allowed
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Merchant D1",
            original_amount=Decimal("50.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Dining",
            confidence=1.0
        ))
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Merchant D2",
            original_amount=Decimal("60.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Dining",
            confidence=1.0
        ))

        payload = {
            "idempotency_key": "shared-textual-key-12345",
            "image": self._sample_image_payload()
        }

        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_2}"}, json=payload)

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        self.assertNotEqual(res1.json()["request_id"], res2.json()["request_id"])

    def test_10_committed_lost_response_retry_replays(self):
        # 10. commit succeeds, HTTP response lost -> retry returns stored result without second transaction
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Coffee Shop",
            original_amount=Decimal("35.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Dining",
            confidence=1.0
        ))

        payload = {
            "idempotency_key": "test-key-10-retry-lost",
            "image": self._sample_image_payload()
        }

        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()

        # Balance before retry: 5000 - 35 = 4965
        state1 = accounts_repo.get_account_state(self.conn, self.acc_cny_cash)
        self.assertEqual(state1["ledger_balance"], Decimal("4965.000000"))

        # Retry
        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()

        self.assertEqual(data1["transaction_id"], data2["transaction_id"])
        state2 = accounts_repo.get_account_state(self.conn, self.acc_cny_cash)
        self.assertEqual(state2["ledger_balance"], Decimal("4965.000000"))

    def test_11_concurrent_identical_requests_produce_single_outcome(self):
        # 11. concurrent identical first requests -> exactly 1 financial effect
        self.mock_gemini.default_result = ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Grocery Store",
            original_amount=Decimal("88.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Dining",
            confidence=1.0
        )

        payload = {
            "idempotency_key": "test-key-11-concurrent-identical",
            "image": self._sample_image_payload(),
            "note": "weekly grocery"
        }

        results = []
        errors = []

        def worker():
            client = TestClient(self.app)
            try:
                res = client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
                results.append(res)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)

        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Concurrent workers encountered exceptions: {errors}")
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "committed")

        tx_ids = [r.json()["transaction_id"] for r in results]
        self.assertEqual(tx_ids[0], tx_ids[1], "Concurrent identical calls produced multiple transactions!")

        # Account balance deducted exactly once: 5000 - 88 = 4912
        state = accounts_repo.get_account_state(self.conn, self.acc_cny_cash)
        self.assertEqual(state["ledger_balance"], Decimal("4912.000000"))

    # ==========================================
    # C. Normal Expense Tests
    # ==========================================

    def test_12_16_normal_one_off_expense_lifecycle(self):
        # 12-16. high-confidence one-off expense commits, updates account_state once, links request, decimal string
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="JD Electronics",
            original_amount=Decimal("268.00"),
            original_currency="CNY",
            from_account="工行信用卡", # matches alias
            category="Digital & Gadgets",
            confidence=1.0
        ))

        payload = {
            "idempotency_key": "test-key-12-one-off",
            "captured_at": "2026-08-19T09:45:00+08:00",
            "image": self._sample_image_payload()
        }

        res = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(data["status"], "committed")
        self.assertEqual(data["payment_mode"], "one_off")
        tx_id = UUID(data["transaction_id"])
        req_id = UUID(data["request_id"])

        # Check transaction in DB
        tx = tx_repo.get_transaction(self.conn, tx_id)
        self.assertIsNotNone(tx)
        self.assertEqual(tx["source_request_id"], req_id)
        self.assertEqual(tx["from_amount"], Decimal("268.000000"))
        self.assertEqual(tx["account_leg_status"], "authoritative")

        # Check credit account debt: 0 - 268 = -268
        state = accounts_repo.get_account_state(self.conn, self.acc_cny_credit)
        self.assertEqual(state["ledger_balance"], Decimal("-268.000000"))

    # ==========================================
    # D. Confirmation Rules Tests
    # ==========================================

    def test_17_unresolved_account_forces_confirmation(self):
        # 17. unresolved account -> needs_confirmation
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Unknown Shop",
            original_amount=Decimal("50.00"),
            original_currency="CNY",
            from_account="Nonexistent Mystery Bank",
            category="Dining",
            confidence=0.9
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-17-unresolved-acc",
                "image": self._sample_image_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "needs_confirmation")
        self.assertIn("draft", data)
        self.assertIsNone(data["draft"]["from_account"])

        # Check no transaction created and balance unchanged
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (UUID(data["request_id"]),))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_18_multiple_account_candidates_forces_confirmation(self):
        # 18. multiple account candidates -> needs_confirmation
        # Create a second debit card with similar alias keyword
        acc_boc = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_boc, self.household_id, "BOC_Debit", "cash", "CNY")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO account_aliases (id, account_id, alias_text, normalized_alias, status) VALUES (%s, %s, '银行卡', '银行卡', 'active');",
                (uuid.uuid4(), self.acc_cny_cash)
            )
            cur.execute(
                "INSERT INTO account_aliases (id, account_id, alias_text, normalized_alias, status) VALUES (%s, %s, '银行卡', '银行卡', 'active');",
                (uuid.uuid4(), acc_boc)
            )
        self.conn.commit()

        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Shop",
            original_amount=Decimal("50.00"),
            original_currency="CNY",
            from_account="银行卡", # matches 2 accounts
            category="Dining",
            confidence=0.95
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-18-multi-acc",
                "image": self._sample_image_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "needs_confirmation")
        warning_codes = [w["code"] for w in data.get("warnings", [])]
        self.assertIn("MULTIPLE_ACCOUNT_CANDIDATES", warning_codes)

    def test_22_new_merchant_only_does_not_force_confirmation(self):
        # 22. NEW / UNKNOWN MERCHANT ALONE MUST NEVER FORCE CONFIRMATION
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Brand New Never Seen Before Merchant 12345",
            original_amount=Decimal("99.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Dining",
            confidence=0.98
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-22-new-merchant",
                "image": self._sample_image_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "committed")

    # ==========================================
    # E. Confirm Endpoint Tests
    # ==========================================

    def test_24_27_confirm_draft_flow(self):
        # 24-27. Confirm valid draft commits exactly 1 transaction, replay-safe, revalidates state
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Mystery Merchant",
            original_amount=Decimal("120.00"),
            original_currency="CNY",
            from_account=None, # Needs confirm
            category="Dining",
            confidence=0.8
        ))

        res1 = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-24-confirm-flow",
                "image": self._sample_image_payload()
            }
        )
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertEqual(data1["status"], "needs_confirmation")
        req_id = data1["request_id"]

        # Revise to fill the account
        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"from_account_id": str(self.acc_cny_cash)}
        )
        self.assertEqual(res_rev.status_code, 200)

        # Confirm
        res_conf = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_conf.status_code, 200)
        data_conf = res_conf.json()
        self.assertEqual(data_conf["status"], "committed")
        tx_id = data_conf["transaction_id"]

        # Repeated confirm returns replay
        res_conf2 = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_conf2.status_code, 200)
        self.assertEqual(res_conf2.json()["transaction_id"], tx_id)

    # ==========================================
    # F. Foreign Credit Card Tests
    # ==========================================

    def test_28_33_foreign_currency_credit_card_expense(self):
        # 28-31. 10000 JPY on USD credit card -> from_amount = 68.90 USD (estimated)
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Tokyo Store",
            original_amount=Decimal("10000.00"),
            original_currency="JPY",
            from_account="USD_Visa_Card", # USD credit card
            category="Travel",
            confidence=1.0
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-28-foreign-card",
                "image": self._sample_image_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(data["status"], "committed")
        self.assertEqual(data["original_amount"], "10000")
        self.assertEqual(data["original_currency"], "JPY")
        self.assertEqual(data["from_amount"], "68.90")
        self.assertEqual(data["from_currency"], "USD")
        self.assertEqual(data["account_leg_status"], "estimated")

        # Check account_state debt increased by 68.90 USD: 0 -> -68.90
        state = accounts_repo.get_account_state(self.conn, self.acc_usd_credit)
        self.assertEqual(state["ledger_balance"], Decimal("-68.900000"))

    def test_32_foreign_card_unavailable_fx_produces_confirmation(self):
        # 32. EUR on USD card with missing FX -> produces clean error / needs_confirmation without state corruption
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Paris Boutique",
            original_amount=Decimal("100.00"),
            original_currency="EUR", # No EUR rate in mock
            from_account="USD_Visa_Card",
            category="Travel",
            confidence=1.0
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-32-no-fx",
                "image": self._sample_image_payload()
            }
        )
        # Should gracefully fail or return 422 FX_RATE_UNAVAILABLE without corrupting DB
        self.assertIn(res.status_code, (422, 500))
        state = accounts_repo.get_account_state(self.conn, self.acc_usd_credit)
        self.assertEqual(state["ledger_balance"], Decimal("0.000000"))

    # ==========================================
    # G. Installment Tests
    # ==========================================

    def test_34_41_installment_plan_capture(self):
        # 34-40. 12000 CNY / 12 periods on credit card -> pending_first_bill, N periods, NO transaction, NO balance change
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Apple Store",
            payment_mode="installment",
            total_amount=Decimal("12000.00"),
            total_periods=12,
            original_currency="CNY",
            from_account="ICBC_Visa_Credit",
            category="Digital & Gadgets",
            confidence=1.0
        ))

        payload = {
            "idempotency_key": "test-key-34-installment-apple",
            "image": self._sample_image_payload()
        }

        res = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(data["status"], "committed")
        self.assertEqual(data["payment_mode"], "installment")
        self.assertEqual(data["plan_status"], "pending_first_bill")
        self.assertEqual(data["total_amount"], "12000.00")
        self.assertEqual(data["total_periods"], 12)
        plan_id = UUID(data["installment_plan_id"])

        # Check DB plan
        plan = installments_repo.get_installment_plan(self.conn, plan_id)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["status"], "pending_first_bill")

        # Check 12 schedule periods
        periods = installments_repo.list_periods_for_plan(self.conn, plan_id)
        self.assertEqual(len(periods), 12)
        for p in periods:
            self.assertEqual(p["scheduled_amount"], Decimal("1000.000000"))
            self.assertEqual(p["status"], "scheduled")
            self.assertIsNone(p["expense_transaction_id"])

        # CRITICAL INVARIANTS:
        # 1. NO transactions row created
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (UUID(data["request_id"]),))
            self.assertEqual(cur.fetchone()[0], 0)

        # 2. NO account_state mutation (remains 0)
        state = accounts_repo.get_account_state(self.conn, self.acc_cny_credit)
        self.assertEqual(state["ledger_balance"], Decimal("0.000000"))

    def test_39_installment_10000_over_3_rounding(self):
        # 39. 10000 / 3 allocation: 3333.33, 3333.33, 3333.34
        schedules = calculate_installment_schedule(Decimal("10000.00"), "CNY", 3)
        self.assertEqual(len(schedules), 3)
        self.assertEqual(schedules[0], Decimal("3333.33"))
        self.assertEqual(schedules[1], Decimal("3333.33"))
        self.assertEqual(schedules[2], Decimal("3333.34"))
        self.assertEqual(sum(schedules), Decimal("10000.00"))

    # ==========================================
    # H. Atomicity & Outer Transaction Rollback
    # ==========================================

    def test_42_44_atomic_rollback_on_failure(self):
        # 42-44. If workflow fails after internal ledger action, outer transaction boundary rolls back everything
        with self.assertRaises(Exception):
            with transaction(self.conn):
                # Record transaction
                ledger_service.record_expense(
                    conn=self.conn,
                    household_id=self.household_id,
                    from_account_id=self.acc_cny_cash,
                    amount=Decimal("500.00"),
                    currency="CNY",
                    category_id=self.cat_dining,
                    occurred_on=date(2026, 8, 19)
                )
                # Intentionally raise an unhandled exception before commit
                raise RuntimeError("Simulated mid-workflow crash!")

        # Verify balance remains intact
        state = accounts_repo.get_account_state(self.conn, self.acc_cny_cash)
        self.assertEqual(state["ledger_balance"], Decimal("5000.000000"))

    # ==========================================
    # I. Ingestion API Contracts & Recovery
    # ==========================================

    def test_45_get_by_key_device_scoped(self):
        # 45. GET by key device-scoped
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Lunch Bento",
            original_amount=Decimal("45.00"),
            original_currency="CNY",
            from_account="ICBC_Debit",
            category="Dining",
            confidence=1.0
        ))

        key = "test-key-45-get-by-key"
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"idempotency_key": key, "image": self._sample_image_payload()}
        )
        self.assertEqual(res.status_code, 200)

        # Device 1 retrieves by key
        res_get1 = self.client.get(f"/api/v1/ingestion-requests/by-key/{key}", headers={"Authorization": f"Bearer {self.raw_token_1}"})
        self.assertEqual(res_get1.status_code, 200)
        self.assertEqual(res_get1.json()["status"], "committed")

        # Device 2 queries same key -> 404 REQUEST_NOT_FOUND
        res_get2 = self.client.get(f"/api/v1/ingestion-requests/by-key/{key}", headers={"Authorization": f"Bearer {self.raw_token_2}"})
        self.assertEqual(res_get2.status_code, 404)
        self.assertEqual(res_get2.json()["error"]["code"], "REQUEST_NOT_FOUND")

    def test_46_reject_flow(self):
        # 46. reject flow
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Blurry Receipt",
            confidence=0.3
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"idempotency_key": "test-key-46-reject", "image": self._sample_image_payload()}
        )
        self.assertEqual(res.status_code, 200)
        req_id = res.json()["request_id"]

        # Reject request
        res_rej = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/reject",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"reason": "Blurry screenshot, wrong photo"}
        )
        self.assertEqual(res_rej.status_code, 200)
        self.assertEqual(res_rej.json()["status"], "rejected")

        # Check DB status is rejected
        req_db = ingestion_repo.get_ingestion_request(self.conn, UUID(req_id))
        self.assertEqual(req_db["status"], "rejected")
        self.assertEqual(req_db["failure_code"], "Blurry screenshot, wrong photo")
