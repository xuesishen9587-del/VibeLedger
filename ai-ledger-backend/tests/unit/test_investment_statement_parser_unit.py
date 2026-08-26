import unittest
from decimal import Decimal
from datetime import date
from unittest.mock import MagicMock, patch

from app.domain.investments import (
    InvestmentCapitalFlow,
    InvestmentStatementExtractionResult
)
from app.domain.transactions import StatementParseFailedError
from app.services.statement_parser import (
    MockStatementParser,
    GeminiStatementParser,
    validate_and_normalize_investment_extraction
)


class TestInvestmentStatementParserUnit(unittest.TestCase):
    """
    Unit tests for investment statement parsing and normalization.
    """

    def setUp(self):
        self.account_cny = {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "IBKR Investment",
            "institution": "Interactive Brokers",
            "currency": "CNY",
            "account_type": "investment"
        }
        self.account_usd = {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Charles Schwab",
            "institution": "Schwab",
            "currency": "USD",
            "account_type": "investment"
        }

    def test_mock_investment_parser_default(self):
        parser = MockStatementParser()
        # Mock pdf extraction without real file
        with patch("app.services.statement_parser.extract_pdf_pages_text", return_value=[(1, "Mock PDF Content")]):
            result = parser.extract_investment_statement("dummy.pdf", account_context=self.account_usd)
            self.assertEqual(result.total_asset_value, Decimal("100000.00"))
            self.assertEqual(result.currency, "USD")
            self.assertEqual(result.clear_capital_flows, [])
            self.assertTrue(result.capital_flow_evidence_complete)

    def test_validate_and_normalize_valid_valuation_only(self):
        extraction = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("150000.00"),
            currency="CNY",
            valuation_as_of=date(2026, 7, 31),
            statement_period_start=date(2026, 7, 1),
            statement_period_end=date(2026, 7, 31),
            clear_capital_flows=[]
        )

        (
            tot_val, curr, val_as_of, p_start, p_end, op_val, op_as_of, norm_flows, complete
        ) = validate_and_normalize_investment_extraction(extraction, self.account_cny)

        self.assertEqual(tot_val, Decimal("150000.00"))
        self.assertEqual(curr, "CNY")
        self.assertEqual(val_as_of, date(2026, 7, 31))
        self.assertEqual(len(norm_flows), 0)
        self.assertTrue(complete)

    def test_validate_and_normalize_with_flows(self):
        extraction = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("200000.00"),
            currency="USD",
            valuation_as_of=date(2026, 8, 31),
            clear_capital_flows=[
                InvestmentCapitalFlow(
                    direction="contribution",
                    amount=Decimal("5000.00"),
                    currency="USD",
                    occurred_on=date(2026, 8, 10),
                    description="Wire deposit from checking"
                ),
                InvestmentCapitalFlow(
                    direction="withdrawal",
                    amount=Decimal("1000.00"),
                    currency="USD",
                    occurred_on=date(2026, 8, 20),
                    description="Wire withdrawal to savings"
                )
            ]
        )

        (
            tot_val, curr, val_as_of, p_start, p_end, op_val, op_as_of, norm_flows, complete
        ) = validate_and_normalize_investment_extraction(extraction, self.account_usd)

        self.assertEqual(tot_val, Decimal("200000.00"))
        self.assertEqual(len(norm_flows), 2)
        self.assertEqual(norm_flows[0].direction, "contribution")
        self.assertEqual(norm_flows[0].amount, Decimal("5000.00"))
        self.assertEqual(norm_flows[1].direction, "withdrawal")
        self.assertEqual(norm_flows[1].amount, Decimal("1000.00"))

    def test_missing_total_asset_value_rejected(self):
        extraction = InvestmentStatementExtractionResult(
            total_asset_value=None,
            currency="USD"
        )
        with self.assertRaises(StatementParseFailedError):
            validate_and_normalize_investment_extraction(extraction, self.account_usd)

    def test_negative_total_asset_value_rejected(self):
        extraction = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("-500.00"),
            currency="USD"
        )
        with self.assertRaises(StatementParseFailedError):
            validate_and_normalize_investment_extraction(extraction, self.account_usd)

    def test_currency_mismatch_rejected(self):
        extraction = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("100000.00"),
            currency="EUR"
        )
        with self.assertRaises(StatementParseFailedError):
            validate_and_normalize_investment_extraction(extraction, self.account_usd)

    def test_flow_currency_mismatch_rejected(self):
        extraction = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("100000.00"),
            currency="USD",
            clear_capital_flows=[
                InvestmentCapitalFlow(
                    direction="contribution",
                    amount=Decimal("1000.00"),
                    currency="CNY"
                )
            ]
        )
        with self.assertRaises(StatementParseFailedError):
            validate_and_normalize_investment_extraction(extraction, self.account_usd)

    def test_redundant_nav_consistency_check(self):
        # Mismatch between total_asset_value and metadata nav_ending_value
        extraction = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("100000.00"),
            currency="USD",
            metadata={"nav_ending_value": "95000.00"}
        )
        with self.assertRaises(StatementParseFailedError):
            validate_and_normalize_investment_extraction(extraction, self.account_usd)

    def test_gemini_investment_statement_parser_mocked(self):
        mock_response = MagicMock()
        mock_response.text = '''{
            "total_asset_value": "29135.31",
            "currency": "USD",
            "valuation_as_of": "2026-07-31",
            "statement_period_start": "2026-07-01",
            "statement_period_end": "2026-07-31",
            "opening_total_asset_value": "32729.83",
            "opening_valuation_as_of": "2026-06-30",
            "clear_capital_flows": [],
            "capital_flow_evidence_complete": true,
            "broker_reported_pnl": "-3589.17"
        }'''

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        parser = GeminiStatementParser(client=mock_client)
        with patch("app.services.statement_parser.extract_pdf_pages_text", return_value=[(1, "IBKR Activity Statement Content")]):
            result = parser.extract_investment_statement("fake.pdf", account_context=self.account_usd)
            self.assertEqual(result.total_asset_value, Decimal("29135.31"))
            self.assertEqual(result.currency, "USD")
            self.assertEqual(result.valuation_as_of, date(2026, 7, 31))
            self.assertEqual(result.opening_total_asset_value, Decimal("32729.83"))
            self.assertEqual(result.clear_capital_flows, [])
            self.assertTrue(result.capital_flow_evidence_complete)
            self.assertEqual(result.broker_reported_pnl, Decimal("-3589.17"))

    def test_missing_valuation_date_fails_closed(self):
        extraction = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("150000.00"),
            currency="CNY",
            valuation_as_of=None,
            statement_period_start=None,
            statement_period_end=None,
            clear_capital_flows=[]
        )
        with self.assertRaises(StatementParseFailedError):
            validate_and_normalize_investment_extraction(extraction, self.account_cny)

    def test_capital_flow_evidence_complete_boolean_normalization(self):
        # Missing or None -> False
        ext1 = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("150000.00"),
            currency="CNY",
            valuation_as_of=date(2026, 7, 31),
            capital_flow_evidence_complete=None
        )
        *_, comp1 = validate_and_normalize_investment_extraction(ext1, self.account_cny)
        self.assertFalse(comp1)

        # False -> False
        ext2 = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("150000.00"),
            currency="CNY",
            valuation_as_of=date(2026, 7, 31),
            capital_flow_evidence_complete=False
        )
        *_, comp2 = validate_and_normalize_investment_extraction(ext2, self.account_cny)
        self.assertFalse(comp2)

        # String "false" -> False
        ext3 = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("150000.00"),
            currency="CNY",
            valuation_as_of=date(2026, 7, 31),
            capital_flow_evidence_complete="false"  # type: ignore
        )
        *_, comp3 = validate_and_normalize_investment_extraction(ext3, self.account_cny)
        self.assertFalse(comp3)

        # True -> True
        ext4 = InvestmentStatementExtractionResult(
            total_asset_value=Decimal("150000.00"),
            currency="CNY",
            valuation_as_of=date(2026, 7, 31),
            capital_flow_evidence_complete=True
        )
        *_, comp4 = validate_and_normalize_investment_extraction(ext4, self.account_cny)
        self.assertTrue(comp4)


if __name__ == "__main__":
    unittest.main()

