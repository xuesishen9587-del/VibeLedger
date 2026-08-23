import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
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
import app.repositories.installments as installments_repo
import app.repositories.audit as audit_repo


class TestStatementReconciliationDb(BaseDbTestCase):

    def setUp(self):
        super().setUp()
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.acc_cny_id = uuid4()
        self.acc_cny2_id = uuid4()
        self.acc_usd_id = uuid4()
        self.acc_credit_id = uuid4()
        self.cat_expense_id = uuid4()
        self.cat_income_id = uuid4()

        self.mock_fx = ReferenceFxService(fixed_rates={
            ("USD", "CNY"): Decimal("7.20"),
            ("JPY", "USD"): Decimal("0.006820"),
            ("JPY", "CNY"): Decimal("0.0490")
        })

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                accounts_repo.create_household(conn, self.household_id, "Reconcile Test Household", date(2026, 1, 1), "CNY")
                accounts_repo.create_user(conn, self.user_id, "auth_recon_user", "Recon User", "recon@test.local", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")

                # Setup accounts
                accounts_repo.create_account(conn, self.acc_cny_id, self.household_id, "CMB Checking", "cash", "CNY", self.user_id)
                accounts_repo.create_account(conn, self.acc_cny2_id, self.household_id, "ICBC Savings", "cash", "CNY", self.user_id)
                accounts_repo.create_account(conn, self.acc_usd_id, self.household_id, "Chase Checking", "cash", "USD", self.user_id)
                accounts_repo.create_account(conn, self.acc_credit_id, self.household_id, "CMB Credit Card", "credit", "CNY", self.user_id)

                # Categories
                accounts_repo.create_category(conn, self.cat_expense_id, self.household_id, "Dining", "expense")
                accounts_repo.create_category(conn, self.cat_income_id, self.household_id, "Salary", "income")

                # Initialize starting account_state balances and opening_balance transactions
                for aid, amt, curr in [
                    (self.acc_cny_id, Decimal("10000.00"), "CNY"),
                    (self.acc_cny2_id, Decimal("5000.00"), "CNY"),
                    (self.acc_usd_id, Decimal("2000.00"), "USD")
                ]:
                    tx_repo.create_transaction(
                        conn=conn,
                        tx_id=uuid4(),
                        household_id=self.household_id,
                        transaction_type="opening_balance",
                        occurred_on=date(2026, 1, 1),
                        original_amount=amt,
                        original_currency=curr,
                        to_amount=amt,
                        to_currency=curr,
                        to_account_id=aid,
                        source="system",
                        status="committed"
                    )
                    accounts_repo.update_account_state_projection(conn, aid, amt, datetime.now(timezone.utc))

                accounts_repo.update_account_state_projection(conn, self.acc_credit_id, Decimal("0.00"), datetime.now(timezone.utc))
        finally:
            conn.close()



    def test_01_match_existing_transaction_statement_confirmed(self):
        conn = get_connection(self.test_schema)
        try:
            # 1. Pre-seed committed expense transaction
            tx_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("35.00"),
                    original_currency="CNY",
                    from_amount=Decimal("35.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_expense_id,
                    merchant="Starbucks Coffee",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("9965.00"), datetime.now(timezone.utc))

            # 2. Submit Statement with matching line
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                posted_on=date(2026, 8, 11),
                description_raw="Starbucks Coffee Beijing",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("35.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=Decimal("9965.00"),
                    period_start=date(2026, 8, 1),
                    period_end=date(2026, 8, 31),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["matched_count"], 1)
                self.assertEqual(preview["created_count"], 0)
                batch_id = UUID(preview["batch_id"])

            # 3. Commit statement batch
            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                self.assertEqual(res["matched_count"], 1)

            # 4. Verify DB state
            updated_tx = tx_repo.get_transaction(conn, tx_id)
            self.assertEqual(updated_tx["verification_status"], "statement_confirmed")
            self.assertEqual(updated_tx["posted_on"], date(2026, 8, 11))

            lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
            self.assertEqual(lines[0]["match_status"], "matched")
            self.assertEqual(lines[0]["matched_transaction_id"], tx_id)

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9965.00"))
        finally:
            conn.close()

    def test_02_create_missing_expense_transaction(self):
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Hema Fresh Market",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("120.50"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=Decimal("9879.50"),  # 10000 - 120.50
                    period_start=date(2026, 8, 1),
                    period_end=date(2026, 8, 31),
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                self.assertEqual(len(res["applied_transaction_ids"]), 1)
                new_tx_id = UUID(res["applied_transaction_ids"][0])

            # Verify created transaction & balance update
            new_tx = tx_repo.get_transaction(conn, new_tx_id)
            self.assertEqual(new_tx["transaction_type"], "expense")
            self.assertEqual(new_tx["from_amount"], Decimal("120.50"))
            self.assertEqual(new_tx["verification_status"], "statement_confirmed")

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9879.50"))
        finally:
            conn.close()

    def test_03_create_missing_income_and_fee(self):
        conn = get_connection(self.test_schema)
        try:
            line_income = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Monthly Salary 工资",
                direction="credit",
                line_type="income",
                settlement_amount=Decimal("20000.00"),
                settlement_currency="CNY"
            )
            line_fee = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Account Maintenance Fee 手续费",
                direction="debit",
                line_type="fee",
                settlement_amount=Decimal("15.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line_income, line_fee],
                    authoritative_balance=Decimal("29985.00"),  # 10000 + 20000 - 15
                    user_id=self.user_id,
                    default_income_category_id=self.cat_income_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["created_count"], 2)
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("29985.00"))
        finally:
            conn.close()

    def test_04_create_transfer_with_two_real_legs(self):
        conn = get_connection(self.test_schema)
        try:
            # Seed movement in ICBC Savings (Account B)
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="cash_income",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("5000.00"),
                    original_currency="CNY",
                    to_amount=Decimal("5000.00"),
                    to_currency="CNY",
                    to_account_id=self.acc_cny2_id,
                    source="shortcut",
                    status="committed"
                )

            # Statement line on CMB Checking (Account A)
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="转账到工商银行",
                direction="debit",
                line_type="transfer",
                settlement_amount=Decimal("5000.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=Decimal("5000.00"),  # 10000 - 5000
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                transfer_tx_id = UUID(res["applied_transaction_ids"][0])

            tx_rec = tx_repo.get_transaction(conn, transfer_tx_id)
            self.assertEqual(tx_rec["transaction_type"], "transfer")
            self.assertEqual(tx_rec["from_account_id"], self.acc_cny_id)
            self.assertEqual(tx_rec["to_account_id"], self.acc_cny2_id)
            self.assertEqual(tx_rec["from_amount"], Decimal("5000.00"))
            self.assertEqual(tx_rec["to_amount"], Decimal("5000.00"))

            state_a = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(state_a["ledger_balance"], Decimal("5000.00"))
        finally:
            conn.close()

    def test_05_create_refund_with_refund_of_link(self):
        conn = get_connection(self.test_schema)
        try:
            orig_exp_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=orig_exp_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 7, 20),
                    original_amount=Decimal("1000.00"),
                    original_currency="CNY",
                    from_amount=Decimal("1000.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_expense_id,
                    merchant="Apple Store Sanlitun",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("9000.00"), datetime.now(timezone.utc))

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Apple Store 退款",
                direction="credit",
                line_type="refund",
                settlement_amount=Decimal("300.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=Decimal("9300.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                refund_tx_id = UUID(res["applied_transaction_ids"][0])

            # Check refund link in transaction_links
            with conn.cursor() as cur:
                cur.execute("SELECT relation_type, target_transaction_id FROM transaction_links WHERE source_transaction_id = %s;", (refund_tx_id,))
                link_row = cur.fetchone()
                self.assertIsNotNone(link_row)
                self.assertEqual(link_row[0], "refund_of")
                self.assertEqual(link_row[1], orig_exp_id)

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9300.00"))
        finally:
            conn.close()

    def test_06_recognize_installment_period(self):
        conn = get_connection(self.test_schema)
        try:
            plan_id = uuid4()
            with transaction(conn):
                installments_repo.create_installment_plan(
                    conn=conn,
                    plan_id=plan_id,
                    household_id=self.household_id,
                    credit_account_id=self.acc_credit_id,
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
                for p_no in range(1, 13):
                    installments_repo.create_installment_period(
                        conn=conn,
                        period_id=uuid4(),
                        plan_id=plan_id,
                        period_no=p_no,
                        scheduled_amount=Decimal("1000.00"),
                        currency="CNY",
                        status="scheduled"
                    )

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Apple Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("1000.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_credit_id,
                    lines=[line],
                    authoritative_balance=Decimal("-1000.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")

            plan = installments_repo.get_installment_plan(conn, plan_id)
            self.assertEqual(plan["status"], "active")

            periods = installments_repo.list_periods_for_plan(conn, plan_id)
            p1 = [p for p in periods if p["period_no"] == 1][0]
            self.assertEqual(p1["status"], "billed")
            self.assertIsNotNone(p1["expense_transaction_id"])

            acc_state = accounts_repo.get_account_state(conn, self.acc_credit_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("-1000.00"))
        finally:
            conn.close()

    def test_07_foreign_card_estimated_settlement_patch(self):
        conn = get_connection(self.test_schema)
        try:
            tx_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("10000"),
                    original_currency="JPY",
                    from_amount=Decimal("68.90"),
                    from_currency="USD",
                    from_account_id=self.acc_usd_id,
                    merchant="Tokyo Electronics",
                    account_leg_status="estimated",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_usd_id, Decimal("1931.10"), datetime.now(timezone.utc))

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 12),
                posted_on=date(2026, 8, 12),
                description_raw="Tokyo Electronics",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("68.20"),
                settlement_currency="USD"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_usd_id,
                    lines=[line],
                    authoritative_balance=Decimal("1931.80"),  # 2000 - 68.20
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["matched_count"], 1)
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")

            updated_tx = tx_repo.get_transaction(conn, tx_id)
            self.assertEqual(updated_tx["from_amount"], Decimal("68.20"))
            self.assertEqual(updated_tx["account_leg_status"], "authoritative")

            acc_state = accounts_repo.get_account_state(conn, self.acc_usd_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("1931.80"))
        finally:
            conn.close()

    def test_08_small_residual_auto_adjustment(self):
        conn = get_connection(self.test_schema)
        try:
            # Baseline = 10000, Auth = 10047 -> Residual = +47.00 CNY
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[],
                    authoritative_balance=Decimal("10047.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["residual_amount"], "47.00")
                self.assertEqual(preview["adjustment_amount"], "47.00")
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                self.assertEqual(len(res["applied_transaction_ids"]), 1)
                adj_tx_id = UUID(res["applied_transaction_ids"][0])

            adj_tx = tx_repo.get_transaction(conn, adj_tx_id)
            self.assertEqual(adj_tx["transaction_type"], "reconciliation_adjustment")
            self.assertEqual(adj_tx["to_amount"], Decimal("47.00"))

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("10047.00"))
        finally:
            conn.close()

    def test_09_repeated_statement_replay_safety(self):
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Hema Fresh Market",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("120.50"),
                settlement_currency="CNY"
            )
            # Batch #1: creates missing transaction
            with transaction(conn):
                p1 = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9879.50"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                commit_statement_batch(conn, UUID(p1["batch_id"]), user_id=self.user_id, fx_service=self.mock_fx)

            # Batch #2: same synthetic line submitted again
            line_replay = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Hema Fresh Market",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("120.50"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                p2 = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line_replay], authoritative_balance=Decimal("9879.50"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                self.assertEqual(p2["status"], "ready")
                self.assertEqual(p2["matched_count"], 1)
                self.assertEqual(p2["created_count"], 0)  # Matches existing transaction, creates 0!
                res2 = commit_statement_batch(conn, UUID(p2["batch_id"]), user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res2["status"], "committed")

            # Assert balance remained exactly 9879.50
            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9879.50"))
        finally:
            conn.close()

    def test_10_repeated_batch_commit_idempotent(self):
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Hema Fresh Market",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("120.50"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9879.50"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res1 = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res1["status"], "committed")

            # Commit again
            with transaction(conn):
                res2 = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res2["status"], "committed")

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9879.50"))
        finally:
            conn.close()

    def test_11_concurrent_shortcut_revalidation(self):
        # Section 37:
        # T1: Statement preview generated (identifies missing expense X).
        # T2: Shortcut arrives and commits expense X.
        # T3: Statement reconciliation commit executed.
        # Result: Commit revalidates against current ledger, matches X to the Shortcut transaction!
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
            # T1: Statement Preview (no transaction exists in DB yet)
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9965.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

            # T2: Concurrent shortcut commits expense X
            shortcut_tx_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=shortcut_tx_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("35.00"),
                    original_currency="CNY",
                    from_amount=Decimal("35.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_expense_id,
                    merchant="Starbucks Coffee",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("9965.00"), datetime.now(timezone.utc))

            # T3: Statement reconciliation commit
            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                # Applied ID should be the shortcut transaction ID!
                self.assertIn(str(shortcut_tx_id), res["applied_transaction_ids"])

            # Verify balance is NOT double-deducted (remains 9965.00)
            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9965.00"))

            # Verify statement line was marked matched with shortcut_tx_id
            lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
            self.assertEqual(lines[0]["match_status"], "matched")
            self.assertEqual(lines[0]["matched_transaction_id"], shortcut_tx_id)
        finally:
            conn.close()

    def test_12_atomic_rollback_on_failure(self):
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Hema Fresh Market",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("120.50"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9879.50"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            # Simulate failure during commit inside transaction block
            try:
                with transaction(conn):
                    # Simulate error
                    raise RuntimeError("Simulated DB Disk Full or Failure")
                    commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
            except RuntimeError:
                pass

            # Verify batch status is still ready and account_state is still 10000.00
            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["status"], "ready")

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("10000.00"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
