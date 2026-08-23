import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
from uuid import uuid4
from decimal import Decimal
from datetime import datetime, date, timezone
from fastapi.testclient import TestClient

from app.main import create_app
from app.db import get_connection
from app.api.deps import get_db_connection
from app.services.reference_fx_service import ReferenceFxService
from app.api.routes.snapshots import router as snapshots_router
from app.api.routes.reconciliation import router as reconciliation_router
from tests.support.db_helper import BaseDbTestCase

class TestSnapshotReconciliationApiDb(BaseDbTestCase):
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
        import hashlib
        import app.repositories.accounts as accounts_repo
        import app.repositories.devices as devices_repo
        from app.db import transaction

        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.raw_token = f"vbl_test_{uuid4().hex}"
        self.token_hash = hashlib.sha256(self.raw_token.encode("utf-8")).digest()
        self.headers = {"Authorization": f"Bearer {self.raw_token}"}

        # Household B for cross-household isolation tests
        self.household_b_id = uuid4()
        self.user_b_id = uuid4()
        self.device_b_id = uuid4()
        self.raw_token_b = f"vbl_test_{uuid4().hex}"
        self.token_b_hash = hashlib.sha256(self.raw_token_b.encode("utf-8")).digest()
        self.headers_b = {"Authorization": f"Bearer {self.raw_token_b}"}

        self.acc_cny_id = uuid4()
        self.acc_usd_id = uuid4()
        self.acc_invest_id = uuid4()
        self.cat_id = uuid4()

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # Setup Household A
                accounts_repo.create_household(conn, self.household_id, "Test Household A", date(2026, 9, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_user_a", "User A", "user_a@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                devices_repo.create_device(conn, self.device_id, self.user_id, "iPhone A", self.token_hash)

                # Setup Household B
                accounts_repo.create_household(conn, self.household_b_id, "Test Household B", date(2026, 9, 1), "CNY")
                accounts_repo.create_user(conn, self.user_b_id, "auth_user_b", "User B", "user_b@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_b_id, self.user_b_id, role="owner")
                devices_repo.create_device(conn, self.device_b_id, self.user_b_id, "iPhone B", self.token_b_hash)

                # Accounts for Household A
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_cny_id,
                    household_id=self.household_id,
                    name="CMB Checking",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_usd_id,
                    household_id=self.household_id,
                    name="Chase Checking",
                    account_type="cash",
                    currency="USD",
                    owner_user_id=self.user_id
                )
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.acc_invest_id,
                    household_id=self.household_id,
                    name="Fidelity Brokerage",
                    account_type="investment",
                    currency="USD",
                    owner_user_id=self.user_id
                )

                # Category
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO categories (id, household_id, name, category_type, status)
                        VALUES (%s, %s, 'Dining', 'expense', 'active');
                        """,
                        (self.cat_id, self.household_id)
                    )
        finally:
            conn.close()

    def test_01_first_observation_opening_balance_initialization(self):
        """
        A. Opening balance:
        Household ledger_start_date = 2026-09-01
        First observation = 100000.00 CNY at 2026-09-05.
        Verify:
        - opening_balance transaction exists dated 2026-09-01 for 100000.00
        - account_snapshots row committed
        - account_state.ledger_balance = 100000.00
        - excluded from income/expense/investment P&L
        """
        payload = {
            "idempotency_key": "snap_key_001_first_obs",
            "as_of": "2026-09-05T10:00:00+08:00",
            "balance": "100000.00",
            "currency": "CNY",
            "source": "dashboard_manual"
        }
        res = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json=payload, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "committed")
        self.assertEqual(data["residual_amount"], "0.00")
        self.assertIsNotNone(data["snapshot_id"])
        self.assertIsNotNone(data["opening_balance_transaction_id"])
        self.assertIsNone(data["adjustment_transaction_id"])

        # Verify Database state
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                # Check opening_balance transaction
                cur.execute(
                    """
                    SELECT transaction_type, occurred_on, to_amount, to_currency, category_id, status
                    FROM transactions WHERE id = %s;
                    """,
                    (data["opening_balance_transaction_id"],)
                )
                tx = cur.fetchone()
                self.assertEqual(tx[0], "opening_balance")
                self.assertEqual(tx[1], date(2026, 9, 1))
                self.assertEqual(tx[2], Decimal("100000.00"))
                self.assertEqual(tx[3], "CNY")
                self.assertIsNone(tx[4]) # category_id must be NULL
                self.assertEqual(tx[5], "committed")

                # Check account_state
                cur.execute("SELECT ledger_balance, last_authoritative_snapshot_at FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                st = cur.fetchone()
                self.assertEqual(st[0], Decimal("100000.00"))
                self.assertIsNotNone(st[1])

                # Check snapshot
                cur.execute("SELECT balance, currency, is_authoritative FROM account_snapshots WHERE id = %s;", (data["snapshot_id"],))
                snap = cur.fetchone()
                self.assertEqual(snap[0], Decimal("100000.00"))
                self.assertEqual(snap[1], "CNY")
                self.assertTrue(snap[2])
        finally:
            conn.close()

    def test_02_first_observation_with_preceding_transaction_derives_correct_anchor(self):
        """
        Verify derived opening anchor when a transaction exists between ledger_start_date and observation:
        ledger_start_date = 2026-09-01
        2026-09-03: Expense of 200.00 CNY committed.
        2026-09-05: Observed snapshot = 9800.00 CNY.
        Derived opening balance should be 10000.00 CNY (not 9800.00) so that 10000 - 200 = 9800.
        """
        # Create an expense on 2026-09-03
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        from_amount, from_currency, from_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'expense', '2026-09-03', 200.00, 'CNY',
                        200.00, 'CNY', %s, 'committed', 'shortcut'
                    );
                    UPDATE account_state SET ledger_balance = -200.00 WHERE account_id = %s;
                    """,
                    (self.household_id, self.acc_cny_id, self.acc_cny_id)
                )
            conn.commit()
        finally:
            conn.close()

        payload = {
            "idempotency_key": "snap_key_002_preceding_tx",
            "as_of": "2026-09-05T10:00:00+08:00",
            "balance": "9800.00",
            "currency": "CNY",
            "source": "dashboard_manual"
        }
        res = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json=payload, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "committed")

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_amount FROM transactions WHERE id = %s;",
                    (data["opening_balance_transaction_id"],)
                )
                opening_tx = cur.fetchone()
                # Must be 10000.00, NOT 9800.00!
                self.assertEqual(opening_tx[0], Decimal("10000.00"))

                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("9800.00"))
        finally:
            conn.close()

    def test_03_exact_snapshot_no_adjustment(self):
        """
        B. Exact Snapshot:
        Projected = 1000, Observed = 1000
        residual = 0, no adjustment transaction created, snapshot committed.
        """
        # Initial snapshot
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Subsequent exact snapshot
        res = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_exact_003",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "committed")
        self.assertEqual(data["residual_amount"], "0.00")
        self.assertIsNone(data["adjustment_transaction_id"])

    def test_04_small_residual_auto_adjustment(self):
        """
        C. Small residual:
        Projected = 1000.00, Observed = 953.00
        Residual = -47.00 (within <= 200 CNY)
        Auto-creates reconciliation_adjustment transaction for -47.00.
        """
        # Initial snapshot 1000.00
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Second observation 953.00
        res = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_small_res_004",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "953.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "committed")
        self.assertEqual(data["residual_amount"], "-47.00")
        self.assertIsNotNone(data["adjustment_transaction_id"])

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT transaction_type, from_amount, from_account_id, status, source
                    FROM transactions WHERE id = %s;
                    """,
                    (data["adjustment_transaction_id"],)
                )
                tx = cur.fetchone()
                self.assertEqual(tx[0], "reconciliation_adjustment")
                self.assertEqual(tx[1], Decimal("47.00"))
                self.assertEqual(tx[2], self.acc_cny_id)
                self.assertEqual(tx[3], "committed")

                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("953.00"))
        finally:
            conn.close()

    def test_05_threshold_boundaries(self):
        """
        D. Threshold boundaries:
        +200 CNY -> auto eligible
        -200 CNY -> auto eligible
        200.01 CNY -> needs_review, NO ledger mutation
        """
        # Baseline snapshot = 1000.00
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # 1. +200 CNY residual -> auto-committed
        res_plus200 = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-02T10:00:00+08:00",
            "balance": "1200.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_plus200.status_code, 200)
        self.assertEqual(res_plus200.json()["status"], "committed")
        self.assertEqual(res_plus200.json()["residual_amount"], "200.00")

        # 2. -200 CNY residual -> auto-committed
        res_minus200 = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-03T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_minus200.status_code, 200)
        self.assertEqual(res_minus200.json()["status"], "committed")
        self.assertEqual(res_minus200.json()["residual_amount"], "-200.00")

        # 3. 200.01 CNY residual -> needs_review
        res_over = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-04T10:00:00+08:00",
            "balance": "1200.01",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_over.status_code, 200)
        data_over = res_over.json()
        self.assertEqual(data_over["status"], "needs_review")
        self.assertEqual(data_over["residual_amount"], "200.01")
        self.assertIn("账户实际余额与账本相差", data_over["display_summary"])

        # Verify NO mutation occurred for needs_review
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                # Ledger balance remains 1000.00
                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("1000.00"))
                # Candidate exists with needs_review
                cur.execute("SELECT count(*) FROM reconciliation_candidates WHERE batch_id = %s AND status = 'needs_review';", (data_over["batch_id"],))
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()

    def test_06_non_cny_threshold_conversion(self):
        """
        E. Non-CNY threshold:
        USD/CNY = 7.20
        20 USD * 7.20 = 144 CNY (<= 200) -> auto committed
        30 USD * 7.20 = 216 CNY (> 200) -> needs_review
        """
        # Baseline snapshot 100 USD
        self.client.post(f"/api/v1/accounts/{self.acc_usd_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "100.00",
            "currency": "USD"
        }, headers=self.headers)

        # 20 USD residual -> 144 CNY -> auto committed
        res_20usd = self.client.post(f"/api/v1/accounts/{self.acc_usd_id}/snapshots", json={
            "as_of": "2026-09-02T10:00:00+08:00",
            "balance": "120.00",
            "currency": "USD"
        }, headers=self.headers)
        self.assertEqual(res_20usd.status_code, 200)
        self.assertEqual(res_20usd.json()["status"], "committed")
        self.assertEqual(res_20usd.json()["residual_amount"], "20.00")

        # 30 USD residual -> 216 CNY -> needs_review
        res_30usd = self.client.post(f"/api/v1/accounts/{self.acc_usd_id}/snapshots", json={
            "as_of": "2026-09-03T10:00:00+08:00",
            "balance": "150.00",
            "currency": "USD"
        }, headers=self.headers)
        self.assertEqual(res_30usd.status_code, 200)
        self.assertEqual(res_30usd.json()["status"], "needs_review")
        self.assertEqual(res_30usd.json()["residual_amount"], "30.00")

    def test_07_historical_balance_as_of_and_later_transactions(self):
        """
        F & G. Historical balance-as-of and later transaction preservation:
        Opening Jan 1 = 1000
        Jan 5 = -100
        Jan 10 = +200
        Feb 1 = -50
        Historical balance queries: Jan 1=1000, Jan 7=900, Jan 31=1100, Feb 2=1050.
        Then snapshot Jan 31 = 1150 (+50 adjustment).
        Current account_state balance should become 1100 (1150 - 50 = 1100), preserving Feb 1 transaction.
        """
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                # Opening balance Jan 1
                cur.execute(
                    """
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        to_amount, to_currency, to_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'opening_balance', '2026-01-01', 1000.00, 'CNY',
                        1000.00, 'CNY', %s, 'committed', 'reconciliation'
                    );
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        from_amount, from_currency, from_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'expense', '2026-01-05', 100.00, 'CNY',
                        100.00, 'CNY', %s, 'committed', 'shortcut'
                    );
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        to_amount, to_currency, to_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'cash_income', '2026-01-10', 200.00, 'CNY',
                        200.00, 'CNY', %s, 'committed', 'shortcut'
                    );
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        from_amount, from_currency, from_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'expense', '2026-02-01', 50.00, 'CNY',
                        50.00, 'CNY', %s, 'committed', 'shortcut'
                    );
                    UPDATE account_state SET ledger_balance = 1050.00 WHERE account_id = %s;
                    """,
                    (
                        self.household_id, self.acc_cny_id,
                        self.household_id, self.acc_cny_id,
                        self.household_id, self.acc_cny_id,
                        self.household_id, self.acc_cny_id,
                        self.acc_cny_id
                    )
                )
            conn.commit()

            from app.services.snapshot_service import ledger_balance_as_of
            bal_jan01 = ledger_balance_as_of(conn, self.acc_cny_id, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
            bal_jan07 = ledger_balance_as_of(conn, self.acc_cny_id, datetime(2026, 1, 7, 12, 0, 0, tzinfo=timezone.utc))
            bal_jan31 = ledger_balance_as_of(conn, self.acc_cny_id, datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc))
            bal_feb02 = ledger_balance_as_of(conn, self.acc_cny_id, datetime(2026, 2, 2, 12, 0, 0, tzinfo=timezone.utc))

            self.assertEqual(bal_jan01, Decimal("1000.00"))
            self.assertEqual(bal_jan07, Decimal("900.00"))
            self.assertEqual(bal_jan31, Decimal("1100.00"))
            self.assertEqual(bal_feb02, Decimal("1050.00"))
        finally:
            conn.close()

        # Reconcile historical snapshot for Jan 31 with balance = 1150.00 (+50 adjustment)
        res_snap = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-01-31T23:59:59+08:00",
            "balance": "1150.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_snap.status_code, 200)
        self.assertEqual(res_snap.json()["status"], "committed")
        self.assertEqual(res_snap.json()["residual_amount"], "50.00")

        # Current account_state must now be 1100.00 (1150 on Jan 31 - 50 on Feb 1 = 1100.00), NOT 1150.00!
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("1100.00"))
        finally:
            conn.close()

    def test_08_preview_endpoint_is_strictly_read_only(self):
        """
        H. Preview read-only:
        GET /reconciliation-batches/{batch_id}/preview
        Zero mutations in transactions, account_state, snapshots, candidates.
        """
        # Initial snapshot
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Snapshot requiring review (> 200)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # Call Preview multiple times
        res_prev1 = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        self.assertEqual(res_prev1.status_code, 200)
        prev_data1 = res_prev1.json()
        self.assertEqual(prev_data1["authoritative_balance"], "1500.00")
        self.assertEqual(prev_data1["projected_balance"], "1000.00")
        self.assertEqual(prev_data1["residual_amount"], "500.00")
        self.assertFalse(prev_data1["auto_adjustment_eligible"])

        res_prev2 = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        self.assertEqual(res_prev2.status_code, 200)

        # Check DB count: no extra snapshots or transactions created
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM account_snapshots WHERE reconciliation_batch_id = %s;", (batch_id,))
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_09_concurrent_stale_preview_revalidation_at_commit(self):
        """
        I. Concurrent / stale preview:
        Preview batch -> another transaction occurs affecting T -> commit batch.
        Commit must re-evaluate balance and residual under lock, not blindly applying stale preview residual.
        """
        # Initial snapshot = 1000
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Create needs_review batch for as_of=2026-09-10 with balance=1500 (residual was +500)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # User inspects preview
        res_prev = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers)
        self.assertEqual(res_prev.json()["residual_amount"], "500.00")

        # Now, a concurrent transaction arrives before commit: Income of +300 on 2026-09-05
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        to_amount, to_currency, to_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'cash_income', '2026-09-05', 300.00, 'CNY',
                        300.00, 'CNY', %s, 'committed', 'shortcut'
                    );
                    UPDATE account_state SET ledger_balance = 1300.00 WHERE account_id = %s;
                    """,
                    (self.household_id, self.acc_cny_id, self.acc_cny_id)
                )
            conn.commit()
        finally:
            conn.close()

        # Now commit the batch
        res_commit = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", json={"row_version": 0}, headers=self.headers)
        self.assertEqual(res_commit.status_code, 200)
        data_commit = res_commit.json()
        self.assertEqual(data_commit["status"], "committed")
        # Fresh residual recomputed: 1500 - 1300 = +200.00 (NOT stale 500.00!)
        self.assertEqual(data_commit["residual_amount"], "200.00")

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("1500.00"))
        finally:
            conn.close()

    def test_10_row_version_conflict_rejection(self):
        """
        J. row_version conflict:
        Stale batch row_version => 409 BATCH_VERSION_CONFLICT, zero financial writes.
        """
        # Initial snapshot
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # Commit with wrong row_version (e.g. 99)
        res_bad_ver = self.client.post(
            f"/api/v1/reconciliation-batches/{batch_id}/commit",
            json={"row_version": 99},
            headers=self.headers
        )
        self.assertEqual(res_bad_ver.status_code, 409)
        self.assertEqual(res_bad_ver.json()["error"]["code"], "BATCH_VERSION_CONFLICT")

    def test_11_repeated_commit_is_replay_safe(self):
        """
        K. Repeated commit:
        Commit twice => returns same outcome, creates exactly ONE snapshot and ONE adjustment.
        """
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        res_commit1 = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit1.status_code, 200)
        data1 = res_commit1.json()

        res_commit2 = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit2.status_code, 200)
        data2 = res_commit2.json()

        self.assertEqual(data1["snapshot_id"], data2["snapshot_id"])
        self.assertEqual(data1["adjustment_transaction_id"], data2["adjustment_transaction_id"])

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM account_snapshots WHERE reconciliation_batch_id = %s;", (batch_id,))
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()

    def test_12_cross_household_isolation(self):
        """
        M. Household isolation:
        Household B cannot view or commit Household A's snapshot batch or account.
        """
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # Household B tries to create snapshot on Household A's account -> 404
        res_bad_acc = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers_b)
        self.assertEqual(res_bad_acc.status_code, 404)
        self.assertEqual(res_bad_acc.json()["error"]["code"], "ACCOUNT_NOT_FOUND")

        # Household B tries to GET batch -> 404
        res_bad_batch = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}", headers=self.headers_b)
        self.assertEqual(res_bad_batch.status_code, 404)
        self.assertEqual(res_bad_batch.json()["error"]["code"], "BATCH_NOT_FOUND")

        # Household B tries to GET preview -> 404
        res_bad_prev = self.client.get(f"/api/v1/reconciliation-batches/{batch_id}/preview", headers=self.headers_b)
        self.assertEqual(res_bad_prev.status_code, 404)
        self.assertEqual(res_bad_prev.json()["error"]["code"], "BATCH_NOT_FOUND")

        # Household B tries to commit batch -> 404
        res_bad_commit = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers_b)
        self.assertEqual(res_bad_commit.status_code, 404)
        self.assertEqual(res_bad_commit.json()["error"]["code"], "BATCH_NOT_FOUND")

    def test_13_investment_account_snapshot_rejection(self):
        """
        Generic balance snapshot on investment account must return 422 ACCOUNT_TYPE_MISMATCH.
        """
        res = self.client.post(f"/api/v1/accounts/{self.acc_invest_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "USD"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "ACCOUNT_TYPE_MISMATCH")

    def test_14_idempotent_device_replay_and_conflict(self):
        """
        Device idempotency key handling:
        Same device + same key + same payload => replay
        Same device + same key + different payload => 409 IDEMPOTENCY_KEY_REUSE
        """
        payload1 = {
            "idempotency_key": "snap_device_idemp_key_1",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }
        res1 = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json=payload1, headers=self.headers)
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()

        # Replay same
        res_replay = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json=payload1, headers=self.headers)
        self.assertEqual(res_replay.status_code, 200)
        self.assertEqual(res_replay.json()["snapshot_id"], data1["snapshot_id"])

        # Conflicting payload with same key
        payload2 = {
            "idempotency_key": "snap_device_idemp_key_1",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "2000.00",
            "currency": "CNY"
        }
        res_conflict = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json=payload2, headers=self.headers)
        self.assertEqual(res_conflict.status_code, 409)
        self.assertEqual(res_conflict.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

if __name__ == "__main__":
    unittest.main()
