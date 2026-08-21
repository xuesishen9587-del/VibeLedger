from decimal import Decimal
from typing import List, Union
from app.domain.money import parse_decimal, validate_currency_code, quantize_money
from app.domain.transactions import InvalidTransactionShapeError, InvalidAmountError

def calculate_installment_schedule(
    total_amount: Union[str, int, Decimal],
    currency: str,
    total_periods: int
) -> List[Decimal]:
    """
    Calculates exact scheduled amounts for N installment periods.
    Allocates equal quantized amounts to periods 1 .. N-1, and allocates
    any rounding remainder strictly to period N.
    Sum of all periods is guaranteed to equal total_amount exactly.
    """
    if total_periods < 2 or total_periods > 120:
        raise InvalidTransactionShapeError(f"total_periods must be between 2 and 120. Given: {total_periods}")

    curr = validate_currency_code(currency)
    dec_total = quantize_money(parse_decimal(total_amount), curr)
    if dec_total <= 0:
        raise InvalidAmountError(f"Installment total amount must be strictly positive. Given: {total_amount}")

    base_period_amount = quantize_money(dec_total / Decimal(total_periods), curr)
    sum_first_n_minus_1 = base_period_amount * Decimal(total_periods - 1)
    final_period_amount = dec_total - sum_first_n_minus_1

    schedule = [base_period_amount] * (total_periods - 1)
    schedule.append(final_period_amount)
    return schedule
