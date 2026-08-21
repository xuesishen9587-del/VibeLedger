from typing import Optional, Dict, Tuple, Union
from decimal import Decimal
from datetime import date
from app.domain.money import parse_decimal, validate_currency_code, quantize_money
from app.domain.transactions import LedgerDomainError

class ReferenceFxService:
    """
    Reference FX rate provider for Product v1 credit card expense settlement estimation.
    Uses strictly Decimal arithmetic.
    """
    def __init__(self, fixed_rates: Optional[Dict[Tuple[str, str], Decimal]] = None):
        self._fixed_rates: Dict[Tuple[str, str], Decimal] = {}
        if fixed_rates:
            for (f, t), r in fixed_rates.items():
                self._fixed_rates[(f.upper(), t.upper())] = parse_decimal(r)

    def set_rate(self, from_currency: str, to_currency: str, rate: Union[str, int, Decimal]) -> None:
        self._fixed_rates[(from_currency.upper(), to_currency.upper())] = parse_decimal(rate)

    def get_rate(self, from_currency: str, to_currency: str, as_of: Optional[date] = None) -> Optional[Decimal]:
        from_curr = validate_currency_code(from_currency)
        to_curr = validate_currency_code(to_currency)
        if from_curr == to_curr:
            return Decimal("1")

        if (from_curr, to_curr) in self._fixed_rates:
            return self._fixed_rates[(from_curr, to_curr)]

        if (to_curr, from_curr) in self._fixed_rates:
            inv = self._fixed_rates[(to_curr, from_curr)]
            if inv > 0:
                return Decimal("1") / inv

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
            raise LedgerDomainError(
                f"No reference FX rate available for {orig_curr} -> {acc_curr}.",
                code="FX_RATE_UNAVAILABLE"
            )

        estimated_amount = quantize_money(original_amount * rate, acc_curr)
        return estimated_amount, rate
