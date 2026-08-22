import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
import uuid
from decimal import Decimal
from app.domain.transactions import (
    calculate_projection_deltas,
    InvalidTransactionShapeError,
    SameAccountTransferError,
    LedgerDomainError
)

class TestTransactionsDomain(unittest.TestCase):
    def test_expense_and_fee_deltas(self):
        acc_id = uuid.uuid4()
        deltas = calculate_projection_deltas(
            transaction_type="expense",
            from_account_id=acc_id,
            to_account_id=None,
            from_amount=Decimal("150.00"),
            to_amount=None
        )
        self.assertEqual(deltas, {acc_id: Decimal("-150.00")})

        fee_deltas = calculate_projection_deltas(
            transaction_type="fee",
            from_account_id=acc_id,
            to_account_id=None,
            from_amount=Decimal("5.00"),
            to_amount=None
        )
        self.assertEqual(fee_deltas, {acc_id: Decimal("-5.00")})

    def test_income_and_refund_deltas(self):
        acc_id = uuid.uuid4()
        inc_deltas = calculate_projection_deltas(
            transaction_type="cash_income",
            from_account_id=None,
            to_account_id=acc_id,
            from_amount=None,
            to_amount=Decimal("500.00")
        )
        self.assertEqual(inc_deltas, {acc_id: Decimal("500.00")})

        ref_deltas = calculate_projection_deltas(
            transaction_type="refund",
            from_account_id=None,
            to_account_id=acc_id,
            from_amount=None,
            to_amount=Decimal("50.00")
        )
        self.assertEqual(ref_deltas, {acc_id: Decimal("50.00")})

    def test_transfer_deltas_and_same_account_rejection(self):
        acc_a = uuid.uuid4()
        acc_b = uuid.uuid4()
        transfer_deltas = calculate_projection_deltas(
            transaction_type="transfer",
            from_account_id=acc_a,
            to_account_id=acc_b,
            from_amount=Decimal("200.00"),
            to_amount=Decimal("200.00")
        )
        self.assertEqual(transfer_deltas, {acc_a: Decimal("-200.00"), acc_b: Decimal("200.00")})

        # Same account transfer rejected
        with self.assertRaises(SameAccountTransferError):
            calculate_projection_deltas(
                transaction_type="transfer",
                from_account_id=acc_a,
                to_account_id=acc_a,
                from_amount=Decimal("200.00"),
                to_amount=Decimal("200.00")
            )

    def test_opening_balance_and_adjustments(self):
        acc_id = uuid.uuid4()
        # Positive opening balance
        pos_deltas = calculate_projection_deltas(
            transaction_type="opening_balance",
            from_account_id=None,
            to_account_id=acc_id,
            from_amount=None,
            to_amount=Decimal("1000.00")
        )
        self.assertEqual(pos_deltas, {acc_id: Decimal("1000.00")})

        # Negative opening balance
        neg_deltas = calculate_projection_deltas(
            transaction_type="opening_balance",
            from_account_id=acc_id,
            to_account_id=None,
            from_amount=Decimal("500.00"),
            to_amount=None
        )
        self.assertEqual(neg_deltas, {acc_id: Decimal("-500.00")})

        # Both legs specified must raise error
        with self.assertRaises(InvalidTransactionShapeError):
            calculate_projection_deltas(
                transaction_type="opening_balance",
                from_account_id=acc_id,
                to_account_id=acc_id,
                from_amount=Decimal("100.00"),
                to_amount=Decimal("100.00")
            )
