import unittest
from uuid import uuid4
from decimal import Decimal
from datetime import date
from unittest.mock import patch, MagicMock

from app.domain.transactions import (
    InvalidCandidatePayloadError,
    CategoryNotFoundError,
    CategoryMismatchError,
    IncompatibleTargetTransactionError
)
import app.services.statement_service as statement_service


class TestReconciliationDirectionUnit(unittest.TestCase):
    """
    Unit test suite for authoritative statement line direction enforcement in statement_service.resolve_candidate().
    """

    def setUp(self):
        self.household_id = uuid4()
        self.account_id = uuid4()
        self.counter_account_id = uuid4()
        self.candidate_id = uuid4()
        self.batch_id = uuid4()
        self.line_id = uuid4()
        self.category_expense_id = uuid4()
        self.category_income_id = uuid4()
        self.orig_expense_id = uuid4()
        self.target_tx_id = uuid4()

        self.account_row = {
            "id": self.account_id,
            "household_id": self.household_id,
            "currency": "CNY",
            "status": "active",
            "account_type": "cash"
        }
        self.counter_account_row = {
            "id": self.counter_account_id,
            "household_id": self.household_id,
            "currency": "CNY",
            "status": "active",
            "account_type": "cash"
        }
        self.batch_row = {
            "id": self.batch_id,
            "household_id": self.household_id,
            "account_id": self.account_id,
            "status": "needs_review",
            "batch_type": "statement",
            "currency": "CNY",
            "row_version": 1,
            "residual_amount": None,
            "adjustment_amount": None,
            "period_end": date(2026, 8, 31),
            "created_at": None,
            "authoritative_balance": None
        }

    def _make_candidate_row(self, candidate_type="create_transaction", reason_code="TYPE_AMBIGUOUS"):
        return (
            self.candidate_id,
            self.batch_id,
            self.line_id,
            candidate_type,
            "needs_review",
            None,
            {"evidence": {"merchant_raw": "Test Merchant"}},
            Decimal("0.80"),
            reason_code,
            "Ambiguous line"
        )

    def _make_line_row(self, direction="debit", amount="100.00", currency="CNY"):
        return {
            "id": self.line_id,
            "batch_id": self.batch_id,
            "direction": direction,
            "line_type": "unknown",
            "amount": Decimal(amount),
            "settlement_amount": Decimal(amount),
            "currency": currency,
            "settlement_currency": currency,
            "transaction_on": date(2026, 8, 10),
            "posted_on": date(2026, 8, 11),
            "description_raw": "Test Raw Line",
            "description_normalized": "Test Normalized Line",
            "merchant_hint": "Test Merchant"
        }

    @patch("app.services.statement_service.recompute_statement_batch_after_review")
    @patch("app.services.statement_service.get_statement_batch_summary")
    @patch("app.repositories.audit.insert_audit_event")
    @patch("app.repositories.accounts.get_category")
    @patch("app.repositories.accounts.get_account")
    @patch("app.repositories.reconciliation.get_statement_line")
    @patch("app.repositories.reconciliation.lock_reconciliation_batch")
    def test_debit_rejects_cash_income_and_refund(
        self, mock_lock_batch, mock_get_line, mock_get_acc, mock_get_cat, mock_audit, mock_summary, mock_recompute
    ):
        mock_lock_batch.return_value = self.batch_row
        mock_get_line.return_value = self._make_line_row(direction="debit")
        mock_get_acc.return_value = self.account_row

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = self._make_candidate_row()

        # 1. Debit -> cash_income must raise InvalidCandidatePayloadError
        with self.assertRaises(InvalidCandidatePayloadError) as ctx:
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="cash_income",
                category_id=self.category_income_id
            )
        self.assertIn("debit", str(ctx.exception).lower())

        # 2. Debit -> refund must raise InvalidCandidatePayloadError
        with self.assertRaises(InvalidCandidatePayloadError) as ctx:
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="refund",
                original_expense_id=self.orig_expense_id
            )
        self.assertIn("debit", str(ctx.exception).lower())

    @patch("app.services.statement_service.recompute_statement_batch_after_review")
    @patch("app.services.statement_service.get_statement_batch_summary")
    @patch("app.repositories.audit.insert_audit_event")
    @patch("app.repositories.accounts.get_category")
    @patch("app.repositories.accounts.get_account")
    @patch("app.repositories.reconciliation.get_statement_line")
    @patch("app.repositories.reconciliation.lock_reconciliation_batch")
    def test_credit_rejects_expense_and_fee(
        self, mock_lock_batch, mock_get_line, mock_get_acc, mock_get_cat, mock_audit, mock_summary, mock_recompute
    ):
        mock_lock_batch.return_value = self.batch_row
        mock_get_line.return_value = self._make_line_row(direction="credit")
        mock_get_acc.return_value = self.account_row

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = self._make_candidate_row(reason_code="INCOME_TRANSFER_REFUND_AMBIGUOUS")

        # 1. Credit -> expense must raise InvalidCandidatePayloadError
        with self.assertRaises(InvalidCandidatePayloadError) as ctx:
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="expense",
                category_id=self.category_expense_id
            )
        self.assertIn("credit", str(ctx.exception).lower())

        # 2. Credit -> fee must raise InvalidCandidatePayloadError
        with self.assertRaises(InvalidCandidatePayloadError) as ctx:
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="fee",
                category_id=self.category_expense_id
            )
        self.assertIn("credit", str(ctx.exception).lower())

    @patch("app.services.statement_service.recompute_statement_batch_after_review")
    @patch("app.services.statement_service.get_statement_batch_summary")
    @patch("app.repositories.audit.insert_audit_event")
    @patch("app.repositories.accounts.get_category")
    @patch("app.repositories.accounts.get_account")
    @patch("app.repositories.reconciliation.get_statement_line")
    @patch("app.repositories.reconciliation.lock_reconciliation_batch")
    def test_unknown_direction_rejects_non_match_resolutions(
        self, mock_lock_batch, mock_get_line, mock_get_acc, mock_get_cat, mock_audit, mock_summary, mock_recompute
    ):
        mock_lock_batch.return_value = self.batch_row
        mock_get_line.return_value = self._make_line_row(direction="unknown")
        mock_get_acc.return_value = self.account_row

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = self._make_candidate_row()

        for res_type in ["expense", "fee", "cash_income", "refund", "transfer"]:
            with self.assertRaises(InvalidCandidatePayloadError) as ctx:
                statement_service.resolve_candidate(
                    conn=mock_conn,
                    candidate_id=self.candidate_id,
                    household_id=self.household_id,
                    resolution_type=res_type,
                    category_id=self.category_expense_id,
                    counter_account_id=self.counter_account_id
                )
            self.assertIn("unknown", str(ctx.exception).lower())

    @patch("app.services.statement_service.recompute_statement_batch_after_review")
    @patch("app.services.statement_service.get_statement_batch_summary")
    @patch("app.repositories.audit.insert_audit_event")
    @patch("app.repositories.accounts.get_category")
    @patch("app.repositories.accounts.get_account")
    @patch("app.repositories.reconciliation.get_statement_line")
    @patch("app.repositories.reconciliation.lock_reconciliation_batch")
    def test_debit_expense_and_credit_cash_income_succeed(
        self, mock_lock_batch, mock_get_line, mock_get_acc, mock_get_cat, mock_audit, mock_summary, mock_recompute
    ):
        mock_lock_batch.return_value = self.batch_row
        mock_summary.return_value = {"status": "ready"}

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # 1. Debit -> expense succeeds
        mock_get_line.return_value = self._make_line_row(direction="debit")
        mock_get_acc.return_value = self.account_row
        mock_cur.fetchone.return_value = self._make_candidate_row(reason_code="TYPE_AMBIGUOUS")
        mock_get_cat.return_value = {
            "id": self.category_expense_id,
            "household_id": self.household_id,
            "status": "active",
            "name": "Dining",
            "category_type": "expense"
        }

        res = statement_service.resolve_candidate(
            conn=mock_conn,
            candidate_id=self.candidate_id,
            household_id=self.household_id,
            resolution_type="expense",
            category_id=self.category_expense_id
        )
        self.assertEqual(res["status"], "ready")

        # 2. Credit -> cash_income succeeds
        mock_get_line.return_value = self._make_line_row(direction="credit")
        mock_cur.fetchone.return_value = self._make_candidate_row(reason_code="INCOME_TRANSFER_REFUND_AMBIGUOUS")
        mock_get_cat.return_value = {
            "id": self.category_income_id,
            "household_id": self.household_id,
            "status": "active",
            "name": "Salary",
            "category_type": "income"
        }

        res2 = statement_service.resolve_candidate(
            conn=mock_conn,
            candidate_id=self.candidate_id,
            household_id=self.household_id,
            resolution_type="cash_income",
            category_id=self.category_income_id
        )
        self.assertEqual(res2["status"], "ready")


if __name__ == "__main__":
    unittest.main()
