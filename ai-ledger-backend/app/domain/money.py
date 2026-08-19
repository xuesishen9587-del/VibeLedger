from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Union

def parse_decimal(value: Union[str, int, Decimal]) -> Decimal:
    """
    Safely converts string, int, or Decimal to Decimal.
    Strictly rejects float types to prevent precision loss.
    """
    if isinstance(value, float):
        raise TypeError("Float values are strictly rejected to prevent precision loss. Use string, int, or Decimal.")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as e:
        raise ValueError(f"Failed to parse value to Decimal: {value}. Error: {e}")

def validate_currency_code(code: str) -> str:
    """
    Validates currency code matches regex ^[A-Z]{3}$ and normalizes to uppercase.
    """
    if not isinstance(code, str):
        raise TypeError("Currency code must be a string.")
    normalized = code.strip().upper()
    if not re.match(r"^[A-Z]{3}$", normalized):
        raise ValueError(f"Invalid currency code format: '{code}'. Must be a 3-letter alphabetic code.")
    return normalized

def quantize_money(amount: Decimal, currency: str) -> Decimal:
    """
    Quantizes amount to the currency's minor units (e.g., JPY -> 0 decimal places, others -> 2 decimal places).
    """
    dec_amount = parse_decimal(amount)
    curr = validate_currency_code(currency)
    
    if curr == "JPY":
        target = Decimal("1")
    else:
        target = Decimal("0.01")
        
    return dec_amount.quantize(target, rounding=ROUND_HALF_UP)

def validate_fx_rate(rate: Decimal) -> Decimal:
    """
    Validates that the FX rate is positive and conforms to NUMERIC(24,12) bounds.
    """
    dec_rate = parse_decimal(rate)
    if dec_rate <= 0:
        raise ValueError(f"FX rate must be positive. Given: {rate}")
    if dec_rate >= Decimal("1e12"):
        raise ValueError(f"FX rate out of bounds for NUMERIC(24,12). Given: {rate}")
    return dec_rate

def quantize_reporting(amount: Decimal) -> Decimal:
    """
    Standard NUMERIC(20,6) scale representation.
    """
    dec_amount = parse_decimal(amount)
    return dec_amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
