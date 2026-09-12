import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
import threading
from uuid import uuid4
import hashlib
from decimal import Decimal
from datetime import date
from fastapi.testclient import TestClient

from app.db import get_connection, transaction
from app.main import create_app
from app.api.deps import get_db_connection
from app.repositories import accounts as accounts_repo
from app.repositories import devices as devices_repo
from app.api.routes.snapshots import router as snapshots_router
from app.api.routes.reconciliation import router as reconciliation_router
from app.services.reference_fx_service import ReferenceFxService

try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

class TestReconciliationConcurrency(BaseDbTestCase):
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

        cls.mock_fx = ReferenceFxService(fixed_rates={
            ("USD", "CNY"): Decimal("7.20"),
            ("EUR", "CNY"): Decimal("7.80")
        })
        snapshots_router._reference_fx_service = cls.mock_fx
        reconciliation_router._reference_fx_service = cls.mock_fx

    def seed_test_data(self):
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.raw_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_token.encode("utf-8")).digest()
        self.headers = {"Authorization": f"Bearer {self.raw_token}"}

        self.acc_id = uuid4()

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_household(conn, self.household_id, "Test Household", date(2026, 9, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_user_conc", "User Conc", "user_conc@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "iPhone Conc", self.token_hash)

                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_id,
                    household_id=self.household_id,
                    name="Checking Conc",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
        finally:
            conn.close()

    def test_concurrent_batch_commits_produce_single_snapshot_and_adjustment(self):
        """
        Multiple concurrent threads commit the same reconciliation batch.
        Guarantees:
        - Exactly 1 account_snapshot row is created for the batch.
        - At most 1 adjustment transaction is created.
        - All threads receive 200 OK (or 409 conflict if row_version mismatch).
        - Final ledger balance is consistent.
        """
        # 1. Establish opening baseline: 1000.00
        self.client.post(f"/api/v1/accounts/{self.acc_id}/snapshots", json={
            "idempotency_key": "snap_conc_base_key_1",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # 2. Create a needs_review batch (balance = 1500.00)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_id}/snapshots", json={
            "idempotency_key": "snap_conc_sub_key_2",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_sub.status_code, 200)
        batch_id = res_sub.json()["batch_id"]

        results = []
        threads = []
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            # Each thread uses a fresh client or shared client
            res = self.client.post(
                f"/api/v1/reconciliation-batches/{batch_id}/commit",
                json={"row_version": 0},
                headers=self.headers
            )
            results.append(res)

        for _ in range(5):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Check status codes: all should be either 200 OK (committed/replayed) or 409 conflict
        status_codes = [r.status_code for r in results]
        self.assertTrue(all(code in (200, 409) for code in status_codes))
        self.assertTrue(any(code == 200 for code in status_codes))

        # Check DB invariants
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                # Exactly 1 snapshot for this batch
                cur.execute("SELECT count(*) FROM account_snapshots WHERE reconciliation_batch_id = %s;", (batch_id,))
                self.assertEqual(cur.fetchone()[0], 1)

                # Exactly 1 adjustment transaction
                cur.execute(
                    """
                    SELECT count(*) FROM transactions
                    WHERE household_id = %s AND transaction_type = 'reconciliation_adjustment';
                    """,
                    (self.household_id,)
                )
                self.assertEqual(cur.fetchone()[0], 1)

                # Account state balance is 1500.00
                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("1500.00"))
        finally:
            conn.close()

if __name__ == "__main__":
    unittest.main()
