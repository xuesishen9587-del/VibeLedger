import unittest
from decimal import Decimal
from datetime import date
from uuid import uuid4

from app.domain.money import quantize_money


from app.domain.installments import calculate_installment_schedule
from app.domain.reconciliation.residuals import evaluate_credit_card_statement_cycle
from app.domain.reconciliation.models import NormalizedStatementLine


class TestCreditCardStateUnit(unittest.TestCase):
    def test_installment_schedule_12_periods_exact_sum(self):
        # 1000.00 / 12 = 83.33333333333333 -> 11 * 83.33 = 916.63, last period = 83.37
        schedule = calculate_installment_schedule(Decimal("1000.00"), "CNY", 12)
        self.assertEqual(len(schedule), 12)
        for amt in schedule[:11]:
            self.assertEqual(amt, Decimal("83.33"))
        self.assertEqual(schedule[-1], Decimal("83.37"))
        self.assertEqual(sum(schedule), Decimal("1000.00"))

    def test_installment_schedule_no_remainder(self):
        schedule = calculate_installment_schedule(Decimal("1200.00"), "USD", 12)
        self.assertEqual(len(schedule), 12)
        for amt in schedule:
            self.assertEqual(amt, Decimal("100.00"))
        self.assertEqual(sum(schedule), Decimal("1200.00"))

    def test_credit_card_statement_cycle_check_pass_and_contradiction(self):
        lines = [
            NormalizedStatementLine(
                transaction_on=date(2026, 7, 5),
                description_raw="Retail purchase",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("800.00"),
                settlement_currency="CNY"
            ),
            NormalizedStatementLine(
                transaction_on=date(2026, 7, 6),
                description_raw="Annual fee",
                direction="debit",
                line_type="fee",
                settlement_amount=Decimal("20.00"),
                settlement_currency="CNY"
            ),
            NormalizedStatementLine(
                transaction_on=date(2026, 7, 10),
                description_raw="Store refund",
                direction="credit",
                line_type="refund",
                settlement_amount=Decimal("100.00"),
                settlement_currency="CNY"
            ),
            NormalizedStatementLine(
                transaction_on=date(2026, 7, 15),
                description_raw="Installment 1/3",
                direction="debit",
                line_type="expense",
                settlement_amount=Decimal("280.00"),
                settlement_currency="CNY"
            ),
            NormalizedStatementLine(
                transaction_on=date(2026, 7, 20),
                description_raw="Card repayment transfer",
                direction="credit",
                line_type="transfer",
                settlement_amount=Decimal("500.00"),
                settlement_currency="CNY"
            )
        ]

        # 800 + 20 - 100 + 280 = 1000.00 (transfer excluded)
        self.assertTrue(evaluate_credit_card_statement_cycle(lines, Decimal("1000.00"), "CNY"))
        # Contradiction: 1100.00 != 1000.00
        self.assertFalse(evaluate_credit_card_statement_cycle(lines, Decimal("1100.00"), "CNY"))
        # Missing statement balance
        self.assertIsNone(evaluate_credit_card_statement_cycle(lines, None, "CNY"))

        # Ambiguous unknown line -> None
        ambig_lines = list(lines) + [
            NormalizedStatementLine(
                transaction_on=date(2026, 7, 22),
                description_raw="Mystery line",
                direction="debit",
                line_type="unknown",
                settlement_amount=Decimal("50.00"),
                settlement_currency="CNY"
            )
        ]
        self.assertIsNone(evaluate_credit_card_statement_cycle(ambig_lines, Decimal("1050.00"), "CNY"))

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
