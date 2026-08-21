import json
import urllib.request
import urllib.error
import socket
from typing import Optional, Dict, Tuple, Union
from decimal import Decimal
from datetime import date, timedelta
from app.domain.money import parse_decimal, validate_currency_code, quantize_money
from app.domain.transactions import FxRateUnavailableError, FxProviderUnavailableError
from app.config import get_settings

class FxRateProvider:
    """
    Abstract interface for reference FX rate providers.
    """
    def fetch_rate(self, from_currency: str, to_currency: str, as_of: Optional[date] = None) -> Optional[Decimal]:
        raise NotImplementedError

class FrankfurterFxProvider(FxRateProvider):
    """
    Production-ready reference FX provider backed by ECB/Frankfurter open reference rates.
    Supports current/T-1 published rates and previous business-day fallback for weekends/holidays.
    Uses strictly Decimal arithmetic and bounded HTTP timeouts.
    """
    def __init__(self, base_url: Optional[str] = None, timeout_seconds: Optional[float] = None):
        settings = get_settings()
        self.base_url = (base_url or settings.FX_API_BASE_URL).rstrip('/')
        self.timeout = timeout_seconds or settings.FX_HTTP_TIMEOUT_SECONDS

    def _format_date(self, d: Optional[date]) -> str:
        if d is None:
            return "latest"
        # If weekend, fallback to Friday
        weekday = d.weekday()  # Monday is 0, Sunday is 6
        if weekday == 5:  # Saturday
            d = d - timedelta(days=1)
        elif weekday == 6:  # Sunday
            d = d - timedelta(days=2)
        return d.isoformat()

    def fetch_rate(self, from_currency: str, to_currency: str, as_of: Optional[date] = None) -> Optional[Decimal]:
        from_curr = validate_currency_code(from_currency)
        to_curr = validate_currency_code(to_currency)
        if from_curr == to_curr:
            return Decimal("1")

        date_str = self._format_date(as_of)
        url = f"{self.base_url}/{date_str}?from={from_curr}&to={to_curr}"

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "VibeLedger/1.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(resp.read().decode('utf-8'), parse_float=Decimal)
                rates = data.get("rates", {})
                if to_curr in rates:
                    rate_val = rates[to_curr]
                    return parse_decimal(rate_val) if not isinstance(rate_val, Decimal) else rate_val
                return None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # If historical date is unavailable, try previous business day fallback
                if as_of is not None:
                    prev_date = as_of - timedelta(days=1)
                    if prev_date.weekday() == 6:  # Sunday
                        prev_date = prev_date - timedelta(days=2)
                    elif prev_date.weekday() == 5:  # Saturday
                        prev_date = prev_date - timedelta(days=1)
                    try:
                        fallback_url = f"{self.base_url}/{prev_date.isoformat()}?from={from_curr}&to={to_curr}"
                        req = urllib.request.Request(
                            fallback_url,
                            headers={"User-Agent": "VibeLedger/1.0", "Accept": "application/json"}
                        )
                        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                            if resp.status == 200:
                                data = json.loads(resp.read().decode('utf-8'), parse_float=Decimal)
                                rates = data.get("rates", {})
                                if to_curr in rates:
                                    rate_val = rates[to_curr]
                                    return parse_decimal(rate_val) if not isinstance(rate_val, Decimal) else rate_val
                    except urllib.error.HTTPError as fe:
                        if 500 <= fe.code < 600:
                            raise FxProviderUnavailableError(f"Reference FX service returned HTTP {fe.code}.")
                    except (urllib.error.URLError, socket.timeout, TimeoutError) as fe:
                        raise FxProviderUnavailableError(f"Reference FX service connection timed out or failed: {fe}")
                    except Exception:
                        pass
                return None
            elif 500 <= e.code < 600:
                raise FxProviderUnavailableError(f"Reference FX service returned HTTP {e.code}.")
            return None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            raise FxProviderUnavailableError(f"Reference FX service connection timed out or failed: {e}")
        except Exception as e:
            if isinstance(e, (FxProviderUnavailableError, FxRateUnavailableError)):
                raise
            raise FxProviderUnavailableError(f"Reference FX service error: {e}")

class ReferenceFxService:
    """
    Reference FX rate service for Product v1 credit card expense settlement estimation.
    Uses strictly Decimal arithmetic.
    """
    def __init__(
        self,
        fixed_rates: Optional[Dict[Tuple[str, str], Decimal]] = None,
        provider: Optional[FxRateProvider] = None
    ):
        self._fixed_rates: Dict[Tuple[str, str], Decimal] = {}
        if fixed_rates:
            for (f, t), r in fixed_rates.items():
                self._fixed_rates[(f.upper(), t.upper())] = parse_decimal(r)
        self.provider = provider or FrankfurterFxProvider()

    def set_rate(self, from_currency: str, to_currency: str, rate: Union[str, int, Decimal]) -> None:
        self._fixed_rates[(from_currency.upper(), to_currency.upper())] = parse_decimal(rate)

    def get_rate(self, from_currency: str, to_currency: str, as_of: Optional[date] = None) -> Optional[Decimal]:
        from_curr = validate_currency_code(from_currency)
        to_curr = validate_currency_code(to_currency)
        if from_curr == to_curr:
            return Decimal("1")

        # 1. Check fixed/injected rates first
        if (from_curr, to_curr) in self._fixed_rates:
            return self._fixed_rates[(from_curr, to_curr)]

        if (to_curr, from_curr) in self._fixed_rates:
            inv = self._fixed_rates[(to_curr, from_curr)]
            if inv > 0:
                return Decimal("1") / inv

        # 2. Query provider
        if self.provider:
            rate = self.provider.fetch_rate(from_curr, to_curr, as_of)
            if rate is not None and rate > 0:
                return rate
            # Try inverse from provider
            inv_rate = self.provider.fetch_rate(to_curr, from_curr, as_of)
            if inv_rate is not None and inv_rate > 0:
                return Decimal("1") / inv_rate

        return None

    def estimate_settlement(
        self,
        original_amount: Decimal,
        original_currency: str,
        account_currency: str,
        as_of: Optional[date] = None
    ) -> Tuple[Decimal, Decimal]:
        """
        Estimates the settlement amount in card account currency.
        Returns: (estimated_from_amount, effective_fx_rate).
        Formula:
          from_amount = quantize_money(original_amount * rate, account_currency)
        """
        orig_curr = validate_currency_code(original_currency)
        acc_curr = validate_currency_code(account_currency)
        if orig_curr == acc_curr:
            return quantize_money(original_amount, acc_curr), Decimal("1")

        rate = self.get_rate(orig_curr, acc_curr, as_of)
        if rate is None or rate <= 0:
            raise FxRateUnavailableError(
                f"No reference FX rate available for {orig_curr} -> {acc_curr}."
            )

        estimated_amount = quantize_money(original_amount * rate, acc_curr)
        return estimated_amount, rate
