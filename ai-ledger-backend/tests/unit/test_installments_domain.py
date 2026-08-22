import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
from decimal import Decimal
from app.domain.installments import calculate_installment_schedule
from app.domain.transactions import InvalidTransactionShapeError, InvalidAmountError

class TestInstallmentsDomain(unittest.TestCase):
    def test_exact_installment_schedule_and_remainder_allocation(self):
        # 100 CNY divided into 3 periods -> 33.33, 33.33, 33.34
        schedule = calculate_installment_schedule("100.00", "CNY", 3)
        self.assertEqual(len(schedule), 3)
        self.assertEqual(schedule[0], Decimal("33.33"))
        self.assertEqual(schedule[1], Decimal("33.33"))
        self.assertEqual(schedule[2], Decimal("33.34"))
        self.assertEqual(sum(schedule), Decimal("100.00"))

    def test_zero_decimal_currency_allocation(self):
        # 1000 JPY divided into 3 periods -> 333, 333, 334
        schedule = calculate_installment_schedule(1000, "JPY", 3)
        self.assertEqual(len(schedule), 3)
        self.assertEqual(schedule[0], Decimal("333"))
        self.assertEqual(schedule[1], Decimal("333"))
        self.assertEqual(schedule[2], Decimal("334"))
        self.assertEqual(sum(schedule), Decimal("1000"))

    def test_invalid_period_bounds_and_amounts(self):
        # Periods must be between 2 and 120
        with self.assertRaises(InvalidTransactionShapeError):
            calculate_installment_schedule("100.00", "CNY", 1)
        with self.assertRaises(InvalidTransactionShapeError):
            calculate_installment_schedule("100.00", "CNY", 121)

        # Amount must be strictly positive
        with self.assertRaises(InvalidAmountError):
            calculate_installment_schedule("0.00", "CNY", 3)
        with self.assertRaises(InvalidAmountError):
            calculate_installment_schedule("-50.00", "CNY", 3)
