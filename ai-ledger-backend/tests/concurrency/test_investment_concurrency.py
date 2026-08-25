import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
import threading
import time
from uuid import uuid4, UUID
from decimal import Decimal
from datetime import datetime, date, timezone

from app.db import get_connection, transaction
from tests.support.db_helper import BaseDbTestCase
import app.repositories.accounts as accounts_repo
import app.repositories.investments as investments_repo
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
        Section 41: Deterministic PostgreSQL concurrency test for two concurrent
        investment snapshots on the same account.
        Worker 1 acquires account_state row lock and submits snapshot 1 (2026-08-10).
        Worker 2 attempts to lock account_state for snapshot 2 (2026-08-20) and blocks.
        Worker 1 commits.
        Worker 2 acquires the lock, re-reads latest authoritative valuation under the lock,
        and computes P&L against Worker 1's valuation.
        No overlapping periods, no lost updates, no deadlock.
        """
        w1_locked = threading.Event()
        w2_started = threading.Event()
        w1_can_commit = threading.Event()

        results = [None, None]
        errors = [None, None]

        def worker_1():
            conn = get_connection(self.test_schema)
            try:
                with transaction(conn):
                    # Lock account_state first
                    accounts_repo.lock_account_states(conn, [self.inv_account_id])
                    w1_locked.set()

                    # Wait until worker 2 has launched and is blocked waiting for lock
                    w2_started.wait()
                    time.sleep(0.1)

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


if __name__ == "__main__":
    unittest.main()
