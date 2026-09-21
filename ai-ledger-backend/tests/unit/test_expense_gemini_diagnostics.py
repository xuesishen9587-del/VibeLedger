import json
from unittest import TestCase
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from google.genai.errors import ClientError, ServerError
from app.domain.transactions import GeminiDependencyError
from app.services.gemini_service import GeminiService
from app.services.gemini_diagnostics import request_context


class ExpenseGeminiDiagnosticsTest(TestCase):
    def invoke(self, service, revision):
        if revision:
            return service.revise_expense_draft({"merchant": "SECRET_DRAFT"}, "SECRET_NOTE", [], [])
        return service.extract_expense(b"SECRET_IMAGE", "image/png", "SECRET_NOTE", [], [])

    def test_failures_are_categorized_without_logging_sensitive_data(self):
        cases = [
            (ClientError(400, {"error": {"message": "SECRET_UPSTREAM schema"}}), None, "upstream", "400"),
            (ServerError(503, {"error": {"message": "SECRET_UPSTREAM"}}), None, "upstream", "503"),
            (ServerError(504, {"error": {"message": "SECRET_UPSTREAM"}}), None, "timeout", "504"),
            (httpx.ReadTimeout("SECRET_UPSTREAM"), None, "timeout", "None"),
            (TimeoutError("SECRET_UPSTREAM"), None, "timeout", "None"),
            (None, "SECRET_RESPONSE invalid json", "response_parse", "None"),
            (None, None, "response_parse", "None"),
            (None, json.dumps({"occurred_on": "SECRET_BAD_DATE"}), "response_validation", "None"),
            (None, json.dumps({"original_amount": "SECRET_BAD_AMOUNT"}), "response_validation", "None"),
            (None, json.dumps({"intent": "SECRET_BAD_INTENT"}), "response_validation", "None"),
            (None, json.dumps({"unexpected": "SECRET_EXTRA"}), "response_validation", "None"),
        ]
        for revision in (False, True):
            for failure, text, category, status in cases:
                with self.subTest(revision=revision, category=category, failure=type(failure).__name__):
                    client = MagicMock()
                    client.models.generate_content.side_effect = failure
                    client.models.generate_content.return_value.text = text
                    identity = str(uuid4())
                    with patch("google.genai.Client", return_value=client), request_context(identity), self.assertLogs("app.gemini", level="WARNING") as logs:
                        with self.assertRaises(GeminiDependencyError) as raised:
                            self.invoke(GeminiService(api_key="SECRET_KEY"), revision)
                    output = "\n".join(logs.output)
                    self.assertIn("request_id=" + identity, output)
                    self.assertIn("category=" + category, output)
                    self.assertIn("upstream_status=" + status, output)
                    self.assertIn("exception_type=", output)
                    self.assertNotIn("SECRET", output + str(raised.exception))
                    self.assertTrue(all(r.exc_info is None for r in logs.records))

    def test_confidence_bounds_and_unknown_fields_still_fail_local_validation(self):
        for payload in ({"confidence": 1.5}, {"field_confidence": {"amount": -0.1}},
                        {"field_confidence": {"intent": 2}}, {"field_confidence": {"surprise": 1}}):
            client = MagicMock()
            client.models.generate_content.return_value.text = json.dumps(payload)
            with patch("google.genai.Client", return_value=client), self.assertLogs("app.gemini"):
                with self.assertRaises(GeminiDependencyError):
                    self.invoke(GeminiService(api_key="test"), False)

    def test_request_context_is_reset_and_missing_fields_stay_conservative(self):
        client = MagicMock()
        client.models.generate_content.return_value.text = "{}"
        with patch("google.genai.Client", return_value=client):
            result = self.invoke(GeminiService(api_key="test"), False)
            self.assertEqual(result.intent, "unknown")
            self.assertEqual(result.date_evidence, "uncertain")
            self.assertEqual(result.field_confidence, {})
            self.assertIsNone(result.original_currency)
            self.assertIsNone(result.payment_mode)
            with request_context(uuid4()):
                pass
            client.models.generate_content.side_effect = RuntimeError("SECRET")
            with self.assertLogs("app.gemini") as logs, self.assertRaises(GeminiDependencyError):
                self.invoke(GeminiService(api_key="test"), False)
            self.assertIn("request_id=None", logs.output[0])
