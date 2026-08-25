import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional, Dict
from uuid import UUID, uuid4
from unittest.mock import patch

from app.db import get_connection, transaction
from tests.support.db_helper import BaseDbTestCase
from app.domain.reconciliation.models import NormalizedStatementLine
from app.domain.installments import calculate_installment_schedule
from app.domain.transactions import InvalidBatchStateError
from app.services.reconciliation_service import (
    create_statement_reconciliation_batch,
    commit_statement_batch
)
from app.services.reference_fx_service import ReferenceFxService
import app.repositories.accounts as accounts_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo
import app.repositories.installments as installments_repo
import app.repositories.credit_cards as credit_cards_repo
import app.repositories.audit as audit_repo


class FixedMockFx(ReferenceFxService):
    def __init__(self):
        super().__init__()
        self.rates = {
            ("JPY", "USD"): Decimal("0.006820000000"),
            ("USD", "CNY"): Decimal("7.250000000000"),
            ("JPY", "CNY"): Decimal("0.049445000000"),
            ("CNY", "CNY"): Decimal("1.000000000000"),
            ("USD", "USD"): Decimal("1.000000000000"),
        }

    def get_rate(self, base_currency: str, target_currency: str, as_of: Optional[date] = None) -> Optional[Decimal]:
        if base_currency == target_currency:
            return Decimal("1.000000000000")
        return self.rates.get((base_currency, target_currency))


from fastapi.testclient import TestClient
from app.main import create_app
from app.api.deps import get_db_connection
import app.repositories.devices as devices_repo


