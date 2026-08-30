import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
import json
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

    def test_gemini_response_schema_has_no_additional_properties(self):
        """
        Verify that ExpenseExtractionTransportSchema generates strict JSON schema
        without additionalProperties, adhering to Gemini Developer API constraints.
        """
        from app.services.gemini_service import ExpenseExtractionTransportSchema
        import json

        # 1. Inspect Pydantic JSON Schema
        pydantic_schema = ExpenseExtractionTransportSchema.model_json_schema()
        pydantic_schema_str = json.dumps(pydantic_schema)
        self.assertNotIn("additionalProperties", pydantic_schema_str)

        # 2. Inspect Google GenAI SDK transformed Schema
        try:
            from google.genai import _transformers
            sdk_schema = _transformers.t_schema(None, ExpenseExtractionTransportSchema)
            sdk_dump = json.dumps(sdk_schema.model_dump(by_alias=True, exclude_none=True) if hasattr(sdk_schema, "model_dump") else str(sdk_schema))
            self.assertNotIn("additionalProperties", sdk_dump)
            self.assertNotIn("additional_properties", sdk_dump)
        except ImportError:
            pass

    def test_gemini_json_response_maps_correctly_to_extraction_result(self):
        """
        Verify that a structured JSON output from Gemini parses into ExpenseExtractionTransportSchema
        and maps cleanly to ExpenseExtractionResult with Dict[str, float] field_confidence.
        """
        from app.services.gemini_service import ExpenseExtractionTransportSchema

        gemini_json_payload = {
            "occurred_on": "2026-08-30",
            "merchant": "FairPrice Finest",
            "original_amount": "45.80",
            "original_currency": "SGD",
            "from_account": "DBS Multiplier",
            "category": "Groceries",
            "payment_mode": "one_off",
            "total_amount": "45.80",
            "total_periods": None,
            "confidence": 0.96,
            "field_confidence": {
                "amount": 0.99,
                "currency": 0.95,
                "account": 0.90,
                "category": 0.85,
                "date": 0.99,
                "total_periods": None
            }
        }

        transport = ExpenseExtractionTransportSchema.model_validate(gemini_json_payload)
        result = ExpenseExtractionResult.from_transport(transport, raw_response=gemini_json_payload)

        self.assertEqual(result.occurred_on, date(2026, 8, 30))
        self.assertEqual(result.merchant, "FairPrice Finest")
        self.assertEqual(result.original_amount, Decimal("45.80"))
        self.assertEqual(result.original_currency, "SGD")
        self.assertEqual(result.from_account, "DBS Multiplier")
        self.assertEqual(result.category, "Groceries")
        self.assertEqual(result.payment_mode, "one_off")
        self.assertEqual(result.total_amount, Decimal("45.80"))
        self.assertIsNone(result.total_periods)
        self.assertEqual(result.confidence, 0.96)
        self.assertEqual(result.field_confidence, {
            "amount": 0.99,
            "currency": 0.95,
            "account": 0.90,
            "category": 0.85,
            "date": 0.99
        })
        self.assertEqual(result.raw_response, gemini_json_payload)

    def test_missing_field_confidence_is_conservative(self):
        """
        Verify conservative fallback when field_confidence is completely omitted or partially populated.
        Downstream .get(field, 0.0) lookups must return 0.0 and not default to high confidence.
        """
        from app.services.gemini_service import ExpenseExtractionTransportSchema

        # Case A: Entire field_confidence object is None/omitted
        payload_none = {
            "merchant": "Mystery Store",
            "original_amount": "100.00",
            "original_currency": "USD",
            "field_confidence": None
        }
        transport_none = ExpenseExtractionTransportSchema.model_validate(payload_none)
        result_none = ExpenseExtractionResult.from_transport(transport_none, raw_response=payload_none)

        self.assertEqual(result_none.field_confidence, {})
        self.assertEqual(result_none.field_confidence.get("amount", 0.0), 0.0)
        self.assertEqual(result_none.field_confidence.get("currency", 0.0), 0.0)
        self.assertEqual(result_none.field_confidence.get("account", 0.0), 0.0)
        self.assertEqual(result_none.field_confidence.get("category", 0.0), 0.0)
        self.assertEqual(result_none.field_confidence.get("date", 0.0), 0.0)

        # Case B: Partial field_confidence
        payload_partial = {
            "merchant": "Target",
            "original_amount": "25.00",
            "field_confidence": {
                "amount": 0.92
            }
        }
        transport_partial = ExpenseExtractionTransportSchema.model_validate(payload_partial)
        result_partial = ExpenseExtractionResult.from_transport(transport_partial, raw_response=payload_partial)

        self.assertEqual(result_partial.field_confidence, {"amount": 0.92})
        self.assertEqual(result_partial.field_confidence.get("amount", 0.0), 0.92)
        self.assertEqual(result_partial.field_confidence.get("currency", 0.0), 0.0)
        self.assertEqual(result_partial.field_confidence.get("date", 0.0), 0.0)

    def test_gemini_service_extract_expense_with_mocked_client(self):
        """
        Verify GeminiService.extract_expense properly sets response_schema to ExpenseExtractionTransportSchema
        and handles response conversion end-to-end.
        """
        from unittest.mock import MagicMock, patch
        from app.services.gemini_service import ExpenseExtractionTransportSchema

        service = GeminiService(api_key="test_api_key")
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "occurred_on": "2026-08-30",
            "merchant": "Mock Shop",
            "original_amount": "12.50",
            "original_currency": "CNY",
            "from_account": "Alipay",
            "category": "Snacks",
            "payment_mode": "one_off",
            "confidence": 0.95,
            "field_confidence": {
                "amount": 0.95,
                "currency": 0.95,
                "account": 0.90,
                "category": 0.85,
                "date": 0.95
            }
        })

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = service.extract_expense(
                image_bytes=b"fake_image_bytes",
                mime_type="image/jpeg",
                note="snack",
                accounts=[{"name": "Alipay", "account_type": "cash", "currency": "CNY"}],
                categories=[{"name": "Snacks", "category_type": "expense"}]
            )

            # Assert client generate_content was called
            self.assertTrue(mock_client.models.generate_content.called)
            call_kwargs = mock_client.models.generate_content.call_args[1]
            config = call_kwargs["config"]
            self.assertEqual(config.response_schema, ExpenseExtractionTransportSchema)

            # Assert parsed result
            self.assertEqual(result.merchant, "Mock Shop")
            self.assertEqual(result.original_amount, Decimal("12.50"))
            self.assertEqual(result.original_currency, "CNY")
            self.assertEqual(result.field_confidence.get("amount", 0.0), 0.95)
            self.assertEqual(result.field_confidence.get("currency", 0.0), 0.95)
            self.assertEqual(result.field_confidence.get("account", 0.0), 0.90)
            self.assertEqual(result.field_confidence.get("category", 0.0), 0.85)
            self.assertEqual(result.field_confidence.get("date", 0.0), 0.95)
            self.assertEqual(result.field_confidence.get("total_periods", 0.0), 0.0)
