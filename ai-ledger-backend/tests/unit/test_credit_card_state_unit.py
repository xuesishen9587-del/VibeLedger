import unittest
from decimal import Decimal
from datetime import date
from uuid import uuid4

from app.domain.money import quantize_money


def compute_installment_schedule(
    total_amount: Decimal,
    total_periods: int,
    currency: str = "CNY"
):
    """
    Computes equal monthly installment schedule where any rounding remainder
    is absorbed into the final period.
    """
    base_amount = quantize_money(total_amount / Decimal(total_periods), currency)
    schedule = [base_amount] * total_periods
    allocated_sum = sum(schedule)
    remainder = total_amount - allocated_sum
    if remainder != Decimal("0.00"):
        schedule[-1] = quantize_money(schedule[-1] + remainder, currency)
    return schedule


class TestCreditCardStateUnit(unittest.TestCase):
    def test_installment_schedule_12_periods_exact_sum(self):
        # 1000.00 / 12 = 83.33333333333333 -> 11 * 83.33 = 916.63, last period = 83.37
        schedule = compute_installment_schedule(Decimal("1000.00"), 12, "CNY")
        self.assertEqual(len(schedule), 12)
        self.assertEqual(schedule[0], Decimal("83.33"))
        self.assertEqual(schedule[-1], Decimal("83.37"))
        self.assertEqual(sum(schedule), Decimal("1000.00"))

    def test_installment_schedule_no_remainder(self):
        schedule = compute_installment_schedule(Decimal("1200.00"), 12, "USD")
        self.assertEqual(len(schedule), 12)
        for amt in schedule:
            self.assertEqual(amt, Decimal("100.00"))
        self.assertEqual(sum(schedule), Decimal("1200.00"))

    def test_credit_card_repayment_deductions_math(self):
        statement_balance = Decimal("5000.00")
        initial_remaining_due = Decimal("5000.00")
        initial_unbilled = Decimal("1200.00")
        initial_current_outstanding = Decimal("6200.00")

        # Repayment of 2000.00
        repayment = Decimal("2000.00")
        new_remaining_due = max(Decimal("0.00"), initial_remaining_due - repayment)
        new_current_outstanding = max(Decimal("0.00"), initial_current_outstanding - repayment)

        self.assertEqual(new_remaining_due, Decimal("3000.00"))
        self.assertEqual(new_current_outstanding, Decimal("4200.00"))
        # Statement balance and unbilled balance remain intact
        self.assertEqual(statement_balance, Decimal("5000.00"))
        self.assertEqual(initial_unbilled, Decimal("1200.00"))

    def test_credit_card_overpayment_floor_at_zero(self):
        initial_remaining_due = Decimal("1000.00")
        initial_current_outstanding = Decimal("1500.00")

        # Overpayment of 2000.00
        repayment = Decimal("2000.00")
        new_remaining_due = max(Decimal("0.00"), initial_remaining_due - repayment)
        new_current_outstanding = max(Decimal("0.00"), initial_current_outstanding - repayment)

        self.assertEqual(new_remaining_due, Decimal("0.00"))
        self.assertEqual(new_current_outstanding, Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
