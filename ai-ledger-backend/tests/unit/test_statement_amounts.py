from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
from fastapi import HTTPException
from app.domain.statement_amounts import statement_amount
from app.domain.spending import money
from app.services.statement_import import prepare, repair_display_amount


class StatementAmountsTest(TestCase):
    def test_existing_draft_repair_preserves_user_edits_and_review_flags(self):
        previous={"original_amount":"-12.30","transaction_type":"expense","requires_review":True}
        extracted={"amount":"-12.30","kind":"expense"}
        result=repair_display_amount(previous,extracted,{"action":"create"})
        self.assertEqual(result["original_amount"],"12.30")
        self.assertTrue(result["requires_review"])
        self.assertEqual(previous["original_amount"],"-12.30")
        for change in ({"original_amount":"-7.00"},{"transaction_type":"refund"}):
            self.assertEqual(repair_display_amount(previous,extracted,change),previous)
        self.assertEqual(repair_display_amount({**previous,"original_amount":"-15.00"},extracted,{} )["original_amount"],"-15.00")
        self.assertEqual(repair_display_amount(previous,{**extracted,"kind":"unknown"},{}),previous)
    def test_opposite_bank_sign_conventions_preserve_kind_and_magnitude(self):
        for kind in ("expense", "fee", "refund", "repayment", "transfer", "income", "investment_trade"):
            for value in ("-123.40", "+123.40", "123.40"):
                with self.subTest(kind=kind, value=value):
                    self.assertEqual(statement_amount(value, kind), "123.40")

    def test_unknown_and_balance_rows_do_not_infer_direction(self):
        for kind in ("unknown", "opening_balance", "asset", "debt"):
            self.assertEqual(statement_amount("-12.00", kind), "-12.00")

    def test_invalid_and_zero_values_still_fail_financial_validation(self):
        for value in ("0", "-0.00", "NaN", "Infinity", "-Infinity", "1,234.56", "(12.00)", "12.001", "100000000000000", None):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                money(statement_amount(value, "expense"), "CNY")
        self.assertEqual(statement_amount("-12.00", "unknown"), "-12.00")

    def test_draft_normalizes_without_overwriting_original_evidence(self):
        categories = [{"id": uuid4(), "name": "Other", "is_fallback": True}]
        data = {"account_hint": "Wallet", "account_currency": "CNY", "account_confidence": .99,
                "period_start": "2026-02-01", "period_end": "2026-02-28", "actual_page_count": 1,
                "processed_pages": [1], "expected_line_count": 3, "complete": True,
                "lines": [{"kind": kind, "amount": "-12.30", "currency": "CNY", "merchant": "Cafe",
                           "occurred_on": "2026-02-03", "category": "Other",
                           "confidence": {k: .99 for k in ("amount", "currency", "date", "intent", "category")}}
                          for kind in ("expense", "refund", "repayment")]}
        original = deepcopy(data)
        with patch("app.services.statement_import.repo.rows", return_value=[]) as rows:
            result = prepare(None, SimpleNamespace(household_id=uuid4()), {"id": uuid4()}, data,
                             {"id": uuid4(), "name": "Wallet", "currency": "CNY", "aliases": []}, categories, {})
        self.assertEqual(data, original)
        self.assertTrue(all(r["original_amount"] == "12.30" for r in result["lines"]))
        self.assertEqual([r["transaction_type"] for r in result["lines"]], ["expense", "refund", None])
        self.assertEqual(result["lines"][1]["remarks"], "账单退款 · Cafe")
        self.assertEqual(result["lines"][2]["action"], "skip")
        self.assertIn('"-12.30"', rows.call_args_list[0].args[2][-1])
