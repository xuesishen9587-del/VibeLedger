import unittest
import uuid
import threading
from decimal import Decimal
from datetime import date
import psycopg2

from app.db import get_connection, transaction
from app.domain import transactions as domain_tx
from app.repositories import accounts as accounts_repo
from app.repositories import transactions as tx_repo
from app.services import ledger_service
try:
    from tests.support.db_helper import BaseDbTestCase
except ModuleNotFoundError:
    from support.db_helper import BaseDbTestCase

class TestLedgerConcurrency(BaseDbTestCase):
    def seed_test_data(self):
        self.household_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.exp_category_id = uuid.uuid4()
        self.inc_category_id = uuid.uuid4()
        self.fee_category_id = uuid.uuid4()

        accounts_repo.create_household(self.conn, self.household_id, "Test Household", date(2026, 1, 1))
        accounts_repo.create_user(self.conn, self.user_id, "auth_user_test", "Test User")
        accounts_repo.create_category(self.conn, self.exp_category_id, self.household_id, "Dining", "expense")
        accounts_repo.create_category(self.conn, self.inc_category_id, self.household_id, "Salary", "income")
        accounts_repo.create_category(self.conn, self.fee_category_id, self.household_id, "Bank Fee", "expense")
        self.conn.commit()

    def test_21_simultaneous_expenses_no_lost_updates(self):
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Checking_Concurrent", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("1000.000000"))
        self.conn.commit()

        num_threads = 10
        errors = []

        def worker():
            t_conn = None
            try:
                t_conn = get_connection(self.test_schema)
                with transaction(t_conn):
                    ledger_service.record_expense(
                        conn=t_conn,
                        household_id=self.household_id,
                        from_account_id=acc_id,
                        amount=Decimal("10.00"),
                        currency="CNY",
                        category_id=self.exp_category_id,
                        occurred_on=date(2026, 8, 20)
                    )
            except Exception as e:
                errors.append(e)
            finally:
                if t_conn and not t_conn.closed:
                    t_conn.close()

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent workers failed: {errors}")
        state = accounts_repo.get_account_state(self.conn, acc_id)
        # 1000 - 10 * 10 = 900
        self.assertEqual(state["ledger_balance"], Decimal("900.000000"))
        self.assertEqual(state["row_version"], 1 + num_threads)

    def test_22_opposite_concurrent_transfers_no_deadlock(self):
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_a, self.household_id, "Acc_A_Lock", "cash", "CNY")
        accounts_repo.create_account(self.conn, acc_b, self.household_id, "Acc_B_Lock", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_a, Decimal("1000.000000"))
        accounts_repo.update_account_state_projection(self.conn, acc_b, Decimal("1000.000000"))
        self.conn.commit()

        num_iterations = 10
        errors = []

        def worker_a_to_b():
            for _ in range(num_iterations):
                t_conn = None
                try:
                    t_conn = get_connection(self.test_schema)
                    with transaction(t_conn):
                        ledger_service.record_transfer(
                            conn=t_conn,
                            household_id=self.household_id,
                            from_account_id=acc_a,
                            to_account_id=acc_b,
                            from_amount=Decimal("5.00"),
                            from_currency="CNY",
                            occurred_on=date(2026, 8, 20)
                        )
                except Exception as e:
                    errors.append(e)
                finally:
                    if t_conn and not t_conn.closed:
                        t_conn.close()

        def worker_b_to_a():
            for _ in range(num_iterations):
                t_conn = None
                try:
                    t_conn = get_connection(self.test_schema)
                    with transaction(t_conn):
                        ledger_service.record_transfer(
                            conn=t_conn,
                            household_id=self.household_id,
                            from_account_id=acc_b,
                            to_account_id=acc_a,
                            from_amount=Decimal("5.00"),
                            from_currency="CNY",
                            occurred_on=date(2026, 8, 20)
                        )
                except Exception as e:
                    errors.append(e)
                finally:
                    if t_conn and not t_conn.closed:
                        t_conn.close()

        t1 = threading.Thread(target=worker_a_to_b)
        t2 = threading.Thread(target=worker_b_to_a)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"Opposite transfers deadlocked or failed: {errors}")
        state_a = accounts_repo.get_account_state(self.conn, acc_a)
        state_b = accounts_repo.get_account_state(self.conn, acc_b)
        self.assertEqual(state_a["ledger_balance"], Decimal("1000.000000"))
        self.assertEqual(state_b["ledger_balance"], Decimal("1000.000000"))

    def test_29_concurrent_refund_and_void_no_deadlock(self):
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Checking_RV", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("1000.000000"))
        self.conn.commit()

        # Create original expense
        with transaction(self.conn):
            orig_exp = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("100.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        errors = []
        outcomes = []

        def worker_refund():
            t_conn = None
            try:
                t_conn = get_connection(self.test_schema)
                with transaction(t_conn):
                    ref = ledger_service.record_refund(
                        conn=t_conn,
                        household_id=self.household_id,
                        original_transaction_id=orig_exp["id"],
                        to_account_id=acc_id,
                        amount=Decimal("100.00"),
                        currency="CNY",
                        occurred_on=date(2026, 8, 21)
                    )
                outcomes.append(("refund", ref["id"]))
            except Exception as e:
                outcomes.append(("refund_error", type(e).__name__))
            finally:
                if t_conn and not t_conn.closed:
                    t_conn.close()

        def worker_void():
            t_conn = None
            try:
                t_conn = get_connection(self.test_schema)
                with transaction(t_conn):
                    v = ledger_service.void_transaction(
                        conn=t_conn,
                        household_id=self.household_id,
                        transaction_id=orig_exp["id"],
                        reason="Concurrent void"
                    )
                outcomes.append(("void", v["id"]))
            except Exception as e:
                outcomes.append(("void_error", type(e).__name__))
            finally:
                if t_conn and not t_conn.closed:
                    t_conn.close()

        t1 = threading.Thread(target=worker_refund)
        t2 = threading.Thread(target=worker_void)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both operations serialize cleanly without database deadlocks
        for tag, val in outcomes:
            self.assertFalse("Deadlock" in str(val), f"Deadlock occurred: {outcomes}")
