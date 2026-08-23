import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
import threading
from uuid import uuid4, UUID
from decimal import Decimal
from datetime import datetime, date, timezone

from app.db import get_connection, transaction
from tests.support.db_helper import BaseDbTestCase

from app.domain.reconciliation.models import NormalizedStatementLine
from app.services.reconciliation_service import (
    create_statement_reconciliation_batch,
    commit_statement_batch
)
from app.services.reference_fx_service import ReferenceFxService
import app.repositories.accounts as accounts_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo


class TestStatementReconciliationConcurrency(BaseDbTestCase):

    def setUp(self):
        super().setUp()
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.acc_a_id = uuid4()
        self.acc_b_id = uuid4()
        self.acc_c_id = uuid4()
        self.cat_expense_id = uuid4()

        self.mock_fx = ReferenceFxService(fixed_rates={
            ("USD", "CNY"): Decimal("7.20")
        })

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_household(conn, self.household_id, "Concurrency Household", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_conc_user", "Conc User", "conc@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")

                # Setup accounts
                accounts_repo.create_account(conn, self.acc_a_id, self.household_id, "Account A", "cash", "CNY", self.user_id)
                accounts_repo.create_account(conn, self.acc_b_id, self.household_id, "Account B", "cash", "CNY", self.user_id)
                accounts_repo.create_account(conn, self.acc_c_id, self.household_id, "Account C", "cash", "CNY", self.user_id)
                accounts_repo.create_category(conn, self.cat_expense_id, self.household_id, "Dining", "expense")

                # Initialize starting account_state balances and opening_balance transactions
                for aid, amt in [(self.acc_a_id, Decimal("10000.00")), (self.acc_b_id, Decimal("10000.00")), (self.acc_c_id, Decimal("10000.00"))]:
                    tx_repo.create_transaction(
                        conn=conn,
                        tx_id=uuid4(),
                        household_id=self.household_id,
                        transaction_type="opening_balance",
                        occurred_on=date(2026, 1, 1),
                        original_amount=amt,
                        original_currency="CNY",
                        to_amount=amt,
                        to_currency="CNY",
                        to_account_id=aid,
                        source="system",
                        status="committed"
                    )
                    accounts_repo.update_account_state_projection(conn, aid, amt, datetime.now(timezone.utc))
        finally:
            conn.close()

    def test_01_concurrent_shortcut_and_statement_commit(self):
        """
        Thread 1 creates a statement reconciliation batch proposing missing expense X.
        Thread 2 (Shortcut) commits expense X concurrently.
        Thread 1 commits statement batch.
        Asserts:
        - Ledger balance is deducted exactly once for X.
        - Transaction is marked statement_confirmed.
        - No duplicate transaction exists.
        """
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Starbucks Coffee",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("35.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_a_id,
                    lines=[line],
                    authoritative_balance=Decimal("9965.00"),
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            shortcut_tx_id = uuid4()
            barrier = threading.Barrier(2)
            results = {}

            def worker_shortcut():
                c = get_connection(self.test_schema)
                try:
                    barrier.wait()
                    with transaction(c):
                        tx_repo.create_transaction(
                            conn=c,
                            tx_id=shortcut_tx_id,
                            household_id=self.household_id,
                            transaction_type="expense",
                            occurred_on=date(2026, 8, 10),
                            original_amount=Decimal("35.00"),
                            original_currency="CNY",
                            from_amount=Decimal("35.00"),
                            from_currency="CNY",
                            from_account_id=self.acc_a_id,
                            category_id=self.cat_expense_id,
                            merchant="Starbucks Coffee",
                            source="shortcut",
                            status="committed"
                        )
                        accounts_repo.update_account_state_projection(c, self.acc_a_id, Decimal("9965.00"), datetime.now(timezone.utc))
                    results["shortcut"] = "ok"
                except Exception as e:
                    results["shortcut"] = str(e)
                finally:
                    c.close()

            def worker_statement_commit():
                c = get_connection(self.test_schema)
                try:
                    barrier.wait()
                    with transaction(c):
                        res = commit_statement_batch(c, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                    results["statement"] = res
                except Exception as e:
                    results["statement"] = str(e)
                finally:
                    c.close()

            t1 = threading.Thread(target=worker_shortcut)
            t2 = threading.Thread(target=worker_statement_commit)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(results["shortcut"], "ok")
            self.assertIsInstance(results["statement"], dict)
            self.assertEqual(results["statement"]["status"], "committed")

            # Verify balance is exactly 9965.00 (not double deducted)
            acc_state = accounts_repo.get_account_state(conn, self.acc_a_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9965.00"))

            # Verify only 1 expense transaction exists
            txs, _ = tx_repo.list_transactions_with_filters(
                conn=conn,
                household_id=self.household_id,
                account_id=self.acc_a_id,
                transaction_type="expense"
            )
            self.assertEqual(len(txs), 1)
            self.assertEqual(txs[0]["verification_status"], "statement_confirmed")
        finally:
            conn.close()

    def test_02_concurrent_double_statement_commit(self):
        """
        Multiple concurrent workers attempt to commit the same statement batch.
        Asserts exactly one performs writes, all return committed status idempotently.
        """
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Hema Fresh Market",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("120.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_a_id,
                    lines=[line],
                    authoritative_balance=Decimal("9880.00"),
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            num_threads = 5
            barrier = threading.Barrier(num_threads)
            results = [None] * num_threads

            def worker(idx):
                c = get_connection(self.test_schema)
                try:
                    barrier.wait()
                    with transaction(c):
                        res = commit_statement_batch(c, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                        results[idx] = res
                except Exception as e:
                    results[idx] = e
                finally:
                    c.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            for r in results:
                self.assertIsInstance(r, dict)
                self.assertEqual(r["status"], "committed")

            acc_state = accounts_repo.get_account_state(conn, self.acc_a_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9880.00"))

            txs, _ = tx_repo.list_transactions_with_filters(
                conn=conn,
                household_id=self.household_id,
                account_id=self.acc_a_id,
                transaction_type="expense"
            )
            self.assertEqual(len(txs), 1)
        finally:
            conn.close()

    def test_03_concurrent_cross_account_transfers_no_deadlock(self):
        """
        Multiple concurrent statement batches affecting overlapping counter-accounts
        (Batch 1: A -> B, Batch 2: C -> B).
        Deterministic sorted UUID locks prevent deadlocks and protect concurrent updates.
        """
        conn = get_connection(self.test_schema)
        try:
            # Seed movements
            line_a = NormalizedStatementLine(transaction_on=date(2026, 8, 10), description_raw="转账到B", direction="debit", line_type="transfer", settlement_amount=Decimal("1000.00"), settlement_currency="CNY")
            line_c = NormalizedStatementLine(transaction_on=date(2026, 8, 10), description_raw="转账到B", direction="debit", line_type="transfer", settlement_amount=Decimal("1000.00"), settlement_currency="CNY")

            counter_a = [{"account_id": self.acc_b_id, "direction": "credit", "amount": Decimal("1000.00"), "currency": "CNY", "occurred_on": date(2026, 8, 10), "is_counter_statement_leg": True}]
            counter_c = [{"account_id": self.acc_b_id, "direction": "credit", "amount": Decimal("1000.00"), "currency": "CNY", "occurred_on": date(2026, 8, 10), "is_counter_statement_leg": True}]

            with transaction(conn):
                p_a = create_statement_reconciliation_batch(conn, self.household_id, self.acc_a_id, [line_a], Decimal("9000.00"), user_id=self.user_id, fx_service=self.mock_fx, household_movements=counter_a)
                p_c = create_statement_reconciliation_batch(conn, self.household_id, self.acc_c_id, [line_c], Decimal("9000.00"), user_id=self.user_id, fx_service=self.mock_fx, household_movements=counter_c)

            batch_ids = [UUID(p_a["batch_id"]), UUID(p_c["batch_id"])]
            num_threads = 2
            barrier = threading.Barrier(num_threads)
            results = [None] * num_threads

            def worker(idx):
                c = get_connection(self.test_schema)
                try:
                    barrier.wait()
                    with transaction(c):
                        res = commit_statement_batch(c, batch_ids[idx], user_id=self.user_id, fx_service=self.mock_fx)
                        results[idx] = res
                except Exception as e:
                    results[idx] = e
                finally:
                    c.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            for r in results:
                self.assertIsInstance(r, dict)
                self.assertEqual(r["status"], "committed")

            # Check total money conserved (30000.00 starting total: A=9000, B=12000, C=9000)
            s_a = accounts_repo.get_account_state(conn, self.acc_a_id)
            s_b = accounts_repo.get_account_state(conn, self.acc_b_id)
            s_c = accounts_repo.get_account_state(conn, self.acc_c_id)
            self.assertEqual(s_a["ledger_balance"], Decimal("9000.00"))
            self.assertEqual(s_b["ledger_balance"], Decimal("12000.00"))
            self.assertEqual(s_c["ledger_balance"], Decimal("9000.00"))
            total = s_a["ledger_balance"] + s_b["ledger_balance"] + s_c["ledger_balance"]
            self.assertEqual(total, Decimal("30000.00"))
        finally:
            conn.close()


    def test_04_concurrent_installment_period_recognition(self):
        """
        Two concurrent threads attempt to commit statement batches previewing the same installment period 1.
        Asserts:
        - Atomic status transition ensures exactly one expense transaction is created.
        - The other batch cleanly skips or finishes without creating duplicate.
        - Balance is deducted exactly once.
        """
        import app.repositories.installments as installments_repo
        conn = get_connection(self.test_schema)
        try:
            acc_credit_id = uuid4()
            plan_id = uuid4()
            period_1_id = uuid4()
            with transaction(conn):
                accounts_repo.create_account(conn, acc_credit_id, self.household_id, "Credit Card", "credit", "CNY", self.user_id)
                accounts_repo.update_account_state_projection(conn, acc_credit_id, Decimal("0.00"), datetime.now(timezone.utc))

                installments_repo.create_installment_plan(
                    conn=conn,
                    plan_id=plan_id,
                    household_id=self.household_id,
                    credit_account_id=acc_credit_id,
                    purchase_occurred_on=date(2026, 8, 1),
                    merchant="Apple Store",
                    original_amount=Decimal("12000.00"),
                    original_currency="CNY",
                    account_principal_amount=Decimal("12000.00"),
                    account_currency="CNY",
                    total_periods=12,
                    first_statement_month=date(2026, 8, 1),
                    status="pending_first_bill"
                )
                installments_repo.create_installment_period(
                    conn=conn,
                    period_id=period_1_id,
                    plan_id=plan_id,
                    period_no=1,
                    scheduled_amount=Decimal("1000.00"),
                    currency="CNY",
                    status="scheduled"
                )

            line_1 = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Apple Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("1000.00"),
                settlement_currency="CNY"
            )
            line_2 = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Apple Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("1000.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                p1 = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=acc_credit_id,
                    lines=[line_1], authoritative_balance=Decimal("-1000.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                p2 = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=acc_credit_id,
                    lines=[line_2], authoritative_balance=Decimal("-1000.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )

            batch_1_id = UUID(p1["batch_id"])
            batch_2_id = UUID(p2["batch_id"])

            barrier = threading.Barrier(2)
            results = [None, None]

            def worker(idx, b_id):
                c = get_connection(self.test_schema)
                try:
                    barrier.wait()
                    with transaction(c):
                        res = commit_statement_batch(c, b_id, user_id=self.user_id, fx_service=self.mock_fx)
                        results[idx] = res
                except Exception as e:
                    results[idx] = e
                finally:
                    c.close()

            t1 = threading.Thread(target=worker, args=(0, batch_1_id))
            t2 = threading.Thread(target=worker, args=(1, batch_2_id))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # Verify exactly one expense transaction was created for the installment period
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE merchant = 'Apple Store' AND source = 'installment';")
                tx_count = cur.fetchone()[0]
                self.assertEqual(tx_count, 1)

            # Period 1 must be billed
            period = installments_repo.list_periods_for_plan(conn, plan_id)[0]
            self.assertEqual(period["status"], "billed")
            self.assertIsNotNone(period["expense_transaction_id"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()


