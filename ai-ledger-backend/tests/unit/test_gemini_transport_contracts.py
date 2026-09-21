from pathlib import Path
import unittest

import app.services.balance_extractor as balance_extractor
import app.services.gemini_service as gemini_service
import app.services.statement_document as statement_document


class GeminiTransportContractTests(unittest.TestCase):

    def _source(self, module):
        return Path(module.__file__).read_text(encoding="utf-8")

    def test_expense_transport_is_plain_and_local_validation_remains(self):
        source = self._source(gemini_service)
        self.assertNotIn("response_schema=", source)
        self.assertNotIn("model_json_schema()", source)
        for name, model in [("_EXPENSE_EXTRACTION_TRANSPORT_SCHEMA", "ExpenseExtractionTransportSchema"),
                            ("_EXPENSE_REVISION_TRANSPORT_SCHEMA", "ExpenseRevisionTransportSchema")]:
            self.assertIn("response_json_schema=" + name, source)
            self.assertIn(model + ".model_validate(data)", source)
            schema = getattr(gemini_service, name)
            self.assertEqual(schema["type"], "object")
            self.assertEqual(set(schema["properties"]), set(getattr(gemini_service, model).model_fields))
            import json
            encoded = json.dumps(schema)
            for rich_keyword in ("$ref", "$defs", "anyOf", "format", "additionalProperties", "default"):
                self.assertNotIn('"' + rich_keyword + '"', encoded)

    def test_balance_uses_simplified_transport_schema(self):
        source = self._source(balance_extractor)

        self.assertIn(
            "response_json_schema=_BALANCE_TRANSPORT_SCHEMA",
            source,
        )
        self.assertNotIn(
            "response_schema=BalanceExtraction",
            source,
        )
        self.assertNotIn(
            "BalanceExtraction.model_json_schema()",
            source,
        )

    def test_statement_document_uses_simplified_transport_schema(self):
        source = self._source(statement_document)

        self.assertIn(
            "response_json_schema=_STATEMENT_TRANSPORT_SCHEMA",
            source,
        )
        self.assertNotIn(
            "response_schema=StatementExtraction",
            source,
        )
        self.assertNotIn(
            "StatementExtraction.model_json_schema()",
            source,
        )

    def test_all_gemini_defaults_use_35_flash_lite(self):
        modules = [
            balance_extractor,
            gemini_service,
            statement_document,
        ]

        combined = "\n".join(self._source(m) for m in modules)

        self.assertNotIn("gemini-2.5-flash", combined)
        self.assertIn("gemini-3.5-flash-lite", combined)

    def test_transport_schemas_are_plain_json_schema_dicts(self):
        self.assertIsInstance(
            balance_extractor._BALANCE_TRANSPORT_SCHEMA,
            dict,
        )
        self.assertIsInstance(
            statement_document._STATEMENT_TRANSPORT_SCHEMA,
            dict,
        )

        self.assertEqual(
            balance_extractor._BALANCE_TRANSPORT_SCHEMA["type"],
            "object",
        )
        self.assertEqual(
            statement_document._STATEMENT_TRANSPORT_SCHEMA["type"],
            "object",
        )


if __name__ == "__main__":
    unittest.main()
