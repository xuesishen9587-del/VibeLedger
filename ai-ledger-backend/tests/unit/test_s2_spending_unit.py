import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from app.domain.spending import money, metadata_reasons
from app.domain.spending_schedules import due_date, due_periods
from app.services.reference_fx_service import FrankfurterFxProvider


class SpendingUnitTests(unittest.TestCase):
    def test_supported_minor_units_and_bounds(self):
        self.assertEqual(money("12", "JPY"), Decimal(12))
        for amount, currency in (("12.1", "JPY"), ("1.001", "CNY"), ("1", "XXX"),
                                 ("NaN", "CNY"), ("Infinity", "CNY"), (0, "USD"), (True, "USD"), (1.1, "USD")):
            with self.subTest(amount=amount, currency=currency), self.assertRaises(HTTPException):
                money(amount, currency)

    def test_leap_year_and_return_to_original_day(self):
        self.assertEqual(due_date(date(2024, 1, 1), 31, 2), date(2024, 2, 29))
        self.assertEqual(due_date(date(2024, 1, 1), 31, 3), date(2024, 3, 31))
        self.assertEqual(due_date(date(2025, 12, 1), 31, 2), date(2026, 1, 31))
        self.assertEqual(due_periods({"start_month": date(2026, 1, 1), "day_of_month": 31,
                                     "period_count": None}, date(2026, 1, 30)), [])

    def test_metadata_acknowledgement_keeps_other_reason(self):
        self.assertEqual(metadata_reasons({"account_review_acknowledged": True, "category_uncertain": True}), ["CATEGORY_UNCERTAIN"])
        self.assertEqual(metadata_reasons({"account_id": "known", "category_uncertain": False}), [])

    def test_fx_effective_date_not_request_date(self):
        provider = FrankfurterFxProvider.__new__(FrankfurterFxProvider)
        provider.base_url, provider.timeout = "https://example.test", 5
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"date":"2026-01-02","rates":{"CNY":7.2}}'
        with patch("urllib.request.urlopen", return_value=response):
            quote = provider.fetch_quote("USD", "CNY", date(2026, 1, 4))
            self.assertEqual(quote["rate_as_of"], date(2026, 1, 2))
            self.assertEqual(quote["rate"], Decimal("7.2"))
            self.assertIsNone(provider.fetch_quote("USD", "CNY", date(2026, 1, 1)))
            self.assertIsNone(provider.fetch_quote("USD", "CNY", date(2026, 1, 10)))
