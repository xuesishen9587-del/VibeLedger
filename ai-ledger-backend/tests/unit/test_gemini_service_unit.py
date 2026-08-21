import unittest
from decimal import Decimal
from datetime import date
from app.services.gemini_service import (
    GeminiService,
    MockGeminiService,
    ExpenseExtractionResult
)
from app.domain.transactions import GeminiDependencyError

class TestGeminiServiceUnit(unittest.TestCase):
    def test_build_system_prompt_includes_accounts_and_aliases(self):
        service = GeminiService(api_key="mock_key")
        accounts = [
            {"name": "Checking", "account_type": "cash", "currency": "CNY", "aliases": ["CMB", "Payroll Card"]},
            {"name": "Visa", "account_type": "credit", "currency": "USD", "aliases": []}
        ]
        categories = [
            {"name": "Dining", "category_type": "expense"},
            {"name": "Salary", "category_type": "income"} # Income category must not be in expense categories list
        ]
        
        prompt = service.build_system_prompt(accounts, categories)
        
        self.assertIn("Checking [cash, CNY] (aliases: CMB, Payroll Card)", prompt)
        self.assertIn("Visa [credit, USD]", prompt)
        self.assertIn("- Dining", prompt)
        self.assertNotIn("- Salary", prompt) # Filtered out non-expense
        self.assertIn("DO NOT default to CNY", prompt)

    def test_mock_gemini_service_queue_and_exceptions(self):
        mock_service = MockGeminiService()
        
        # 1. Default result
        res1 = mock_service.extract_expense(b"bytes", "image/png", "lunch", [], [])
        self.assertEqual(res1.merchant, "Test Merchant")
        self.assertEqual(res1.original_amount, Decimal("268.00"))
        self.assertEqual(mock_service.call_count, 1)

        # 2. Queued custom result
        custom = ExpenseExtractionResult(
            occurred_on=date(2026, 8, 20),
            merchant="Custom Store",
            original_amount=Decimal("99.50"),
            original_currency="USD",
            confidence=0.95
        )
        mock_service.set_next_result(custom)
        res2 = mock_service.extract_expense(b"bytes", "image/png", None, [], [])
        self.assertEqual(res2.merchant, "Custom Store")
        self.assertEqual(res2.original_amount, Decimal("99.50"))
        self.assertEqual(mock_service.call_count, 2)

        # 3. Exception simulation
        mock_service.should_raise = GeminiDependencyError("AI rate limit reached")
        with self.assertRaises(GeminiDependencyError):
            mock_service.extract_expense(b"bytes", "image/png", None, [], [])
        self.assertEqual(mock_service.call_count, 3)

    def test_expense_extraction_result_decimal_parsing(self):
        res = ExpenseExtractionResult(
            original_amount=Decimal("150.75"),
            original_currency="CNY",
            payment_mode="one_off",
            total_amount=Decimal("150.75")
        )
        self.assertIsInstance(res.original_amount, Decimal)
        self.assertEqual(res.original_amount, Decimal("150.75"))
        self.assertIsNone(res.occurred_on)
