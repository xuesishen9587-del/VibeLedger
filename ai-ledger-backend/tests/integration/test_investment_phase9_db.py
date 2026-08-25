import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
import io
import pypdf
from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject

from app.db import get_connection, transaction
from tests.support.db_helper import BaseDbTestCase
from app.domain.investments import (
    InvestmentCapitalFlow,
    InvestmentStatementExtractionResult,
    calculate_investment_pnl
)
from app.services.reference_fx_service import ReferenceFxService
import app.repositories.accounts as accounts_repo
import app.repositories.snapshots as snapshots_repo
import app.repositories.investments as investments_repo
import app.repositories.reconciliation as reconciliation_repo
import app.repositories.transactions as tx_repo
import app.repositories.audit as audit_repo
import app.services.investment_service as investment_service
import app.services.statement_service as statement_service
import app.services.reconciliation_service as reconciliation_service
import app.services.dashboard_service as dashboard_service
from app.services.statement_parser import MockStatementParser


def make_pdf_bytes(lines: List[str]) -> bytes:
    writer = pypdf.PdfWriter()
    for t in lines:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream_content = f"BT /F1 12 Tf 72 712 Td ({t}) Tj ET".encode("latin-1", errors="replace")
        stream.set_data(stream_content)

        resources = DictionaryObject()
        fonts = DictionaryObject()
        f1 = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        fonts[NameObject("/F1")] = f1
        resources[NameObject("/Font")] = fonts

        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = stream

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class FixedMockFx(ReferenceFxService):
    def __init__(self):
        super().__init__()
        self.rates = {
            ("USD", "CNY"): Decimal("7.250000000000"),
            ("CNY", "USD"): Decimal("0.137931034483"),
            ("CNY", "CNY"): Decimal("1.000000000000"),
            ("USD", "USD"): Decimal("1.000000000000"),
        }

    def get_rate(self, base_currency: str, target_currency: str, as_of: Optional[date] = None) -> Optional[Decimal]:
        if base_currency == target_currency:
            return Decimal("1.000000000000")
        return self.rates.get((base_currency, target_currency))


