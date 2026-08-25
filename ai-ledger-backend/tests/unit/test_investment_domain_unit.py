import unittest
from decimal import Decimal
from datetime import date

from app.domain.investments import (
    InvestmentCapitalFlow,
    InvestmentStatementExtractionResult,
    calculate_investment_pnl
)


class TestInvestmentDomainUnit(unittest.TestCase):
    """
    Unit tests for investment domain models and canonical P&L formula:
        P&L = closing_value - opening_value - contributions + withdrawals
    """

    def test_positive_pnl_with_contribution(self):
        # Opening: 100,000; Contribution: 50,000; Closing: 160,000
        # P&L = 160,000 - 100,000 - 50,000 + 0 = 10,000
        pnl = calculate_investment_pnl(
            opening_value=Decimal("100000.00"),
            closing_value=Decimal("160000.00"),
            contributions=Decimal("50000.00"),
            withdrawals=Decimal("0.00"),
            currency="CNY"
        )
        self.assertEqual(pnl, Decimal("10000.00"))

    def test_positive_pnl_with_withdrawal(self):
        # Opening: 100,000; Withdrawal: 20,000; Closing: 90,000
        # P&L = 90,000 - 100,000 - 0 + 20,000 = 10,000
        pnl = calculate_investment_pnl(
            opening_value=Decimal("100000.00"),
            closing_value=Decimal("90000.00"),
            contributions=Decimal("0.00"),
            withdrawals=Decimal("20000.00"),
            currency="CNY"
        )
        self.assertEqual(pnl, Decimal("10000.00"))

    def test_negative_pnl_without_flows(self):
        # Opening: 100,000; No flows; Closing: 90,000
        # P&L = 90,000 - 100,000 - 0 + 0 = -10,000
        pnl = calculate_investment_pnl(
            opening_value=Decimal("100000.00"),
            closing_value=Decimal("90000.00"),
            contributions=Decimal("0.00"),
            withdrawals=Decimal("0.00"),
            currency="CNY"
        )
        self.assertEqual(pnl, Decimal("-10000.00"))

    def test_pnl_with_both_contribution_and_withdrawal(self):
        # Opening: 100,000; Contribution: 30,000; Withdrawal: 15,000; Closing: 120,000
        # P&L = 120,000 - 100,000 - 30,000 + 15,000 = 5,000
        pnl = calculate_investment_pnl(
            opening_value=Decimal("100000.00"),
            closing_value=Decimal("120000.00"),
            contributions=Decimal("30000.00"),
            withdrawals=Decimal("15000.00"),
            currency="USD"
        )
        self.assertEqual(pnl, Decimal("5000.00"))

    def test_pnl_negative_contributions_or_withdrawals_rejected(self):
        with self.assertRaises(ValueError):
            calculate_investment_pnl(
                opening_value=Decimal("100.00"),
                closing_value=Decimal("150.00"),
                contributions=Decimal("-10.00"),
                withdrawals=Decimal("0.00"),
                currency="USD"
            )

        with self.assertRaises(ValueError):
            calculate_investment_pnl(
                opening_value=Decimal("100.00"),
                closing_value=Decimal("150.00"),
                contributions=Decimal("0.00"),
                withdrawals=Decimal("-10.00"),
                currency="USD"
            )

    def test_pnl_quantization_minor_units(self):
        # JPY (0 decimal places)
        pnl_jpy = calculate_investment_pnl(
            opening_value=Decimal("100000.45"),
            closing_value=Decimal("120000.80"),
            contributions=Decimal("0.00"),
            withdrawals=Decimal("0.00"),
            currency="JPY"
        )
        self.assertEqual(pnl_jpy, Decimal("20000"))

    def test_real_broker_statement_calibration_fixture(self):
        """
        Regression modeled on Section 18B:
        Statement:
            Opening NAV: 32729.83 USD as of 2026-06-30
            Closing NAV: 29135.31 USD as of 2026-07-31
            Ending Cash: 8312.16
            Ending Stock Value: 20823.15
            Security Purchase: 3415.00
            Broker-reported Mark-to-Market: -3595.50
            Broker Interest: 6.33
            Change in interest accrual: -5.35
            Broker-reported Total P/L: -3589.17
            External Deposits: None
            External Withdrawals: None
        """
        opening_nav = Decimal("32729.83")
        closing_nav = Decimal("29135.31")
        external_contributions = Decimal("0.00")
        external_withdrawals = Decimal("0.00")

        # Canonical VibeLedger P&L: 29135.31 - 32729.83 - 0 + 0 = -3594.52 USD
        canonical_pnl = calculate_investment_pnl(
            opening_value=opening_nav,
            closing_value=closing_nav,
            contributions=external_contributions,
            withdrawals=external_withdrawals,
            currency="USD"
        )
        self.assertEqual(canonical_pnl, Decimal("-3594.52"))

        # Broker-reported -3589.17 is NOT canonical P&L
        broker_reported_pnl = Decimal("-3589.17")
        self.assertNotEqual(canonical_pnl, broker_reported_pnl)

    def test_investment_capital_flow_model_validation(self):
        flow = InvestmentCapitalFlow(
            direction="contribution",
            amount=Decimal("5000.00"),
            currency="USD",
            occurred_on=date(2026, 7, 15)
        )
        self.assertEqual(flow.direction, "contribution")
        self.assertEqual(flow.amount, Decimal("5000.00"))

        with self.assertRaises(ValueError):
            InvestmentCapitalFlow(
                direction="invalid_direction",
                amount=Decimal("100.00"),
                currency="USD"
            )

        with self.assertRaises(ValueError):
            InvestmentCapitalFlow(
                direction="contribution",
                amount=Decimal("-10.00"),
                currency="USD"
            )


if __name__ == "__main__":
    unittest.main()
