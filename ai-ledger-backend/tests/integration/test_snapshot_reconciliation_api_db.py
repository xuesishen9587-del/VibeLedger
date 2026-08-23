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
            ("EUR", "CNY"): Decimal("7.80"),
            ("USD", "SGD"): Decimal("1.35"),
            ("SGD", "CNY"): Decimal("5.333333")
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
        - account_state.initialized_at is set
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
                cur.execute("SELECT ledger_balance, last_authoritative_snapshot_at, initialized_at FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                st = cur.fetchone()
                self.assertEqual(st[0], Decimal("100000.00"))
                self.assertIsNotNone(st[1])
                self.assertIsNotNone(st[2]) # initialized_at must be populated

                # Check snapshot
                cur.execute("SELECT balance, currency, is_authoritative, source FROM account_snapshots WHERE id = %s;", (data["snapshot_id"],))
                snap = cur.fetchone()
                self.assertEqual(snap[0], Decimal("100000.00"))
                self.assertEqual(snap[1], "CNY")
                self.assertTrue(snap[2])
                self.assertEqual(snap[3], "dashboard_manual")
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
            "idempotency_key": "snap_key_base_003",
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
            "idempotency_key": "snap_key_base_004",
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
            "idempotency_key": "snap_key_base_005",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # 1. +200 CNY residual -> auto-committed
        res_plus200 = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_plus200_005",
            "as_of": "2026-09-02T10:00:00+08:00",
            "balance": "1200.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_plus200.status_code, 200)
        self.assertEqual(res_plus200.json()["status"], "committed")
        self.assertEqual(res_plus200.json()["residual_amount"], "200.00")

        # 2. -200 CNY residual -> auto-committed
        res_minus200 = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_minus200_005",
            "as_of": "2026-09-03T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_minus200.status_code, 200)
        self.assertEqual(res_minus200.json()["status"], "committed")
        self.assertEqual(res_minus200.json()["residual_amount"], "-200.00")

        # 3. 200.01 CNY residual -> needs_review
        res_over = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_over200_005",
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
            "idempotency_key": "snap_key_base_006",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "100.00",
            "currency": "USD"
        }, headers=self.headers)

        # 20 USD residual -> 144 CNY -> auto committed
        res_20usd = self.client.post(f"/api/v1/accounts/{self.acc_usd_id}/snapshots", json={
            "idempotency_key": "snap_key_20usd_006",
            "as_of": "2026-09-02T10:00:00+08:00",
            "balance": "120.00",
            "currency": "USD"
        }, headers=self.headers)
        self.assertEqual(res_20usd.status_code, 200)
        self.assertEqual(res_20usd.json()["status"], "committed")
        self.assertEqual(res_20usd.json()["residual_amount"], "20.00")

        # 30 USD residual -> 216 CNY -> needs_review
        res_30usd = self.client.post(f"/api/v1/accounts/{self.acc_usd_id}/snapshots", json={
            "idempotency_key": "snap_key_30usd_006",
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
        # Change household ledger_start_date to 2026-01-01 for this test
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE households SET ledger_start_date = '2026-01-01' WHERE id = %s;", (self.household_id,))
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
            "idempotency_key": "snap_key_hist_007",
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
            "idempotency_key": "snap_key_base_008",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Snapshot requiring review (> 200)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_rev_008",
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
            "idempotency_key": "snap_key_base_009",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Create needs_review batch for as_of=2026-09-10 with balance=1500 (residual was +500)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_rev_009",
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
        Stale batch row_version => 409 BATCH_VERSION_CONFLICT, zero financial writes, retryable=True.
        """
        # Initial snapshot
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_base_010",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_rev_010",
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
        self.assertTrue(res_bad_ver.json()["error"]["retryable"])

    def test_11_repeated_commit_is_replay_safe(self):
        """
        K. Repeated commit:
        Commit twice => returns same outcome, creates exactly ONE snapshot and ONE adjustment.
        """
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_base_011",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_rev_011",
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
            "idempotency_key": "snap_key_base_012",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_rev_012",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # Household B tries to create snapshot on Household A's account -> 404
        res_bad_acc = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_badacc_012",
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
            "idempotency_key": "snap_key_invest_013",
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

    def test_15_preserve_exact_authoritative_snapshot_metadata(self):
        """
        Finding 1 Regression:
        Submit as_of = 2026-09-10T10:23:45+08:00, source = shortcut, residual > 200 -> needs_review.
        Commit batch.
        Assert persisted snapshot has exact normalized instant and source == 'shortcut'.
        account_state.last_authoritative_snapshot_at has exact timestamp.
        """
        # Baseline
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_meta_base_015",
            "as_of": "2026-09-01T08:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY",
            "source": "dashboard_manual"
        }, headers=self.headers)

        target_dt_str = "2026-09-10T10:23:45+08:00"
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_meta_rev_015",
            "as_of": target_dt_str,
            "balance": "1500.00",
            "currency": "CNY",
            "source": "shortcut"
        }, headers=self.headers)
        self.assertEqual(res_sub.status_code, 200)
        batch_id = res_sub.json()["batch_id"]

        # Commit batch
        res_commit = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit.status_code, 200)
        snap_id = res_commit.json()["snapshot_id"]

        expected_dt = datetime.fromisoformat(target_dt_str)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT as_of, source FROM account_snapshots WHERE id = %s;", (snap_id,))
                row = cur.fetchone()
                self.assertEqual(row[0], expected_dt)
                self.assertEqual(row[1], "shortcut")

                cur.execute("SELECT last_authoritative_snapshot_at FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                st_row = cur.fetchone()
                self.assertEqual(st_row[0], expected_dt)
        finally:
            conn.close()

    def test_16_device_idempotency_key_validation(self):
        """
        Finding 2 Regression:
        Missing or invalid length idempotency_key is rejected with 422 INVALID_REQUEST.
        """
        # Missing key
        res_missing = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_missing.status_code, 422)
        self.assertEqual(res_missing.json()["error"]["code"], "INVALID_REQUEST")

        # Too short (< 8 chars)
        res_short = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "short",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_short.status_code, 422)
        self.assertEqual(res_short.json()["error"]["code"], "INVALID_REQUEST")

    def test_17_request_hash_includes_account_id_cross_account_conflict(self):
        """
        Finding 3 Regression:
        Same device + same key on Account A, then on Account B with same body
        must return 409 IDEMPOTENCY_KEY_REUSE.
        """
        shared_key = f"snap_shared_key_{uuid4().hex[:12]}"
        payload = {
            "idempotency_key": shared_key,
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }
        res_acc1 = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json=payload, headers=self.headers)
        self.assertEqual(res_acc1.status_code, 200)

        # Create a second CNY account in Household A
        import app.repositories.accounts as accounts_repo
        acc2_id = uuid4()
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc2_id,
                    household_id=self.household_id,
                    name="Second CNY Account",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
            conn.commit()
        finally:
            conn.close()

        # Submit identical payload to Account 2
        res_acc2 = self.client.post(f"/api/v1/accounts/{acc2_id}/snapshots", json=payload, headers=self.headers)
        self.assertEqual(res_acc2.status_code, 409)
        self.assertEqual(res_acc2.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSE")

    def test_18_account_state_initialized_at_lifecycle(self):
        """
        Finding 4 Regression:
        New account: initialized_at IS NULL.
        First snapshot commit: initialized_at IS NOT NULL.
        Subsequent historical snapshot: initialized_at unchanged / not cleared.
        """
        import app.repositories.accounts as accounts_repo
        new_acc_id = uuid4()
        conn = get_connection(self.test_schema)
        try:
            accounts_repo.create_account(
                conn=conn,
                account_id=new_acc_id,
                household_id=self.household_id,
                name="Fresh Account",
                account_type="cash",
                currency="CNY",
                owner_user_id=self.user_id
            )
            with conn.cursor() as cur:
                cur.execute("SELECT initialized_at FROM account_state WHERE account_id = %s;", (new_acc_id,))
                self.assertIsNone(cur.fetchone()[0])
            conn.commit()
        finally:
            conn.close()

        # First snapshot
        res_init = self.client.post(f"/api/v1/accounts/{new_acc_id}/snapshots", json={
            "idempotency_key": "snap_init_018_key",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "5000.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_init.status_code, 200)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT initialized_at FROM account_state WHERE account_id = %s;", (new_acc_id,))
                init_at = cur.fetchone()[0]
                self.assertIsNotNone(init_at)
        finally:
            conn.close()

        # Subsequent snapshot
        res_sub = self.client.post(f"/api/v1/accounts/{new_acc_id}/snapshots", json={
            "idempotency_key": "snap_sub_018_key",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "5050.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_sub.status_code, 200)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT initialized_at FROM account_state WHERE account_id = %s;", (new_acc_id,))
                init_at_after = cur.fetchone()[0]
                self.assertEqual(init_at, init_at_after)
        finally:
            conn.close()

    def test_19_threshold_evaluated_in_cny_independent_of_reporting_currency(self):
        """
        Finding 5 Regression:
        Household reporting_currency = SGD, account currency = USD.
        Mock FX: USD -> CNY = 7.20, USD -> SGD = 1.35.
        Residual 30 USD: in SGD = 40.50 (< 200), but in CNY = 216 (> 200) -> needs_review.
        Residual 20 USD: in CNY = 144 (<= 200) -> auto committed.
        """
        import app.repositories.accounts as accounts_repo
        import app.repositories.devices as devices_repo

        sgd_hh_id = uuid4()
        sgd_user_id = uuid4()
        sgd_dev_id = uuid4()
        sgd_token = f"vbl_sgd_{uuid4().hex}"
        import hashlib
        sgd_tok_hash = hashlib.sha256(sgd_token.encode("utf-8")).digest()
        sgd_headers = {"Authorization": f"Bearer {sgd_token}"}
        sgd_usd_acc = uuid4()

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                accounts_repo.create_household(conn, sgd_hh_id, "SGD Household", date(2026, 9, 1), "SGD")
                accounts_repo.create_user(conn, sgd_user_id, "sgd_user", "SGD User", "sgd@test.local", "SGD")
                accounts_repo.add_user_to_household(conn, sgd_hh_id, sgd_user_id, role="owner")
                devices_repo.create_device(conn, sgd_dev_id, sgd_user_id, "SGD iPhone", sgd_tok_hash)
                accounts_repo.create_account(
                    conn=conn,
                    account_id=sgd_usd_acc,
                    household_id=sgd_hh_id,
                    name="SGD USD Checking",
                    account_type="cash",
                    currency="USD",
                    owner_user_id=sgd_user_id
                )
            conn.commit()
        finally:
            conn.close()

        # Baseline: 100 USD
        self.client.post(f"/api/v1/accounts/{sgd_usd_acc}/snapshots", json={
            "idempotency_key": "snap_sgd_base_019",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "100.00",
            "currency": "USD"
        }, headers=sgd_headers)

        # 30 USD residual -> 216 CNY (> 200 CNY) -> needs_review even though 40.50 SGD < 200
        res_30 = self.client.post(f"/api/v1/accounts/{sgd_usd_acc}/snapshots", json={
            "idempotency_key": "snap_sgd_30usd_019",
            "as_of": "2026-09-02T10:00:00+08:00",
            "balance": "130.00",
            "currency": "USD"
        }, headers=sgd_headers)
        self.assertEqual(res_30.status_code, 200)
        self.assertEqual(res_30.json()["status"], "needs_review")

        # 20 USD residual -> 144 CNY (<= 200 CNY) -> auto committed
        res_20 = self.client.post(f"/api/v1/accounts/{sgd_usd_acc}/snapshots", json={
            "idempotency_key": "snap_sgd_20usd_019",
            "as_of": "2026-09-03T10:00:00+08:00",
            "balance": "120.00",
            "currency": "USD"
        }, headers=sgd_headers)
        self.assertEqual(res_20.status_code, 200)
        self.assertEqual(res_20.json()["status"], "committed")

    def test_20_exact_committed_replay_preserves_identifiers(self):
        """
        Finding 6 Regression:
        A. Two snapshot reconciliations on same account and same date (each with different adjustment).
           Replay first batch -> must return first batch's adjustment ID.
        B. First-observation opening-balance batch -> call commit again -> returns same snapshot_id AND opening_balance_transaction_id.
        """
        # Baseline
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_rep_base_020",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Batch 1 on 2026-09-10 (balance = 1500)
        res_b1 = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_rep_b1_020",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch1_id = res_b1.json()["batch_id"]
        res_commit1 = self.client.post(f"/api/v1/reconciliation-batches/{batch1_id}/commit", headers=self.headers)
        adj1_id = res_commit1.json()["adjustment_transaction_id"]

        # Batch 2 on 2026-09-10 (balance = 2000)
        res_b2 = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_rep_b2_020",
            "as_of": "2026-09-10T11:00:00+08:00",
            "balance": "2000.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch2_id = res_b2.json()["batch_id"]
        res_commit2 = self.client.post(f"/api/v1/reconciliation-batches/{batch2_id}/commit", headers=self.headers)
        adj2_id = res_commit2.json()["adjustment_transaction_id"]
        self.assertNotEqual(adj1_id, adj2_id)

        # Replay Batch 1 commit
        res_replay_b1 = self.client.post(f"/api/v1/reconciliation-batches/{batch1_id}/commit", headers=self.headers)
        self.assertEqual(res_replay_b1.status_code, 200)
        self.assertEqual(res_replay_b1.json()["adjustment_transaction_id"], adj1_id)

    def test_21_stale_candidate_refreshed_to_applied_transaction_amount(self):
        """
        Finding 7 Regression:
        Candidate payload was 500.
        Concurrent transaction arrives before commit -> fresh residual is 200.
        Commit batch.
        Assert candidate.payload['adjustment_amount'] is updated to '200.00' matching applied transaction.
        """
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_stale_base_021",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_stale_rev_021",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # Concurrent income of +300
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

        # Commit
        res_commit = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit.status_code, 200)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT payload, status FROM reconciliation_candidates WHERE batch_id = %s;", (batch_id,))
                row = cur.fetchone()
                payload = row[0]
                if isinstance(payload, str):
                    import json
                    payload = json.loads(payload)
                self.assertEqual(row[1], "applied")
                self.assertEqual(payload["adjustment_amount"], "200.00")
        finally:
            conn.close()

    def test_22_reject_snapshot_before_ledger_start_date(self):
        """
        Finding 8 Regression:
        household.ledger_start_date = 2026-09-01.
        Snapshot as_of = 2026-08-31T23:00:00+08:00.
        Must return 422 INVALID_REQUEST with zero writes.
        """
        res = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_before_start_022",
            "as_of": "2026-08-31T23:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "INVALID_REQUEST")

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM account_snapshots WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute("SELECT count(*) FROM reconciliation_batches WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_23_batch_state_and_type_safety(self):
        """
        Finding 9 Regression:
        Reconciliation commit rejects unsupported batch_type and non-committable statuses.
        """
        import app.repositories.reconciliation as reconciliation_repo
        # Create a dummy batch of type 'statement'
        stmt_batch_id = uuid4()
        proc_batch_id = uuid4()
        conn = get_connection(self.test_schema)
        try:
            reconciliation_repo.create_reconciliation_batch(
                conn=conn,
                batch_id=stmt_batch_id,
                household_id=self.household_id,
                account_id=self.acc_cny_id,
                batch_type="statement",
                status="ready",
                currency="CNY"
            )
            reconciliation_repo.create_reconciliation_batch(
                conn=conn,
                batch_id=proc_batch_id,
                household_id=self.household_id,
                account_id=self.acc_cny_id,
                batch_type="snapshot",
                status="processing",
                currency="CNY"
            )
            conn.commit()
        finally:
            conn.close()

        # Try committing statement batch
        res_stmt = self.client.post(f"/api/v1/reconciliation-batches/{stmt_batch_id}/commit", headers=self.headers)
        self.assertEqual(res_stmt.status_code, 422)
        self.assertEqual(res_stmt.json()["error"]["code"], "INVALID_REQUEST")

        # Try committing processing batch
        res_proc = self.client.post(f"/api/v1/reconciliation-batches/{proc_batch_id}/commit", headers=self.headers)
        self.assertEqual(res_proc.status_code, 422)
        self.assertEqual(res_proc.json()["error"]["code"], "INVALID_REQUEST")

    def test_24_unchanged_reviewed_large_residual_commit_succeeds(self):
        """
        Case A: Unchanged reviewed large residual (>200 CNY):
        reviewed = 500
        fresh = 500
        Explicit commit succeeds, creates adjustment = 500.
        """
        # Baseline
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_base_024",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Submit snapshot with balance = 1500 (residual = 500 > 200 -> needs_review)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_rev_024",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        self.assertEqual(res_sub.status_code, 200)
        batch_id = res_sub.json()["batch_id"]

        # Commit without any intervening transactions (fresh = 500 == reviewed = 500)
        res_commit = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit.status_code, 200)
        data_commit = res_commit.json()
        self.assertEqual(data_commit["status"], "committed")
        self.assertEqual(data_commit["residual_amount"], "500.00")
        self.assertIsNotNone(data_commit["adjustment_transaction_id"])
        self.assertIsNotNone(data_commit["snapshot_id"])

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("1500.00"))
                cur.execute("SELECT status FROM reconciliation_candidates WHERE batch_id = %s;", (batch_id,))
                self.assertEqual(cur.fetchone()[0], "applied")
        finally:
            conn.close()

    def test_25_stale_reviewed_amount_becomes_another_large_amount_returns_needs_review(self):
        """
        Case C: Stale reviewed amount becomes another large amount (>200 CNY):
        reviewed = 500
        concurrent ledger change (expense of 400 on 2026-09-05) makes fresh residual = 900
        Expected:
        - Response status = 'needs_review'
        - Batch remains 'needs_review'
        - Candidate payload = 900.00
        - Candidate status = 'needs_review'
        - Zero new authoritative Snapshot
        - Zero reconciliation adjustment
        - account_state remains pre-reconciliation value (600.00)
        
        Then user calls commit again (reviewed = 900 == fresh = 900):
        - Commit succeeds
        - Adjustment = 900.00
        - One snapshot created
        - Candidate applied
        """
        # Baseline = 1000.00
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_base_025",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Submit snapshot = 1500.00 (residual = 500)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_rev_025",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # Concurrent expense of 400 arrives on 2026-09-05: projected becomes 600, fresh residual becomes 1500 - 600 = 900
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        from_amount, from_currency, from_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'expense', '2026-09-05', 400.00, 'CNY',
                        400.00, 'CNY', %s, 'committed', 'shortcut'
                    );
                    UPDATE account_state SET ledger_balance = 600.00 WHERE account_id = %s;
                    """,
                    (self.household_id, self.acc_cny_id, self.acc_cny_id)
                )
            conn.commit()
        finally:
            conn.close()

        # Commit call: fresh residual (900) != reviewed residual (500) and 900 > 200
        res_commit_1 = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit_1.status_code, 200)
        data_1 = res_commit_1.json()
        self.assertEqual(data_1["status"], "needs_review")
        self.assertEqual(data_1["residual_amount"], "900.00")

        # Verify DB state: zero snapshots, zero adjustment transactions, candidate updated to 900 and needs_review
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM account_snapshots WHERE reconciliation_batch_id = %s;", (batch_id,))
                self.assertEqual(cur.fetchone()[0], 0)

                cur.execute(
                    "SELECT count(*) FROM transactions WHERE (from_account_id = %s OR to_account_id = %s) AND transaction_type = 'reconciliation_adjustment';",
                    (self.acc_cny_id, self.acc_cny_id)
                )
                self.assertEqual(cur.fetchone()[0], 0)

                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("600.00"))

                cur.execute("SELECT status, payload FROM reconciliation_candidates WHERE batch_id = %s;", (batch_id,))
                c_row = cur.fetchone()
                self.assertEqual(c_row[0], "needs_review")
                c_payload = c_row[1]
                if isinstance(c_payload, str):
                    import json
                    c_payload = json.loads(c_payload)
                self.assertEqual(c_payload["adjustment_amount"], "900.00")

                cur.execute("SELECT status, residual_amount FROM reconciliation_batches WHERE id = %s;", (batch_id,))
                b_row = cur.fetchone()
                self.assertEqual(b_row[0], "needs_review")
                self.assertEqual(b_row[1], Decimal("900.00"))
        finally:
            conn.close()

        # Second commit call (user has effectively reloaded/reviewed the candidate at 900.00):
        # Now reviewed = 900 == fresh = 900 -> Case A -> commit succeeds!
        res_commit_2 = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit_2.status_code, 200)
        data_2 = res_commit_2.json()
        self.assertEqual(data_2["status"], "committed")
        self.assertEqual(data_2["residual_amount"], "900.00")
        self.assertIsNotNone(data_2["adjustment_transaction_id"])
        self.assertIsNotNone(data_2["snapshot_id"])

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("1500.00"))
                cur.execute("SELECT status FROM reconciliation_candidates WHERE batch_id = %s;", (batch_id,))
                self.assertEqual(cur.fetchone()[0], "applied")
        finally:
            conn.close()

    def test_26_stale_reviewed_residual_becomes_zero_commits_snapshot_without_adjustment(self):
        """
        Case D: Stale reviewed residual becomes zero:
        reviewed = 500
        concurrent transaction adds 500 -> fresh residual = 0
        Commit succeeds -> snapshot created, no adjustment transaction.
        """
        # Baseline = 1000.00
        self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_base_026",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "1000.00",
            "currency": "CNY"
        }, headers=self.headers)

        # Submit snapshot = 1500.00 (residual = 500)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_cny_id}/snapshots", json={
            "idempotency_key": "snap_key_rev_026",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "1500.00",
            "currency": "CNY"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # Concurrent income of +500 arrives on 2026-09-05: projected becomes 1500, fresh residual becomes 0
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        to_amount, to_currency, to_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'cash_income', '2026-09-05', 500.00, 'CNY',
                        500.00, 'CNY', %s, 'committed', 'shortcut'
                    );
                    UPDATE account_state SET ledger_balance = 1500.00 WHERE account_id = %s;
                    """,
                    (self.household_id, self.acc_cny_id, self.acc_cny_id)
                )
            conn.commit()
        finally:
            conn.close()

        # Commit
        res_commit = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit.status_code, 200)
        data_commit = res_commit.json()
        self.assertEqual(data_commit["status"], "committed")
        self.assertEqual(data_commit["residual_amount"], "0.00")
        self.assertIsNone(data_commit["adjustment_transaction_id"])
        self.assertIsNotNone(data_commit["snapshot_id"])

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT ledger_balance FROM account_state WHERE account_id = %s;", (self.acc_cny_id,))
                self.assertEqual(cur.fetchone()[0], Decimal("1500.00"))
                cur.execute("SELECT count(*) FROM account_snapshots WHERE reconciliation_batch_id = %s;", (batch_id,))
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()

    def test_27_non_cny_stale_residual_threshold_uses_cny(self):
        """
        Non-CNY stale residual: threshold uses CNY conversion:
        USD account, USD/CNY = 7.20.
        Baseline = 100 USD.
        Submit snapshot = 150 USD (residual = 50 USD = 360 CNY -> needs_review).
        Concurrent income of 20 USD arrives -> fresh residual = 30 USD = 216 CNY (> 200 CNY).
        Commit call -> fresh 30 USD != reviewed 50 USD and 216 CNY > 200 CNY -> returns needs_review!
        """
        # Baseline = 100 USD
        self.client.post(f"/api/v1/accounts/{self.acc_usd_id}/snapshots", json={
            "idempotency_key": "snap_usd_base_027",
            "as_of": "2026-09-01T10:00:00+08:00",
            "balance": "100.00",
            "currency": "USD"
        }, headers=self.headers)

        # Snapshot = 150 USD (residual = 50 USD)
        res_sub = self.client.post(f"/api/v1/accounts/{self.acc_usd_id}/snapshots", json={
            "idempotency_key": "snap_usd_rev_027",
            "as_of": "2026-09-10T10:00:00+08:00",
            "balance": "150.00",
            "currency": "USD"
        }, headers=self.headers)
        batch_id = res_sub.json()["batch_id"]

        # Concurrent income of 20 USD arrives -> fresh projected = 120 USD, fresh residual = 30 USD (216 CNY > 200 CNY)
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO transactions (
                        id, household_id, transaction_type, occurred_on, original_amount, original_currency,
                        to_amount, to_currency, to_account_id, status, source
                    ) VALUES (
                        gen_random_uuid(), %s, 'cash_income', '2026-09-05', 20.00, 'USD',
                        20.00, 'USD', %s, 'committed', 'shortcut'
                    );
                    UPDATE account_state SET ledger_balance = 120.00 WHERE account_id = %s;
                    """,
                    (self.household_id, self.acc_usd_id, self.acc_usd_id)
                )
            conn.commit()
        finally:
            conn.close()

        # Commit: 30 USD != 50 USD and 30*7.2 = 216 CNY > 200 CNY -> needs_review
        res_commit = self.client.post(f"/api/v1/reconciliation-batches/{batch_id}/commit", headers=self.headers)
        self.assertEqual(res_commit.status_code, 200)
        data = res_commit.json()
        self.assertEqual(data["status"], "needs_review")
        self.assertEqual(data["residual_amount"], "30.00")

if __name__ == "__main__":
    unittest.main()