class TestCreditCardPhase8Db(BaseDbTestCase):
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

    def setUp(self):
        super().setUp()
        self.mock_fx = FixedMockFx()
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                self.household_id = uuid4()
                accounts_repo.create_household(conn, self.household_id, "Phase 8 Test Household", date(2026, 1, 1), "CNY")

                self.user_id = uuid4()
                accounts_repo.create_user(conn, self.user_id, f"auth|{self.user_id}", "CC User", "cc_user@test.com", "CNY")
                accounts_repo.add_user_to_household(conn, self.household_id, self.user_id, role="owner")

                # CNY Savings Account
                self.acc_checking_id = uuid4()
                accounts_repo.create_account(
                    conn, self.acc_checking_id, self.household_id, "Checking", "savings", "CNY", self.user_id
                )
                accounts_repo.update_account_state_projection(
                    conn, self.acc_checking_id, Decimal("50000.00"), datetime.now(timezone.utc)
                )

                # USD Credit Card Account
                self.acc_usd_credit_id = uuid4()
                accounts_repo.create_account(
                    conn, self.acc_usd_credit_id, self.household_id, "USD Visa Card", "credit", "USD", self.user_id
                )

                # CNY Credit Card Account
                self.acc_cny_credit_id = uuid4()
                accounts_repo.create_account(
                    conn, self.acc_cny_credit_id, self.household_id, "CNY Credit Card", "credit", "CNY", self.user_id
                )

                self.cat_expense_id = uuid4()
                accounts_repo.create_category(conn, self.cat_expense_id, self.household_id, "General Expense", "expense")
        finally:
            conn.close()

    def test_01_foreign_card_settlement_and_reporting_fx_freeze(self):
        conn = get_connection(self.test_schema)
        try:
            # 1. Create shortcut transaction: original 10000 JPY, estimated from_amount 68.90 USD on 2026-08-10
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
                    from_account_id=self.acc_usd_credit_id,
                    merchant="Tokyo Electronics",
                    account_leg_status="estimated",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(
                    conn, self.acc_usd_credit_id, Decimal("-68.90"), datetime.now(timezone.utc)
                )

            # 2. Statement line arrives with authoritative settlement 68.20 USD posted on 2026-08-12
            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
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
                    account_id=self.acc_usd_credit_id,
                    lines=[line],
                    authoritative_balance=Decimal("-68.20"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["matched_count"], 1)
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(res["status"], "committed")

            # 3. Assertions:
            updated_tx = tx_repo.get_transaction(conn, tx_id)
            self.assertEqual(updated_tx["verification_status"], "statement_confirmed")
            self.assertEqual(updated_tx["from_amount"], Decimal("68.20"))
            self.assertEqual(updated_tx["account_leg_status"], "authoritative")
            self.assertEqual(updated_tx["posted_on"], date(2026, 8, 12))
            self.assertIsNotNone(updated_tx["reporting_fx_locked_at"])
            # 68.20 USD * 7.25 = 494.45 CNY
            self.assertEqual(updated_tx["reporting_amount"], Decimal("494.45"))
            self.assertEqual(updated_tx["reporting_currency"], "CNY")
            self.assertEqual(updated_tx["reporting_fx_rate"], Decimal("7.250000000000"))

            # Account projection delta applied: -68.90 + (+0.70) = -68.20 USD
            acc_state = accounts_repo.get_account_state(conn, self.acc_usd_credit_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("-68.20"))

            # Audit event logged with action = "reconcile"
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, entity_type, before_data, after_data FROM audit_events WHERE entity_id = %s;",
                    (tx_id,)
                )
                audit_row = cur.fetchone()
                self.assertIsNotNone(audit_row)
                self.assertEqual(audit_row[0], "reconcile")
                self.assertEqual(audit_row[1], "transaction")
                self.assertEqual(Decimal(str(audit_row[2]["from_amount"])), Decimal("68.90"))
                self.assertEqual(Decimal(str(audit_row[3]["from_amount"])), Decimal("68.20"))
                self.assertEqual(audit_row[3]["account_leg_status"], "authoritative")
        finally:
            conn.close()

    def test_02_reporting_fx_immutability(self):
        conn = get_connection(self.test_schema)
        try:
            tx_id = uuid4()
            locked_dt = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("100.00"),
                    original_currency="USD",
                    from_amount=Decimal("100.00"),
                    from_currency="USD",
                    from_account_id=self.acc_usd_credit_id,
                    merchant="USD Merchant",
                    reporting_amount=Decimal("725.00"),
                    reporting_currency="CNY",
                    reporting_fx_rate=Decimal("7.250000000000"),
                    reporting_fx_locked_at=locked_dt,
                    status="committed"
                )
                accounts_repo.update_account_state_projection(
                    conn, self.acc_usd_credit_id, Decimal("-100.00"), datetime.now(timezone.utc)
                )

            line = NormalizedStatementLine(
                transaction_on=date(2026, 8, 1),
                posted_on=date(2026, 8, 2),
                description_raw="USD Merchant",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("100.00"),
                settlement_currency="USD"
            )

            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_usd_credit_id,
                    lines=[line],
                    authoritative_balance=Decimal("-100.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            with transaction(conn):
                commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)

            tx = tx_repo.get_transaction(conn, tx_id)
            self.assertEqual(tx["reporting_amount"], Decimal("725.00"))
            self.assertEqual(tx["reporting_currency"], "CNY")
            self.assertEqual(tx["reporting_fx_rate"], Decimal("7.250000000000"))
            self.assertEqual(tx["reporting_fx_locked_at"], locked_dt)
        finally:
            conn.close()

    def test_03_credit_card_statement_snapshot_persistence_and_repayment_flow(self):
        conn = get_connection(self.test_schema)
        try:
            # 1. Prepare credit card statement batch with snapshot metadata payload
            snap_payload = {
                "credit_card_snapshot": {
                    "statement_date": "2026-07-31",
                    "statement_period_start": "2026-07-01",
                    "statement_period_end": "2026-07-31",
                    "statement_balance": "5000.00",
                    "remaining_statement_due": "5000.00",
                    "unbilled_balance": "1500.00",
                    "current_outstanding": "6500.00",
                    "currency": "CNY"
                }
            }

            line = NormalizedStatementLine(
                transaction_on=date(2026, 7, 15),
                posted_on=date(2026, 7, 16),
                description_raw="Restaurant",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("5000.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=[line],
                    statement_balance=Decimal("5000.00"),
                    current_outstanding=Decimal("6500.00"),
                    unbilled_balance=Decimal("1500.00"),
                    period_start=date(2026, 7, 1),
                    period_end=date(2026, 7, 31),
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx,
                    credit_card_snapshot_payload=snap_payload
                )
                batch_id = UUID(preview["batch_id"])
                # Verify snapshot candidate exists
                snap_cands = [c for c in preview["candidates"] if c["candidate_type"] == "snapshot"]
                self.assertEqual(len(snap_cands), 1)
                self.assertEqual(snap_cands[0]["status"], "accepted")

            # 2. Commit batch
            with transaction(conn):
                commit_res = commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(commit_res["status"], "committed")

            # 3. Check snapshot was persisted in credit_card_snapshots
            snapshot = credit_cards_repo.get_credit_card_snapshot_by_batch_id(conn, batch_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["statement_balance"], Decimal("5000.00"))
            self.assertEqual(snapshot["remaining_statement_due"], Decimal("5000.00"))
            self.assertEqual(snapshot["unbilled_balance"], Decimal("1500.00"))
            self.assertEqual(snapshot["current_outstanding"], Decimal("6500.00"))

            # 4. Check initial credit card state via repo
            state = credit_cards_repo.get_current_credit_card_state(conn, self.acc_cny_credit_id, self.household_id)
            self.assertEqual(state["statement_balance"], Decimal("5000.00"))
            self.assertEqual(state["remaining_statement_due"], Decimal("5000.00"))
            self.assertEqual(state["unbilled_balance"], Decimal("1500.00"))
            self.assertEqual(state["current_outstanding"], Decimal("6500.00"))

            # 5. Post-statement repayment: transfer 2000.00 CNY from checking to credit card on 2026-08-05
            with transaction(conn):
                rep_tx_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=rep_tx_id,
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 5),
                    original_amount=Decimal("2000.00"),
                    original_currency="CNY",
                    from_amount=Decimal("2000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("2000.00"),
                    to_currency="CNY",
                    from_account_id=self.acc_checking_id,
                    to_account_id=self.acc_cny_credit_id,
                    status="committed"
                )

            # 6. Verify dynamic deduction from state
            state_after = credit_cards_repo.get_current_credit_card_state(conn, self.acc_cny_credit_id, self.household_id)
            self.assertEqual(state_after["statement_balance"], Decimal("5000.00"))
            self.assertEqual(state_after["remaining_statement_due"], Decimal("3000.00")) # 5000 - 2000
            self.assertEqual(state_after["unbilled_balance"], Decimal("1500.00"))
            self.assertEqual(state_after["current_outstanding"], Decimal("4500.00")) # 6500 - 2000

            # 7. Additional overpayment: transfer 4000.00 CNY (total repaid = 6000.00)
            with transaction(conn):
                rep2_tx_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=rep2_tx_id,
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("4000.00"),
                    original_currency="CNY",
                    from_amount=Decimal("4000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("4000.00"),
                    to_currency="CNY",
                    from_account_id=self.acc_checking_id,
                    to_account_id=self.acc_cny_credit_id,
                    status="committed"
                )

            state_overpaid = credit_cards_repo.get_current_credit_card_state(conn, self.acc_cny_credit_id, self.household_id)
            self.assertEqual(state_overpaid["remaining_statement_due"], Decimal("0.00")) # Floored at 0
            self.assertEqual(state_overpaid["current_outstanding"], Decimal("500.00")) # 6500 - 6000 = 500
        finally:
            conn.close()

    def test_04_installment_plan_lifecycle_full_completion(self):
        conn = get_connection(self.test_schema)
        try:
            # 1. Create a 3-period installment plan
            plan_id = uuid4()
            with transaction(conn):
                installments_repo.create_installment_plan(
                    conn=conn,
                    plan_id=plan_id,
                    household_id=self.household_id,
                    credit_account_id=self.acc_cny_credit_id,
                    purchase_occurred_on=date(2026, 5, 10),
                    original_amount=Decimal("300.00"),
                    original_currency="CNY",
                    account_currency="CNY",
                    total_periods=3,
                    merchant="Apple Store",
                    status="pending_first_bill"
                )
                p1_id = uuid4()
                p2_id = uuid4()
                p3_id = uuid4()
                installments_repo.create_installment_period(conn, p1_id, plan_id, 1, Decimal("100.00"), "CNY", status="scheduled")
                installments_repo.create_installment_period(conn, p2_id, plan_id, 2, Decimal("100.00"), "CNY", status="scheduled")
                installments_repo.create_installment_period(conn, p3_id, plan_id, 3, Decimal("100.00"), "CNY", status="scheduled")

            # 2. Month 1 statement arrives with Period 1
            line1 = NormalizedStatementLine(
                transaction_on=date(2026, 6, 1),
                posted_on=date(2026, 6, 2),
                description_raw="Apple Store (1/3)",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("100.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                prev1 = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=[line1],
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                batch1_id = UUID(prev1["batch_id"])

            with transaction(conn):
                commit_statement_batch(conn, batch1_id, user_id=self.user_id, fx_service=self.mock_fx)

            plan = installments_repo.get_installment_plan(conn, plan_id)
            self.assertEqual(plan["status"], "active")
            self.assertEqual(plan["first_statement_month"], date(2026, 6, 1))

            # Scheduled periods recognition months are populated
            periods = installments_repo.list_periods_for_plan(conn, plan_id)
            self.assertEqual(periods[0]["status"], "billed")
            self.assertEqual(periods[1]["status"], "scheduled")
            self.assertEqual(periods[1]["recognition_month"], date(2026, 7, 1))
            self.assertEqual(periods[2]["status"], "scheduled")
            self.assertEqual(periods[2]["recognition_month"], date(2026, 8, 1))

            # 3. Month 2 statement arrives with Period 2
            line2 = NormalizedStatementLine(
                transaction_on=date(2026, 7, 1),
                posted_on=date(2026, 7, 2),
                description_raw="Apple Store (2/3)",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("100.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                prev2 = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=[line2],
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                batch2_id = UUID(prev2["batch_id"])

            with transaction(conn):
                commit_statement_batch(conn, batch2_id, user_id=self.user_id, fx_service=self.mock_fx)

            plan = installments_repo.get_installment_plan(conn, plan_id)
            self.assertEqual(plan["status"], "active")

            # 4. Month 3 statement arrives with Period 3 (final period)
            line3 = NormalizedStatementLine(
                transaction_on=date(2026, 8, 1),
                posted_on=date(2026, 8, 2),
                description_raw="Apple Store (3/3)",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("100.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                prev3 = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=[line3],
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                batch3_id = UUID(prev3["batch_id"])

            with transaction(conn):
                commit_statement_batch(conn, batch3_id, user_id=self.user_id, fx_service=self.mock_fx)

            # Plan transitions to completed!
            plan = installments_repo.get_installment_plan(conn, plan_id)
            self.assertEqual(plan["status"], "completed")

            periods = installments_repo.list_periods_for_plan(conn, plan_id)
            for p in periods:
                self.assertEqual(p["status"], "billed")
                self.assertIsNotNone(p["expense_transaction_id"])
        finally:
            conn.close()

    def test_05_atomic_rollback_on_credit_card_batch_failure(self):
        conn = get_connection(self.test_schema)
        try:
            snap_payload = {
                "credit_card_snapshot": {
                    "statement_date": "2026-07-31",
                    "statement_period_start": "2026-07-01",
                    "statement_period_end": "2026-07-31",
                    "statement_balance": "3000.00",
                    "remaining_statement_due": "3000.00",
                    "unbilled_balance": "500.00",
                    "current_outstanding": "3500.00",
                    "currency": "CNY"
                }
            }

            line = NormalizedStatementLine(
                transaction_on=date(2026, 7, 10),
                posted_on=date(2026, 7, 11),
                description_raw="Supermarket Rollback",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("3000.00"),
                settlement_currency="CNY"
            )

            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=[line],
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx,
                    credit_card_snapshot_payload=snap_payload
                )
                batch_id = UUID(preview["batch_id"])

            with patch("app.repositories.accounts.update_account_state_projection", side_effect=RuntimeError("Injected crash")):
                try:
                    with transaction(conn):
                        commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)
                except RuntimeError:
                    pass

            # Verify batch status is still ready
            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["status"], "ready")

            # Verify zero snapshot rows created
            snapshot = credit_cards_repo.get_credit_card_snapshot_by_batch_id(conn, batch_id)
            self.assertIsNone(snapshot)

            # Verify zero transactions created
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE merchant = 'Supermarket Rollback';")
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_06_credit_card_state_api_endpoint(self):
        import hashlib
        conn = get_connection(self.test_schema)
        try:
            device_id = uuid4()
            raw_token = f"vbl_test_{uuid4().hex}"
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
            with transaction(conn):
                devices_repo.create_device(conn, device_id, self.user_id, "iPhone CC", token_hash)

            headers = {"Authorization": f"Bearer {raw_token}"}

            # 1. Before snapshot, latest_snapshot is None
            resp = self.client.get(f"/api/v1/credit-cards/{self.acc_cny_credit_id}/state", headers=headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["account_id"], str(self.acc_cny_credit_id))
            self.assertEqual(data["currency"], "CNY")
            self.assertIsNone(data["latest_snapshot"])

            # 2. Add a credit card snapshot
            with transaction(conn):
                credit_cards_repo.create_credit_card_snapshot(
                    conn=conn,
                    snapshot_id=uuid4(),
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    as_of=datetime(2026, 7, 31, 0, 0, 0, tzinfo=timezone.utc),
                    statement_period_start=date(2026, 7, 1),
                    statement_period_end=date(2026, 7, 31),
                    statement_balance=Decimal("4000.00"),
                    remaining_statement_due=Decimal("4000.00"),
                    unbilled_balance=Decimal("1000.00"),
                    current_outstanding=Decimal("5000.00"),
                    currency="CNY",
                    source="statement"
                )

            # 3. GET endpoint returns snapshot
            resp2 = self.client.get(f"/api/v1/credit-cards/{self.acc_cny_credit_id}/state", headers=headers)
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.json()
            self.assertIsNotNone(data2["latest_snapshot"])
            self.assertEqual(data2["latest_snapshot"]["statement_balance"], "4000.00")
            self.assertEqual(data2["latest_snapshot"]["remaining_statement_due"], "4000.00")
            self.assertEqual(data2["latest_snapshot"]["unbilled_balance"], "1000.00")
            self.assertEqual(data2["latest_snapshot"]["current_outstanding"], "5000.00")

            # 4. Post-statement repayment transfer of 1500.00 CNY
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 5),
                    original_amount=Decimal("1500.00"),
                    original_currency="CNY",
                    from_amount=Decimal("1500.00"),
                    from_currency="CNY",
                    to_amount=Decimal("1500.00"),
                    to_currency="CNY",
                    from_account_id=self.acc_checking_id,
                    to_account_id=self.acc_cny_credit_id,
                    status="committed"
                )

            # 5. GET endpoint reflects dynamic repayment deduction
            resp3 = self.client.get(f"/api/v1/credit-cards/{self.acc_cny_credit_id}/state", headers=headers)
            self.assertEqual(resp3.status_code, 200)
            data3 = resp3.json()
            self.assertEqual(data3["latest_snapshot"]["statement_balance"], "4000.00")
            self.assertEqual(data3["latest_snapshot"]["remaining_statement_due"], "2500.00") # 4000 - 1500
            self.assertEqual(data3["latest_snapshot"]["unbilled_balance"], "1000.00")
            self.assertEqual(data3["latest_snapshot"]["current_outstanding"], "3500.00") # 5000 - 1500
        finally:
            conn.close()

    def test_07_post_statement_repayment_cutoff_with_as_of(self):
        """
        Regression for Fix 1:
        statement_period_end = 2026-07-30
        statement_date/as_of = 2026-07-31
        Repayment A: occurred_on = 2026-07-31 (on as_of date) -> already reflected in snapshot -> MUST NOT be subtracted.
        Repayment B: occurred_on = 2026-08-01 (after as_of date) -> post-statement repayment -> MUST reduce remaining due.
        statement_balance remains unchanged.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                snap_id = uuid4()
                credit_cards_repo.create_credit_card_snapshot(
                    conn=conn,
                    snapshot_id=snap_id,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    as_of=datetime(2026, 7, 31, 18, 0, 0, tzinfo=timezone.utc),
                    statement_period_start=date(2026, 7, 1),
                    statement_period_end=date(2026, 7, 30),
                    statement_balance=Decimal("5000.00"),
                    remaining_statement_due=Decimal("5000.00"),
                    unbilled_balance=Decimal("1000.00"),
                    current_outstanding=Decimal("6000.00"),
                    currency="CNY",
                    source="statement"
                )

                # Repayment A: occurred on 2026-07-31 (same day as statement/snapshot issuance)
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 7, 31),
                    original_amount=Decimal("1000.00"),
                    original_currency="CNY",
                    from_amount=Decimal("1000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("1000.00"),
                    to_currency="CNY",
                    from_account_id=self.acc_checking_id,
                    to_account_id=self.acc_cny_credit_id,
                    status="committed"
                )

            # State check after Repayment A: occurred_on (2026-07-31) <= as_of (2026-07-31), so not subtracted
            state_a = credit_cards_repo.get_current_credit_card_state(conn, self.acc_cny_credit_id, self.household_id)
            self.assertEqual(state_a["statement_balance"], Decimal("5000.00"))
            self.assertEqual(state_a["remaining_statement_due"], Decimal("5000.00"))
            self.assertEqual(state_a["unbilled_balance"], Decimal("1000.00"))
            self.assertEqual(state_a["current_outstanding"], Decimal("6000.00"))

            # Repayment B: occurred on 2026-08-01 (post-statement)
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 1),
                    original_amount=Decimal("1500.00"),
                    original_currency="CNY",
                    from_amount=Decimal("1500.00"),
                    from_currency="CNY",
                    to_amount=Decimal("1500.00"),
                    to_currency="CNY",
                    from_account_id=self.acc_checking_id,
                    to_account_id=self.acc_cny_credit_id,
                    status="committed"
                )

            # State check after Repayment B: occurred_on (2026-08-01) > as_of (2026-07-31), so subtracted
            state_b = credit_cards_repo.get_current_credit_card_state(conn, self.acc_cny_credit_id, self.household_id)
            self.assertEqual(state_b["statement_balance"], Decimal("5000.00")) # statement_balance unchanged
            self.assertEqual(state_b["remaining_statement_due"], Decimal("3500.00")) # 5000 - 1500
            self.assertEqual(state_b["unbilled_balance"], Decimal("1000.00"))
            self.assertEqual(state_b["current_outstanding"], Decimal("4500.00")) # 6000 - 1500
        finally:
            conn.close()

    def test_08_reporting_fx_unavailable_fails_closed_and_rolls_back(self):
        """
        Regression for Fix 2:
        A. USD transaction, reporting currency CNY, FX service returns None:
           -> commit fails (ValueError)
           -> entire transaction rolls back
           -> transaction reporting fields remain unchanged
           -> account_state unchanged
           -> snapshot not created
           -> batch remains uncommitted (status 'ready')
        B. Same currency CNY -> CNY: rate exactly 1 succeeds.
        """
        conn = get_connection(self.test_schema)
        try:
            # Setup Case A: USD transaction
            tx_usd_id = uuid4()
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=tx_usd_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("100.00"),
                    original_currency="USD",
                    from_amount=Decimal("100.00"),
                    from_currency="USD",
                    from_account_id=self.acc_usd_credit_id,
                    merchant="US Tech",
                    account_leg_status="estimated",
                    source="shortcut",
                    status="committed"
                )
                accounts_repo.update_account_state_projection(
                    conn, self.acc_usd_credit_id, Decimal("-100.00"), datetime.now(timezone.utc)
                )

            line_usd = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                posted_on=date(2026, 8, 11),
                description_raw="US Tech",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("100.00"),
                settlement_currency="USD"
            )

            with transaction(conn):
                preview = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_usd_credit_id,
                    lines=[line_usd],
                    authoritative_balance=Decimal("-100.00"),
                    user_id=self.user_id,
                    fx_service=self.mock_fx
                )
                batch_id = UUID(preview["batch_id"])

            # Mock FX service that returns None for USD -> CNY
            class FailingFx(ReferenceFxService):
                def get_rate(self, base: str, target: str, as_of: Optional[date] = None) -> Optional[Decimal]:
                    if base == target:
                        return Decimal("1.000000000000")
                    return None

            failing_fx = FailingFx()

            # Attempting commit must fail closed and roll back
            with self.assertRaises(ValueError):
                with transaction(conn):
                    commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=failing_fx)

            # Assert rollback: transaction reporting fields unchanged
            tx_after = tx_repo.get_transaction(conn, tx_usd_id)
            self.assertIsNone(tx_after.get("reporting_fx_rate"))
            self.assertIsNone(tx_after.get("reporting_amount"))
            self.assertIsNone(tx_after.get("reporting_fx_locked_at"))
            self.assertEqual(tx_after.get("account_leg_status"), "estimated")

            # Account state projection unchanged (-100.00)
            acc_state = accounts_repo.get_account_state(conn, self.acc_usd_credit_id)
            self.assertEqual(acc_state["ledger_balance"], Decimal("-100.00"))

            # Snapshot not created
            snap = credit_cards_repo.get_credit_card_snapshot_by_batch_id(conn, batch_id)
            self.assertIsNone(snap)

            # Batch remains uncommitted
            batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
            self.assertEqual(batch["status"], "ready")

            # Setup Case B: Same currency CNY -> CNY succeeds with rate 1.0
            line_cny = NormalizedStatementLine(
                transaction_on=date(2026, 8, 10),
                posted_on=date(2026, 8, 11),
                description_raw="CNY Store",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("50.00"),
                settlement_currency="CNY"
            )
            with transaction(conn):
                preview_cny = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=[line_cny],
                    authoritative_balance=Decimal("-50.00"),
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=failing_fx # Same currency CNY->CNY succeeds even with failing_fx
                )
                batch_cny_id = UUID(preview_cny["batch_id"])

            with transaction(conn):
                commit_res = commit_statement_batch(conn, batch_cny_id, user_id=self.user_id, fx_service=failing_fx)
                self.assertEqual(commit_res["status"], "committed")
        finally:
            conn.close()

    def test_09_statement_balance_cycle_check_and_contradiction_handling(self):
        """
        Regression for Fix 3:
        purchases 800 + fee 20 - refund 100 + installment 280 = statement_balance 1000 -> PASS.
        Post-statement repayment 600 -> statement_balance remains 1000, remaining_statement_due decreases to 400.
        Contradiction: statement_balance 1100 -> needs_review, no snapshot/ledger mutation.
        """
        conn = get_connection(self.test_schema)
        try:
            # 1. Setup installment plan for the 280 portion
            inst_plan_id = uuid4()
            p1_id = uuid4()
            with transaction(conn):
                installments_repo.create_installment_plan(
                    conn=conn,
                    plan_id=inst_plan_id,
                    household_id=self.household_id,
                    credit_account_id=self.acc_cny_credit_id,
                    purchase_occurred_on=date(2026, 6, 15),
                    original_amount=Decimal("840.00"),
                    original_currency="CNY",
                    account_currency="CNY",
                    total_periods=3,
                    merchant="Furniture Mall",
                    status="pending_first_bill"
                )
                installments_repo.create_installment_period(conn, p1_id, inst_plan_id, 1, Decimal("280.00"), "CNY", status="scheduled")
                installments_repo.create_installment_period(conn, uuid4(), inst_plan_id, 2, Decimal("280.00"), "CNY", status="scheduled")
                installments_repo.create_installment_period(conn, uuid4(), inst_plan_id, 3, Decimal("280.00"), "CNY", status="scheduled")

                # Setup prior expense for refund of 100
                orig_exp_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=orig_exp_id,
                    household_id=self.household_id,
                    transaction_type="expense",
                    occurred_on=date(2026, 6, 20),
                    original_amount=Decimal("200.00"),
                    original_currency="CNY",
                    from_amount=Decimal("200.00"),
                    from_currency="CNY",
                    from_account_id=self.acc_cny_credit_id,
                    category_id=self.cat_expense_id,
                    merchant="Shoe Store",
                    status="committed"
                )

            lines = [
                NormalizedStatementLine(
                    transaction_on=date(2026, 7, 5),
                    description_raw="Supermarket purchase",
                    direction="debit",
                    line_type="expense",
                    settlement_amount=Decimal("800.00"),
                    settlement_currency="CNY"
                ),
                NormalizedStatementLine(
                    transaction_on=date(2026, 7, 6),
                    description_raw="Late fee",
                    direction="debit",
                    line_type="fee",
                    settlement_amount=Decimal("20.00"),
                    settlement_currency="CNY"
                ),
                NormalizedStatementLine(
                    transaction_on=date(2026, 7, 10),
                    description_raw="Shoe Store Refund",
                    merchant_hint="Shoe Store",
                    direction="credit",
                    line_type="refund",
                    settlement_amount=Decimal("100.00"),
                    settlement_currency="CNY"
                ),
                NormalizedStatementLine(
                    transaction_on=date(2026, 7, 15),
                    description_raw="Furniture Mall (1/3)",
                    direction="debit",
                    line_type="expense",
                    settlement_amount=Decimal("280.00"),
                    settlement_currency="CNY"
                )
            ]

            # A. Valid cycle: statement_balance = 1000.00 (800 + 20 - 100 + 280 = 1000.00)
            snap_payload = {
                "credit_card_snapshot": {
                    "statement_date": "2026-07-31",
                    "statement_period_start": "2026-07-01",
                    "statement_period_end": "2026-07-31",
                    "statement_balance": "1000.00",
                    "remaining_statement_due": "1000.00",
                    "unbilled_balance": "560.00",
                    "current_outstanding": "1560.00",
                    "currency": "CNY"
                }
            }

            with transaction(conn):
                preview_pass = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=lines,
                    statement_balance=Decimal("1000.00"),
                    current_outstanding=Decimal("1560.00"),
                    unbilled_balance=Decimal("560.00"),
                    period_start=date(2026, 7, 1),
                    period_end=date(2026, 7, 31),
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx,
                    credit_card_snapshot_payload=snap_payload
                )
                self.assertEqual(preview_pass["status"], "ready")
                batch_pass_id = UUID(preview_pass["batch_id"])

            with transaction(conn):
                commit_res = commit_statement_batch(conn, batch_pass_id, user_id=self.user_id, fx_service=self.mock_fx)
                self.assertEqual(commit_res["status"], "committed")

            state_initial = credit_cards_repo.get_current_credit_card_state(conn, self.acc_cny_credit_id, self.household_id)
            self.assertEqual(state_initial["statement_balance"], Decimal("1000.00"))
            self.assertEqual(state_initial["remaining_statement_due"], Decimal("1000.00"))

            # Post-statement repayment of 600.00 CNY on 2026-08-05
            with transaction(conn):
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 5),
                    original_amount=Decimal("600.00"),
                    original_currency="CNY",
                    from_amount=Decimal("600.00"),
                    from_currency="CNY",
                    to_amount=Decimal("600.00"),
                    to_currency="CNY",
                    from_account_id=self.acc_checking_id,
                    to_account_id=self.acc_cny_credit_id,
                    status="committed"
                )

            state_after_repay = credit_cards_repo.get_current_credit_card_state(conn, self.acc_cny_credit_id, self.household_id)
            self.assertEqual(state_after_repay["statement_balance"], Decimal("1000.00")) # statement_balance unchanged
            self.assertEqual(state_after_repay["remaining_statement_due"], Decimal("400.00")) # 1000 - 600 = 400

            # B. Contradiction case: statement_balance = 1100.00 (computed cycle 1000 != 1100)
            lines_fail = [
                NormalizedStatementLine(
                    transaction_on=date(2026, 7, 5),
                    description_raw="Supermarket purchase",
                    direction="debit",
                    line_type="expense",
                    settlement_amount=Decimal("800.00"),
                    settlement_currency="CNY"
                ),
                NormalizedStatementLine(
                    transaction_on=date(2026, 7, 6),
                    description_raw="Late fee",
                    direction="debit",
                    line_type="fee",
                    settlement_amount=Decimal("20.00"),
                    settlement_currency="CNY"
                ),
                NormalizedStatementLine(
                    transaction_on=date(2026, 7, 10),
                    description_raw="Shoe Store Refund",
                    merchant_hint="Shoe Store",
                    direction="credit",
                    line_type="refund",
                    settlement_amount=Decimal("100.00"),
                    settlement_currency="CNY"
                ),
                NormalizedStatementLine(
                    transaction_on=date(2026, 7, 15),
                    description_raw="Furniture Mall (1/3)",
                    direction="debit",
                    line_type="expense",
                    settlement_amount=Decimal("280.00"),
                    settlement_currency="CNY"
                )
            ]
            with transaction(conn):
                preview_fail = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=lines_fail,
                    statement_balance=Decimal("1100.00"),
                    period_start=date(2026, 7, 1),
                    period_end=date(2026, 7, 31),
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                # Batch must become needs_review and must not hide contradiction via auto-adjustment
                self.assertEqual(preview_fail["status"], "needs_review")
                self.assertIsNone(preview_fail.get("adjustment_amount"))
                batch_fail_id = UUID(preview_fail["batch_id"])

            # Attempt to commit needs_review batch must fail closed
            with self.assertRaises((ValueError, InvalidBatchStateError)):
                with transaction(conn):
                    commit_statement_batch(conn, batch_fail_id, user_id=self.user_id, fx_service=self.mock_fx)
        finally:
            conn.close()

    def test_10_twelve_period_installment_full_lifecycle(self):
        """
        Regression for Fix 4:
        Full 12-period installment plan lifecycle:
        - 12 scheduled periods (1000.00 CNY principal: 11 * 83.33 + 1 * 83.37).
        - No future financial transactions at capture.
        - First bill -> active.
        - Intermediate billed periods leave future periods scheduled.
        - Final billed period -> completed.
        - Exactly 12 billed expense transactions total.
        - Sum == exact account principal (1000.00 CNY).
        - Repeated final statement does not duplicate final expense.
        """
        conn = get_connection(self.test_schema)
        try:
            total_principal = Decimal("1000.00")
            schedule = calculate_installment_schedule(total_principal, "CNY", 12)
            self.assertEqual(len(schedule), 12)
            self.assertEqual(sum(schedule), total_principal)

            plan_id = uuid4()
            period_ids = [uuid4() for _ in range(12)]

            with transaction(conn):
                installments_repo.create_installment_plan(
                    conn=conn,
                    plan_id=plan_id,
                    household_id=self.household_id,
                    credit_account_id=self.acc_cny_credit_id,
                    purchase_occurred_on=date(2026, 1, 15),
                    original_amount=total_principal,
                    original_currency="CNY",
                    account_currency="CNY",
                    total_periods=12,
                    merchant="Laptop Center",
                    status="pending_first_bill"
                )
                for idx, (p_id, amt) in enumerate(zip(period_ids, schedule), start=1):
                    installments_repo.create_installment_period(
                        conn=conn,
                        period_id=p_id,
                        plan_id=plan_id,
                        period_no=idx,
                        scheduled_amount=amt,
                        currency="CNY",
                        status="scheduled"
                    )

            # Bill all 12 periods month by month
            for month_idx in range(1, 13):
                stmt_month = month_idx + 1 # Feb 2026 .. Jan 2027
                stmt_year = 2026 if stmt_month <= 12 else 2027
                actual_month = stmt_month if stmt_month <= 12 else (stmt_month - 12)
                stmt_date = date(stmt_year, actual_month, 1)

                line = NormalizedStatementLine(
                    transaction_on=stmt_date,
                    posted_on=date(stmt_year, actual_month, 2),
                    description_raw=f"Laptop Center ({month_idx}/12)",
                    direction="debit",
                    line_type="expense",
                    settlement_amount=schedule[month_idx - 1],
                    settlement_currency="CNY"
                )

                with transaction(conn):
                    prev = create_statement_reconciliation_batch(
                        conn=conn,
                        household_id=self.household_id,
                        account_id=self.acc_cny_credit_id,
                        lines=[line],
                        user_id=self.user_id,
                        default_expense_category_id=self.cat_expense_id,
                        fx_service=self.mock_fx
                    )
                    batch_id = UUID(prev["batch_id"])

                with transaction(conn):
                    commit_statement_batch(conn, batch_id, user_id=self.user_id, fx_service=self.mock_fx)

                plan_state = installments_repo.get_installment_plan(conn, plan_id)
                if month_idx == 1:
                    self.assertEqual(plan_state["status"], "active")
                    self.assertEqual(plan_state["first_statement_month"], stmt_date)
                elif month_idx < 12:
                    self.assertEqual(plan_state["status"], "active")
                else:
                    self.assertEqual(plan_state["status"], "completed")

            # Verify final state: plan completed, all 12 periods billed
            plan_final = installments_repo.get_installment_plan(conn, plan_id)
            self.assertEqual(plan_final["status"], "completed")

            all_periods = installments_repo.list_periods_for_plan(conn, plan_id)
            self.assertEqual(len(all_periods), 12)
            expense_tx_ids = []
            for p in all_periods:
                self.assertEqual(p["status"], "billed")
                self.assertIsNotNone(p["expense_transaction_id"])
                expense_tx_ids.append(p["expense_transaction_id"])

            # Verify exactly 12 billed expense transactions created and sum equals 1000.00
            self.assertEqual(len(set(expense_tx_ids)), 12)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT SUM(from_amount) FROM transactions WHERE id = ANY(%s);",
                    (expense_tx_ids,)
                )
                total_billed = Decimal(str(cur.fetchone()[0]))
                self.assertEqual(total_billed, Decimal("1000.00"))

            # Repeated final Statement replay safety: replaying month 12 does not duplicate expense
            line_repeat_12 = NormalizedStatementLine(
                transaction_on=date(2027, 1, 1),
                posted_on=date(2027, 1, 2),
                description_raw="Laptop Center (12/12)",
                direction="debit",
                line_type="expense",
                settlement_amount=schedule[11],
                settlement_currency="CNY"
            )
            with transaction(conn):
                prev_repeat = create_statement_reconciliation_batch(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.acc_cny_credit_id,
                    lines=[line_repeat_12],
                    user_id=self.user_id,
                    default_expense_category_id=self.cat_expense_id,
                    fx_service=self.mock_fx
                )
                # Matches existing period 12 expense transaction
                self.assertEqual(prev_repeat["matched_count"], 1)
                self.assertEqual(prev_repeat["created_count"], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

