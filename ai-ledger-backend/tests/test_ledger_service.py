import os
os.environ["ENVIRONMENT"] = "test"
os.environ["DB_SCHEMA"] = "vibeledger_test_runner"

import unittest
import uuid
import threading
from decimal import Decimal
from datetime import date, datetime, timezone
import psycopg2
from psycopg2 import sql

from app import config
from app.db import get_connection, transaction
from migrations import runner
from app.domain import transactions as domain_tx
from app.repositories import accounts as accounts_repo
from app.repositories import transactions as tx_repo
from app.repositories import audit as audit_repo
from app.services import ledger_service

class TestLedgerService(unittest.TestCase):
    def setUp(self):
        if not config.is_safe_for_testing():
            self.skipTest("Skipping integration test. ENVIRONMENT must be 'test'.")
            
        self.test_schema = f"vibeledger_test_{uuid.uuid4().hex[:12]}"
        runner.run_migrations(self.test_schema)
        self.conn = get_connection(self.test_schema)

        # Common test household, user, and categories
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

    def tearDown(self):
        if hasattr(self, "conn") and self.conn:
            self.conn.close()
            
        if config.is_safe_for_testing() and hasattr(self, "test_schema"):
            config.validate_test_schema(self.test_schema)
            settings = config.get_settings()
            conn = get_connection(settings.DB_SCHEMA)
            try:
                with conn.cursor() as cur:
                    quoted_schema = sql.Identifier(self.test_schema)
                    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE").format(schema=quoted_schema))
                conn.commit()
            except Exception as e:
                print(f"Warning: failed to drop test schema {self.test_schema}: {e}")
            finally:
                conn.close()

    # --- A. Expense / Income ---

    def test_01_asset_expense_projection(self):
        # 1. asset expense: start 1000, expense 200 => final 800
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Checking_CNY", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("1000.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("200.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20),
                merchant="Supermarket"
            )

        self.assertIsNotNone(tx)
        self.assertEqual(tx["transaction_type"], "expense")
        self.assertEqual(tx["from_amount"], Decimal("200.000000"))
        self.assertEqual(tx["account_leg_status"], "authoritative")

        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("800.000000"))
        self.assertEqual(state["row_version"], 2)

    def test_02_credit_card_expense_projection(self):
        # 2. credit-card expense: start 0, expense 100 => final -100
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Visa_CNY", "credit", "CNY")
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount="100.00",
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        self.assertIsNotNone(tx)
        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("-100.000000"))

    def test_03_cash_income_projection(self):
        # 3. cash income: start 1000, income 500 => final 1500
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Savings_CNY", "savings", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("1000.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_cash_income(
                conn=self.conn,
                household_id=self.household_id,
                to_account_id=acc_id,
                amount=Decimal("500.00"),
                currency="CNY",
                category_id=self.inc_category_id,
                occurred_on=date(2026, 8, 20)
            )

        self.assertIsNotNone(tx)
        self.assertEqual(tx["transaction_type"], "cash_income")
        self.assertEqual(tx["to_amount"], Decimal("500.000000"))
        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("1500.000000"))

    # --- B. Transfers ---

    def test_04_same_currency_transfer(self):
        # 4. same currency: A 1000, B 500, transfer 300 => A 700 / B 800, FX = 1
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_a, self.household_id, "Acc_A", "cash", "CNY")
        accounts_repo.create_account(self.conn, acc_b, self.household_id, "Acc_B", "savings", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_a, Decimal("1000.000000"))
        accounts_repo.update_account_state_projection(self.conn, acc_b, Decimal("500.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_transfer(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_a,
                to_account_id=acc_b,
                from_amount=Decimal("300.00"),
                from_currency="CNY",
                occurred_on=date(2026, 8, 20)
            )

        self.assertEqual(tx["from_amount"], Decimal("300.000000"))
        self.assertEqual(tx["to_amount"], Decimal("300.000000"))
        self.assertEqual(tx["effective_fx_rate"], Decimal("1.000000000000"))
        self.assertIsNone(tx["category_id"])

        state_a = accounts_repo.get_account_state(self.conn, acc_a)
        state_b = accounts_repo.get_account_state(self.conn, acc_b)
        self.assertEqual(state_a["ledger_balance"], Decimal("700.000000"))
        self.assertEqual(state_b["ledger_balance"], Decimal("800.000000"))

    def test_05_cross_currency_transfer(self):
        # 5. cross currency: A 10000 CNY, B 0 USD, 7250 CNY -> 1000 USD => A 2750 / B 1000, FX = 7.25
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_a, self.household_id, "CNY_Account", "cash", "CNY")
        accounts_repo.create_account(self.conn, acc_b, self.household_id, "USD_Account", "savings", "USD")
        accounts_repo.update_account_state_projection(self.conn, acc_a, Decimal("10000.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_transfer(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_a,
                to_account_id=acc_b,
                from_amount=Decimal("7250.00"),
                from_currency="CNY",
                to_amount=Decimal("1000.00"),
                to_currency="USD",
                occurred_on=date(2026, 8, 20)
            )

        self.assertEqual(tx["from_amount"], Decimal("7250.000000"))
        self.assertEqual(tx["to_amount"], Decimal("1000.000000"))
        self.assertEqual(tx["effective_fx_rate"], Decimal("7.250000000000"))

        state_a = accounts_repo.get_account_state(self.conn, acc_a)
        state_b = accounts_repo.get_account_state(self.conn, acc_b)
        self.assertEqual(state_a["ledger_balance"], Decimal("2750.000000"))
        self.assertEqual(state_b["ledger_balance"], Decimal("1000.000000"))

    def test_06_missing_cross_currency_leg_rejected(self):
        # 6. missing cross-currency leg: rejected, no transaction, no balance mutation
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_a, self.household_id, "CNY_Acc", "cash", "CNY")
        accounts_repo.create_account(self.conn, acc_b, self.household_id, "USD_Acc", "savings", "USD")
        accounts_repo.update_account_state_projection(self.conn, acc_a, Decimal("5000.000000"))
        self.conn.commit()

        with self.assertRaises(domain_tx.CrossCurrencyMissingLegError):
            with transaction(self.conn):
                ledger_service.record_transfer(
                    conn=self.conn,
                    household_id=self.household_id,
                    from_account_id=acc_a,
                    to_account_id=acc_b,
                    from_amount=Decimal("700.00"),
                    from_currency="CNY",
                    to_currency="USD",
                    to_amount=None,  # Missing mandatory leg
                    occurred_on=date(2026, 8, 20)
                )

        state_a = accounts_repo.get_account_state(self.conn, acc_a)
        state_b = accounts_repo.get_account_state(self.conn, acc_b)
        self.assertEqual(state_a["ledger_balance"], Decimal("5000.000000"))
        self.assertEqual(state_b["ledger_balance"], Decimal("0.000000"))

    def test_07_transfer_with_atomic_fee(self):
        # 7. transfer fee: 1 transfer + 1 fee transaction, A delta = -7270 CNY, B delta = +1000 USD
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_a, self.household_id, "Acc_Src", "cash", "CNY")
        accounts_repo.create_account(self.conn, acc_b, self.household_id, "Acc_Dst", "savings", "USD")
        accounts_repo.update_account_state_projection(self.conn, acc_a, Decimal("10000.000000"))
        self.conn.commit()

        with transaction(self.conn):
            res = ledger_service.record_transfer(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_a,
                to_account_id=acc_b,
                from_amount=Decimal("7250.00"),
                from_currency="CNY",
                to_amount=Decimal("1000.00"),
                to_currency="USD",
                fee_amount=Decimal("20.00"),
                fee_currency="CNY",
                fee_category_id=self.fee_category_id,
                occurred_on=date(2026, 8, 20)
            )

        self.assertIn("fee_transaction", res)
        fee_tx = res["fee_transaction"]
        self.assertEqual(fee_tx["transaction_type"], "fee")
        self.assertEqual(fee_tx["from_amount"], Decimal("20.000000"))

        state_a = accounts_repo.get_account_state(self.conn, acc_a)
        state_b = accounts_repo.get_account_state(self.conn, acc_b)
        # 10000 - 7250 - 20 = 2730
        self.assertEqual(state_a["ledger_balance"], Decimal("2730.000000"))
        self.assertEqual(state_b["ledger_balance"], Decimal("1000.000000"))

    # --- C. Refund ---

    def test_08_full_refund(self):
        # 8. full refund
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Card_Ref", "credit", "CNY")
        self.conn.commit()

        # Step 1: Expense 500
        with transaction(self.conn):
            exp_tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("500.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_id)["ledger_balance"], Decimal("-500.000000"))

        # Step 2: Full refund 500
        with transaction(self.conn):
            ref_tx = ledger_service.record_refund(
                conn=self.conn,
                household_id=self.household_id,
                original_expense_id=exp_tx["id"],
                to_account_id=acc_id,
                amount=Decimal("500.00"),
                currency="CNY",
                occurred_on=date(2026, 8, 21)
            )

        self.assertEqual(ref_tx["transaction_type"], "refund")
        self.assertEqual(ref_tx["to_amount"], Decimal("500.000000"))
        self.assertEqual(ref_tx["category_id"], self.exp_category_id) # Inherited

        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("0.000000"))

    def test_09_partial_refunds(self):
        # 9. partial refunds: expense 1000, refunds 300 + 200, remaining refundable 500
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Checking_Ref", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("1000.000000"))
        self.conn.commit()

        with transaction(self.conn):
            exp_tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("1000.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        # Refund 1: 300
        with transaction(self.conn):
            ledger_service.record_refund(
                conn=self.conn,
                household_id=self.household_id,
                original_expense_id=exp_tx["id"],
                to_account_id=acc_id,
                amount=Decimal("300.00"),
                currency="CNY",
                occurred_on=date(2026, 8, 21)
            )

        # Refund 2: 200
        with transaction(self.conn):
            ledger_service.record_refund(
                conn=self.conn,
                household_id=self.household_id,
                original_expense_id=exp_tx["id"],
                to_account_id=acc_id,
                amount=Decimal("200.00"),
                currency="CNY",
                occurred_on=date(2026, 8, 22)
            )

        active_refunds = tx_repo.get_active_refunds_for_expense(self.conn, exp_tx["id"])
        self.assertEqual(len(active_refunds), 2)
        total_refunded = sum((r["to_amount"] for r in active_refunds), Decimal("0"))
        self.assertEqual(total_refunded, Decimal("500.000000"))

        state = accounts_repo.get_account_state(self.conn, acc_id)
        # 1000 - 1000 + 300 + 200 = 500
        self.assertEqual(state["ledger_balance"], Decimal("500.000000"))

    def test_10_over_refund_blocked_atomically(self):
        # 10. over-refund: existing 800, new 300 => reject atomically
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Card_OverRef", "credit", "CNY")
        self.conn.commit()

        with transaction(self.conn):
            exp_tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("1000.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        # First refund 800 (allowed)
        with transaction(self.conn):
            ledger_service.record_refund(
                conn=self.conn,
                household_id=self.household_id,
                original_expense_id=exp_tx["id"],
                to_account_id=acc_id,
                amount=Decimal("800.00"),
                currency="CNY",
                occurred_on=date(2026, 8, 21)
            )

        # Second refund 300 (800 + 300 = 1100 > 1000 -> must raise RefundExceedsOriginalError)
        with self.assertRaises(domain_tx.RefundExceedsOriginalError):
            with transaction(self.conn):
                ledger_service.record_refund(
                    conn=self.conn,
                    household_id=self.household_id,
                    original_expense_id=exp_tx["id"],
                    to_account_id=acc_id,
                    amount=Decimal("300.00"),
                    currency="CNY",
                    occurred_on=date(2026, 8, 22)
                )

        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("-200.000000"))

    def test_11_refund_link_and_original_expense_preservation(self):
        # 11. refund_of link exists and original expense remains committed
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Cash_Preserve", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("500.000000"))
        self.conn.commit()

        with transaction(self.conn):
            exp_tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("150.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        with transaction(self.conn):
            ref_tx = ledger_service.record_refund(
                conn=self.conn,
                household_id=self.household_id,
                original_expense_id=exp_tx["id"],
                to_account_id=acc_id,
                amount=Decimal("150.00"),
                currency="CNY",
                occurred_on=date(2026, 8, 21)
            )

        # Check link exists
        links = tx_repo.list_transaction_links_for_source(self.conn, ref_tx["id"])
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["relation_type"], "refund_of")
        self.assertEqual(links[0]["target_transaction_id"], exp_tx["id"])

        # Check original expense remains committed
        orig_check = tx_repo.get_transaction(self.conn, exp_tx["id"])
        self.assertEqual(orig_check["status"], "committed")
        self.assertIsNone(orig_check["deleted_at"])

    # --- D. Opening / Adjustment ---

    def test_12_positive_opening_balance(self):
        # 12. positive opening balance establishes baseline
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Bank_Initial", "savings", "CNY")
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_opening_balance(
                conn=self.conn,
                household_id=self.household_id,
                account_id=acc_id,
                amount=Decimal("50000.00"),
                currency="CNY",
                occurred_on=date(2026, 1, 1),
                is_positive=True
            )

        self.assertEqual(tx["transaction_type"], "opening_balance")
        self.assertEqual(tx["to_account_id"], acc_id)
        self.assertIsNone(tx["from_account_id"])
        self.assertIsNone(tx["category_id"])

        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("50000.000000"))
        self.assertIsNotNone(state["initialized_at"])

    def test_13_negative_opening_balance(self):
        # 13. negative opening balance / credit debt baseline
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Credit_Initial_Debt", "credit", "CNY")
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_opening_balance(
                conn=self.conn,
                household_id=self.household_id,
                account_id=acc_id,
                amount=Decimal("3500.00"),
                currency="CNY",
                occurred_on=date(2026, 1, 1),
                is_positive=False
            )

        self.assertEqual(tx["transaction_type"], "opening_balance")
        self.assertEqual(tx["from_account_id"], acc_id)
        self.assertIsNone(tx["to_account_id"])

        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("-3500.000000"))
        self.assertIsNotNone(state["initialized_at"])

    def test_14_opening_balance_not_classified_as_income_or_expense(self):
        # 14. opening_balance not classified as income/expense
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Cash_Base", "cash", "CNY")
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_opening_balance(
                conn=self.conn,
                household_id=self.household_id,
                account_id=acc_id,
                amount=Decimal("1000.00"),
                currency="CNY",
                occurred_on=date(2026, 1, 1)
            )

        self.assertEqual(tx["transaction_type"], "opening_balance")
        self.assertIsNone(tx["category_id"])

    def test_15_reconciliation_adjustments(self):
        # 15. positive and negative reconciliation_adjustment projection
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Acc_Recon", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("100.000000"))
        self.conn.commit()

        # Positive adjustment +5.50
        with transaction(self.conn):
            tx_pos = ledger_service.record_reconciliation_adjustment(
                conn=self.conn,
                household_id=self.household_id,
                account_id=acc_id,
                amount=Decimal("5.50"),
                currency="CNY",
                occurred_on=date(2026, 8, 20),
                is_positive=True
            )
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_id)["ledger_balance"], Decimal("105.500000"))
        self.assertIsNone(tx_pos["category_id"])

        # Negative adjustment -2.00
        with transaction(self.conn):
            tx_neg = ledger_service.record_reconciliation_adjustment(
                conn=self.conn,
                household_id=self.household_id,
                account_id=acc_id,
                amount=Decimal("2.00"),
                currency="CNY",
                occurred_on=date(2026, 8, 20),
                is_positive=False
            )
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_id)["ledger_balance"], Decimal("103.500000"))
        self.assertIsNone(tx_neg["category_id"])

    # --- E. Void ---

    def test_16_void_expense_reverses_projection(self):
        # 16. void expense reverses projection
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Acc_VoidExp", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("1000.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("250.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_id)["ledger_balance"], Decimal("750.000000"))

        with transaction(self.conn):
            voided_tx = ledger_service.void_transaction(
                conn=self.conn,
                household_id=self.household_id,
                transaction_id=tx["id"],
                delete_reason="Duplicate entry",
                deleted_by_user_id=self.user_id
            )

        self.assertEqual(voided_tx["status"], "voided")
        self.assertEqual(voided_tx["delete_reason"], "Duplicate entry")
        self.assertIsNotNone(voided_tx["deleted_at"])

        # Projection restored back to 1000
        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertEqual(state["ledger_balance"], Decimal("1000.000000"))

    def test_17_void_transfer_reverses_both_legs(self):
        # 17. void transfer reverses BOTH legs
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_a, self.household_id, "Acc_VA", "cash", "CNY")
        accounts_repo.create_account(self.conn, acc_b, self.household_id, "Acc_VB", "savings", "USD")
        accounts_repo.update_account_state_projection(self.conn, acc_a, Decimal("10000.000000"))
        accounts_repo.update_account_state_projection(self.conn, acc_b, Decimal("0.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_transfer(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_a,
                to_account_id=acc_b,
                from_amount=Decimal("7250.00"),
                from_currency="CNY",
                to_amount=Decimal("1000.00"),
                to_currency="USD",
                occurred_on=date(2026, 8, 20)
            )

        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_a)["ledger_balance"], Decimal("2750.000000"))
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_b)["ledger_balance"], Decimal("1000.000000"))

        with transaction(self.conn):
            ledger_service.void_transaction(
                conn=self.conn,
                household_id=self.household_id,
                transaction_id=tx["id"],
                delete_reason="Incorrect transfer"
            )

        # Both accounts restored
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_a)["ledger_balance"], Decimal("10000.000000"))
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_b)["ledger_balance"], Decimal("0.000000"))

    def test_18_void_fee_and_refund_reversals(self):
        # 18. void fee/refund reverses correct projection
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Acc_FeeRefVoid", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("500.000000"))
        self.conn.commit()

        with transaction(self.conn):
            fee_tx = ledger_service.record_fee(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("15.00"),
                currency="CNY",
                category_id=self.fee_category_id,
                occurred_on=date(2026, 8, 20)
            )
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_id)["ledger_balance"], Decimal("485.000000"))

        with transaction(self.conn):
            ledger_service.void_transaction(
                conn=self.conn,
                household_id=self.household_id,
                transaction_id=fee_tx["id"],
                delete_reason="Fee waived"
            )
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_id)["ledger_balance"], Decimal("500.000000"))

    def test_19_repeated_void_rejected(self):
        # 19. repeated void rejected, no double reversal
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Acc_RepVoid", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("1000.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("100.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        with transaction(self.conn):
            ledger_service.void_transaction(
                conn=self.conn,
                household_id=self.household_id,
                transaction_id=tx["id"],
                delete_reason="First void"
            )
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_id)["ledger_balance"], Decimal("1000.000000"))

        # Second void must raise TransactionAlreadyVoidedError
        with self.assertRaises(domain_tx.TransactionAlreadyVoidedError):
            with transaction(self.conn):
                ledger_service.void_transaction(
                    conn=self.conn,
                    household_id=self.household_id,
                    transaction_id=tx["id"],
                    delete_reason="Second void attempt"
                )

        # Projection remains strictly 1000 (not 1100)
        self.assertEqual(accounts_repo.get_account_state(self.conn, acc_id)["ledger_balance"], Decimal("1000.000000"))

    def test_20_void_transaction_retained_and_audit_appended(self):
        # 20. void transaction retained, audit appended
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Acc_AuditVoid", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("200.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("50.00"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        with transaction(self.conn):
            ledger_service.void_transaction(
                conn=self.conn,
                household_id=self.household_id,
                transaction_id=tx["id"],
                delete_reason="Accidental double swipe"
            )

        # Row physically exists with status voided
        db_tx = tx_repo.get_transaction(self.conn, tx["id"])
        self.assertIsNotNone(db_tx)
        self.assertEqual(db_tx["status"], "voided")
        self.assertEqual(db_tx["delete_reason"], "Accidental double swipe")

        # Audit events for this transaction
        audits = audit_repo.list_audit_events_for_entity(self.conn, "transaction", tx["id"])
        self.assertEqual(len(audits), 2)
        actions = [a["action"] for a in audits]
        self.assertIn("create", actions)
        self.assertIn("void", actions)

    # --- F. Real PostgreSQL Concurrency & Atomicity ---

    def test_21_simultaneous_expenses_no_lost_updates(self):
        # 21. two simultaneous expenses from same account: start 1000, -100, -200 => final 700
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Acc_ConcurrentExp", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("1000.000000"))
        self.conn.commit()

        errors = []

        def worker(amount):
            conn = get_connection(self.test_schema)
            try:
                with transaction(conn):
                    ledger_service.record_expense(
                        conn=conn,
                        household_id=self.household_id,
                        from_account_id=acc_id,
                        amount=Decimal(str(amount)),
                        currency="CNY",
                        category_id=self.exp_category_id,
                        occurred_on=date(2026, 8, 20)
                    )
            except Exception as e:
                errors.append(e)
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=(100,))
        t2 = threading.Thread(target=worker, args=(200,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"Concurrent workers encountered errors: {errors}")
        state = accounts_repo.get_account_state(self.conn, acc_id)
        # 1000 - 100 - 200 = 700 (NEVER 800 or 900)
        self.assertEqual(state["ledger_balance"], Decimal("700.000000"))

    def test_22_opposite_concurrent_transfers_no_deadlock(self):
        # 22. opposite concurrent transfers: A -> B and B -> A, no permanent deadlock, correct final balances
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_a, self.household_id, "Acc_OppA", "cash", "CNY")
        accounts_repo.create_account(self.conn, acc_b, self.household_id, "Acc_OppB", "savings", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_a, Decimal("1000.000000"))
        accounts_repo.update_account_state_projection(self.conn, acc_b, Decimal("1000.000000"))
        self.conn.commit()

        errors = []

        def transfer_a_to_b():
            conn = get_connection(self.test_schema)
            try:
                for _ in range(5):
                    with transaction(conn):
                        ledger_service.record_transfer(
                            conn=conn,
                            household_id=self.household_id,
                            from_account_id=acc_a,
                            to_account_id=acc_b,
                            from_amount=Decimal("10.00"),
                            from_currency="CNY",
                            occurred_on=date(2026, 8, 20)
                        )
            except Exception as e:
                errors.append(e)
            finally:
                conn.close()

        def transfer_b_to_a():
            conn = get_connection(self.test_schema)
            try:
                for _ in range(5):
                    with transaction(conn):
                        ledger_service.record_transfer(
                            conn=conn,
                            household_id=self.household_id,
                            from_account_id=acc_b,
                            to_account_id=acc_a,
                            from_amount=Decimal("10.00"),
                            from_currency="CNY",
                            occurred_on=date(2026, 8, 20)
                        )
            except Exception as e:
                errors.append(e)
            finally:
                conn.close()

        t1 = threading.Thread(target=transfer_a_to_b)
        t2 = threading.Thread(target=transfer_b_to_a)

        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertFalse(t1.is_alive(), "Thread 1 deadlocked or timed out!")
        self.assertFalse(t2.is_alive(), "Thread 2 deadlocked or timed out!")
        self.assertEqual(len(errors), 0, f"Opposite transfer workers encountered errors: {errors}")

        state_a = accounts_repo.get_account_state(self.conn, acc_a)
        state_b = accounts_repo.get_account_state(self.conn, acc_b)
        # Net transfer delta is 0, both should be 1000
        self.assertEqual(state_a["ledger_balance"], Decimal("1000.000000"))
        self.assertEqual(state_b["ledger_balance"], Decimal("1000.000000"))

    # --- G. Numeric Correctness ---

    def test_23_all_financial_results_decimal(self):
        # 23. all financial results returned as Decimal
        acc_id = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_id, self.household_id, "Acc_DecCheck", "cash", "CNY")
        accounts_repo.update_account_state_projection(self.conn, acc_id, Decimal("500.000000"))
        self.conn.commit()

        with transaction(self.conn):
            tx = ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_id,
                amount=Decimal("123.45"),
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        self.assertIsInstance(tx["original_amount"], Decimal)
        self.assertIsInstance(tx["from_amount"], Decimal)
        state = accounts_repo.get_account_state(self.conn, acc_id)
        self.assertIsInstance(state["ledger_balance"], Decimal)

    def test_24_exact_db_decimal_roundtrip(self):
        # 24. exact DB Decimal roundtrip maintained (e.g. 0.1 + 0.2, JPY integer amounts)
        acc_cny = uuid.uuid4()
        acc_jpy = uuid.uuid4()
        accounts_repo.create_account(self.conn, acc_cny, self.household_id, "Acc_Roundtrip_CNY", "cash", "CNY")
        accounts_repo.create_account(self.conn, acc_jpy, self.household_id, "Acc_Roundtrip_JPY", "cash", "JPY")
        self.conn.commit()

        # CNY 0.10 + 0.20
        with transaction(self.conn):
            ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_cny,
                amount="0.10",
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )
            ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_cny,
                amount="0.20",
                currency="CNY",
                category_id=self.exp_category_id,
                occurred_on=date(2026, 8, 20)
            )

        state_cny = accounts_repo.get_account_state(self.conn, acc_cny)
        self.assertEqual(state_cny["ledger_balance"], Decimal("-0.300000"))

        # JPY 1500 (integer)
        with transaction(self.conn):
            cat_jpy = uuid.uuid4()
            accounts_repo.create_category(self.conn, cat_jpy, self.household_id, "JPY Food", "expense")
            ledger_service.record_expense(
                conn=self.conn,
                household_id=self.household_id,
                from_account_id=acc_jpy,
                amount=1500,
                currency="JPY",
                category_id=cat_jpy,
                occurred_on=date(2026, 8, 20)
            )

        state_jpy = accounts_repo.get_account_state(self.conn, acc_jpy)
        self.assertEqual(state_jpy["ledger_balance"], Decimal("-1500.000000"))
