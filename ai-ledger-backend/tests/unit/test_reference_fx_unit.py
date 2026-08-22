import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
from decimal import Decimal
from datetime import date
from unittest.mock import patch, MagicMock
import urllib.error
from app.services.reference_fx_service import (
    ReferenceFxService,
    FrankfurterFxProvider,
    FxRateProvider
)
from app.domain.transactions import FxRateUnavailableError, FxProviderUnavailableError

class TestReferenceFxUnit(unittest.TestCase):
    def test_fixed_rates_and_inverse(self):
        service = ReferenceFxService(fixed_rates={("USD", "CNY"): Decimal("7.20")})
        
        # Direct rate
        self.assertEqual(service.get_rate("USD", "CNY"), Decimal("7.20"))
        # Same currency
        self.assertEqual(service.get_rate("USD", "USD"), Decimal("1"))
        # Inverse rate
        inv = service.get_rate("CNY", "USD")
        self.assertEqual(inv, Decimal("1") / Decimal("7.20"))
        self.assertIsInstance(inv, Decimal)
        
        # Settlement estimation
        est_cny, rate = service.estimate_settlement(Decimal("100.00"), "USD", "CNY")
        self.assertEqual(est_cny, Decimal("720.00"))
        self.assertEqual(rate, Decimal("7.20"))

    def test_weekend_and_date_formatting(self):
        provider = FrankfurterFxProvider(base_url="https://mock.fx")
        
        # None -> latest
        self.assertEqual(provider._format_date(None), "latest")
        
        # Friday 2026-08-21 -> 2026-08-21
        self.assertEqual(provider._format_date(date(2026, 8, 21)), "2026-08-21")
        
        # Saturday 2026-08-22 -> Friday 2026-08-21
        self.assertEqual(provider._format_date(date(2026, 8, 22)), "2026-08-21")
        
        # Sunday 2026-08-23 -> Friday 2026-08-21
        self.assertEqual(provider._format_date(date(2026, 8, 23)), "2026-08-21")

    def test_mocked_network_boundary_success_and_failures(self):
        provider = FrankfurterFxProvider(base_url="https://mock.fx")
        
        # 1. Successful HTTP 200 response
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"amount":1.0,"base":"USD","date":"2026-08-21","rates":{"CNY":7.2456}}'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        
        with patch("urllib.request.urlopen", return_value=mock_resp):
            rate = provider.fetch_rate("USD", "CNY", as_of=date(2026, 8, 21))
            self.assertEqual(rate, Decimal("7.2456"))

        # 2. HTTP 503 error raises FxProviderUnavailableError
        http_err_503 = urllib.error.HTTPError("https://mock.fx", 503, "Service Unavailable", {}, None)
        with patch("urllib.request.urlopen", side_effect=http_err_503):
            with self.assertRaises(FxProviderUnavailableError):
                provider.fetch_rate("USD", "CNY", as_of=date(2026, 8, 21))

        # 3. Connection timeout / network error raises FxProviderUnavailableError
        with patch("urllib.request.urlopen", side_effect=TimeoutError("Connection timed out")):
            with self.assertRaises(FxProviderUnavailableError):
                provider.fetch_rate("USD", "CNY", as_of=date(2026, 8, 21))
