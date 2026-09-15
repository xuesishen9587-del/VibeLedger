import unittest
import uuid
from uuid import UUID, uuid4
import hashlib
import base64
from decimal import Decimal
from datetime import date, datetime, timezone, timedelta
import psycopg2
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
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
from app.services.gemini_service import ExpenseExtractionResult, ExpenseRevisionResult, MockGeminiService
from app.services.reference_fx_service import ReferenceFxService, FxRateProvider
from app.domain.transactions import GeminiDependencyError, FxProviderUnavailableError
try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

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

class TestExpenseApiDb(BaseDbTestCase):
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

    def seed_test_data(self):
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id_1 = uuid4()
        self.device_id_2 = uuid4()
        self.raw_token_1 = f"vbl_test_{uuid4().hex}"
        self.raw_token_2 = f"vbl_test_{uuid4().hex}"

        self.mock_gemini = MockGeminiService()
        self.mock_fx = ReferenceFxService(fixed_rates={
            ("JPY", "USD"): Decimal("0.00689"),
            ("USD", "CNY"): Decimal("7.200000"),
            ("EUR", "CNY"): Decimal("7.850000"),
        })

        expenses_router._gemini_service = self.mock_gemini
        expenses_router._reference_fx_service = self.mock_fx
        ingestion_router._gemini_service = self.mock_gemini
        ingestion_router._reference_fx_service = self.mock_fx

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
                "idempotency_key": "key-test-01",
                "captured_at": "2026-08-20T12:00:00Z",
                "client_version": "1.0.0",
                "image": self._sample_png_payload(),
                "note": "Lunch"
            }
        )
        self.assertEqual(res.status_code, 401)
        err = res.json()["error"]
        self.assertEqual(err["code"], "UNAUTHORIZED")
        self.assertEqual(err["retryable"], False)

    def test_02_invalid_or_unknown_token_rejected(self):
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": "Bearer invalid_unknown_token_12345"},
            json={
                "idempotency_key": "key-test-02",
                "captured_at": "2026-08-20T12:00:00Z",
                "client_version": "1.0.0",
                "image": self._sample_png_payload(),
                "note": "Lunch"
            }
        )
        self.assertEqual(res.status_code, 401)
        err = res.json()["error"]
        self.assertEqual(err["code"], "UNAUTHORIZED")
        self.assertEqual(err["retryable"], False)

    def test_03_revoked_device_token_rejected(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE devices
                SET status = 'revoked', revoked_at = now()
                WHERE id = %s;
                """,
                (self.device_id_1,)
            )
        self.conn.commit()

        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "key-test-03",
                "captured_at": "2026-08-20T12:00:00Z",
                "client_version": "1.0.0",
                "image": self._sample_png_payload(),
                "note": "Lunch"
            }
        )
        self.assertEqual(res.status_code, 401)
        err = res.json()["error"]
        self.assertEqual(err["code"], "DEVICE_REVOKED")

    def test_04_valid_token_authenticates_and_updates_last_seen(self):
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "key-test-04",
                "captured_at": "2026-08-20T12:00:00Z",
                "client_version": "1.0.0",
                "image": self._sample_png_payload(),
                "note": "Lunch"
            }
        )
        self.assertEqual(res.status_code, 200)
        
        with self.conn.cursor() as cur:
            cur.execute("SELECT last_seen_at FROM devices WHERE id = %s;", (self.device_id_1,))
            last_seen = cur.fetchone()[0]
            self.assertIsNotNone(last_seen)

    def test_05_raw_token_never_persisted(self):
        # 1. Perform an authenticated API request using the raw Bearer token
        payload = {
            "idempotency_key": f"key-token-test-{uuid4().hex[:8]}",
            "captured_at": "2026-08-20T12:00:00Z",
            "client_version": "1.0.0",
            "image": self._sample_png_payload(),
            "note": "Test raw token never persisted"
        }
        res = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json=payload
        )
        self.assertEqual(res.status_code, 200)

        # 2. Verify the persisted device token remains SHA-256 only and raw token is not persisted
        with self.conn.cursor() as cur:
            cur.execute("SELECT token_hash, last_seen_at FROM devices WHERE id = %s;", (self.device_id_1,))
            row = cur.fetchone()
            stored_hash, last_seen_at = row[0], row[1]
            expected_hash = hashlib.sha256(self.raw_token_1.encode('utf-8')).digest()
            
            # Persisted token_hash must be bytea, exactly 32 bytes SHA-256 binary digest
            self.assertEqual(bytes(stored_hash), expected_hash)
            self.assertEqual(len(bytes(stored_hash)), 32)
            self.assertNotIn(self.raw_token_1.encode('utf-8'), bytes(stored_hash))
            self.assertIsNotNone(last_seen_at)

            # Assert raw token string is nowhere in the devices table
            cur.execute("SELECT device_name, platform FROM devices WHERE id = %s;", (self.device_id_1,))
            dev_row = cur.fetchone()
            self.assertNotIn(self.raw_token_1, str(dev_row))

    # =========================================================================
    # 2. INGESTION IDEMPOTENCY & ISOLATION
    # =========================================================================

    def test_06_same_device_same_key_same_payload_replays_without_duplicate(self):
        payload = {
            "idempotency_key": "key-replay-06",
            "captured_at": "2026-08-20T12:00:00Z",
            "client_version": "1.0.0",
            "image": self._sample_png_payload(),
            "note": "Lunch 06"
        }

        # First request
        res1 = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json=payload
        )
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()

        # Replay request
        res2 = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json=payload
        )
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()

        self.assertEqual(data1["request_id"], data2["request_id"])
        self.assertEqual(data1["status"], data2["status"])

        # Check single ingestion record
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingestion_requests WHERE idempotency_key = 'key-replay-06';")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_08_same_device_same_key_different_payload_returns_409(self):
        payload1 = {
            "idempotency_key": "key-conflict-08",
            "captured_at": "2026-08-20T12:00:00Z",
            "client_version": "1.0.0",
            "image": self._sample_png_payload(),
            "note": "Lunch original"
        }
        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload1)
        self.assertEqual(res1.status_code, 200)

        # Second request modifies payload
        payload2 = dict(payload1, note="Lunch modified")
        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload2)
        self.assertEqual(res2.status_code, 409)
        err = res2.json()["error"]
        self.assertEqual(err["code"], "IDEMPOTENCY_KEY_REUSE")
        self.assertEqual(err["retryable"], False)

    def test_09_different_devices_use_same_textual_key_independently(self):
        payload = {
            "idempotency_key": "shared-text-key",
            "captured_at": "2026-08-20T12:00:00Z",
            "client_version": "1.0.0",
            "image": self._sample_png_payload(),
            "note": "Independent Devices"
        }

        res1 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        res2 = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_2}"}, json=payload)

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        self.assertNotEqual(res1.json()["request_id"], res2.json()["request_id"])

    def test_10_recovery_by_idempotency_key(self):
        key = f"key-recovery-{uuid4().hex}"
        payload = {
            "idempotency_key": key,
            "captured_at": "2026-08-20T12:00:00Z",
            "client_version": "1.0.0",
            "image": self._sample_png_payload(),
            "note": "Recovery Test"
        }

        res_post = self.client.post("/api/v1/expenses", headers={"Authorization": f"Bearer {self.raw_token_1}"}, json=payload)
        self.assertEqual(res_post.status_code, 200)
        committed_data = res_post.json()

        # GET recovery endpoint under /api/v1/ingestion-requests
        res_get = self.client.get(f"/api/v1/ingestion-requests/by-key/{key}", headers={"Authorization": f"Bearer {self.raw_token_1}"})
        self.assertEqual(res_get.status_code, 200)
        self.assertEqual(res_get.json(), committed_data)

    # =========================================================================
    # 3. EXTRACTION CONFIDENCE & AUTO-COMMITTED VS CONFIRMATION BRANCHING
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
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="从未见过的新商户",
            original_amount=Decimal("99.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.75,
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
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="海外店铺",
            original_amount=Decimal("100.00"),
            original_currency=None,
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
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Apple Store",
            total_amount=Decimal("9999.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
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
                original_currency="EUR",
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

    # =========================================================================
    # 6. CAPTURED_AT & DATE FALLBACK
    # =========================================================================

    def test_33_occurred_on_fallback_uses_captured_at_local_calendar_date(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=None,
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
                "captured_at": "2026-08-19T00:30:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        tx = tx_repo.get_transaction(self.conn, UUID(data["transaction_id"]))
        self.assertEqual(tx["occurred_on"], date(2026, 8, 19))

    # =========================================================================
    # 7. GEMINI DEPENDENCY FAILURE
    # =========================================================================

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

        # Zero financial mutation in DB
        state = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)
        self.assertEqual(state["ledger_balance"], Decimal("10000.000000"))

    # =========================================================================
    # 8. REVISE, REJECT & CONCURRENT CONFIRM
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
    # 9. ATOMIC ROLLBACK TESTS
    # =========================================================================

    def test_42_atomic_rollback_on_failure_no_partial_mutations(self):
        initial_balance = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)["ledger_balance"]

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
            total_periods=None,
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

        # Confirming draft without total_periods must return 422
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
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Apple Store",
            original_amount=Decimal("6000.00"),
            original_currency="CNY",
            from_account="招商银行信用卡",
            category="餐饮美食",
            payment_mode=None,
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

        # Confirm
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

    def test_49_field_confidence_conservative_branching(self):
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 19),
            merchant="Test Merchant",
            original_amount=Decimal("100.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=1.0,
            field_confidence={}
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

        current_balance = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)["ledger_balance"]
        self.assertEqual(current_balance, initial_balance)

        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE merchant = 'Cafe';")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_51_natural_language_revision_and_confirm_lifecycle(self):
        """
        Integration test: Natural language revision populates draft, preserves request_id,
        leaves status needs_confirmation, and then confirms successfully into a committed transaction.
        """
        # Initial submission produces needs_confirmation draft (account & category unresolved)
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 20),
            merchant=None,
            original_amount=None,
            original_currency=None,
            from_account=None,
            category=None,
            payment_mode="one_off",
            confidence=0.5
        ))

        res_init = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-51-nl-revision-lifecycle",
                "captured_at": "2026-08-20T12:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        self.assertEqual(res_init.status_code, 200)
        self.assertEqual(res_init.json()["status"], "needs_confirmation")
        req_id = res_init.json()["request_id"]
        # Initial draft has warnings
        self.assertGreater(len(res_init.json()["warnings"]), 0)

        # Queue natural-language revision result
        self.mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            occurred_on=date(2026, 8, 20),
            merchant="优衣库",
            original_amount=Decimal("199.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off"
        ))

        # Call POST /revise with natural-language note
        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "correction_note": "在优衣库花了199元，用招商银行储蓄卡支付，分类餐饮美食"
            }
        )
        self.assertEqual(res_rev.status_code, 200)
        rev_data = res_rev.json()
        self.assertEqual(rev_data["status"], "needs_confirmation")
        self.assertEqual(rev_data["request_id"], str(req_id))
        self.assertEqual(rev_data["draft"]["merchant"], "优衣库")
        self.assertEqual(rev_data["draft"]["original_amount"], "199.00")
        self.assertEqual(rev_data["draft"]["original_currency"], "CNY")
        self.assertEqual(rev_data["draft"]["from_account"]["id"], str(self.acc_cny_checking))
        self.assertEqual(rev_data["draft"]["category"]["id"], str(self.cat_food))
        self.assertEqual(rev_data["warnings"], [])
        self.assertNotIn("None", rev_data["display_summary"])

        # Explicit confirm boundary
        res_conf = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_conf.status_code, 200)
        self.assertEqual(res_conf.json()["status"], "committed")
        self.assertEqual(res_conf.json()["request_id"], str(req_id))

        # Exactly 1 transaction recorded in DB
        with self.conn.cursor() as cur:
            cur.execute("SELECT original_amount, original_currency, merchant FROM transactions WHERE source_request_id = %s;", (UUID(req_id),))
            tx_row = cur.fetchone()
            self.assertIsNotNone(tx_row)
            self.assertEqual(tx_row[0], Decimal("199.00"))
            self.assertEqual(tx_row[1], "CNY")
            self.assertEqual(tx_row[2], "优衣库")

    def test_52_revise_with_gemini_dependency_failure_rolls_back(self):
        """
        Integration test: If Gemini service fails during revision, endpoint returns 503,
        database transaction rolls back, and existing draft remains completely uncorrupted.
        """
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 20),
            merchant="咖啡厅",
            original_amount=Decimal("35.00"),
            original_currency="CNY",
            from_account=None,
            category=None,
            payment_mode="one_off",
            confidence=0.8
        ))

        res_init = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-52-gemini-fail-rollback",
                "captured_at": "2026-08-20T12:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_init.json()["request_id"]
        original_draft = res_init.json()["draft"]

        # Simulate Gemini dependency outage
        self.mock_gemini.should_raise = GeminiDependencyError("Gemini API 503 Overloaded")

        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"correction_note": "把金额改成40元"}
        )
        self.assertEqual(res_rev.status_code, 503)
        self.assertEqual(res_rev.json()["error"]["code"], "GEMINI_SERVICE_UNAVAILABLE")

        # Reset mock
        self.mock_gemini.should_raise = None

        # Verify draft in DB is uncorrupted and still in needs_confirmation
        row = ingestion_repo.get_ingestion_request(self.conn, UUID(req_id))
        self.assertEqual(row["status"], "needs_confirmation")
        self.assertEqual(row["draft_payload"]["original_amount"], original_draft["original_amount"])
        self.assertEqual(row["draft_payload"]["merchant"], original_draft["merchant"])

    def test_53_natural_language_revision_with_account_alias(self):
        """
        Integration test: Revising draft using an account alias resolves to the canonical account ID.
        '招行信用卡' alias resolves to self.acc_cny_credit.
        """
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 20),
            merchant="餐厅",
            original_amount=Decimal("150.00"),
            original_currency="CNY",
            from_account=None,
            category=None,
            payment_mode="one_off",
            confidence=0.8
        ))

        res_init = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-53-alias-resolution",
                "captured_at": "2026-08-20T12:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_init.json()["request_id"]

        # Gemini returns alias string '招行信用卡'
        self.mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            from_account="招行信用卡",
            category="餐饮美食"
        ))

        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"correction_note": "用招行信用卡付的，选餐饮美食"}
        )
        self.assertEqual(res_rev.status_code, 200)
        draft = res_rev.json()["draft"]
        self.assertIsNotNone(draft["from_account"])
        self.assertEqual(draft["from_account"]["id"], str(self.acc_cny_credit))
        self.assertEqual(draft["from_account"]["name"], "招商银行信用卡")
        self.assertEqual(draft["category"]["id"], str(self.cat_food))
        self.assertEqual(res_rev.json()["warnings"], [])

    def test_54_natural_language_unresolved_account_clears_field_and_warns(self):
        """
        Integration test for clarification 2:
        When an explicit natural-language correction specifies an unknown account,
        from_account MUST be cleared to null and produce an ACCOUNT_UNRESOLVED warning.
        Previous account must NOT be silently retained.
        """
        # Initially resolved with checking card, but category is None -> needs_confirmation
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 20),
            merchant="药房",
            original_amount=Decimal("45.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category=None,
            payment_mode="one_off",
            confidence=0.8,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 0.0, "date": 1.0}
        ))

        res_init = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-54-unresolved-clears-account",
                "captured_at": "2026-08-20T12:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_init.json()["request_id"]
        # In this initial draft, account was resolved
        self.assertIsNotNone(res_init.json()["draft"]["from_account"])

        # Now user explicitly says to change to non-existent account
        self.mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            from_account="火星虚拟银行卡"
        ))

        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"correction_note": "改成火星虚拟银行卡"}
        )
        self.assertEqual(res_rev.status_code, 200)
        rev_data = res_rev.json()
        self.assertIsNone(rev_data["draft"]["from_account"])
        codes = {w["code"] for w in rev_data["warnings"]}
        self.assertIn("ACCOUNT_UNRESOLVED", codes)

    def test_55_revise_canonicalizes_payment_mode_and_confirms_successfully(self):
        """
        Regression test: Draft has payment_mode=' ONE_OFF '.
        Revision canonicalizes payment_mode to 'one_off', warnings=[], and explicit Confirm succeeds.
        """
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 20),
            merchant="超市",
            original_amount=Decimal("66.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category=None,
            payment_mode=" ONE_OFF ",
            confidence=0.8,
            field_confidence={"amount": 1.0, "currency": 1.0, "account": 1.0, "category": 0.0, "date": 1.0}
        ))

        res_init = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": "test-key-55-canonicalize-pm-confirm",
                "captured_at": "2026-08-20T12:00:00+08:00",
                "image": self._sample_jpeg_payload()
            }
        )
        req_id = res_init.json()["request_id"]

        # Revise category
        res_rev = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/revise",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={"category_id": str(self.cat_food)}
        )
        self.assertEqual(res_rev.status_code, 200)
        rev_data = res_rev.json()
        self.assertEqual(rev_data["draft"]["payment_mode"], "one_off")
        self.assertEqual(rev_data["warnings"], [])

        # Confirm must succeed because payment_mode is exact canonical "one_off"
        res_conf = self.client.post(
            f"/api/v1/ingestion-requests/{req_id}/confirm",
            headers={"Authorization": f"Bearer {self.raw_token_1}"}
        )
        self.assertEqual(res_conf.status_code, 200)
        self.assertEqual(res_conf.json()["status"], "committed")
        self.assertEqual(res_conf.json()["payment_mode"], "one_off")
