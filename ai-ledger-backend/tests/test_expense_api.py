import os
os.environ["ENVIRONMENT"] = "test"
os.environ["DB_SCHEMA"] = "vibeledger_test_runner"

import unittest
import uuid
from uuid import UUID, uuid4
import hashlib
import base64
import threading
from decimal import Decimal
from datetime import date, datetime, timezone, timedelta
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
from app.services import ledger_service
from app.services.gemini_service import ExpenseExtractionResult, MockGeminiService, GeminiService
from app.services.reference_fx_service import ReferenceFxService, FxRateProvider, FrankfurterFxProvider
from app.domain.transactions import (
    LedgerDomainError,
    FxProviderUnavailableError,
    FxRateUnavailableError,
    GeminiDependencyError,
    InvalidImagePayloadError
)

# Valid minimal 1x1 image fixture bytes
VALID_PNG_BYTES = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
    b'\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82'
)
VALID_JPEG_BYTES = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb'
    b'\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c'
    b'\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#'
    b'\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01'
    b'\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00'
    b'\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08'
    b'\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'
)

class TestExpenseApi(unittest.TestCase):
    def setUp(self):
        if not config.is_safe_for_testing():
            self.skipTest("Skipping integration test. ENVIRONMENT must be 'test'.")

        self.schema_name = f"vibeledger_test_{uuid4().hex[:12]}"
        runner.run_migrations(self.schema_name)
        self.conn = get_connection(self.schema_name)
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id_1 = uuid4()
        self.device_id_2 = uuid4()
        self.raw_token_1 = f"vbl_test_{uuid4().hex}"
        self.raw_token_2 = f"vbl_test_{uuid4().hex}"

        # Setup standard app and client
        self.app = create_app()
        self.client = TestClient(self.app)

        # Mock AI and FX services
        self.mock_gemini = MockGeminiService()
        self.mock_fx = ReferenceFxService(fixed_rates={
            ("JPY", "USD"): Decimal("0.00689"),
            ("USD", "CNY"): Decimal("7.200000"),
            ("EUR", "CNY"): Decimal("7.850000"),
        })

        # Inject mocks onto router instances
        expenses_router._gemini_service = self.mock_gemini
        expenses_router._reference_fx_service = self.mock_fx
        ingestion_router._gemini_service = self.mock_gemini
        ingestion_router._reference_fx_service = self.mock_fx

        # Override DB connection in app dependency
        def _get_db():
            conn = get_connection(self.schema_name)
            try:
                yield conn
            finally:
                conn.close()
        self.app.dependency_overrides[get_db_connection] = _get_db

        with self.conn.cursor() as cur:
            # 1. Household & User
            cur.execute(
                """
                INSERT INTO households (id, name, reporting_currency, ledger_start_date, status, created_at, updated_at)
                VALUES (%s, %s, %s, '2026-01-01', 'active', now(), now());
                """,
                (self.household_id, "Test Household", "CNY")
            )
            cur.execute(
                """
                INSERT INTO users (id, auth_subject, email, display_name, default_currency, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'Test User', 'CNY', 'active', now(), now());
                """,
                (self.user_id, f"auth_{uuid4().hex[:8]}", f"user_{uuid4().hex[:6]}@example.com")
            )
            cur.execute(
                """
                INSERT INTO household_members (household_id, user_id, role, joined_at)
                VALUES (%s, %s, 'owner', now());
                """,
                (self.household_id, self.user_id)
            )

            # 2. Devices
            t1_hash = hashlib.sha256(self.raw_token_1.encode('utf-8')).digest()
            devices_repo.create_device(
                conn=self.conn,
                device_id=self.device_id_1,
                user_id=self.user_id,
                device_name="iPhone 15 Pro",
                token_hash=t1_hash,
                platform="ios_shortcuts"
            )

            t2_hash = hashlib.sha256(self.raw_token_2.encode('utf-8')).digest()
            devices_repo.create_device(
                conn=self.conn,
                device_id=self.device_id_2,
                user_id=self.user_id,
                device_name="iPad Pro",
                token_hash=t2_hash,
                platform="ios_shortcuts"
            )

            # 3. Accounts
            self.acc_cny_checking = uuid4()
            self.acc_usd_credit = uuid4()
            self.acc_cny_credit = uuid4()

            cur.execute(
                """
                INSERT INTO accounts (id, household_id, name, account_type, currency, status, created_at, updated_at)
                VALUES
                (%s, %s, '招商银行储蓄卡', 'cash', 'CNY', 'active', now(), now()),
                (%s, %s, 'USD_Visa_Card', 'credit', 'USD', 'active', now(), now()),
                (%s, %s, '招商银行信用卡', 'credit', 'CNY', 'active', now(), now());
                """,
                (self.acc_cny_checking, self.household_id, self.acc_usd_credit, self.household_id, self.acc_cny_credit, self.household_id)
            )

            cur.execute(
                """
                INSERT INTO account_state (account_id, ledger_balance, row_version, updated_at)
                VALUES
                (%s, 10000.00, 0, now()),
                (%s, 0.00, 0, now()),
                (%s, 0.00, 0, now());
                """,
                (self.acc_cny_checking, self.acc_usd_credit, self.acc_cny_credit)
            )

            # 4. Aliases
            cur.execute(
                """
                INSERT INTO account_aliases (id, account_id, alias_text, normalized_alias, status, created_at)
                VALUES
                (%s, %s, '招行卡', '招行卡', 'active', now()),
                (%s, %s, '招行信用卡', '招行信用卡', 'active', now());
                """,
                (uuid4(), self.acc_cny_checking, uuid4(), self.acc_cny_credit)
            )

            # 5. Categories
            self.cat_food = uuid4()
            self.cat_transport = uuid4()
            cur.execute(
                """
                INSERT INTO categories (id, household_id, name, category_type, status, created_at, updated_at)
                VALUES
                (%s, %s, '餐饮美食', 'expense', 'active', now(), now()),
                (%s, %s, '交通出行', 'expense', 'active', now(), now());
                """,
                (self.cat_food, self.household_id, self.cat_transport, self.household_id)
            )
        self.conn.commit()

    def tearDown(self):
        self.app.dependency_overrides.clear()
        if hasattr(self, "conn") and self.conn:
            self.conn.close()

        if config.is_safe_for_testing() and hasattr(self, "schema_name"):
            config.validate_test_schema(self.schema_name)
            drop_conn = get_connection()
            try:
                with drop_conn.cursor() as cur:
                    quoted_schema = sql.Identifier(self.schema_name)
                    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE;").format(schema=quoted_schema))
                drop_conn.commit()
            except Exception:
                pass
            finally:
                drop_conn.close()

    def _sample_png_payload(self):
        return {
            "mime_type": "image/png",
            "base64": base64.b64encode(VALID_PNG_BYTES).decode('utf-8')
        }

    def _sample_jpeg_payload(self):
        return {
            "mime_type": "image/jpeg",
            "base64": base64.b64encode(VALID_JPEG_BYTES).decode('utf-8')
        }

    # =========================================================================
    # 1. AUTHENTICATION & SECURITY
    # =========================================================================

    def test_01_missing_auth_token_rejected(self):
        res = self.client.post(
            "/api/v1/expenses",
            json={
                "idempotency_key": "test-key-01-missing-auth",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    def test_02_invalid_or_unknown_token_rejected(self):
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": "Bearer vbl_invalid_token_1234567890"},
            json={
                "idempotency_key": "test-key-02-invalid-token",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    def test_03_revoked_device_token_rejected(self):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE devices SET status = 'revoked', revoked_at = now() WHERE id = %s;", (self.device_id_1,))
        self.conn.commit()

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-03-revoked-token",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"]["code"], "DEVICE_REVOKED")

    def test_04_valid_token_authenticates_and_updates_last_seen(self):
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-04-valid-auth",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertIn(res.status_code, (200, 201))
        dev = devices_repo.get_active_device_by_token_hash(
            self.conn,
            hashlib.sha256(self.raw_token_1.encode('utf-8')).digest()
        )
        self.assertIsNotNone(dev)
        self.assertIsNotNone(dev["last_seen_at"])

    def test_05_raw_token_never_persisted(self):
        self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-05-privacy-check",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM devices WHERE token_hash = %s;", (self.raw_token_1.encode('utf-8'),))
            self.assertEqual(cur.fetchone()[0], 0)

    # =========================================================================
    # 2. IDEMPOTENCY & CONCURRENCY
    # =========================================================================

    def test_06_same_device_same_key_same_payload_replays_without_duplicate(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="星巴克咖啡",
            original_amount=Decimal("38.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0}
        ))

        payload = {
            "idempotency_key": "test-key-06-replay-idempotency",
            "captured_at": "2026-08-19T10:00:00+08:00",
            "client_version": "1.0.0",
            "image": self._sample_jpeg_payload(),
            "note": "星巴克咖啡"
        }

        # 1st call -> commits
        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertEqual(data1["status"], "committed")

        # 2nd call -> exact replay, no second AI call
        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data1, data2)
        self.assertEqual(self.mock_gemini.call_count, 1)

        # Verify only 1 transaction in DB
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (UUID(data1["request_id"]),))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_08_same_device_same_key_different_payload_returns_409(self):
        payload1 = {
            "idempotency_key": "test-key-08-conflict-key",
            "captured_at": "2026-08-19T10:00:00+08:00",
            "image": self._sample_jpeg_payload(),
            "note": "First note"
        }
        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload1)
        self.assertEqual(res1.status_code, 200)

        payload2 = {
            "idempotency_key": "test-key-08-conflict-key",
            "captured_at": "2026-08-19T10:00:00+08:00",
            "image": self._sample_jpeg_payload(),
            "note": "Second DIFFERENT note"
        }
        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload2)
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

    def test_09_different_devices_use_same_textual_key_independently(self):
        payload = {
            "idempotency_key": "shared-text-key-between-devices",
            "captured_at": "2026-08-19T10:00:00+08:00",
            "image": self._sample_jpeg_payload()
        }
        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res1.status_code, 200)

        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_2}"}, json=payload)
        self.assertEqual(res2.status_code, 200)
        self.assertNotEqual(res1.json()["request_id"], res2.json()["request_id"])

    def test_10_recovery_by_idempotency_key(self):
        key = "test-key-10-recovery-flow"
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": key,
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        committed_data = res.json()

        res_get = self.client.get(
            f"/api/v1/ingestion-requests/by-key/{key}",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_get.status_code, 200)
        self.assertEqual(res_get.json(), committed_data)

    def test_11_concurrent_identical_requests_produce_single_outcome(self):
        key = "test-key-11-concurrent-race"
        results = []
        errors = []

        def worker():
            try:
                client = TestClient(self.app)
                r = client.post(
                    "/api/v1/expenses",
                    headers={"Authorization": f"Bearer {self.raw_token_1}"},
                    json={
                        "idempotency_key": key,
                        "captured_at": "2026-08-19T10:00:00+08:00",
                        "image": self._sample_jpeg_payload()
                    }
                )
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Exceptions in worker: {errors}")
        for r in results:
            self.assertEqual(r.status_code, 200)

        # Assert exactly one transaction row
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingestion_requests WHERE idempotency_key = %s;", (key,))
            self.assertEqual(cur.fetchone()[0], 1)

    # =========================================================================
    # 3. ONE-OFF EXPENSE & CONFIRMATION RULES
    # =========================================================================

    def test_12_normal_one_off_expense_lifecycle(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="全家便利店",
            original_amount=Decimal("15.50"),
            original_currency="CNY",
            from_account="招行卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-12-normal-expense",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "committed")
        self.assertIn("transaction_id", data)

        # Check account_state balance decreased: 10000 - 15.50 = 9984.50
        state = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)
        self.assertEqual(state["ledger_balance"], Decimal("9984.500000"))

    def test_13_new_merchant_alone_does_not_force_confirmation(self):
        # MERCHANT NOVELTY ALONE MUST NEVER FORCE CONFIRMATION
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="从未见过的全新未知新奇商户12345",
            original_amount=Decimal("99.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.75, # lower overall confidence due to new merchant
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-13-new-merchant",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "committed")

    def test_14_unresolved_account_forces_confirmation(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="商户A",
            original_amount=Decimal("50.00"),
            original_currency="CNY",
            from_account=None,
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.8,
            field_confidence={"amount": 1.0, "currency": 1.0, "category": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-14-unresolved-acc",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "needs_confirmation")
        self.assertTrue(any(w["code"] == "ACCOUNT_UNRESOLVED" for w in data["warnings"]))

    def test_17_missing_currency_forces_confirmation_no_silent_cny_default(self):
        # MUST NOT treat unknown currency as CNY
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="海外店铺",
            original_amount=Decimal("100.00"),
            original_currency=None, # Missing currency
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.9,
            field_confidence={"amount": 1.0, "account": 1.0, "category": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-17-missing-currency",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "needs_confirmation")
        self.assertTrue(any(w["code"] == "CURRENCY_UNCLEAR" for w in data["warnings"]))

    def test_18_low_currency_confidence_forces_confirmation(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="商店",
            original_amount=Decimal("100.00"),
            original_currency="USD",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.9,
            field_confidence={"amount": 1.0, "currency": 0.50, "account": 1.0, "category": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-18-low-curr-conf",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "needs_confirmation")
        self.assertTrue(any(w["code"] == "LOW_CURRENCY_CONFIDENCE" for w in data["warnings"]))

    def test_19_low_amount_confidence_forces_confirmation(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="商店",
            original_amount=Decimal("100.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.9,
            field_confidence={"amount": 0.60, "currency": 1.0, "account": 1.0, "category": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-19-low-amt-conf",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "needs_confirmation")
        self.assertTrue(any(w["code"] == "LOW_AMOUNT_CONFIDENCE" for w in data["warnings"]))

    def test_20_invalid_payment_mode_forces_confirmation(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="商店",
            original_amount=Decimal("100.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="random_mode_invalid",
            confidence=0.9,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-20-invalid-mode",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "needs_confirmation")
        self.assertTrue(any(w["code"] == "INVALID_PAYMENT_MODE" for w in data["warnings"]))

    # =========================================================================
    # 4. INSTALLMENT VALIDATION & CAPTURE
    # =========================================================================

    def test_21_installment_missing_or_invalid_periods_forces_confirmation(self):
        for invalid_period in (None, 1, 150):
            self.mock_gemini.set_next_result(ExpenseExtractionResult(
                occurred_on=date(2026, 8, 19),
                merchant="Apple Store",
                total_amount=Decimal("9999.00"),
                original_currency="CNY",
                from_account="招商银行信用卡",
                payment_mode="installment",
                total_periods=invalid_period,
                confidence=1.0,
                field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0}
            ))

            res = self.client.post(
                "/api/v1/expenses",
                headers={"Authorization": f"Bearer {self.raw_token_1}"},
                json={
                    "idempotency_key": f"test-key-21-periods-{invalid_period}",
                    "captured_at": "2026-08-19T10:00:00+08:00",
                    "image": self._sample_jpeg_payload()
                }
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "needs_confirmation")
            self.assertTrue(any(w["code"] == "INVALID_INSTALLMENT_PERIODS" for w in data["warnings"]))

            # Assert ZERO plans, periods, transactions
            with self.conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM installment_plans WHERE source_request_id = %s;", (UUID(data["request_id"]),))
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (UUID(data["request_id"]),))
                self.assertEqual(cur.fetchone()[0], 0)

    def test_22_installment_non_credit_account_forces_confirmation(self):
        # Installment on Cash account -> rejected into confirmation
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Apple Store",
            total_amount=Decimal("9999.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡", # Cash account!
            payment_mode="installment",
            total_periods=12,
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "total_periods": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-22-non-credit-installment",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "needs_confirmation")
        self.assertTrue(any(w["code"] == "NON_CREDIT_INSTALLMENT_ACCOUNT" for w in data["warnings"]))

    def test_23_installment_plan_capture_and_exact_rounding_allocation(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Apple Store",
            total_amount=Decimal("10000.00"),
            original_currency="CNY",
            from_account="招商银行信用卡",
            payment_mode="installment",
            total_periods=3,
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "total_periods": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-23-installment-capture",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "committed")
        self.assertEqual(data["plan_status"], "pending_first_bill")
        self.assertIn("installment_plan_id", data)

        plan_id = UUID(data["installment_plan_id"])
        periods = installments_repo.list_periods_for_plan(self.conn, plan_id)
        self.assertEqual(len(periods), 3)

        # Check exact rounding: 3333.33, 3333.33, 3333.34
        self.assertEqual(periods[0]["scheduled_amount"], Decimal("3333.33"))
        self.assertEqual(periods[1]["scheduled_amount"], Decimal("3333.33"))
        self.assertEqual(periods[2]["scheduled_amount"], Decimal("3333.34"))
        self.assertEqual(sum(p["scheduled_amount"] for p in periods), Decimal("10000.00"))

        # Zero financial transaction rows & zero account_state mutations
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (UUID(data["request_id"]),))
            self.assertEqual(cur.fetchone()[0], 0)
        state = accounts_repo.get_account_state(self.conn, self.acc_cny_credit)
        self.assertEqual(state["ledger_balance"], Decimal("0.00"))

    # =========================================================================
    # 5. FOREIGN CREDIT CARD & REFERENCE FX
    # =========================================================================

    def test_25_foreign_currency_credit_card_expense_estimated_leg(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Tokyo Ginza Store",
            original_amount=Decimal("10000"),
            original_currency="JPY",
            from_account="USD_Visa_Card",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-25-foreign-card",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
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

        state = accounts_repo.get_account_state(self.conn, self.acc_usd_credit)
        self.assertEqual(state["ledger_balance"], Decimal("-68.900000"))

    def test_26_foreign_card_unavailable_fx_raises_error_no_ledger_mutation(self):
        class OfflineNullProvider(FxRateProvider):
            def fetch_rate(self, f, t, as_of=None):
                return None

        expenses_router._reference_fx_service = ReferenceFxService(fixed_rates={}, provider=OfflineNullProvider())
        try:
            self.mock_gemini.set_next_result(ExpenseExtractionResult(
                occurred_on=date(2026, 8, 19),
                merchant="Paris Boutique",
                original_amount=Decimal("100.00"),
                original_currency="EUR", # No EUR -> USD rate in mock
                from_account="USD_Visa_Card",
                category="餐饮美食",
                payment_mode="one_off",
                confidence=1.0,
                field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0}
            ))

            res = self.client.post(
                "/api/v1/expenses",
                headers={"Authorization": f"Bearer {self.raw_token_1}"},
                json={
                    "idempotency_key": "test-key-26-unavailable-fx",
                    "captured_at": "2026-08-19T10:00:00+08:00",
                    "image": self._sample_jpeg_payload()
                }
            )
            self.assertEqual(res.status_code, 422)
            self.assertEqual(res.json()["error"]["code"], "FX_RATE_UNAVAILABLE")

            # Zero balance change
            state = accounts_repo.get_account_state(self.conn, self.acc_usd_credit)
            self.assertEqual(state["ledger_balance"], Decimal("0.00"))
        finally:
            expenses_router._reference_fx_service = self.mock_fx

    def test_27_production_fx_provider_weekend_and_t_minus_1_fallback(self):
        class MockHttpProvider(FxRateProvider):
            def __init__(self):
                self.queried_dates = []

            def fetch_rate(self, from_curr, to_curr, as_of=None):
                self.queried_dates.append(as_of)
                if from_curr == "EUR" and to_curr == "USD":
                    return Decimal("1.0850")
                return None

        mock_provider = MockHttpProvider()
        svc = ReferenceFxService(provider=mock_provider)

        # Saturday date should fallback
        saturday = date(2026, 8, 22)
        rate = svc.get_rate("EUR", "USD", as_of=saturday)
        self.assertEqual(rate, Decimal("1.0850"))
        self.assertEqual(mock_provider.queried_dates[0], saturday)

    # =========================================================================
    # 6. IMAGE VALIDATION
    # =========================================================================

    def test_28_image_validation_valid_jpeg_and_png(self):
        for img_payload in (self._sample_jpeg_payload(), self._sample_png_payload()):
            self.mock_gemini.set_next_result(ExpenseExtractionResult(
                occurred_on=date(2026, 8, 19),
                merchant="Test",
                original_amount=Decimal("10.00"),
                original_currency="CNY",
                from_account="招商银行储蓄卡",
                category="餐饮美食",
                payment_mode="one_off",
                confidence=1.0,
                field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 1.0}
            ))
            res = self.client.post(
                "/api/v1/expenses",
                headers={"Authorization": f"Bearer {self.raw_token_1}"},
                json={
                    "idempotency_key": f"test-img-{uuid4().hex[:8]}",
                    "captured_at": "2026-08-19T10:00:00+08:00",
                    "image": img_payload
                }
            )
            self.assertEqual(res.status_code, 200)

    def test_29_image_validation_malformed_base64_returns_422(self):
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-29-bad-b64",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": {
                    "mime_type": "image/jpeg",
                    "base64": "!!!not_valid_base64!!!"
                }
            }
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "INVALID_IMAGE_PAYLOAD")

    def test_30_image_validation_fake_bytes_or_mime_mismatch_returns_422(self):
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-30-fake-bytes",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": {
                    "mime_type": "image/jpeg",
                    "base64": base64.b64encode(b"not_a_real_jpeg").decode('utf-8')
                }
            }
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "INVALID_IMAGE_PAYLOAD")

    def test_31_image_validation_oversized_image_returns_422(self):
        huge_fake_jpeg = b"\xff\xd8\xff" + (b"0" * 15 * 1024 * 1024)
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-31-oversized",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": {
                    "mime_type": "image/jpeg",
                    "base64": base64.b64encode(huge_fake_jpeg).decode('utf-8')
                }
            }
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "INVALID_IMAGE_PAYLOAD")

    # =========================================================================
    # 7. CAPTURED_AT & DATE FALLBACK
    # =========================================================================

    def test_32_captured_at_required_and_timezone_validation(self):
        # Missing captured_at
        res1 = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-32-missing-captured-at",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res1.status_code, 422)
        self.assertEqual(res1.json()["error"]["code"], "INVALID_REQUEST")

        # Timezone-naive captured_at
        res2 = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-32-naive-captured-at",
                "captured_at": "2026-08-19T10:00:00", # No timezone offset!
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res2.status_code, 422)
        self.assertEqual(res2.json()["error"]["code"], "INVALID_REQUEST")

    def test_33_occurred_on_fallback_uses_captured_at_local_calendar_date(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=None, # Missing date in receipt
            merchant="咖啡馆",
            original_amount=Decimal("30.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 0.1}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-33-local-date-fallback",
                "captured_at": "2026-08-19T00:30:00+08:00", # Local date is 2026-08-19 (UTC is 2026-08-18)
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        tx = tx_repo.get_transaction(self.conn, UUID(data["transaction_id"]))
        self.assertEqual(tx["occurred_on"], date(2026, 8, 19))

    # =========================================================================
    # 8. ACCOUNT ALIASES & GEMINI DEPENDENCY FAILURE
    # =========================================================================

    def test_34_gemini_system_prompt_includes_account_aliases(self):
        svc = GeminiService(api_key="test_key")
        accounts = [
            {"name": "招商银行储蓄卡", "account_type": "cash", "currency": "CNY", "aliases": ["招行卡", "工资卡"]}
        ]
        categories = [{"name": "餐饮美食", "category_type": "expense"}]
        prompt = svc.build_system_prompt(accounts, categories)
        self.assertIn("招商银行储蓄卡", prompt)
        self.assertIn("aliases: 招行卡, 工资卡", prompt)

    def test_35_gemini_dependency_failure_returns_503_retryable(self):
        self.mock_gemini.should_raise = GeminiDependencyError("Gemini rate limit exceeded.")

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-35-gemini-503",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 503)
        data = res.json()
        self.assertEqual(data["error"]["code"], "GEMINI_SERVICE_UNAVAILABLE")
        self.assertTrue(data["error"]["retryable"])

        self.mock_gemini.should_raise = None

    # =========================================================================
    # 9. REVISE, REJECT & CONCURRENT CONFIRM
    # =========================================================================

    def test_36_revise_flow_and_state_machine_validation(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="超市",
            original_amount=Decimal("120.00"),
            original_currency="CNY",
            from_account=None,
            category=None,
            payment_mode="one_off",
            confidence=0.8
        ))

        res_draft = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-36-revise-flow",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_draft.json()["request_id"]

        # Revise with structured account and category IDs
        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "from_account_id": str(self.acc_cny_checking),
                "category_id": str(self.cat_food)
            }
        )
        self.assertEqual(res_rev.status_code, 200)
        draft = res_rev.json()["draft"]
        self.assertEqual(draft["from_account"]["id"], str(self.acc_cny_checking))
        self.assertEqual(draft["category"]["id"], str(self.cat_food))

        # Confirm the revised draft
        res_conf = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_conf.status_code, 200)
        self.assertEqual(res_conf.json()["status"], "committed")

    def test_37_revise_with_invalid_account_or_category_rejected(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="超市",
            original_amount=Decimal("50.00"),
            original_currency="CNY",
            from_account=None,
            category=None
        ))
        res_draft = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-37-invalid-revise-ids",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_draft.json()["request_id"]

        # Attempt to revise with nonexistent account ID
        res_bad = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"from_account_id": str(uuid4())}
        )
        self.assertEqual(res_bad.status_code, 422)
        self.assertEqual(res_bad.json()["error"]["code"], "ACCOUNT_NOT_FOUND")

    def test_38_reject_flow_and_deterministic_replay(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="未知",
            original_amount=Decimal("10.00"),
            original_currency="CNY",
            from_account=None
        ))
        res_draft = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-38-reject-flow",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_draft.json()["request_id"]

        # 1st reject
        res_rej1 = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/reject",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"reason": "Not an expense"}
        )
        self.assertEqual(res_rej1.status_code, 200)
        self.assertEqual(res_rej1.json()["status"], "rejected")

        # 2nd reject -> deterministic replay
        res_rej2 = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/reject",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_rej2.status_code, 200)
        self.assertEqual(res_rej2.json()["status"], "rejected")

        # Cannot revise a rejected request
        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"merchant": "Revised"}
        )
        self.assertEqual(res_rev.status_code, 422)
        self.assertEqual(res_rev.json()["error"]["code"], "INVALID_REQUEST_STATE")

    def test_39_concurrent_confirm_produces_single_commit(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="电影院",
            original_amount=Decimal("45.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category=None, # forces confirmation
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 1.0}
        ))
        res_draft = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-39-concurrent-confirm",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_draft.json()["request_id"]

        # Revise category
        self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"category_id": str(self.cat_food)}
        )

        results = []
        errors = []
        def confirm_worker():
            try:
                client = TestClient(self.app)
                r = client.post(
                    f"/api/v1/ingestion-requests/{req_id}/confirm",
                    headers={"Authorization": f"Bearer {self.raw_token_1}"}
                )
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=confirm_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Exceptions in confirm_worker: {errors}")
        for r in results:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "committed")

        # Exactly 1 transaction in DB
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (UUID(req_id),))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_40_confirm_rejects_stale_inactive_account_or_category(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="书店",
            original_amount=Decimal("80.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category=None,
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 1.0}
        ))
        res_draft = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-40-stale-confirm",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_draft.json()["request_id"]

        # Revise category
        self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"category_id": str(self.cat_food)}
        )

        # Inactivate account before confirmation
        with self.conn.cursor() as cur:
            cur.execute("UPDATE accounts SET status = 'inactive' WHERE id = %s;", (self.acc_cny_checking,))
        self.conn.commit()

        res_conf = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_conf.status_code, 422)
        self.assertEqual(res_conf.json()["error"]["code"], "ACCOUNT_INACTIVE")

    # =========================================================================
    # 10. ATOMIC ROLLBACK TESTS
    # =========================================================================

    def test_42_atomic_rollback_on_failure_no_partial_mutations(self):
        initial_balance = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)["ledger_balance"]

        # Explicitly verify rollback with exact RuntimeError
        try:
            with transaction(self.conn):
                ledger_service.record_expense(
                    conn=self.conn,
                    household_id=self.household_id,
                    from_account_id=self.acc_cny_checking,
                    amount=Decimal("100.00"),
                    currency="CNY",
                    category_id=self.cat_food,
                    occurred_on=date(2026, 8, 19),
                    merchant="Should Rollback"
                )
                raise RuntimeError("Simulated mid-workflow crash")
        except RuntimeError:
            pass

        # Verify state is completely untouched
        current_balance = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)["ledger_balance"]
        self.assertEqual(current_balance, initial_balance)

        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE merchant = 'Should Rollback';")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(*) FROM audit_events WHERE after_data->>'merchant' = 'Should Rollback';")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_43_atomic_rollback_expense_workflow_failure(self):
        initial_balance = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)["ledger_balance"]
        req_id = uuid4()

        try:
            with transaction(self.conn):
                ingestion_repo.create_ingestion_request(
                    conn=self.conn,
                    request_id=req_id,
                    device_id=self.device_id_1,
                    idempotency_key="test-key-43-workflow-rollback",
                    request_kind="expense",
                    request_hash=b"fakehash",
                    status="processing"
                )
                ledger_service.record_expense(
                    conn=self.conn,
                    household_id=self.household_id,
                    from_account_id=self.acc_cny_checking,
                    amount=Decimal("200.00"),
                    currency="CNY",
                    category_id=self.cat_food,
                    occurred_on=date(2026, 8, 19),
                    source_request_id=req_id
                )
                raise RuntimeError("Simulated failure before transaction commit")
        except RuntimeError:
            pass

        # Zero rows persisted
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingestion_requests WHERE id = %s;", (req_id,))
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (req_id,))
            self.assertEqual(cur.fetchone()[0], 0)

        current_balance = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)["ledger_balance"]
        self.assertEqual(current_balance, initial_balance)

    def test_44_atomic_rollback_installment_workflow_failure(self):
        plan_id = uuid4()
        req_id = uuid4()

        try:
            with transaction(self.conn):
                ingestion_repo.create_ingestion_request(
                    conn=self.conn,
                    request_id=req_id,
                    device_id=self.device_id_1,
                    idempotency_key="test-key-44-installment-rollback",
                    request_kind="expense",
                    request_hash=b"fakehash",
                    status="processing"
                )
                installments_repo.create_installment_plan(
                    conn=self.conn,
                    plan_id=plan_id,
                    household_id=self.household_id,
                    credit_account_id=self.acc_cny_credit,
                    purchase_occurred_on=date(2026, 8, 19),
                    original_amount=Decimal("6000.00"),
                    original_currency="CNY",
                    account_currency="CNY",
                    total_periods=6,
                    status="pending_first_bill",
                    source_request_id=req_id
                )
                installments_repo.create_installment_period(
                    conn=self.conn,
                    period_id=uuid4(),
                    plan_id=plan_id,
                    period_no=1,
                    scheduled_amount=Decimal("1000.00"),
                    currency="CNY",
                    status="scheduled"
                )
                raise RuntimeError("Crash after partial schedule creation")
        except RuntimeError:
            pass

        # Zero installment rows persisted
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingestion_requests WHERE id = %s;", (req_id,))
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(*) FROM installment_plans WHERE id = %s;", (plan_id,))
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(*) FROM installment_periods WHERE plan_id = %s;", (plan_id,))
            self.assertEqual(cur.fetchone()[0], 0)

    # =========================================================================
    # 12. PHASE 3 FINAL-FIX REGRESSION TESTS
    # =========================================================================

    def test_45_confirm_installment_rejects_missing_periods(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Apple Store",
            original_amount=Decimal("9000.00"),
            original_currency="CNY",
            from_account="招商银行信用卡",
            category="餐饮美食",
            payment_mode="installment",
            total_amount=Decimal("9000.00"),
            total_periods=None, # Missing total periods
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-45-missing-periods",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "needs_confirmation")
        req_id = res.json()["request_id"]

        # Directly confirming draft without total_periods must be rejected with 422 (never default to 12)
        res_conf = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_conf.status_code, 422)
        self.assertEqual(res_conf.json()["error"]["code"], "INVALID_INSTALLMENT_PERIODS")

        # Zero installment plans created
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM installment_plans WHERE source_request_id = %s;", (UUID(req_id),))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_46_revise_payment_mode_and_total_periods_then_confirm(self):
        # Start with draft where payment_mode was missing / invalid
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Apple Store",
            original_amount=Decimal("6000.00"),
            original_currency="CNY",
            from_account="招商银行信用卡",
            category="餐饮美食",
            payment_mode=None, # Unresolved
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 1.0}
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-46-revise-installment",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "needs_confirmation")
        req_id = res.json()["request_id"]

        # Revise payment_mode and total_periods
        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "payment_mode": "installment",
                "total_periods": 6
            }
        )
        self.assertEqual(res_rev.status_code, 200)
        self.assertEqual(res_rev.json()["draft"]["payment_mode"], "installment")
        self.assertEqual(res_rev.json()["draft"]["total_periods"], 6)

        # Now confirm successfully
        res_conf = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_conf.status_code, 200)
        self.assertEqual(res_conf.json()["status"], "committed")
        self.assertEqual(res_conf.json()["payment_mode"], "installment")
        self.assertEqual(res_conf.json()["total_periods"], 6)

        # Exactly 1 installment plan and 6 periods in DB
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM installment_plans WHERE source_request_id = %s;", (UUID(req_id),))
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT total_periods FROM installment_plans WHERE source_request_id = %s;", (UUID(req_id),))
            self.assertEqual(cur.fetchone()[0], 6)

    def test_47_frankfurter_provider_mocked_network_boundary(self):
        import urllib.request
        from unittest.mock import patch, MagicMock

        provider = FrankfurterFxProvider(base_url="https://api.frankfurter.app", timeout_seconds=5.0)

        def make_mock_resp(status: int, body: bytes):
            m = MagicMock()
            m.status = status
            m.read.return_value = body
            m.__enter__.return_value = m
            m.__exit__.return_value = False
            return m

        # 1. Weekend date (Saturday 2026-08-22) queries Friday (2026-08-21)
        mock_resp_sat = make_mock_resp(200, b'{"rates":{"USD": 1.0850}}')

        with patch("urllib.request.urlopen", return_value=mock_resp_sat) as mock_url:
            rate = provider.fetch_rate("EUR", "USD", as_of=date(2026, 8, 22))
            self.assertEqual(rate, Decimal("1.0850"))
            # Assert Friday was in requested URL
            req_arg = mock_url.call_args[0][0]
            self.assertIn("2026-08-21", req_arg.full_url)

        # 2. Historical 404 fallback to previous business day
        err_404 = urllib.error.HTTPError(url="http://fake", code=404, msg="Not Found", hdrs={}, fp=None)
        mock_resp_prev = make_mock_resp(200, b'{"rates":{"USD": 1.0825}}')

        with patch("urllib.request.urlopen", side_effect=[err_404, mock_resp_prev]):
            rate = provider.fetch_rate("EUR", "USD", as_of=date(2026, 8, 19))
            self.assertEqual(rate, Decimal("1.0825"))

        # 3. Timeout raises FxProviderUnavailableError
        with patch("urllib.request.urlopen", side_effect=TimeoutError("Connection timed out")):
            with self.assertRaises(FxProviderUnavailableError):
                provider.fetch_rate("EUR", "USD", as_of=date(2026, 8, 19))

        # 4. HTTP 500 raises FxProviderUnavailableError
        err_500 = urllib.error.HTTPError(url="http://fake", code=500, msg="Internal Server Error", hdrs={}, fp=None)
        with patch("urllib.request.urlopen", side_effect=err_500):
            with self.assertRaises(FxProviderUnavailableError):
                provider.fetch_rate("EUR", "USD", as_of=date(2026, 8, 19))

        # 5. Unsupported / missing rate returns None
        mock_resp_empty = make_mock_resp(200, b'{"rates":{}}')
        with patch("urllib.request.urlopen", return_value=mock_resp_empty):
            rate = provider.fetch_rate("EUR", "XYZ", as_of=date(2026, 8, 19))
            self.assertIsNone(rate)

        # 6. Strict Decimal preservation (no float intermediate)
        mock_resp_dec = make_mock_resp(200, b'{"rates":{"USD": 1.085000000000000001}}')
        with patch("urllib.request.urlopen", return_value=mock_resp_dec):
            rate = provider.fetch_rate("EUR", "USD", as_of=date(2026, 8, 19))
            self.assertIsInstance(rate, Decimal)
            self.assertEqual(rate, Decimal("1.085000000000000001"))

        # 7. Inverse rate calculation in ReferenceFxService
        svc = ReferenceFxService(provider=provider)
        with patch("urllib.request.urlopen", side_effect=[mock_resp_empty, mock_resp_sat]):
            inv_rate = svc.get_rate("USD", "EUR", as_of=date(2026, 8, 22))
            self.assertIsInstance(inv_rate, Decimal)
            self.assertEqual(inv_rate, Decimal("1") / Decimal("1.0850"))

    def test_48_image_validation_corrupted_payload_with_valid_magic_bytes(self):
        # Valid JPEG magic bytes (\xff\xd8\xff) followed by corrupted garbage bytes
        corrupted_jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00corrupted_non_image_garbage_payload_12345"
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-48-corrupted-image",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": {
                    "mime_type": "image/jpeg",
                    "base64": base64.b64encode(corrupted_jpeg_bytes).decode('utf-8')
                }
            }
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "INVALID_IMAGE_PAYLOAD")

    def test_49_field_confidence_conservative_branching(self):
        # Overall confidence is high (1.0), but field_confidence is empty/omits key fields
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Test Merchant",
            original_amount=Decimal("100.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={} # Empty field confidence -> must be treated conservatively
        ))

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-49-missing-field-conf",
                "captured_at": "2026-08-19T10:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "needs_confirmation")

    def test_50_workflow_rollback_on_ingestion_persistence_failure(self):
        initial_balance = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)["ledger_balance"]

        # Configure high confidence valid extraction
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Cafe",
            original_amount=Decimal("50.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 1.0, "date": 1.0}
        ))

        from unittest.mock import patch
        with patch("app.repositories.ingestion.update_ingestion_request_status", side_effect=RuntimeError("DB error updating response status")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/api/v1/expenses",
                    headers={"Authorization": f"Bearer {self.raw_token_1}"},
                    json={
                        "idempotency_key": "test-key-50-workflow-rollback",
                        "captured_at": "2026-08-19T10:00:00+08:00",
                        "image": self._sample_jpeg_payload()
                    }
                )

        # Assert outer database transaction cleanly rolled back
        current_balance = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)["ledger_balance"]
        self.assertEqual(current_balance, initial_balance)

        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE merchant = 'Cafe';")
            self.assertEqual(cur.fetchone()[0], 0)

if __name__ == "__main__":
    unittest.main()
