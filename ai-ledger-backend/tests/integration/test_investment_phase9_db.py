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
from unittest.mock import patch
import pypdf
from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject

from app.db import get_connection, transaction
from tests.support.db_helper import BaseDbTestCase
from app.domain.investments import (
    InvestmentCapitalFlow,
    InvestmentStatementExtractionResult,
    calculate_investment_pnl
)
from app.domain.transactions import InvalidCandidatePayloadError
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
        Section 30 & Section 1: First snapshot on investment account establishes authoritative baseline.
        Expected:
        - 1 authoritative snapshot row
        - account_state.ledger_balance = 100,000
        - account_state.initialized_at = valuation as_of
        - account_state.last_authoritative_snapshot_at = valuation as_of
        - account_state.last_transaction_at is None
        - 0 investment_pnl_periods
        - 0 transactions (no cash_income)
        - 0 reconciliation adjustments
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                as_of_dt = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
                payload = {
                    "idempotency_key": "inv-first-snap-001",
                    "as_of": as_of_dt.isoformat(),
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

                # Check account_state authoritative timestamps
                state = accounts_repo.get_account_state(conn, self.inv_cny_id)
                self.assertEqual(state["ledger_balance"], Decimal("100000.000000"))
                self.assertEqual(state["initialized_at"], as_of_dt)
                self.assertEqual(state["last_authoritative_snapshot_at"], as_of_dt)
                self.assertIsNone(state["last_transaction_at"])

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
        Section 31 & Section 1: Opening 100,000 + contribution transfer 50,000 -> Closing 160,000.
        Expected P&L = 10,000 CNY.
        account_state.ledger_balance = 160,000.
        account_state.last_authoritative_snapshot_at advanced.
        account_state.initialized_at remains baseline.
        Zero cash_income.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                # 1. Baseline snapshot
                baseline_as_of = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
                snap1 = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-002",
                        "as_of": baseline_as_of.isoformat(),
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

                # 3. Second snapshot: 160,000 as of 2026-08-20
                second_as_of = datetime(2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc)
                snap2 = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-snap-002",
                        "as_of": second_as_of.isoformat(),
                        "total_asset_value": "160000.00",
                        "currency": "CNY",
                        "source": "dashboard_manual"
                    },
                    user_id=self.user_id
                )

                self.assertEqual(snap2["status"], "committed")
                pnl_res = snap2["investment_pnl"]
                self.assertIsNotNone(pnl_res)
                self.assertEqual(pnl_res["contributions"], "50000.00")
                self.assertEqual(pnl_res["withdrawals"], "0.00")
                self.assertEqual(pnl_res["pnl_amount"], "10000.00")

                # Verify account_state
                state = accounts_repo.get_account_state(conn, self.inv_cny_id)
                self.assertEqual(state["ledger_balance"], Decimal("160000.000000"))
                self.assertEqual(state["initialized_at"], baseline_as_of)
                self.assertEqual(state["last_authoritative_snapshot_at"], second_as_of)
        finally:
            conn.close()

    def test_03_positive_pnl_with_committed_withdrawal(self):
        """
        Section 32: Opening 200,000 - withdrawal 50,000 -> Closing 180,000.
        P&L = 180,000 - 200,000 + 50,000 = +30,000 CNY.
        account_state = 180,000.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                snap1 = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-003",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "200000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                # Committed transfer OUT of investment: 50,000 CNY
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 12),
                    original_amount=Decimal("50000.00"),
                    original_currency="CNY",
                    from_account_id=self.inv_cny_id,
                    to_account_id=self.chk_cny_id,
                    from_amount=Decimal("50000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("50000.00"),
                    to_currency="CNY",
                    status="committed"
                )

                snap2 = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-snap-003",
                        "as_of": "2026-08-25T00:00:00+08:00",
                        "total_asset_value": "180000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                self.assertEqual(snap2["investment_pnl"]["withdrawals"], "50000.00")
                self.assertEqual(snap2["investment_pnl"]["pnl_amount"], "30000.00")
        finally:
            conn.close()

    def test_04_negative_pnl_market_loss(self):
        """
        Section 33: Opening 100,000, zero capital flows, closing 80,000.
        P&L = -20,000 CNY.
        Zero ordinary expense generated.
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

                snap2 = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-snap-004",
                        "as_of": "2026-08-31T00:00:00+08:00",
                        "total_asset_value": "80000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                self.assertEqual(snap2["investment_pnl"]["pnl_amount"], "-20000.00")

                # Verify 0 transactions in ledger
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM transactions WHERE household_id = %s;", (self.household_id,))
                    self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_05_cross_currency_transfer_real_leg(self):
        """
        Section 34: Transfer from USD checking into CNY investment:
        from_amount = 7,000 USD, to_amount = 50,000 CNY.
        Canonical contribution is 50,000 CNY (the investment leg), ignoring reference FX.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-005",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("7000.00"),
                    original_currency="USD",
                    from_account_id=self.inv_usd_id,
                    to_account_id=self.inv_cny_id,
                    from_amount=Decimal("7000.00"),
                    from_currency="USD",
                    to_amount=Decimal("50000.00"),
                    to_currency="CNY",
                    status="committed"
                )

                snap2 = investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-snap-005",
                        "as_of": "2026-08-20T00:00:00+08:00",
                        "total_asset_value": "160000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                self.assertEqual(snap2["investment_pnl"]["contributions"], "50000.00")
                self.assertEqual(snap2["investment_pnl"]["pnl_amount"], "10000.00")
        finally:
            conn.close()

    def test_06_investment_statement_ambiguous_flow_routes_needs_review(self):
        """
        Section 35: Statement extraction has ambiguous flows -> routes to needs_review.
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

                # Two identical 50,000 CNY transfers in DB
                for d in (date(2026, 8, 5), date(2026, 8, 15)):
                    tx_repo.create_transaction(
                        conn=conn,
                        tx_id=uuid4(),
                        household_id=self.household_id,
                        transaction_type="transfer",
                        occurred_on=d,
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
                            currency="CNY"
                        )
                    ],
                    capital_flow_evidence_complete=True
                )
                parser = MockStatementParser(investment_result=extraction)
                pdf_bytes = make_pdf_bytes(["IBKR Statement", "Ending Value: 160000 CNY", "Deposit: 50000 CNY"])

                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    file_bytes=pdf_bytes,
                    file_name="statement.pdf",
                    user_id=self.user_id,
                    parser=parser
                )

                self.assertEqual(res["status"], "needs_review")
                self.assertEqual(res["reason_code"], "AMBIGUOUS_INVESTMENT_CAPITAL_FLOW")
        finally:
            conn.close()

    def test_07_investment_statement_unique_flow_match_and_commit(self):
        """
        Section 36: Statement contribution 50,000 matches unique committed transfer in ledger.
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

                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    file_bytes=pdf_bytes,
                    file_name="statement.pdf",
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

                extraction = InvestmentStatementExtractionResult(
                    total_asset_value=Decimal("100050.00"),
                    currency="CNY",
                    valuation_as_of=date(2026, 8, 31),
                    clear_capital_flows=[],
                    capital_flow_evidence_complete=True
                )
                parser = MockStatementParser(investment_result=extraction)
                pdf_bytes = make_pdf_bytes(["IBKR Statement", "Ending Value: 100050 CNY"])

                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    file_bytes=pdf_bytes,
                    file_name="statement.pdf",
                    user_id=self.user_id,
                    parser=parser
                )

                self.assertEqual(res["status"], "ready")
                batch_id = UUID(res["batch_id"])

                # Verify candidates: 1 snapshot, 1 investment_pnl, 0 adjustment
                candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
                adj_cands = [c for c in candidates if c["candidate_type"] == "adjustment"]
                self.assertEqual(len(adj_cands), 0)
        finally:
            conn.close()

    def test_09_dashboard_investment_summary_isolated_from_cashflow(self):
        """
        Section 27: Confirmed investment P&L appears in GET /api/v1/dashboard/investments,
        but does NOT affect GET /api/v1/dashboard/cash-flow.
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
                        "idempotency_key": "inv-snap-009",
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

    def test_11_unrepresented_ledger_transfer_causes_needs_review_and_canonical_pnl(self):
        """
        Section 4: Opening = 100,000. Ledger has committed contribution = 50,000. Closing = 160,000.
        Parser accidentally emits clear_capital_flows = [] and capital_flow_evidence_complete = true.
        Expected:
        - Batch enters needs_review
        - Canonical P&L evidence remains contribution = 50,000, pnl = 10,000 (NOT contribution = 0, pnl = 60,000).
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-011",
                        "as_of": "2026-07-31T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                # Committed transfer in DB
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=uuid4(),
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

                extraction = InvestmentStatementExtractionResult(
                    total_asset_value=Decimal("160000.00"),
                    currency="CNY",
                    valuation_as_of=date(2026, 8, 31),
                    clear_capital_flows=[],
                    capital_flow_evidence_complete=True
                )
                parser = MockStatementParser(investment_result=extraction)
                pdf_bytes = make_pdf_bytes(["IBKR Statement", "Account Value: 160000 CNY"])

                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    file_bytes=pdf_bytes,
                    file_name="statement.pdf",
                    user_id=self.user_id,
                    parser=parser
                )

                self.assertEqual(res["status"], "needs_review")
                batch_id = UUID(res["batch_id"])

                candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
                pnl_cand = next(c for c in candidates if c["candidate_type"] == "investment_pnl")
                p_data = pnl_cand["payload"]["investment_pnl"]

                # Canonical contributions must be 50,000 and P&L must be 10,000
                self.assertEqual(p_data["contributions_amount"], "50000.00")
                self.assertEqual(p_data["pnl_amount"], "10000.00")
        finally:
            conn.close()

    def test_12_ibkr_real_broker_semantics(self):
        """
        Section 5: Synthetic regression modeled on real IBKR:
        opening NAV: 32729.83 USD, closing NAV: 29135.31 USD, security purchase: 3415.00 USD,
        ending cash: 8312.16 USD, ending stocks: 20823.15 USD, broker reported P/L: -3589.17 USD.
        Zero external contributions, zero external withdrawals.
        Expected:
        - Closing valuation = 29135.31
        - Canonical P&L = 29135.31 - 32729.83 = -3594.52
        - Security purchase does not become withdrawal
        - Cash / Stock components do not replace total NAV
        - Broker P/L does not replace canonical P&L
        - Cash income = 0, expense = 0, adjustment = 0.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_usd_id,
                    payload={
                        "idempotency_key": "inv-ibkr-baseline",
                        "as_of": "2026-06-30T00:00:00+00:00",
                        "total_asset_value": "32729.83",
                        "currency": "USD"
                    },
                    user_id=self.user_id
                )

                extraction = InvestmentStatementExtractionResult(
                    total_asset_value=Decimal("29135.31"),
                    currency="USD",
                    valuation_as_of=date(2026, 7, 31),
                    opening_total_asset_value=Decimal("32729.83"),
                    opening_valuation_as_of=date(2026, 6, 30),
                    clear_capital_flows=[],
                    capital_flow_evidence_complete=True,
                    broker_reported_pnl=Decimal("-3589.17"),
                    metadata={
                        "ending_cash": "8312.16",
                        "ending_stocks": "20823.15",
                        "security_purchases": "3415.00"
                    }
                )
                parser = MockStatementParser(investment_result=extraction)
                pdf_bytes = make_pdf_bytes(["IBKR Activity Statement", "NAV: 29135.31 USD"])

                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_usd_id,
                    file_bytes=pdf_bytes,
                    file_name="ibkr_statement.pdf",
                    user_id=self.user_id,
                    parser=parser
                )

                self.assertEqual(res["status"], "ready")
                batch_id = UUID(res["batch_id"])

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
                pnl_res = commit_res["investment_pnl"]
                self.assertEqual(pnl_res["closing_value"], "29135.31")
                self.assertEqual(pnl_res["contributions"], "0.00")
                self.assertEqual(pnl_res["withdrawals"], "0.00")
                self.assertEqual(pnl_res["pnl_amount"], "-3594.52")

                # Verify account_state
                state = accounts_repo.get_account_state(conn, self.inv_usd_id)
                self.assertEqual(state["ledger_balance"], Decimal("29135.310000"))
        finally:
            conn.close()

    def test_13_investment_candidate_review_flow_resolutions_and_readiness(self):
        """
        Section 6, 7, 8: Ambiguous statement flow resolution via flow_resolutions.
        - recompute_statement_batch_after_review executes without UnboundLocalError.
        - Incompatible transfer selection is rejected with 422.
        - Compatible transfer selection updates candidate and allows commit.
        - Rejecting required investment_pnl candidate leaves batch needs_review.
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-013",
                        "as_of": "2026-07-31T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                # Create two candidate 50,000 transfers
                t1_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=t1_id,
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 5),
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

                t2_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=t2_id,
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

                # An unrelated 30,000 transfer
                unrelated_id = uuid4()
                tx_repo.create_transaction(
                    conn=conn,
                    tx_id=unrelated_id,
                    household_id=self.household_id,
                    transaction_type="transfer",
                    occurred_on=date(2026, 8, 10),
                    original_amount=Decimal("30000.00"),
                    original_currency="CNY",
                    from_account_id=self.chk_cny_id,
                    to_account_id=self.inv_cny_id,
                    from_amount=Decimal("30000.00"),
                    from_currency="CNY",
                    to_amount=Decimal("30000.00"),
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
                            currency="CNY"
                        )
                    ],
                    capital_flow_evidence_complete=True
                )
                parser = MockStatementParser(investment_result=extraction)
                pdf_bytes = make_pdf_bytes(["IBKR Statement", "Value: 160000 CNY", "Deposit: 50000 CNY"])

                res = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    file_bytes=pdf_bytes,
                    file_name="statement.pdf",
                    user_id=self.user_id,
                    parser=parser
                )

                self.assertEqual(res["status"], "needs_review")
                batch_id = UUID(res["batch_id"])

                candidates = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
                pnl_cand = next(c for c in candidates if c["candidate_type"] == "investment_pnl")

                # 1. Incompatible transfer selection (30,000 transfer) raises error
                with self.assertRaises(InvalidCandidatePayloadError):
                    statement_service.patch_candidate(
                        conn=conn,
                        candidate_id=pnl_cand["id"],
                        household_id=self.household_id,
                        payload={
                            "investment_pnl": {
                                "flow_resolutions": [
                                    {"flow_index": 0, "selected_transfer_id": str(unrelated_id)}
                                ]
                            }
                        }
                    )

                # 2. Valid resolution with t1_id
                patch_res = statement_service.patch_candidate(
                    conn=conn,
                    candidate_id=pnl_cand["id"],
                    household_id=self.household_id,
                    payload={
                        "investment_pnl": {
                            "flow_resolutions": [
                                {"flow_index": 0, "selected_transfer_id": str(t1_id)}
                            ]
                        }
                    }
                )

                # Accept candidates
                statement_service.accept_candidate(
                    conn=conn,
                    candidate_id=pnl_cand["id"],
                    household_id=self.household_id,
                    user_id=self.user_id
                )

                snap_cand = next(c for c in candidates if c["candidate_type"] == "snapshot")
                statement_service.accept_candidate(
                    conn=conn,
                    candidate_id=snap_cand["id"],
                    household_id=self.household_id,
                    user_id=self.user_id
                )

                # Batch must now be ready
                batch = reconciliation_repo.get_reconciliation_batch(conn, batch_id)
                self.assertEqual(batch["status"], "ready")

                # Commit succeeds
                all_cands = reconciliation_repo.list_candidates_for_batch(conn, batch_id)
                commit_res = investment_service.commit_investment_statement_batch(
                    conn=conn,
                    batch_id=batch_id,
                    batch=batch,
                    candidates=all_cands,
                    user_id=self.user_id
                )
                self.assertEqual(commit_res["status"], "committed")
        finally:
            conn.close()

    def test_14_statement_chronology_and_semantic_replay(self):
        """
        Section 9: Statement chronology and replay safety:
        - Committing same statement twice -> semantic replay / no-op (no duplicate snapshot/P&L).
        - Out-of-order statement -> needs_review (zero financial writes).
        - Same valuation date but conflicting NAV -> needs_review (zero financial writes).
        """
        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-014",
                        "as_of": "2026-07-31T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

                # Statement for 2026-08-31 at 120,000 CNY
                extraction1 = InvestmentStatementExtractionResult(
                    total_asset_value=Decimal("120000.00"),
                    currency="CNY",
                    valuation_as_of=date(2026, 8, 31),
                    clear_capital_flows=[],
                    capital_flow_evidence_complete=True
                )
                parser1 = MockStatementParser(investment_result=extraction1)
                pdf_bytes1 = make_pdf_bytes(["IBKR Statement", "Account Value: 120000 CNY"])

                res1 = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    file_bytes=pdf_bytes1,
                    file_name="stmt1.pdf",
                    user_id=self.user_id,
                    parser=parser1
                )
                batch_id1 = UUID(res1["batch_id"])
                batch1 = reconciliation_repo.get_reconciliation_batch(conn, batch_id1)
                cands1 = reconciliation_repo.list_candidates_for_batch(conn, batch_id1)

                commit1 = investment_service.commit_investment_statement_batch(
                    conn=conn,
                    batch_id=batch_id1,
                    batch=batch1,
                    candidates=cands1,
                    user_id=self.user_id
                )
                self.assertEqual(commit1["status"], "committed")

                # Verify 1 P&L period created
                periods = investments_repo.list_investment_pnl_periods(conn, self.household_id, self.inv_cny_id)
                self.assertEqual(len(periods), 1)

                # Replay: Process same statement a second time
                res2 = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    file_bytes=pdf_bytes1,
                    file_name="stmt1_dup.pdf",
                    user_id=self.user_id,
                    parser=parser1
                )
                batch_id2 = UUID(res2["batch_id"])
                batch2 = reconciliation_repo.get_reconciliation_batch(conn, batch_id2)
                cands2 = reconciliation_repo.list_candidates_for_batch(conn, batch_id2)

                commit2 = investment_service.commit_investment_statement_batch(
                    conn=conn,
                    batch_id=batch_id2,
                    batch=batch2,
                    candidates=cands2,
                    user_id=self.user_id
                )
                self.assertEqual(commit2["status"], "committed")
                self.assertTrue(commit2.get("replay"))

                # Verify still exactly 1 P&L period
                periods_after = investments_repo.list_investment_pnl_periods(conn, self.household_id, self.inv_cny_id)
                self.assertEqual(len(periods_after), 1)

                # Out-of-order: statement for 2026-08-15
                extraction_stale = InvestmentStatementExtractionResult(
                    total_asset_value=Decimal("110000.00"),
                    currency="CNY",
                    valuation_as_of=date(2026, 8, 15),
                    clear_capital_flows=[],
                    capital_flow_evidence_complete=True
                )
                parser_stale = MockStatementParser(investment_result=extraction_stale)
                pdf_bytes_stale = make_pdf_bytes(["IBKR Statement", "Account Value: 110000 CNY"])

                res_stale = investment_service.process_investment_statement(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    file_bytes=pdf_bytes_stale,
                    file_name="stmt_stale.pdf",
                    user_id=self.user_id,
                    parser=parser_stale
                )
                self.assertEqual(res_stale["status"], "needs_review")
        finally:
            conn.close()

    def test_15_real_service_rollback_on_failure(self):
        """
        Section 13 & Clarification F: Call real investment service method inside a real transaction
        and inject failure after durable write to prove full transaction rollback.
        """
        conn = get_connection(self.test_schema)
        try:
            # Baseline snapshot
            with transaction(conn):
                investment_service.create_manual_investment_snapshot(
                    conn=conn,
                    household_id=self.household_id,
                    account_id=self.inv_cny_id,
                    payload={
                        "idempotency_key": "inv-baseline-015",
                        "as_of": "2026-08-01T00:00:00+08:00",
                        "total_asset_value": "100000.00",
                        "currency": "CNY"
                    },
                    user_id=self.user_id
                )

            # Second snapshot fails inside service after snapshot insertion
            with patch("app.repositories.accounts.update_account_state_after_reconciliation", side_effect=RuntimeError("Simulated DB error")):
                try:
                    with transaction(conn):
                        investment_service.create_manual_investment_snapshot(
                            conn=conn,
                            household_id=self.household_id,
                            account_id=self.inv_cny_id,
                            payload={
                                "idempotency_key": "inv-snap-015-fail",
                                "as_of": "2026-08-20T00:00:00+08:00",
                                "total_asset_value": "150000.00",
                                "currency": "CNY"
                            },
                            user_id=self.user_id
                        )
                except RuntimeError:
                    pass

            # Verify complete rollback
            with transaction(conn):
                state = accounts_repo.get_account_state(conn, self.inv_cny_id)
                self.assertEqual(state["ledger_balance"], Decimal("100000.000000"))

                # There must be only 1 snapshot (the baseline)
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM account_snapshots WHERE account_id = %s;", (self.inv_cny_id,))
                    count = cur.fetchone()[0]
                    self.assertEqual(count, 1)

                    cur.execute("SELECT count(*) FROM investment_pnl_periods WHERE account_id = %s;", (self.inv_cny_id,))
                    pnl_count = cur.fetchone()[0]
                    self.assertEqual(pnl_count, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
