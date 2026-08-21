import unittest
from decimal import Decimal
from app.domain import money

class TestMoney(unittest.TestCase):
    def test_parse_decimal(self):
        self.assertEqual(money.parse_decimal("123.45"), Decimal("123.45"))
        self.assertEqual(money.parse_decimal(100), Decimal("100"))
        self.assertEqual(money.parse_decimal(Decimal("10.01")), Decimal("10.01"))
        
        # Float must be strictly rejected
        with self.assertRaises(TypeError):
            money.parse_decimal(12.34)

    def test_exact_arithmetic(self):
        # 0.1 + 0.2 using parse_decimal must be exactly 0.3
        d1 = money.parse_decimal("0.1")
        d2 = money.parse_decimal("0.2")
        self.assertEqual(d1 + d2, Decimal("0.3"))

    def test_validate_currency_code(self):
        self.assertEqual(money.validate_currency_code("cny"), "CNY")
        self.assertEqual(money.validate_currency_code("USD"), "USD")
        self.assertEqual(money.validate_currency_code("  jpy  "), "JPY")
        
        with self.assertRaises(ValueError):
            money.validate_currency_code("US")
        with self.assertRaises(ValueError):
            money.validate_currency_code("CNY1")
        with self.assertRaises(TypeError):
            money.validate_currency_code(123)

    def test_quantize_money(self):
        # CNY / USD: 2 decimal places
        self.assertEqual(money.quantize_money(Decimal("100.567"), "CNY"), Decimal("100.57"))
        self.assertEqual(money.quantize_money("123.444", "USD"), Decimal("123.44"))
        
        # JPY: 0 decimal places
        self.assertEqual(money.quantize_money(Decimal("1234.56"), "JPY"), Decimal("1235"))
        self.assertEqual(money.quantize_money("500.2", "JPY"), Decimal("500"))

    def test_validate_fx_rate(self):
        self.assertEqual(money.validate_fx_rate(Decimal("7.2456")), Decimal("7.2456"))
        self.assertEqual(money.validate_fx_rate("0.0001"), Decimal("0.0001"))
        
        with self.assertRaises(ValueError):
            money.validate_fx_rate(Decimal("-1.0"))
        with self.assertRaises(ValueError):
            money.validate_fx_rate("0")
        with self.assertRaises(ValueError):
            money.validate_fx_rate(Decimal("1e13")) # Out of bounds

    def test_quantize_reporting(self):
        # Standard NUMERIC(20,6) scale
        self.assertEqual(money.quantize_reporting(Decimal("1.23456789")), Decimal("1.234568"))
        self.assertEqual(money.quantize_reporting("10"), Decimal("10.000000"))
