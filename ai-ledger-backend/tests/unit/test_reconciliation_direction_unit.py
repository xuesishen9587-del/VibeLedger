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

    @patch("app.services.statement_service.recompute_statement_batch_after_review")
    @patch("app.services.statement_service.get_statement_batch_summary")
    @patch("app.repositories.accounts.get_account")
    @patch("app.repositories.reconciliation.get_statement_line")
    @patch("app.repositories.reconciliation.lock_reconciliation_batch")
    def test_resolve_candidate_restricted_to_ambiguous_reasons_and_review_state(
        self, mock_lock_batch, mock_get_line, mock_get_acc, mock_summary, mock_recompute
    ):
        mock_lock_batch.return_value = self.batch_row
        mock_get_line.return_value = self._make_line_row(direction="debit")
        mock_get_acc.return_value = self.account_row

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        from app.domain.transactions import InvalidCandidateStateError

        # 1. Reject accepted candidate
        mock_cur.fetchone.return_value = (
            self.candidate_id, self.batch_id, self.line_id, "create_transaction",
            "accepted", None, {}, Decimal("0.80"), "TYPE_AMBIGUOUS", None
        )
        with self.assertRaises(InvalidCandidateStateError):
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="expense",
                category_id=self.category_expense_id
            )

        # 2. Reject MULTIPLE_TRANSACTION_MATCHES
        mock_cur.fetchone.return_value = self._make_candidate_row(reason_code="MULTIPLE_TRANSACTION_MATCHES")
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="expense",
                category_id=self.category_expense_id
            )

        # 3. Reject CATEGORY_REQUIRED
        mock_cur.fetchone.return_value = self._make_candidate_row(reason_code="CATEGORY_REQUIRED")
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="expense",
                category_id=self.category_expense_id
            )

        # 4. Reject candidate without reason_code
        mock_cur.fetchone.return_value = self._make_candidate_row(reason_code=None)
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="expense",
                category_id=self.category_expense_id
            )

    @patch("app.services.statement_service.recompute_statement_batch_after_review")
    @patch("app.services.statement_service.get_statement_batch_summary")
    @patch("app.repositories.audit.insert_audit_event")
    @patch("app.repositories.accounts.get_category")
    @patch("app.repositories.accounts.get_account")
    @patch("app.repositories.reconciliation.get_statement_line")
    @patch("app.repositories.reconciliation.lock_reconciliation_batch")
    def test_patch_candidate_semantic_guards(
        self, mock_lock_batch, mock_get_line, mock_get_acc, mock_get_cat, mock_audit, mock_summary, mock_recompute
    ):
        mock_lock_batch.return_value = self.batch_row
        mock_get_line.return_value = self._make_line_row(direction="debit")
        mock_get_acc.return_value = self.account_row
        mock_summary.return_value = {"status": "ready"}

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # 1. Generic PATCH on TYPE_AMBIGUOUS candidate must be rejected
        mock_cur.fetchone.return_value = self._make_candidate_row(reason_code="TYPE_AMBIGUOUS")
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.patch_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                payload={"transaction": {"category_id": str(self.category_expense_id)}}
            )

        # 2. Generic PATCH mutating transaction_type must be rejected
        mock_cur.fetchone.return_value = (
            self.candidate_id, self.batch_id, self.line_id, "create_transaction",
            "needs_review", None, {"transaction": {"transaction_type": "expense"}},
            Decimal("0.80"), "CATEGORY_REQUIRED", None
        )
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.patch_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                payload={"transaction": {"transaction_type": "cash_income"}}
            )

        # 3. Generic PATCH on CATEGORY_REQUIRED with valid category succeeds and clears reason_code
        mock_get_cat.return_value = {
            "id": self.category_expense_id,
            "household_id": self.household_id,
            "status": "active",
            "name": "Dining",
            "category_type": "expense"
        }
        res = statement_service.patch_candidate(
            conn=mock_conn,
            candidate_id=self.candidate_id,
            household_id=self.household_id,
            payload={"transaction": {"category_id": str(self.category_expense_id)}}
        )
        self.assertEqual(res["status"], "ready")

    def test_validate_candidate_payload_direction_invariants(self):
        # 1. Debit create_transaction rejecting cash_income
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.validate_candidate_payload_for_type(
                conn=MagicMock(),
                candidate_type="create_transaction",
                merged_payload={"transaction": {"transaction_type": "cash_income"}},
                account=self.account_row,
                household_id=self.household_id,
                statement_line={"direction": "debit"}
            )

        # 2. Credit create_transaction rejecting expense
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.validate_candidate_payload_for_type(
                conn=MagicMock(),
                candidate_type="create_transaction",
                merged_payload={"transaction": {"transaction_type": "expense"}},
                account=self.account_row,
                household_id=self.household_id,
                statement_line={"direction": "credit"}
            )

        # 3. Debit transfer rejecting reconciled account as to_account
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.validate_candidate_payload_for_type(
                conn=MagicMock(),
                candidate_type="create_transfer",
                merged_payload={"transfer": {"to_account_id": str(self.account_id)}},
                account=self.account_row,
                household_id=self.household_id,
                statement_line={"direction": "debit"}
            )

        # 4. Credit transfer rejecting reconciled account as from_account
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.validate_candidate_payload_for_type(
                conn=MagicMock(),
                candidate_type="create_transfer",
                merged_payload={"transfer": {"from_account_id": str(self.account_id)}},
                account=self.account_row,
                household_id=self.household_id,
                statement_line={"direction": "credit"}
            )

        # 5. Refund rejecting debit statement line
        with self.assertRaises(InvalidCandidatePayloadError):
            statement_service.validate_candidate_payload_for_type(
                conn=MagicMock(),
                candidate_type="refund",
                merged_payload={"refund": {}},
                account=self.account_row,
                household_id=self.household_id,
                statement_line={"direction": "debit"}
            )

        # 6. Refund with valid original expense executes without NameError
        mock_conn = MagicMock()
        with patch("app.repositories.transactions.get_transaction") as mock_get_tx, \
             patch("app.repositories.transactions.get_active_refunds_for_expense") as mock_get_refunds:
            mock_get_tx.return_value = {
                "id": self.orig_expense_id,
                "household_id": self.household_id,
                "status": "committed",
                "transaction_type": "expense",
                "from_account_id": self.account_id,
                "from_amount": Decimal("100.00"),
                "from_currency": "CNY"
            }
            mock_get_refunds.return_value = []

            # Valid amount (50.00 <= 100.00)
            statement_service.validate_candidate_payload_for_type(
                conn=mock_conn,
                candidate_type="refund",
                merged_payload={"refund": {"original_expense_id": str(self.orig_expense_id), "amount": "50.00", "currency": "CNY"}},
                account=self.account_row,
                household_id=self.household_id,
                statement_line={"direction": "credit", "settlement_amount": Decimal("50.00"), "settlement_currency": "CNY"}
            )

            # Over-refund amount (150.00 > 100.00) raises InvalidCandidatePayloadError
            with self.assertRaises(InvalidCandidatePayloadError) as ctx:
                statement_service.validate_candidate_payload_for_type(
                    conn=mock_conn,
                    candidate_type="refund",
                    merged_payload={"refund": {"original_expense_id": str(self.orig_expense_id), "amount": "150.00", "currency": "CNY"}},
                    account=self.account_row,
                    household_id=self.household_id,
                    statement_line={"direction": "credit", "settlement_amount": Decimal("150.00"), "settlement_currency": "CNY"}
                )
            self.assertIn("exceeds", str(ctx.exception).lower())

    @patch("app.services.statement_service.recompute_statement_batch_after_review")
    @patch("app.services.statement_service.get_statement_batch_summary")
    @patch("app.repositories.audit.insert_audit_event")
    @patch("app.repositories.transactions.get_active_refunds_for_expense")
    @patch("app.repositories.transactions.get_transaction")
    @patch("app.repositories.accounts.get_account")
    @patch("app.repositories.reconciliation.get_statement_line")
    @patch("app.repositories.reconciliation.lock_reconciliation_batch")
    def test_resolve_candidate_refund_workflow_and_over_refund(
        self, mock_lock_batch, mock_get_line, mock_get_acc, mock_get_tx, mock_get_refunds,
        mock_audit, mock_summary, mock_recompute
    ):
        mock_lock_batch.return_value = self.batch_row
        mock_get_acc.return_value = self.account_row
        mock_summary.return_value = {"status": "ready"}

        mock_get_tx.return_value = {
            "id": self.orig_expense_id,
            "household_id": self.household_id,
            "status": "committed",
            "transaction_type": "expense",
            "from_account_id": self.account_id,
            "from_amount": Decimal("100.00"),
            "from_currency": "CNY"
        }
        mock_get_refunds.return_value = []

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = self._make_candidate_row(reason_code="INCOME_TRANSFER_REFUND_AMBIGUOUS")

        # 1. Valid credit refund (50.00 <= 100.00) succeeds
        mock_get_line.return_value = self._make_line_row(direction="credit", amount="50.00")
        res = statement_service.resolve_candidate(
            conn=mock_conn,
            candidate_id=self.candidate_id,
            household_id=self.household_id,
            resolution_type="refund",
            original_expense_id=self.orig_expense_id
        )
        self.assertEqual(res["status"], "ready")

        # 2. Over-refund (150.00 > 100.00) raises RefundExceedsOriginalError
        from app.domain.transactions import RefundExceedsOriginalError
        mock_get_line.return_value = self._make_line_row(direction="credit", amount="150.00")
        with self.assertRaises(RefundExceedsOriginalError):
            statement_service.resolve_candidate(
                conn=mock_conn,
                candidate_id=self.candidate_id,
                household_id=self.household_id,
                resolution_type="refund",
                original_expense_id=self.orig_expense_id
            )


if __name__ == "__main__":
    unittest.main()
