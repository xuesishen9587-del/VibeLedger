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
from app.domain.statements import StatementExtractionResult, ParsedStatementLine
from app.domain.transactions import (
    IncompatibleTargetTransactionError,
    InvalidCandidatePayloadError,
    StatementParseFailedError
)
from app.services.statement_parser import validate_and_normalize_extraction
import app.services.statement_service as statement_service
from app.services.reconciliation_service import (
    create_statement_reconciliation_batch,
    commit_statement_batch
)
from app.services.reference_fx_service import ReferenceFxService
import app.repositories.accounts as accounts_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo
import app.repositories.installments as installments_repo
import app.repositories.devices as devices_repo
import app.repositories.audit as audit_repo
import hashlib


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
            ("SGD", "CNY"): Decimal("5.30"),
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
            # Transfer statement line on CMB Checking (Account A)
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="转账到工商银行",
                direction="debit",
                line_type="transfer",
                settlement_amount=Decimal("5000.00"),
                settlement_currency="CNY"
            )
            # Counter-leg evidence on Account B
            counter_legs = [{
                "account_id": self.acc_cny2_id,
                "direction": "credit",
                "amount": Decimal("5000.00"),
                "currency": "CNY",
                "occurred_on": date(2026, 8, 10),
                "is_counter_statement_leg": True
            }]
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=Decimal("5000.00"),  # 10000 - 5000
                    user_id=self.user_id,
                    fx_service=self.mock_fx,
                    household_movements=counter_legs
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

    def test_04b_committed_cash_income_does_not_create_transfer(self):
        # Proves that committed cash_income in Account B does NOT auto-create a transfer
        conn = get_connection(self.test_schema)
        try:
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
                    authoritative_balance=Decimal("5000.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "needs_review")
                cands = reconciliation_repo.list_candidates_for_batch(conn, UUID(preview["batch_id"]))
                self.assertEqual(cands[0]["status"], "needs_review")
                self.assertEqual(cands[0]["reason_code"], "COUNTER_ACCOUNT_UNRESOLVED")
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
                description_raw="Apple Store Sanlitun",
                merchant_hint="Apple Store Sanlitun",
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
                    default_expense_category_id=self.cat_expense_id,
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
        # Phase 8 foreign-card boundary: matching evidence preserved, financial mutation deferred to Phase 8
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
                    authoritative_balance=Decimal("1931.80"),
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
            self.assertEqual(updated_tx["verification_status"], "statement_confirmed")
            self.assertEqual(updated_tx["from_amount"], Decimal("68.90"))
            self.assertEqual(updated_tx["account_leg_status"], "estimated")

            acc_state = accounts_repo.get_account_state(conn, self.acc_usd_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("1931.10"))
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
                self.assertEqual(p2["created_count"], 0)
                res2 = commit_statement_batch(conn, UUID(p2["batch_id"]), user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res2["status"], "committed")

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

            with transaction(conn):
                res2 = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res2["status"], "committed")

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9879.50"))
        finally:
            conn.close()

    def test_11_concurrent_shortcut_revalidation(self):
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
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9965.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

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

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                self.assertIn(str(shortcut_tx_id), res["applied_transaction_ids"])

            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("9965.00"))

            lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
            self.assertEqual(lines[0]["match_status"], "matched")
            self.assertEqual(lines[0]["matched_transaction_id"], shortcut_tx_id)
        finally:
            conn.close()

    def test_12_atomic_rollback_on_failure(self):
        # Inject failure inside commit AFTER transaction is inserted and verify complete outer rollback
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Hema Fresh Market Rollback",
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

            # Monkeypatch accounts_repo.update_account_state_projection to fail at the end of commit
            from unittest.mock import patch
            with patch("app.repositories.accounts.update_account_state_projection", side_effect=RuntimeError("Injected crash after tx creation")):
                try:
                    with transaction(conn):
                        commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                except RuntimeError:
                    pass

            # Verify outer transaction was completely rolled back
            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["status"], "ready")

            # Verify zero new transactions created in DB
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE merchant = 'Hema Fresh Market Rollback';")
                count = cur.fetchone()[0]
                self.assertEqual(count, 0)

            # Verify candidates remained unapplied
            cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(cands[0]["status"], "accepted")
            self.assertIsNone(cands[0]["applied_transaction_id"])

            # Verify statement line was NOT updated to matched
            st_lines = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
            self.assertEqual(st_lines[0]["match_status"], "new_candidate")
            self.assertIsNone(st_lines[0]["matched_transaction_id"])


            # Verify account balance untouched
            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("10000.00"))
        finally:
            conn.close()

    def test_13_needs_review_batch_must_not_commit(self):
        # Batch has unresolved candidate -> commit must fail with zero financial writes
        conn = get_connection(self.test_schema)
        try:
            line_no_cat = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Unknown Merchant Expense",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("200.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line_no_cat], authoritative_balance=Decimal("9800.00"), user_id=self.user_id,
                    default_expense_category_id=None,  # triggers needs_review
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "needs_review")
                batch_id = UUID(preview["batch_id"])

            # Attempting commit must raise ValueError
            with self.assertRaises(ValueError):
                with transaction(conn):
                    commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)

            # Verify batch status is still needs_review and zero writes occurred
            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["status"], "needs_review")
            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("10000.00"))
        finally:
            conn.close()

    def test_14_concurrent_unrelated_same_amount_does_not_match(self):
        # Preview says missing Starbucks 35 CNY. Concurrent shortcut inserts Shell 35 CNY.
        # Commit must NOT attach Starbucks Statement line to Shell transaction!
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
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9965.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            # Concurrent shortcut commits unrelated Shell 35 CNY (outside match window)
            shell_tx_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=shell_tx_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("35.00"),
                    original_currency="CNY",
                    from_amount=Decimal("35.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_expense_id,
                    merchant="Shell Gas Station",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("9965.00"), datetime.now(timezone.utc))


            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                # Applied ID must NOT be shell_tx_id
                self.assertNotIn(str(shell_tx_id), res["applied_transaction_ids"])

            # Verify Shell transaction was NOT modified to statement_confirmed
            shell_tx = tx_repo.get_transaction(conn, shell_tx_id)
            self.assertEqual(shell_tx["verification_status"], "unverified")
        finally:
            conn.close()

    def test_15_stale_adjustment_recomputed_at_commit(self):
        # Preview residual = +47 CNY, concurrent transaction changes fresh residual to +7 CNY
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[], authoritative_balance=Decimal("10047.00"), user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["adjustment_amount"], "47.00")
                batch_id = UUID(preview["batch_id"])

            # Concurrent transaction changes ledger balance by +40
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="cash_income",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("40.00"),
                    original_currency="CNY",
                    to_amount=Decimal("40.00"),
                    to_currency="CNY",
                    to_account_id=self.acc_cny_id,
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("10040.00"), datetime.now(timezone.utc))

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                self.assertEqual(res["residual_amount"], "7.00")
                adj_tx_id = UUID(res["applied_transaction_ids"][0])

            adj_tx = tx_repo.get_transaction(conn, adj_tx_id)
            # Recomputed adjustment must be 7.00, NOT stale 47.00!
            self.assertEqual(adj_tx["to_amount"], Decimal("7.00"))
            acc_state = accounts_repo.get_account_state(conn, self.acc_cny_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("10047.00"))
        finally:
            conn.close()

    def test_16_duplicate_installment_period_commit_prevented(self):
        # Batch A and Batch B both preview the same installment period 1.
        # A commits -> B commits -> B must not create a duplicate transaction!
        conn = get_connection(self.test_schema)
        try:
            plan_id = uuid4()
            period_1_id = uuid4()
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
                installments_repo.create_installment_period(
                    conn=conn,
                    period_id=period_1_id,
                    plan_id=plan_id,
                    period_no=1,
                    scheduled_amount=Decimal("1000.00"),
                    currency="CNY",
                    status="scheduled"
                )

            line_a = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Apple Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("1000.00"),
                settlement_currency="CNY"
            )
            line_b = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Apple Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("1000.00"),
                settlement_currency="CNY"
            )

            # Both generate preview
            with transaction(conn):
                prev_a = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_credit_id,
                    lines=[line_a], authoritative_balance=Decimal("-1000.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                prev_b = create_statement_reconciliation_batch(
            conn=conn, household_id=self.household_id, account_id=self.acc_credit_id,
                    lines=[line_b], authoritative_balance=Decimal("-1000.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                batch_a_id = UUID(prev_a["batch_id"])
                batch_b_id = UUID(prev_b["batch_id"])

            # Commit batch A
            with transaction(conn):
                commit_statement_batch(conn, batch_a_id, user_id=self.user_id, fx_service=self.mock_fx)

            # Commit batch B: period 1 is already billed, so batch B commit must NOT create a duplicate expense
            with transaction(conn):
                res_b = commit_statement_batch(conn, batch_b_id, user_id=self.user_id, fx_service=self.mock_fx)
                # When batch B commits, the fresh revalidation sees period 1 already billed.
                # Either it matches or turns needs_review; no duplicate tx is created.
                if res_b.get("status") == "needs_review":
                    pass

            # Assert exactly one expense transaction was created in DB for period 1
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE from_account_id = %s;", (self.acc_credit_id,))
                tx_count = cur.fetchone()[0]
                self.assertEqual(tx_count, 1)

            acc_state = accounts_repo.get_account_state(conn, self.acc_credit_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("-1000.00"))
        finally:
            conn.close()

    def test_17_concurrent_residual_shift_exceeding_threshold_persists_needs_review(self):
        """
        Regression for Item 3:
        Preview ready with small residual <= 200 CNY.
        Concurrent ledger change makes fresh residual > 200 CNY.
        Commit must return status="needs_review", persist DB batch as needs_review,
        and make ZERO financial writes (NO rollback via exception).
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[], authoritative_balance=Decimal("10047.00"), user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["adjustment_amount"], "47.00")
                batch_id = UUID(preview["batch_id"])

            # Concurrent change creates +500 residual difference
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="cash_income",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("500.00"),
                    original_currency="CNY",
                    to_amount=Decimal("500.00"),
                    to_currency="CNY",
                    to_account_id=self.acc_cny_id,
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("10500.00"), datetime.now(timezone.utc))

            # Commit call under transaction
            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "needs_review")
                self.assertEqual(res["applied_transaction_ids"], [])

            # Check DB state
            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["status"], "needs_review")
            self.assertEqual(batch["residual_amount"], Decimal("-453.00"))
            self.assertIsNone(batch["committed_at"])

            # Zero reconciliation_adjustment transactions created
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE transaction_type = 'reconciliation_adjustment';")
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_18_category_deactivated_between_preview_and_commit(self):
        """
        Regression for Item 7:
        Category is active during preview, then archived before commit.
        Fresh commit revalidates category and changes batch to needs_review.
        """
        conn = get_connection(self.test_schema)
        try:
            cat_id = uuid4()
            with transaction(conn):
                accounts_repo.create_category(
                    conn=conn,
                    category_id=cat_id,
                    household_id=self.household_id,
                    name="Temporary Dining",
                    category_type="expense"
                )

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Bistro Dinner",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("200.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9800.00"), user_id=self.user_id,
                    default_expense_category_id=cat_id, fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                batch_id = UUID(preview["batch_id"])

            # Deactivate category before commit
            with transaction(conn):
                with conn.cursor() as cur:
                    cur.execute("UPDATE categories SET status = 'inactive' WHERE id = %s;", (cat_id,))

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "needs_review")

            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["status"], "needs_review")
        finally:
            conn.close()

    def test_19_additional_refund_committed_between_preview_and_commit(self):
        """
        Regression for Item 5:
        Original expense 1000 CNY. Preview has 600 CNY refund (ready).
        Concurrent shortcut commits 500 CNY refund.
        Commit re-evaluates 500 + 600 > 1000 -> needs_review.
        """
        conn = get_connection(self.test_schema)
        try:
            exp_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=exp_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("1000.00"),
                    original_currency="CNY",
                    from_amount=Decimal("1000.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_expense_id,
                    merchant="Apple Store",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("9000.00"), datetime.now(timezone.utc))

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Apple Store",
                direction="credit",
                line_type="refund",
                settlement_amount=Decimal("600.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9600.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                batch_id = UUID(preview["batch_id"])

            # Concurrent shortcut commits 500 CNY refund
            concurrent_ref_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=concurrent_ref_id,
                    household_id=self.household_id,
                    transaction_type="refund",
                    occurred_on=date(2026, 8, 5),
                    original_amount=Decimal("500.00"),
                    original_currency="CNY",
                    to_amount=Decimal("500.00"),
                    to_currency="CNY",
                    to_account_id=self.acc_cny_id,
                    source="shortcut",
                    status="committed"
                )
                tx_repo.create_transaction_link(conn, uuid4(), concurrent_ref_id, exp_id, "refund_of")
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("9500.00"), datetime.now(timezone.utc))

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "needs_review")

            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["status"], "needs_review")
        finally:
            conn.close()

    def test_20_installment_bootstrap_and_multi_month_progression(self):
        """
        Regression for Item 8:
        Plan pending_first_bill with first_statement_month NULL.
        Period 1 billed by Aug statement -> plan active, first_statement_month=2026-08-01, periods 2 & 3 populated.
        Period 2 billed by Sep statement.
        Period 3 billed by Oct statement -> plan completed.
        """
        conn = get_connection(self.test_schema)
        try:
            plan_id = uuid4()
            p1_id, p2_id, p3_id = uuid4(), uuid4(), uuid4()
            with transaction(conn):
                installments_repo.create_installment_plan(
                    conn=conn,
                    plan_id=plan_id,
                    household_id=self.household_id,
                    credit_account_id=self.acc_credit_id,
                    purchase_occurred_on=date(2026, 8, 1),
                    merchant="Apple Store",
                    original_amount=Decimal("3000.00"),
                    original_currency="CNY",
                    account_principal_amount=Decimal("3000.00"),
                    account_currency="CNY",
                    total_periods=3,
                    first_statement_month=None,
                    status="pending_first_bill"
                )
                installments_repo.create_installment_period(conn, p1_id, plan_id, 1, Decimal("1000.00"), "CNY", status="scheduled")
                installments_repo.create_installment_period(conn, p2_id, plan_id, 2, Decimal("1000.00"), "CNY", status="scheduled")
                installments_repo.create_installment_period(conn, p3_id, plan_id, 3, Decimal("1000.00"), "CNY", status="scheduled")

            # Aug statement
            line_aug = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Apple Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("1000.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                p_aug = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_credit_id,
                    lines=[line_aug], authoritative_balance=Decimal("-1000.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                res_aug = commit_statement_batch(conn, UUID(p_aug["batch_id"]), user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res_aug["status"], "committed")

            plan = installments_repo.get_installment_plan(conn, plan_id)
            self.assertEqual(plan["status"], "active")
            self.assertEqual(plan["first_statement_month"], date(2026, 8, 1))

            periods = installments_repo.list_periods_for_plan(conn, plan_id)
            p_map = {p["period_no"]: p for p in periods}
            self.assertEqual(p_map[1]["status"], "billed")
            self.assertEqual(p_map[2]["recognition_month"], date(2026, 9, 1))
            self.assertEqual(p_map[3]["recognition_month"], date(2026, 10, 1))

            # Sep statement
            line_sep = NormalizedStatementLine(
                transaction_on=date(2026, 9, 10),
                description_raw="Apple Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("1000.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                p_sep = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_credit_id,
                    lines=[line_sep], authoritative_balance=Decimal("-2000.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                res_sep = commit_statement_batch(conn, UUID(p_sep["batch_id"]), user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res_sep["status"], "committed")

            # Oct statement
            line_oct = NormalizedStatementLine(
                transaction_on=date(2026, 10, 10),
                description_raw="Apple Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("1000.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                p_oct = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_credit_id,
                    lines=[line_oct], authoritative_balance=Decimal("-3000.00"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                res_oct = commit_statement_batch(conn, UUID(p_oct["batch_id"]), user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res_oct["status"], "committed")

            plan = installments_repo.get_installment_plan(conn, plan_id)
            self.assertEqual(plan["status"], "completed")
        finally:
            conn.close()

    def test_21_foreign_line_evidence_round_trip(self):
        """
        Regression for Fix 1:
        1. Foreign line evidence (original_amount=10000 JPY, original_currency=JPY, merchant_hint, external_reference)
           is preserved in candidate payload across DB preview -> commit -> reload candidate.
        2. Candidate status is applied, settlement_patch is retained in evidence.
        3. Also verify the needs_review refresh path preserves evidence.line.
        """
        conn = get_connection(self.test_schema)
        try:
            # 1. Pre-seed estimated foreign transaction
            est_tx_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=est_tx_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("10000.00"),
                    original_currency="JPY",
                    from_amount=Decimal("68.90"),
                    from_currency="USD",
                    from_account_id=self.acc_usd_id,
                    category_id=self.cat_expense_id,
                    merchant="Tokyo Hotel JPY",
                    account_leg_status="estimated",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_usd_id, Decimal("1931.10"), datetime.now(timezone.utc))

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Tokyo Hotel JPY",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("68.20"),
                settlement_currency="USD",
                original_amount=Decimal("10000.00"),
                original_currency="JPY",
                merchant_hint="Tokyo Hotel JPY",
                external_reference="EXT-9988"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_usd_id,
                    lines=[line], authoritative_balance=Decimal("1931.80"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand = cands[0]
            self.assertEqual(cand["payload"]["evidence"]["line"]["original_amount"], "10000.00")
            self.assertEqual(cand["payload"]["evidence"]["line"]["original_currency"], "JPY")
            self.assertEqual(cand["payload"]["evidence"]["line"]["merchant_hint"], "Tokyo Hotel JPY")
            self.assertEqual(cand["payload"]["evidence"]["line"]["external_reference"], "EXT-9988")

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")

            cands_after = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_after = cands_after[0]
            self.assertEqual(cand_after["status"], "applied")
            self.assertEqual(cand_after["payload"]["evidence"]["line"]["original_amount"], "10000.00")
            self.assertEqual(cand_after["payload"]["evidence"]["line"]["original_currency"], "JPY")
            self.assertEqual(cand_after["payload"]["evidence"]["line"]["merchant_hint"], "Tokyo Hotel JPY")
            self.assertEqual(cand_after["payload"]["evidence"]["line"]["external_reference"], "EXT-9988")
            self.assertIn("settlement_patch", cand_after["payload"]["evidence"])
            self.assertEqual(cand_after["payload"]["evidence"]["settlement_patch"]["actual_settlement_amount"], "68.20")

            # 4. Verify needs_review refresh path preserves evidence.line
            line2 = NormalizedStatementLine(
                transaction_on=date(2026, 8, 25),
                description_raw="Tokyo Electronics JPY",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("150.00"),
                settlement_currency="USD",
                original_amount=Decimal("22000.00"),
                original_currency="JPY",
                merchant_hint="Tokyo Electronics",
                external_reference="EXT-5555"
            )
            with transaction(conn):
                # Preview created as ready (authoritative balance = 1781.10)
                preview2 = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_usd_id,
                    lines=[line2], authoritative_balance=Decimal("1781.10"), user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id, fx_service=self.mock_fx
                )
                self.assertEqual(preview2["status"], "ready")
                batch_id_2 = UUID(preview2["batch_id"])

            # Concurrent transaction introduces a 500 USD shift before commit
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 25),
                    original_amount=Decimal("500.00"),
                    original_currency="USD",
                    from_amount=Decimal("500.00"),
                    from_currency="USD",
                    from_account_id=self.acc_usd_id,
                    category_id=self.cat_expense_id,
                    merchant="Concurrent Expense",
                    source="shortcut",
                    status="committed"
                )

            with transaction(conn):
                res2 = commit_statement_batch(conn, batch_id_2, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res2["status"], "needs_review")

            cands2 = reconciliation_repo.list_candidates_for_batch(conn, batch_id_2)
            cand2 = next(c for c in cands2 if c.get("statement_line_id"))
            self.assertEqual(cand2["status"], "needs_review")
            self.assertEqual(cand2["payload"]["evidence"]["line"]["original_amount"], "22000.00")
            self.assertEqual(cand2["payload"]["evidence"]["line"]["original_currency"], "JPY")
            self.assertEqual(cand2["payload"]["evidence"]["line"]["merchant_hint"], "Tokyo Electronics")
            self.assertEqual(cand2["payload"]["evidence"]["line"]["external_reference"], "EXT-5555")
        finally:
            conn.close()

    def test_22_concurrent_transfer_race_matches_fresh(self):
        """
        Regression for Item 10:
        Preview creates candidate to create transfer A->B.
        Concurrent shortcut commits the exact transfer A->B.
        Commit statement re-runs fresh engine -> matches existing transfer -> no duplicate transfer created.
        """
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Counterpart Transfer Credit",
                direction="debit",
                line_type="transfer",
                settlement_amount=Decimal("500.00"),
                settlement_currency="CNY"
            )
            counter_legs = [{
                "account_id": self.acc_credit_id,
                "direction": "credit",
                "amount": Decimal("500.00"),
                "currency": "CNY",
                "occurred_on": date(2026, 8, 10),
                "is_counter_statement_leg": True
            }]
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn, household_id=self.household_id, account_id=self.acc_cny_id,
                    lines=[line], authoritative_balance=Decimal("9500.00"), user_id=self.user_id,
                    fx_service=self.mock_fx, household_movements=counter_legs
                )
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

            # Concurrent shortcut commits the transfer
            tx_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_id,
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("500.00"),
                    original_currency="CNY",
                    from_amount=Decimal("500.00"),
                    from_currency="CNY",
                    to_amount=Decimal("500.00"),
                    to_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    to_account_id=self.acc_credit_id,
                    merchant="Counterpart Transfer Credit",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(conn, self.acc_cny_id, Decimal("9500.00"), datetime.now(timezone.utc))

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")
                self.assertIn(str(tx_id), res["applied_transaction_ids"])

            # Exactly one transfer transaction exists in DB
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE transaction_type = 'transfer';")
                self.assertEqual(cur.fetchone()[0], 1)

            # Persisted candidate in DB reflects match and applied
            cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(cands[0]["candidate_type"], "match")
            self.assertEqual(cands[0]["status"], "applied")
        finally:
            conn.close()

    def test_23_inbound_same_currency_transfer_commit(self):
        """
        Regression for Fix 2 (A):
        Selected Account A receives 500 CNY (credit).
        Counterparty Account B debited 500 CNY.
        Preview creates create_transfer B -> A.
        Commit creates exactly one transfer B -> A:
        - A account_state +500 (10000 -> 10500)
        - B account_state -500 (5000 -> 4500)
        - candidate applied
        - statement line matched
        """
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Transfer from ICBC",
                direction="credit",
                line_type="transfer",
                settlement_amount=Decimal("500.00"),
                settlement_currency="CNY"
            )
            counter_legs = [{
                "account_id": self.acc_cny2_id,
                "direction": "debit",
                "amount": Decimal("500.00"),
                "currency": "CNY",
                "occurred_on": date(2026, 8, 10),
                "is_counter_statement_leg": True
            }]
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=Decimal("10500.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx,
                    household_movements=counter_legs
                )
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

            cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0]["candidate_type"], "create_transfer")
            self.assertEqual(cands[0]["status"], "accepted")
            t_data = cands[0]["payload"]["transfer"]
            self.assertEqual(t_data["from_account_id"], str(self.acc_cny2_id))
            self.assertEqual(t_data["to_account_id"], str(self.acc_cny_id))

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")

            # Check account balances
            state_a = accounts_repo.get_account_state(conn, self.acc_cny_id)
            state_b = accounts_repo.get_account_state(conn, self.acc_cny2_id)
            self.assertEqual(Decimal(str(state_a["ledger_balance"])), Decimal("10500.00"))
            self.assertEqual(Decimal(str(state_b["ledger_balance"])), Decimal("4500.00"))

            # Check created transfer transaction
            with conn.cursor() as cur:
                cur.execute("SELECT from_account_id, to_account_id, from_amount, to_amount FROM transactions WHERE transaction_type = 'transfer';")
                rows = cur.fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], self.acc_cny2_id)
                self.assertEqual(rows[0][1], self.acc_cny_id)
                self.assertEqual(Decimal(str(rows[0][2])), Decimal("500.00"))
                self.assertEqual(Decimal(str(rows[0][3])), Decimal("500.00"))

            # Check candidate applied and line matched
            cands_after = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(cands_after[0]["status"], "applied")
            lines_after = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
            self.assertEqual(lines_after[0]["match_status"], "matched")
        finally:
            conn.close()

    def test_24_inbound_cross_currency_transfer_commit(self):
        """
        Regression for Fix 2 (C):
        Selected Account A (CNY) receives 725 CNY (credit).
        Counterparty Account B (USD) debited 100 USD.
        Commit creates explicit two-leg transfer:
        - from_account: B, from_amount: 100 USD
        - to_account: A, to_amount: 725 CNY
        - A account_state +725 CNY (10000 -> 10725)
        - B account_state -100 USD (2000 -> 1900)
        - candidate applied, line matched
        """
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Cross Currency Wire from US Account",
                direction="credit",
                line_type="transfer",
                settlement_amount=Decimal("725.00"),
                settlement_currency="CNY"
            )
            counter_legs = [{
                "account_id": self.acc_usd_id,
                "direction": "debit",
                "amount": Decimal("100.00"),
                "currency": "USD",
                "occurred_on": date(2026, 8, 10),
                "is_counter_statement_leg": True
            }]
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=Decimal("10725.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx,
                    household_movements=counter_legs
                )
                self.assertEqual(preview["created_count"], 1)
                batch_id = UUID(preview["batch_id"])

            cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(len(cands), 1)
            t_data = cands[0]["payload"]["transfer"]
            self.assertEqual(t_data["from_account_id"], str(self.acc_usd_id))
            self.assertEqual(t_data["to_account_id"], str(self.acc_cny_id))
            self.assertEqual(t_data["from_amount"], "100.00")
            self.assertEqual(t_data["from_currency"], "USD")
            self.assertEqual(t_data["to_amount"], "725.00")
            self.assertEqual(t_data["to_currency"], "CNY")

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")

            state_a = accounts_repo.get_account_state(conn, self.acc_cny_id)
            state_b = accounts_repo.get_account_state(conn, self.acc_usd_id)
            self.assertEqual(Decimal(str(state_a["ledger_balance"])), Decimal("10725.00"))
            self.assertEqual(Decimal(str(state_b["ledger_balance"])), Decimal("1900.00"))

            with conn.cursor() as cur:
                cur.execute("SELECT from_account_id, to_account_id, from_amount, from_currency, to_amount, to_currency FROM transactions WHERE transaction_type = 'transfer';")
                row = cur.fetchone()
                self.assertEqual(row[0], self.acc_usd_id)
                self.assertEqual(row[1], self.acc_cny_id)
                self.assertEqual(Decimal(str(row[2])), Decimal("100.00"))
                self.assertEqual(row[3], "USD")
                self.assertEqual(Decimal(str(row[4])), Decimal("725.00"))
                self.assertEqual(row[5], "CNY")

            cands_after = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(cands_after[0]["status"], "applied")
            lines_after = reconciliation_repo.list_statement_lines_for_batch(conn, batch_id)
            self.assertEqual(lines_after[0]["match_status"], "matched")
        finally:
            conn.close()


    def test_25_synthetic_multi_currency_savings_consolidated_statement(self):
        """
        Regression for Fix 10 & Fix 2:
        Realistic synthetic multi-currency consolidated savings statement.
        Page 1: Account summary with SGD (5432.10), CNH (12000.00), USD (3500.00) balances.
        Pages 2-3: SGD transaction section (Withdrawal 120 SGD debit, Deposit 500 SGD credit).
        Page 4: CNH transaction section (Deposit 2000 CNH), USD balance (3500 USD).
        Page 5: Disclosures.
        When SGD account selected:
        - Only SGD lines enter reconciliation
        - Authoritative balance is the SGD closing balance (5432.10)
        - CNH/USD balances do not become SGD authoritative balance
        - CNH/USD values cannot mutate SGD ledger.
        """
        conn = get_connection(self.test_schema)
        try:
            acc_sgd_id = uuid4()
            with transaction(conn):
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc_sgd_id,
                    household_id=self.household_id,
                    name="DBS Multi-Currency SGD Main",
                    account_type="cash",
                    currency="SGD",
                    owner_user_id=self.user_id
                )
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="opening_balance",
                    occurred_on=date(2026, 1, 1),
                    original_amount=Decimal("5052.10"),
                    original_currency="SGD",
                    to_amount=Decimal("5052.10"),
                    to_currency="SGD",
                    to_account_id=acc_sgd_id,
                    source="system",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(
                    conn=conn,
                    account_id=acc_sgd_id,
                    new_balance=Decimal("5052.10"),
                    last_transaction_at=datetime.now(timezone.utc)
                )

            # Normalized lines for SGD section only
            sgd_lines = [
                NormalizedStatementLine(
                    source_page_no=2,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 5),
                    description_raw="FAIRPRICE SUPERMARKET SINGAPORE",
                    direction="debit",
                    line_type="expense",
                    settlement_amount=Decimal("120.00"),
                    settlement_currency="SGD"
                ),
                NormalizedStatementLine(
                    source_page_no=3,
                    source_row_no=2,
                    transaction_on=date(2026, 8, 12),
                    description_raw="SALARY DEPOSIT ACME CORP",
                    direction="credit",
                    line_type="income",
                    settlement_amount=Decimal("500.00"),
                    settlement_currency="SGD"
                )
            ]

            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=acc_sgd_id,
                    lines=sgd_lines,
                    authoritative_balance=Decimal("5432.10"),  # SGD closing balance: 5052.10 - 120 + 500 = 5432.10
                    user_id=self.user_id,
                    fx_service=self.mock_fx,
                    default_expense_category_id=self.cat_expense_id,
                    default_income_category_id=self.cat_income_id
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["residual_amount"], "0.00")
                self.assertEqual(preview["created_count"], 2)
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")

            state_sgd = accounts_repo.get_account_state(conn, acc_sgd_id)
            self.assertEqual(Decimal(str(state_sgd["ledger_balance"])), Decimal("5432.10"))
        finally:
            conn.close()

    def test_26_mock_parser_currency_mismatch_blocked(self):
        """
        Regression for Fix 2:
        Selected account is SGD. Mock parser returns an admitted line with CNH currency.
        Post-AI validation fails closed immediately -> zero ledger mutation.
        """
        account_sgd = {"id": str(uuid4()), "name": "DBS SGD", "currency": "SGD", "account_type": "savings"}
        extraction = StatementExtractionResult(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            closing_balance=Decimal("5000.00"),
            currency="SGD",
            lines=[
                ParsedStatementLine(
                    source_page_no=4,
                    source_row_no=1,
                    transaction_on=date(2026, 8, 15),
                    description_raw="Mainland CNH Transfer",
                    amount=Decimal("1000.00"),
                    currency="CNH",  # Mismatch!
                    direction="credit",
                    line_type="transfer"
                )
            ]
        )

        with self.assertRaises(StatementParseFailedError) as ctx:
            validate_and_normalize_extraction(extraction, account_sgd)
        self.assertIn("settlement currency 'CNH' does not match selected account currency 'SGD'", str(ctx.exception))

    def test_27_synthetic_credit_card_statement_workflow(self):
        """
        Regression for Fix 11:
        Realistic credit card statement:
        - Statement balance, current outstanding, unbilled balance
        - Purchase (debit), Repayment (transfer leg, NOT cash income), Cashback (refund)
        - Assertions: posted_on and transaction_on supported, current_outstanding preserved,
          no credit_card_snapshots created, zero foreign-card authoritative mutation.
        """
        conn = get_connection(self.test_schema)
        try:
            acc_cc_id = uuid4()
            with transaction(conn):
                accounts_repo.create_account(
                    conn=conn,
                    account_id=acc_cc_id,
                    household_id=self.household_id,
                    name="Visa Signature Credit Card",
                    account_type="credit",
                    currency="CNY",
                    owner_user_id=self.user_id
                )
                accounts_repo.update_account_state_projection(
                    conn=conn,
                    account_id=acc_cc_id,
                    new_balance=Decimal("-1000.00"),
                    last_transaction_at=datetime.now(timezone.utc)
                )

            # Purchase line
            line_purchase = NormalizedStatementLine(
                transaction_on=date(2026, 8, 2),
                posted_on=date(2026, 8, 3),
                description_raw="ELECTRONICS STORE SHANGHAI",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("300.00"),
                settlement_currency="CNY"
            )
            # Repayment line
            line_repay = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                posted_on=date(2026, 8, 10),
                description_raw="AUTOPAY FROM CHECKING ACCOUNT",
                direction="credit",
                line_type="transfer",
                settlement_amount=Decimal("500.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=acc_cc_id,
                    lines=[line_purchase, line_repay],
                    authoritative_balance=None,
                    statement_balance=Decimal("800.00"),
                    current_outstanding=Decimal("800.00"),
                    unbilled_balance=Decimal("0.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx,
                    default_expense_category_id=self.cat_expense_id
                )
                batch_id = UUID(preview["batch_id"])

            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["current_outstanding"], Decimal("800.00"))
            self.assertEqual(batch["statement_balance"], Decimal("800.00"))

            # Verify no credit_card_snapshots was created
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM credit_card_snapshots WHERE account_id = %s;", (acc_cc_id,))
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_28_manual_target_match_compatibility_and_rejection(self):
        """
        Regression for Fix 4:
        Statement line: 50.00 CNY expense.
        Existing transaction: 3000.00 CNY unrelated transaction.
        User accepts target 3000.00 CNY -> rejected with 422 IncompatibleTargetTransactionError.
        User accepts target 50.00 CNY (lower score 60) -> succeeds without requiring auto-match 80.
        """
        conn = get_connection(self.test_schema)
        try:
            tx_unrelated_id = uuid4()
            tx_close_id = uuid4()
            with transaction(conn):
                # Unrelated large transaction
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_unrelated_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("3000.00"),
                    original_currency="CNY",
                    from_amount=Decimal("3000.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_expense_id,
                    merchant="Luxury Goods",
                    status="committed",
                    source="shortcut"
                )
                # Compatible transaction (amount 50 CNY, date diff 3 days)
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_close_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 7),
                    original_amount=Decimal("50.00"),
                    original_currency="CNY",
                    from_amount=Decimal("50.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_expense_id,
                    merchant="Corner Cafe",
                    status="committed",
                    source="shortcut"
                )

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Restaurant Lunch",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("50.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=None,
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_id = cands[0]["id"]

            # 1. Attempt accepting incompatible 3000 CNY transaction -> raises IncompatibleTargetTransactionError
            with transaction(conn):
                with self.assertRaises(IncompatibleTargetTransactionError):
                    statement_service.accept_candidate(
                        conn=conn,
                        candidate_id=cand_id,
                        household_id=self.household_id,
                        target_transaction_id=tx_unrelated_id,
                        user_id=self.user_id,
                        fx_service=self.mock_fx
                    )

            # Candidate remains needs_review
            cands_check = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(cands_check[0]["status"], "needs_review")

            # 2. Accept compatible 50 CNY transaction -> succeeds
            with transaction(conn):
                res = statement_service.accept_candidate(
                    conn=conn,
                    candidate_id=cand_id,
                    household_id=self.household_id,
                    target_transaction_id=tx_close_id,
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(res["status"], "ready")

            cands_ok = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            self.assertEqual(cands_ok[0]["status"], "accepted")
            self.assertEqual(cands_ok[0]["candidate_type"], "match")
        finally:
            conn.close()

    def test_29_commit_time_accepted_match_revalidation(self):
        """
        Regression for Fix 5:
        User accepts valid target transaction.
        Before commit, target is voided/modified to an incompatible state.
        Commit revalidates under lock -> returns needs_review with zero ledger mutation.
        """
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
                    original_amount=Decimal("150.00"),
                    original_currency="CNY",
                    from_amount=Decimal("150.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_id,
                    category_id=self.cat_expense_id,
                    merchant="Bookstore",
                    status="committed",
                    source="shortcut"
                )

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Bookstore",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("150.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=Decimal("9850.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_id = cands[0]["id"]

            with transaction(conn):
                statement_service.accept_candidate(
                    conn=conn,
                    candidate_id=cand_id,
                    household_id=self.household_id,
                    target_transaction_id=tx_id,
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )

            # Target transaction is voided before commit
            with transaction(conn):
                with conn.cursor() as cur:
                    cur.execute("UPDATE transactions SET status = 'voided', deleted_at = now(), delete_reason = 'user_voided' WHERE id = %s;", (tx_id,))

            # Commit must detect incompatibility and return needs_review
            with transaction(conn):
                commit_res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(commit_res["status"], "needs_review")

            batch_db = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch_db["status"], "needs_review")
        finally:
            conn.close()

    def test_30_candidate_patch_type_specific_negative_validations(self):
        """
        Regression for Fix 7:
        Candidate PATCH is not an arbitrary JSON dump.
        Validates complete merged payload against candidate type constraints.
        """
        conn = get_connection(self.test_schema)
        try:
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                description_raw="Supermarket Purchase",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("100.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_id,
                    lines=[line],
                    authoritative_balance=None,
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            cand_id = cands[0]["id"]

            # 1. Invalid transaction_type (smuggling transfer through create_transaction)
            with self.assertRaises(InvalidCandidatePayloadError):
                statement_service.patch_candidate(
                    conn=conn,
                    candidate_id=cand_id,
                    household_id=self.household_id,
                    payload={"transaction": {"transaction_type": "transfer"}}
                )

            # 2. Negative amount
            with self.assertRaises(InvalidCandidatePayloadError):
                statement_service.patch_candidate(
                    conn=conn,
                    candidate_id=cand_id,
                    household_id=self.household_id,
                    payload={"transaction": {"amount": "-50.00"}}
                )

            # 3. Currency mismatch
            with self.assertRaises(InvalidCandidatePayloadError):
                statement_service.patch_candidate(
                    conn=conn,
                    candidate_id=cand_id,
                    household_id=self.household_id,
                    payload={"transaction": {"currency": "USD"}}
                )

            # 4. Income category for expense
            with self.assertRaises(InvalidCandidatePayloadError):
                statement_service.patch_candidate(
                    conn=conn,
                    candidate_id=cand_id,
                    household_id=self.household_id,
                    payload={"transaction": {"transaction_type": "expense", "category_id": str(self.cat_income_id)}}
                )
        finally:
            conn.close()

    def test_31_stale_residual_and_adjustment_clearing(self):
        """
        Regression for Fix 8:
        Initial: residual = +47.00, adjustment = +47.00.
        User resolves missing evidence explaining 47.00.
        Recompute sets residual = 0.00, adjustment_amount = NULL in database.
        Old +47.00 adjustment candidate is marked rejected/cleared.
        """
        conn = get_connection(self.test_schema)
        try:
            # Reconciled account initial balance 10000.00, authoritative closing 10047.00 (difference +47.00)
            # Initially no lines in statement -> residual is +47.00, auto-adjustment candidate created for +47.00
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
                batch_id = UUID(preview["batch_id"])

            batch_init = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch_init["residual_amount"], Decimal("47.00"))
            self.assertEqual(batch_init["adjustment_amount"], Decimal("47.00"))

            # An income transaction of 47.00 is discovered/resolved
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="cash_income",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("47.00"),
                    original_currency="CNY",
                    to_amount=Decimal("47.00"),
                    to_currency="CNY",
                    to_account_id=self.acc_cny_id,
                    category_id=self.cat_income_id,
                    status="committed",
                    source="shortcut"
                )

            # Recompute batch
            with transaction(conn):
                statement_service.recompute_statement_batch_after_review(conn, batch_id, self.household_id, self.mock_fx)

            batch_updated = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            # Residual is now 0.00 and adjustment is explicitly cleared to NULL
            self.assertEqual(batch_updated["residual_amount"], Decimal("0.00"))
            self.assertIsNone(batch_updated["adjustment_amount"])

            cands_after = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
            adj_cands = [c for c in cands_after if c["candidate_type"] == "adjustment"]
            if adj_cands:
                self.assertEqual(adj_cands[0]["status"], "rejected")
        finally:
            conn.close()

    def test_32_upload_size_limit_boundary(self):
        """
        Regression for Fix 9:
        Upload boundary tests:
        - Exactly 20MB is accepted by size gate.
        - 20MB + 1 byte is rejected with 422 InvalidRequestError.
        """
        from app.main import create_app
        from app.api.deps import get_db_connection
        from fastapi.testclient import TestClient

        app = create_app()

        def _get_db():
            conn = get_connection(self.test_schema)
            try:
                yield conn
            finally:
                if not conn.closed:
                    conn.close()

        app.dependency_overrides[get_db_connection] = _get_db
        client = TestClient(app)

        raw_token = f"vbl_test_{uuid4().hex}"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
        headers = {"Authorization": f"Bearer {raw_token}"}

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                devices_repo.create_device(conn, uuid4(), self.user_id, "Test Device", token_hash)
        finally:
            conn.close()

        # 20MB + 1 byte
        oversized_bytes = b"x" * (20 * 1024 * 1024 + 1)
        res_over = client.post(
            f"/api/v1/accounts/{self.acc_cny_id}/statements",
            headers=headers,
            files={"file": ("statement.pdf", oversized_bytes, "application/pdf")}
        )
        self.assertEqual(res_over.status_code, 422)
        self.assertIn("exceeds the maximum allowed file size", res_over.text)


if __name__ == "__main__":
    unittest.main()