class TestInvestmentPhase9Db(BaseDbTestCase):
    def setUp(self):
        super().setUp()
        self.fx = FixedMockFx()
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                self.household_id = uuid4()
                unique_sfx = uuid4().hex[:8]
                accounts_repo.create_household(
                    conn=conn,
                    household_id=self.household_id,
                    name=f"Phase9 Household {unique_sfx}",
                    reporting_currency="CNY",
                    ledger_start_date=date(2026, 1, 1)
                )

                self.user_id = uuid4()
                accounts_repo.create_user(
                    conn=conn,
                    user_id=self.user_id,
                    auth_subject=f"investor_{unique_sfx}",
                    display_name="Investor User",
                    email=f"investor_{unique_sfx}@example.com",
                    default_currency="CNY"
                )

                # Investment account (CNY)
                self.inv_cny_id = uuid4()
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.inv_cny_id,
                    household_id=self.household_id,
                    name="CNY Stock Account",
                    account_type="investment",
                    currency="CNY",
                    institution="China Securities"
                )

                # Investment account (USD)
                self.inv_usd_id = uuid4()
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.inv_usd_id,
                    household_id=self.household_id,
                    name="IBKR Account",
                    account_type="investment",
                    currency="USD",
                    institution="Interactive Brokers"
                )

                # Checking account (CNY)
                self.chk_cny_id = uuid4()
                accounts_repo.create_account(
                    conn=conn,
                    account_id=self.chk_cny_id,
                    household_id=self.household_id,
                    name="Bank Checking",
                    account_type="cash",
                    currency="CNY",
                    institution="ICBC"
                )

                # Expense category
                self.cat_expense_id = uuid4()
                accounts_repo.create_category(
                    conn=conn,
                    category_id=self.cat_expense_id,
                    household_id=self.household_id,
                    name="General Expense",
                    category_type="expense"
                )
        finally:
            conn.close()

    def test_01_first_snapshot_baseline(self):
        """
        Section 30: First snapshot on investment account establishes authoritative baseline.
        Expected:
        - 1 authoritative snapshot row
        - account_state.ledger_balance = 100,000
        - 0 investment_pnl_periods
        - 0 transactions (no cash_income)
        - 0 reconciliation adjustments
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                payload = {
                    "idempotency_key": "inv-first-snap-001",
                    "as_of": "2026-08-01T10:00:00+08:00",
                    "total_asset_value": "100000.00",
                    "currency": "CNY",
                    "source": "dashboard_manual"
                }
                result = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload=payload,
                    user_id=self.user_id,
                    device_id=None
                )

                self.assertEqual(result["status"], "committed")
                self.assertIsNotNone(result["snapshot_id"])
                self.assertIsNone(result["investment_pnl"])

                # Check account_state
                state = accounts_repo.get_account_state(conn, self.inv_cny_id)
                self.assertEqual(state["ledger_balance"], Decimal("100000.000000"))

                # Check snapshot row
                snap = snapshots_repo.get_snapshot(conn, UUID(result["snapshot_id"]))
                self.assertIsNotNone(snap)
                self.assertEqual(snap["snapshot_type"], "investment_valuation")
                self.assertEqual(snap["balance"], Decimal("100000.000000"))

                # Check 0 P&L periods
                periods = investments_repo.list_investment_pnl_periods(conn, self.household_id, self.inv_cny_id)
                self.assertEqual(len(periods), 0)

                # Check 0 transactions
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM transactions WHERE household_id = %s;", (self.household_id,))
                    tx_count = cur.fetchone()[0]
                self.assertEqual(tx_count, 0)
        finally:
            conn.close()

    def test_02_positive_pnl_with_committed_contribution(self):
        """
        Section 31: Opening 100,000 + contribution transfer 50,000 -> Closing 160,000.
        Expected P&L = 10,000 CNY.
        account_state = 160,000.
        Zero cash_income.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # 1. Baseline snapshot
                snap1 = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-002",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY",
                        "source": "dashboard_manual"
                    },
                    user_id=self.user_id
                )

                # 2. Committed transfer: 50,000 CNY into investment
                transfer_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=transfer_id,
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("50000.00"),
                    original_currency="CNY",
                    from_account_id=self.chk_cny_id,
                    to_account_id=self.inv_cny_id,
                    from_amount=Decimal("50000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("50000.00"),
                    to_currency="CNY",
                    status="committed"
                )

                # 3. Closing snapshot at 160,000
                snap2 = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-closing-002",
                        "as_of": "2026-08-20T00:00:00+08:00",
                        "total_asset_value": "160000.00",
                        "currency": "CNY",
                        "source": "dashboard_manual"
                    },
                    user_id=self.user_id
                )

                self.assertEqual(snap2["status"], "committed")
                self.assertIsNotNone(snap2["investment_pnl"])
                self.assertEqual(snap2["investment_pnl"]["pnl_amount"], "10000.00")
                self.assertEqual(snap2["investment_pnl"]["status"], "confirmed")

                # Check account_state
                state = accounts_repo.get_account_state(conn, self.inv_cny_id)
                self.assertEqual(state["ledger_balance"], Decimal("160000.000000"))

                # Check cash flow summary
                cf = dashboard_service.get_cash_flow(
                    conn=conn,
                    household_id=self.household_id,
                    from_date=date(2026, 8, 1),
                    to_date=date(2026, 8, 31),
                    fx_service=self.fx
                )
                self.assertEqual(cf["cash_income"], "0.00")
                self.assertEqual(cf["expense"], "0.00")
        finally:
            conn.close()

    def test_03_withdrawal_pnl(self):
        """
        Section 32: Opening 100,000 + withdrawal 20,000 -> Closing 90,000.
        P&L = 90,000 - 100,000 - 0 + 20,000 = 10,000.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-003",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                # Withdrawal transfer: 20,000 out
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("20000.00"),
                    original_currency="CNY",
                    from_account_id=self.inv_cny_id,
                    to_account_id=self.chk_cny_id,
                    from_amount=Decimal("20000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("20000.00"),
                    to_currency="CNY",
                    status="committed"
                )

                res = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-closing-003",
                        "as_of": "2026-08-20T00:00:00+08:00",
                        "total_asset_value": "90000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                self.assertEqual(res["status"], "committed")
                self.assertEqual(res["investment_pnl"]["pnl_amount"], "10000.00")
        finally:
            conn.close()

    def test_04_negative_pnl_no_flows(self):
        """
        Section 33: Opening 100,000 -> Closing 90,000 without capital flows.
        P&L = -10,000.
        Expected confirmed P&L, NO ordinary expense created.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-004",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                res = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-closing-004",
                        "as_of": "2026-08-20T00:00:00+08:00",
                        "total_asset_value": "90000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                self.assertEqual(res["status"], "committed")
                self.assertEqual(res["investment_pnl"]["pnl_amount"], "-10000.00")

                cf = dashboard_service.get_cash_flow(
                    conn=conn,
                    household_id=self.household_id,
                    from_date=date(2026, 8, 1),
                    to_date=date(2026, 8, 31),
                    fx_service=self.fx
                )
                self.assertEqual(cf["expense"], "0.00")
        finally:
            conn.close()

    def test_05_cross_currency_capital_flow(self):
        """
        Section 34: Investment account in USD, funding in CNY.
        Committed transfer: from 7250 CNY to 1000 USD.
        Expected contribution for P&L is 1000 USD (the real leg), NOT 7250 CNY or FX converted.
        Opening 10,000 USD + contribution 1000 USD -> Closing 12,500 USD.
        P&L = 12500 - 10000 - 1000 + 0 = 1500 USD.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_usd_id,
                    payload={
                        "idempotency_key": "inv-baseline-005",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "10000.00",
                        "currency": "USD"
                    },
                    user_id=self.user_id
                )

                # Cross-currency transfer
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 5),
                    original_amount=Decimal("7250.00"),
                    original_currency="CNY",
                    from_account_id=self.chk_cny_id,
                    to_account_id=self.inv_usd_id,
                    from_amount=Decimal("7250.00"),
                    from_currency="CNY",
                    to_amount=Decimal("1000.00"),
                    to_currency="USD",
                    status="committed"
                )

                res = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_usd_id,
                    payload={
                        "idempotency_key": "inv-closing-005",
                        "as_of": "2026-08-20T00:00:00+08:00",
                        "total_asset_value": "12500.00",
                        "currency": "USD"
                    },
                    user_id=self.user_id
                )

                self.assertEqual(res["status"], "committed")
                self.assertEqual(res["investment_pnl"]["pnl_amount"], "1500.00")
                self.assertEqual(res["investment_pnl"]["currency"], "USD")
        finally:
            conn.close()

    def test_06_investment_statement_ambiguous_capital_flow(self):
        """
        Section 35: Statement has closing 160,000 and contribution 50,000,
        but ledger has NO compatible committed transfer.
        Expected: needs_review, AMBIGUOUS_INVESTMENT_CAPITAL_FLOW, no closing snapshot or pnl row in DB yet.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-006",
                        "as_of": "2026-07-31T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                extraction = InvestmentStatementExtractionResult(
                    total_asset_value=Decimal("160000.00"),
                    currency="CNY",
                    valuation_as_of=date(2026, 8, 31),
                    clear_capital_flows=[
                        InvestmentCapitalFlow(
                            direction="contribution",
                            amount=Decimal("50000.00"),
                            currency="CNY",
                            occurred_on=date(2026, 8, 15),
                            description="Unmatched wire deposit"
                        )
                    ],
                    capital_flow_evidence_complete=True
                )
                parser = MockStatementParser(investment_result=extraction)
                pdf_bytes = make_pdf_bytes(["IBKR Statement", "Account Value: 160000 CNY", "Deposit: 50000 CNY"])

                account = accounts_repo.get_account(conn, self.inv_cny_id)
                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account=account,
                    file_bytes=pdf_bytes,
                    filename="statement.pdf",
                    user_id=self.user_id,
                    parser=parser
                )

                self.assertEqual(res["status"], "needs_review")
                self.assertEqual(res["reason_code"], "AMBIGUOUS_INVESTMENT_CAPITAL_FLOW")
                batch_id = UUID(res["batch_id"])

                # Verify NO confirmed P&L period created before commit
                periods = investments_repo.list_investment_pnl_periods(conn, self.household_id, self.inv_cny_id)
                self.assertEqual(len(periods), 0)

                # Verify account_state is still 100,000 (not mutated)
                state = accounts_repo.get_account_state(conn, self.inv_cny_id)
                self.assertEqual(state["ledger_balance"], Decimal("100000.000000"))
        finally:
            conn.close()

    def test_07_investment_statement_unique_flow_match_and_commit(self):
        """
        Section 36: Statement contribution 50,000 matches unique committed transfer in ledger.
        Batch is ready -> Commit batch -> closing snapshot + confirmed P&L created.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-007",
                        "as_of": "2026-07-31T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                # Create matching transfer in DB
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 15),
                    original_amount=Decimal("50000.00"),
                    original_currency="CNY",
                    from_account_id=self.chk_cny_id,
                    to_account_id=self.inv_cny_id,
                    from_amount=Decimal("50000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("50000.00"),
                    to_currency="CNY",
                    status="committed"
                )

                extraction = InvestmentStatementExtractionResult(
                    total_asset_value=Decimal("160000.00"),
                    currency="CNY",
                    valuation_as_of=date(2026, 8, 31),
                    clear_capital_flows=[
                        InvestmentCapitalFlow(
                            direction="contribution",
                            amount=Decimal("50000.00"),
                            currency="CNY",
                            occurred_on=date(2026, 8, 15)
                        )
                    ],
                    capital_flow_evidence_complete=True
                )
                parser = MockStatementParser(investment_result=extraction)
                pdf_bytes = make_pdf_bytes(["IBKR Statement", "Account Value: 160000 CNY", "Deposit: 50000 CNY"])

                account = accounts_repo.get_account(conn, self.inv_cny_id)
                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account=account,
                    file_bytes=pdf_bytes,
                    filename="statement.pdf",
                    user_id=self.user_id,
                    parser=parser
                )

                self.assertEqual(res["status"], "ready")
                batch_id = UUID(res["batch_id"])

                # Commit batch
                batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
                candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)

                commit_res = investment_service.commit_investment_statement_batch(
                    conn=conn,
                    batch_id=batch_id,
                    batch=batch,
                    candidates=candidates,
                    user_id=self.user_id
                )

                self.assertEqual(commit_res["status"], "committed")
                self.assertEqual(commit_res["investment_pnl"]["pnl_amount"], "10000.00")

                # Verify confirmed P&L in DB
                periods = investments_repo.list_investment_pnl_periods(conn, self.household_id, self.inv_cny_id)
                self.assertEqual(len(periods), 1)
                self.assertEqual(periods[0]["pnl_amount"], Decimal("10000.000000"))

                # Verify account_state
                state = accounts_repo.get_account_state(conn, self.inv_cny_id)
                self.assertEqual(state["ledger_balance"], Decimal("160000.000000"))
        finally:
            conn.close()

    def test_08_no_plus_minus_200_adjustment_on_investment(self):
        """
        Section 26: A small discrepancy (e.g. 50 CNY) on investment valuation
        must NOT create a reconciliation_adjustment.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-008",
                        "as_of": "2026-07-31T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                # Statement upload with 100,050.00 total value (50 CNY change, no flows)
                extraction = InvestmentStatementExtractionResult(
                    total_asset_value=Decimal("100050.00"),
                    currency="CNY",
                    valuation_as_of=date(2026, 8, 31),
                    clear_capital_flows=[],
                    capital_flow_evidence_complete=True
                )
                parser = MockStatementParser(investment_result=extraction)
                pdf_bytes = make_pdf_bytes(["IBKR Statement", "Ending Value: 100050 CNY"])

                account = accounts_repo.get_account(conn, self.inv_cny_id)
                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account=account,
                    file_bytes=pdf_bytes,
                    filename="statement.pdf",
                    user_id=self.user_id,
                    parser=parser
                )

                batch_id = UUID(res["batch_id"])
                candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)

                # Ensure NO adjustment candidate exists
                adj_cands = [c for c in candidates if c["candidate_type"] == "adjustment"]
                self.assertEqual(len(adj_cands), 0)
        finally:
            conn.close()

    def test_09_dashboard_investment_pnl_reporting_integration(self):
        """
        Section 28 & 38: Confirmed investment P&L appears in investment summary
        and does NOT affect cash income or ordinary expenses.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-009",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-closing-009",
                        "as_of": "2026-08-31T00:00:00+08:00",
                        "total_asset_value": "115000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                # Query dashboard investments summary
                inv_sum = dashboard_service.get_investments_summary(
                    conn=conn,
                    household_id=self.household_id,
                    from_date=date(2026, 8, 1),
                    to_date=date(2026, 8, 31),
                    fx_service=self.fx
                )

                self.assertEqual(inv_sum["total_pnl"], "15000.00")
                self.assertEqual(len(inv_sum["items"]), 1)

                # Query cash flow summary
                cf = dashboard_service.get_cash_flow(
                    conn=conn,
                    household_id=self.household_id,
                    from_date=date(2026, 8, 1),
                    to_date=date(2026, 8, 31),
                    fx_service=self.fx
                )

                self.assertEqual(cf["cash_income"], "0.00")
                self.assertEqual(cf["expense"], "0.00")
                self.assertEqual(cf["net_cash_flow"], "0.00")
        finally:
            conn.close()

    def test_10_atomic_rollback_on_failure(self):
        """
        Section 42: Forced failure during transaction rolls back snapshot, P&L, and account state.
        """
        conn = get_connection(self.test_schema)
        try:
            # First establish baseline cleanly
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-010",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

            # Second transaction attempts snapshot but fails
            try:
                with transaction(conn):
                    snapshots_repo.create_account_snapshot(
                        conn=conn,
                        snapshot_id=uuid4(),
                        household_id=self.household_id,
                        account_id=self.inv_cny_id,
                        as_of=datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc),
                        balance=Decimal("200000.00"),
                        currency="CNY",
                        snapshot_type="investment_valuation",
                        source="dashboard_manual"
                    )
                    accounts_repo.update_account_state_projection(
                        conn, self.inv_cny_id, Decimal("200000.00"), datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc)
                    )
                    # Simulated unexpected failure
                    raise RuntimeError("Simulated crash before transaction completion")
            except RuntimeError:
                pass

            # Verify rollback
            with transaction(conn):
                state = accounts_repo.get_account_state(conn, self.inv_cny_id)
                self.assertEqual(state["ledger_balance"], Decimal("100000.000000"))
                periods = investments_repo.list_investment_pnl_periods(conn, self.household_id, self.inv_cny_id)
                self.assertEqual(len(periods), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
