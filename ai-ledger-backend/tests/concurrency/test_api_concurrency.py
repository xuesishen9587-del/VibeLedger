import unittest
import uuid
from uuid import UUID, uuid4
import hashlib
import base64
import threading
from decimal import Decimal
from datetime import date
from fastapi.testclient import TestClient

from app.db import get_connection
from app.main import create_app
from app.api.deps import get_db_connection
from app.api.routes.expenses import router as expenses_router
from app.api.routes.ingestion import router as ingestion_router
from app.repositories import accounts as accounts_repo
from app.repositories import devices as devices_repo
from app.services.gemini_service import ExpenseExtractionResult, MockGeminiService
from app.services.reference_fx_service import ReferenceFxService
try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

VALID_PNG_BYTES = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
    b'\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82'
)

class TestApiConcurrency(BaseDbTestCase):
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
        self.raw_token_1 = f"vbl_test_{uuid4().hex}"

        self.mock_gemini = MockGeminiService()
        self.mock_fx = ReferenceFxService()

        expenses_router._gemini_service = self.mock_gemini
        expenses_router._reference_fx_service = self.mock_fx
        ingestion_router._gemini_service = self.mock_gemini
        ingestion_router._reference_fx_service = self.mock_fx

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO households (id, name, reporting_currency, ledger_start_date, status, created_at, updated_at)
                VALUES (%s, %s, 'CNY', '2026-01-01', 'active', now(), now());
                """,
                (self.household_id, "Test Household")
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

            t1_hash = hashlib.sha256(self.raw_token_1.encode('utf-8')).digest()
            devices_repo.create_device(
                conn=self.conn,
                device_id=self.device_id_1,
                user_id=self.user_id,
                device_name="iPhone 15 Pro",
                token_hash=t1_hash,
                platform="ios_shortcuts"
            )

            self.acc_cny_checking = uuid4()
            cur.execute(
                """
                INSERT INTO accounts (id, household_id, name, account_type, currency, status, created_at, updated_at)
                VALUES (%s, %s, '招商银行储蓄卡', 'cash', 'CNY', 'active', now(), now());
                """,
                (self.acc_cny_checking, self.household_id)
            )
            cur.execute(
                """
                INSERT INTO account_state (account_id, ledger_balance, row_version, updated_at)
                VALUES (%s, 10000.00, 0, now());
                """,
                (self.acc_cny_checking,)
            )

            self.cat_food = uuid4()
            cur.execute(
                """
                INSERT INTO categories (id, household_id, name, category_type, status, created_at, updated_at)
                VALUES (%s, %s, '餐饮美食', 'expense', 'active', now(), now());
                """,
                (self.cat_food, self.household_id)
            )
        self.conn.commit()

    def _sample_png_payload(self):
        return {
            "mime_type": "image/png",
            "base64": base64.b64encode(VALID_PNG_BYTES).decode('utf-8')
        }

    def test_11_concurrent_identical_requests_produce_single_outcome(self):
        key = f"key-concurrent-{uuid4().hex}"
        payload = {
            "idempotency_key": key,
            "captured_at": "2026-08-20T12:00:00Z",
            "client_version": "1.0.0",
            "image": self._sample_png_payload(),
            "note": "Concurrent Test"
        }

        results = []

        def worker():
            client = TestClient(self.app)
            res = client.post(
                "/api/v1/expenses",
                headers={"Authorization": f"Bearer {self.raw_token_1}"},
                json=payload
            )
            results.append((res.status_code, res.json()))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 5 requests must return HTTP 200 with the exact same request_id
        self.assertEqual(len(results), 5)
        status_codes = [r[0] for r in results]
        self.assertEqual(set(status_codes), {200})

        request_ids = {r[1]["request_id"] for r in results}
        self.assertEqual(len(request_ids), 1, f"Multiple request IDs created: {request_ids}")

        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingestion_requests WHERE idempotency_key = %s;", (key,))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_39_concurrent_confirm_produces_single_commit(self):
        # 1. Create a pending confirmation request
        self.mock_gemini.set_next_result(ExpenseExtractionResult(
            occurred_on=date(2026, 8, 20),
            merchant="商户",
            original_amount=Decimal("100.00"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            confidence=0.70 # forces pending_confirmation
        ))

        res_init = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {self.raw_token_1}"},
            json={
                "idempotency_key": f"key-conf-race-{uuid4().hex}",
                "captured_at": "2026-08-20T12:00:00Z",
                "client_version": "1.0.0",
                "image": self._sample_png_payload()
            }
        )
        req_id = res_init.json()["request_id"]

        # 2. Concurrently call /confirm under /api/v1/ingestion-requests
        results = []

        def worker():
            client = TestClient(self.app)
            res = client.post(
                f"/api/v1/ingestion-requests/{req_id}/confirm",
                headers={"Authorization": f"Bearer {self.raw_token_1}"}
            )
            results.append((res.status_code, res.json()))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All succeed or replay committed outcome with 200
        for code, body in results:
            self.assertEqual(code, 200)
            self.assertEqual(body["status"], "committed")

        # Single transaction committed in ledger
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions WHERE source_request_id = %s;", (UUID(req_id),))
            self.assertEqual(cur.fetchone()[0], 1)

        # Single balance deduction of 100.00
        state = accounts_repo.get_account_state(self.conn, self.acc_cny_checking)
        self.assertEqual(state["ledger_balance"], Decimal("9900.000000")) # 10000 - 100
