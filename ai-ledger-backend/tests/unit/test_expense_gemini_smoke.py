from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.services.gemini_service import ExpenseExtractionResult, ExpenseRevisionResult
from scripts import smoke_expense_gemini as smoke


class ExpenseLiveSmokeTest(TestCase):
    def test_command_checks_facts_and_revision_without_printing_financial_data(self):
        image = Mock(suffix=".png")
        image.stat.return_value.st_size = 10
        image.read_bytes.return_value = b"SECRET_IMAGE"
        args = SimpleNamespace(image=image, amount=Decimal("12.50"), currency="SGD",
            date=date(2026, 9, 20), merchant="SECRET_MERCHANT", revision=True)
        good = ExpenseExtractionResult(original_amount=args.amount, original_currency=args.currency,
            occurred_on=args.date, merchant=args.merchant, intent="expense", date_evidence="visible")
        for result, revision, expected in (
            (good, ExpenseRevisionResult(original_amount=Decimal("13.25")), 0),
            (good.model_copy(update={"intent": "unknown"}), ExpenseRevisionResult(), 1),
            (good, ExpenseRevisionResult(original_amount=Decimal("13.25"), merchant="SECRET_WRONG"), 1),
        ):
            with self.subTest(expected=expected):
                service = Mock()
                service.extract_expense.return_value = result
                service.revise_expense_draft.return_value = revision
                output = StringIO()
                with patch.object(smoke.argparse.ArgumentParser, "parse_args", return_value=args), \
                     patch.object(smoke, "decode_image", return_value=(b"SECRET_IMAGE", "image/png")), \
                     patch.object(smoke, "GeminiService", return_value=service), \
                     patch.object(smoke.logging, "basicConfig"), redirect_stdout(output):
                    self.assertEqual(smoke.main(), expected)
                self.assertNotIn("SECRET", output.getvalue())
                service.extract_expense.assert_called_once()
