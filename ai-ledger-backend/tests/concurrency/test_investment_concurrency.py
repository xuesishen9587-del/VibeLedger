import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
import threading
import hashlib
from uuid import uuid4, UUID
from decimal import Decimal
from datetime import datetime, date, timezone

from app.db import get_connection, transaction
from tests.support.db_helper import BaseDbTestCase
import app.repositories.accounts as accounts_repo
import app.repositories.investments as investments_repo
import app.repositories.devices as devices_repo
import app.services.investment_service as investment_service


class TestInvestmentConcurrency(BaseDbTestCase):
    def setUp(self):
        super().setUp()
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.inv_account_id = uuid4()

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_household(conn, self.household_id, "Invest Concurrency HH", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "invest_conc_user", "Invest User", "invest@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.inv_account_id,
                    household_id=self.household_id,
                    name="Stock Account",
                    account_type="investment",
                    currency="CNY",
                    owner_user_id=self.user_id
                )

                # Initialize first snapshot baseline at 100,000 CNY as of 2026-08-01
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_account_id,
                    payload={
                        "idempotency_key": "conc-baseline-001",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )
        finally:
            conn.close()

    def test_concurrent_investment_snapshots_serialized_by_account_lock(self):
        """
        Section 41 & Section 11: Sleep-free deterministic PostgreSQL concurrency test for two concurrent
        investment snapshots on the same account.
        Worker 1 acquires account_state row lock and submits snapshot 1 (2026-08-10).
        Worker 2 enters transaction and blocks on account_state lock for snapshot 2 (2026-08-20).
        Worker 1 commits.
        Worker 2 acquires the lock, re-reads latest authoritative valuation under the lock,
        and computes P&L against Worker 1's valuation.
        No overlapping periods, no lost updates, no deadlock.
        """
        w1_locked = threading.Event()
        w2_started = threading.Event()

        results = [None, None]
        errors = [None, None]

        def worker_1():
            conn = get_connection(self.test_schema)
            try:
                with transaction(conn):
                    # Lock account_state first
                    accounts_repo.lock_account_states(conn, [self.inv_account_id])
                    w1_locked.set()

                    # Wait until worker 2 has launched and reached its attempt to lock
                    w2_started.wait()

                    res = investment_service.create_manual_investment_snapshot(
                        conn=conn,
                        household_id=self.household_id,
                        account_id=self.inv_account_id,
                        payload={
                            "idempotency_key": "conc-snap-t1",
                            "as_of": "2026-08-10T00:00:00+08:00",
                            "total_asset_value": "120000.00",
                            "currency": "CNY"
                        },
                        user_id=self.user_id
                    )
                    results[0] = res
            except Exception as e:
                errors[0] = e
            finally:
                conn.close()

        def worker_2():
            conn = get_connection(self.test_schema)
            try:
                # Wait for Worker 1 to hold the lock
                w1_locked.wait()
                w2_started.set()

                with transaction(conn):
                    res = investment_service.create_manual_investment_snapshot(
                        conn=conn,
                        household_id=self.household_id,
                        account_id=self.inv_account_id,
                        payload={
                            "idempotency_key": "conc-snap-t2",
                            "as_of": "2026-08-20T00:00:00+08:00",
                            "total_asset_value": "150000.00",
                            "currency": "CNY"
                        },
                        user_id=self.user_id
                    )
                    results[1] = res
            except Exception as e:
                errors[1] = e
            finally:
                conn.close()

        t1 = threading.Thread(target=worker_1)
        t2 = threading.Thread(target=worker_2)

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        self.assertIsNone(errors[0], f"Worker 1 failed: {errors[0]}")
        self.assertIsNone(errors[1], f"Worker 2 failed: {errors[1]}")

        self.assertEqual(results[0]["status"], "committed")
        self.assertEqual(results[1]["status"], "committed")

        # Verify DB state
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # Account state must be 150,000 (latest valuation)
                state = accounts_repo.get_account_state(conn, self.inv_account_id)
                self.assertEqual(state["ledger_balance"], Decimal("150000.000000"))

                # There must be exactly 2 confirmed P&L periods
                periods = investments_repo.list_investment_pnl_periods(conn, self.household_id, self.inv_account_id)
                self.assertEqual(len(periods), 2)

                # Period 1: 100,000 -> 120,000, P&L = 20,000
                self.assertEqual(periods[0]["pnl_amount"], Decimal("20000.000000"))

                # Period 2: 120,000 -> 150,000, P&L = 30,000
                self.assertEqual(periods[1]["pnl_amount"], Decimal("30000.000000"))
        finally:
            conn.close()

    def test_concurrent_manual_snapshot_idempotency_ownership(self):
        """
        Section 10 & Clarification A: Concurrent same-device requests with same idempotency key
        and same payload execute exactly ONCE under PostgreSQL uniqueness arbitration.
        """
        device_id = uuid4()
        token_raw = "test-idem-ownership-token"
        token_hash = hashlib.sha256(token_raw.encode("utf-8")).digest()

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                devices_repo.create_device(
                    conn=conn,
                    device_id=device_id,
                    user_id=self.user_id,
                    device_name="Test Idem Device",
                    token_hash=token_hash
                )
        finally:
            conn.close()

        barrier = threading.Barrier(2)
        results = [None, None]
        errors = [None, None]

        payload = {
            "idempotency_key": "conc-idem-key-001",
            "as_of": "2026-08-15T00:00:00+08:00",
            "total_asset_value": "135000.00",
            "currency": "CNY",
            "source": "shortcut"
        }

        def worker(idx):
            conn = get_connection(self.test_schema)
            try:
                barrier.wait()
                with transaction(conn):
                    res = investment_service.create_manual_investment_snapshot(
                        conn=conn,
                        household_id=self.household_id,
                        account_id=self.inv_account_id,
                        payload=payload,
                        user_id=self.user_id,
                        device_id=device_id
                    )
                    results[idx] = res
            except Exception as e:
                errors[idx] = e
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        # Both callers resolve to compatible committed result with same snapshot_id (or one gets retryable)
        succeeded_results = [r for r in results if r is not None]
        self.assertGreaterEqual(len(succeeded_results), 1)

        if len(succeeded_results) == 2:
            self.assertEqual(results[0]["status"], "committed")
            self.assertEqual(results[1]["status"], "committed")
            self.assertEqual(results[0]["snapshot_id"], results[1]["snapshot_id"])

        # Verify DB has exactly ONE business execution (exactly 1 snapshot and 1 P&L period)
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT count(*) FROM account_snapshots
                        WHERE account_id = %s AND snapshot_type = 'investment_valuation'
                          AND as_of = '2026-08-15T00:00:00+08:00';
                        """,
                        (self.inv_account_id,)
                    )
                    count = cur.fetchone()[0]
                    self.assertEqual(count, 1)

                    cur.execute(
                        """
                        SELECT count(*) FROM investment_pnl_periods
                        WHERE account_id = %s AND period_end = '2026-08-15T00:00:00+08:00';
                        """,
                        (self.inv_account_id,)
                    )
                    pnl_count = cur.fetchone()[0]
                    self.assertEqual(pnl_count, 1)

                state = accounts_repo.get_account_state(conn, self.inv_account_id)
                self.assertEqual(state["ledger_balance"], Decimal("135000.000000"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
