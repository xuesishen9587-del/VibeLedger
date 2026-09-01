import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
from uuid import uuid4
from decimal import Decimal
from datetime import date
from unittest.mock import MagicMock, patch

from app.services.expense_service import (
    recompute_draft_warnings,
    revise_ingestion_request
)
from app.services.gemini_service import (
    MockGeminiService,
    ExpenseRevisionResult
)
from app.domain.transactions import (
    GeminiDependencyError,
    AccountNotFoundError,
    CategoryNotFoundError,
    InvalidPaymentModeError,
    InvalidInstallmentPeriodsError
)


class TestExpenseServiceReviseUnit(unittest.TestCase):
    def setUp(self):
        self.household_id = uuid4()
        self.user_id = uuid4()
        self.device_id = uuid4()
        self.device = {
            "device_id": self.device_id,
            "household_id": self.household_id,
            "user_id": self.user_id
        }

        self.acc_checking = {
            "id": uuid4(),
            "household_id": self.household_id,
            "name": "招商银行储蓄卡",
            "account_type": "cash",
            "currency": "CNY",
            "status": "active",
            "aliases": ["CMB Checking", "工资卡"]
        }
        self.acc_credit = {
            "id": uuid4(),
            "household_id": self.household_id,
            "name": "招商银行信用卡",
            "account_type": "credit",
            "currency": "CNY",
            "status": "active",
            "aliases": ["CMB Credit", "招行卡"]
        }
        self.active_accounts = [self.acc_checking, self.acc_credit]

        self.cat_dining = {
            "id": uuid4(),
            "household_id": self.household_id,
            "name": "餐饮美食",
            "category_type": "expense",
            "status": "active"
        }
        self.cat_shopping = {
            "id": uuid4(),
            "household_id": self.household_id,
            "name": "日常购物",
            "category_type": "expense",
            "status": "active"
        }
        self.active_categories = [self.cat_dining, self.cat_shopping]

    def test_recompute_warnings_all_null_draft_never_empty(self):
        """
        Draft with all null fields must NEVER return warnings=[].
        Must produce warnings for amount, currency, account, category, and payment mode.
        """
        draft = {
            "occurred_on": None,
            "merchant": None,
            "original_amount": None,
            "original_currency": None,
            "from_account": None,
            "category": None,
            "payment_mode": None,
            "total_periods": None
        }

        warnings = recompute_draft_warnings(draft, self.active_accounts, self.active_categories)
        self.assertGreater(len(warnings), 0)
        codes = {w["code"] for w in warnings}
        self.assertIn("INVALID_PAYMENT_MODE", codes)
        self.assertIn("ACCOUNT_UNRESOLVED", codes)
        self.assertIn("CURRENCY_UNCLEAR", codes)
        self.assertIn("AMOUNT_UNCLEAR", codes)
        self.assertIn("CATEGORY_UNRESOLVED", codes)

    def test_recompute_warnings_valid_one_off_returns_empty(self):
        """
        When all one-off fields and deterministic relationships are valid, warnings must be [].
        """
        draft = {
            "occurred_on": "2026-08-25",
            "merchant": "星巴克",
            "original_amount": "38.00",
            "original_currency": "CNY",
            "from_account": {"id": str(self.acc_checking["id"]), "name": self.acc_checking["name"]},
            "category": {"id": str(self.cat_dining["id"]), "name": self.cat_dining["name"]},
            "payment_mode": "one_off",
            "total_periods": None
        }

        warnings = recompute_draft_warnings(draft, self.active_accounts, self.active_categories)
        self.assertEqual(warnings, [])

    def test_recompute_warnings_currency_mismatch_on_non_credit_account(self):
        """
        Debit checking account paying mismatched foreign currency must generate CURRENCY_MISMATCH warning.
        """
        draft = {
            "occurred_on": "2026-08-25",
            "merchant": "Amazon",
            "original_amount": "50.00",
            "original_currency": "USD",
            "from_account": {"id": str(self.acc_checking["id"]), "name": self.acc_checking["name"]},
            "category": {"id": str(self.cat_shopping["id"]), "name": self.cat_shopping["name"]},
            "payment_mode": "one_off",
            "total_periods": None
        }

        warnings = recompute_draft_warnings(draft, self.active_accounts, self.active_categories)
        codes = {w["code"] for w in warnings}
        self.assertIn("CURRENCY_MISMATCH", codes)

    def test_recompute_warnings_foreign_credit_card_is_valid(self):
        """
        Credit card account paying foreign currency is supported (estimated FX), so no CURRENCY_MISMATCH.
        """
        draft = {
            "occurred_on": "2026-08-25",
            "merchant": "Amazon",
            "original_amount": "50.00",
            "original_currency": "USD",
            "from_account": {"id": str(self.acc_credit["id"]), "name": self.acc_credit["name"]},
            "category": {"id": str(self.cat_shopping["id"]), "name": self.cat_shopping["name"]},
            "payment_mode": "one_off",
            "total_periods": None
        }

        warnings = recompute_draft_warnings(draft, self.active_accounts, self.active_categories)
        self.assertEqual(warnings, [])

    def test_recompute_warnings_installment_non_credit_account(self):
        """
        Installment mode on non-credit account must generate NON_CREDIT_INSTALLMENT_ACCOUNT.
        """
        draft = {
            "occurred_on": "2026-08-25",
            "merchant": "Apple Store",
            "original_amount": "6000.00",
            "original_currency": "CNY",
            "from_account": {"id": str(self.acc_checking["id"]), "name": self.acc_checking["name"]},
            "category": None,
            "payment_mode": "installment",
            "total_periods": 6
        }

        warnings = recompute_draft_warnings(draft, self.active_accounts, self.active_categories)
        codes = {w["code"] for w in warnings}
        self.assertIn("NON_CREDIT_INSTALLMENT_ACCOUNT", codes)

    def test_recompute_warnings_installment_invalid_periods(self):
        """
        Installment mode with missing or invalid periods must generate INVALID_INSTALLMENT_PERIODS.
        """
        draft_none = {
            "occurred_on": "2026-08-25",
            "merchant": "Apple Store",
            "original_amount": "6000.00",
            "original_currency": "CNY",
            "from_account": {"id": str(self.acc_credit["id"]), "name": self.acc_credit["name"]},
            "category": None,
            "payment_mode": "installment",
            "total_periods": None
        }
        warnings_none = recompute_draft_warnings(draft_none, self.active_accounts, self.active_categories)
        self.assertIn("INVALID_INSTALLMENT_PERIODS", {w["code"] for w in warnings_none})

        draft_bad = dict(draft_none)
        draft_bad["total_periods"] = 1
        warnings_bad = recompute_draft_warnings(draft_bad, self.active_accounts, self.active_categories)
        self.assertIn("INVALID_INSTALLMENT_PERIODS", {w["code"] for w in warnings_bad})

    @patch("app.repositories.accounts.list_accounts")
    @patch("app.repositories.accounts.list_categories")
    @patch("app.repositories.ingestion.lock_ingestion_request")
    @patch("app.repositories.ingestion.update_ingestion_request_status")
    def test_revise_natural_language_full_supplementation(
        self,
        mock_update_status,
        mock_lock_request,
        mock_list_categories,
        mock_list_accounts
    ):
        """
        A mostly-null draft + natural-language note => all fields extracted via Gemini,
        status remains needs_confirmation, same request_id preserved.
        """
        req_id = uuid4()
        initial_draft = {
            "occurred_on": None,
            "merchant": None,
            "original_amount": None,
            "original_currency": None,
            "from_account": None,
            "category": None,
            "payment_mode": None,
            "total_periods": None
        }

        mock_lock_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "draft_payload": initial_draft,
            "response_payload": None
        }
        mock_list_accounts.return_value = self.active_accounts
        mock_list_categories.return_value = self.active_categories

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # Mock cursor for alias query
        mock_cursor.fetchall.return_value = [
            (uuid4(), self.acc_checking["id"], "工资卡", "工资卡", "active"),
            (uuid4(), self.acc_credit["id"], "招行卡", "招行卡", "active")
        ]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_gemini = MockGeminiService()
        mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            occurred_on=date(2026, 8, 25),
            merchant="全家便利店",
            original_amount=Decimal("25.50"),
            original_currency="CNY",
            from_account="招商银行储蓄卡",
            category="餐饮美食",
            payment_mode="one_off",
            total_periods=None
        ))

        res = revise_ingestion_request(
            conn=mock_conn,
            request_id=req_id,
            device=self.device,
            correction_note="全家便利店花了25.5元，用招商银行储蓄卡支付，分类选餐饮美食",
            gemini_service=mock_gemini
        )

        self.assertEqual(res["status"], "needs_confirmation")
        self.assertEqual(res["request_id"], str(req_id))
        draft = res["draft"]
        self.assertEqual(draft["merchant"], "全家便利店")
        self.assertEqual(draft["original_amount"], "25.50")
        self.assertEqual(draft["original_currency"], "CNY")
        self.assertEqual(draft["from_account"]["id"], str(self.acc_checking["id"]))
        self.assertEqual(draft["category"]["id"], str(self.cat_dining["id"]))
        self.assertEqual(draft["payment_mode"], "one_off")
        self.assertEqual(res["warnings"], [])
        self.assertNotIn("None", res["display_summary"])

    @patch("app.repositories.accounts.list_accounts")
    @patch("app.repositories.accounts.list_categories")
    @patch("app.repositories.ingestion.lock_ingestion_request")
    @patch("app.repositories.ingestion.update_ingestion_request_status")
    def test_revise_partial_correction_preserves_existing_values(
        self,
        mock_update_status,
        mock_lock_request,
        mock_list_categories,
        mock_list_accounts
    ):
        """
        Partial correction ('支付卡改成招商银行信用卡') updates account while
        leaving original_amount, currency, category, merchant completely untouched.
        """
        req_id = uuid4()
        initial_draft = {
            "occurred_on": "2026-08-20",
            "merchant": "盒马鲜生",
            "original_amount": "158.00",
            "original_currency": "CNY",
            "from_account": {"id": str(self.acc_checking["id"]), "name": self.acc_checking["name"]},
            "category": {"id": str(self.cat_dining["id"]), "name": self.cat_dining["name"]},
            "payment_mode": "one_off",
            "total_periods": None
        }

        mock_lock_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "draft_payload": initial_draft,
            "response_payload": None
        }
        mock_list_accounts.return_value = self.active_accounts
        mock_list_categories.return_value = self.active_categories

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_gemini = MockGeminiService()
        # Gemini only returns from_account; all others are None
        mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            from_account="招商银行信用卡"
        ))

        res = revise_ingestion_request(
            conn=mock_conn,
            request_id=req_id,
            device=self.device,
            correction_note="支付卡改成招商银行信用卡",
            gemini_service=mock_gemini
        )

        draft = res["draft"]
        # Account was updated
        self.assertEqual(draft["from_account"]["id"], str(self.acc_credit["id"]))
        # Existing values are preserved!
        self.assertEqual(draft["merchant"], "盒马鲜生")
        self.assertEqual(draft["original_amount"], "158.00")
        self.assertEqual(draft["original_currency"], "CNY")
        self.assertEqual(draft["category"]["id"], str(self.cat_dining["id"]))
        self.assertEqual(draft["payment_mode"], "one_off")
        self.assertEqual(res["warnings"], [])

    @patch("app.repositories.accounts.list_accounts")
    @patch("app.repositories.accounts.list_categories")
    @patch("app.repositories.ingestion.lock_ingestion_request")
    @patch("app.repositories.ingestion.update_ingestion_request_status")
    def test_revise_unresolved_account_clears_field_and_warns(
        self,
        mock_update_status,
        mock_lock_request,
        mock_list_categories,
        mock_list_accounts
    ):
        """
        Regression test for user clarification 2:
        When user explicitly changes account to an unresolvable candidate ('火星银行'),
        the draft field MUST be set to null and emit ACCOUNT_UNRESOLVED warning.
        It must NEVER silently retain the previous account!
        """
        req_id = uuid4()
        initial_draft = {
            "occurred_on": "2026-08-20",
            "merchant": "盒马鲜生",
            "original_amount": "158.00",
            "original_currency": "CNY",
            "from_account": {"id": str(self.acc_checking["id"]), "name": self.acc_checking["name"]},
            "category": {"id": str(self.cat_dining["id"]), "name": self.cat_dining["name"]},
            "payment_mode": "one_off",
            "total_periods": None
        }

        mock_lock_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "draft_payload": initial_draft,
            "response_payload": None
        }
        mock_list_accounts.return_value = self.active_accounts
        mock_list_categories.return_value = self.active_categories

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_gemini = MockGeminiService()
        mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            from_account="火星银行信用卡"
        ))

        res = revise_ingestion_request(
            conn=mock_conn,
            request_id=req_id,
            device=self.device,
            correction_note="支付卡改成火星银行信用卡",
            gemini_service=mock_gemini
        )

        draft = res["draft"]
        # Must be null, NOT the old checking account!
        self.assertIsNone(draft["from_account"])
        codes = {w["code"] for w in res["warnings"]}
        self.assertIn("ACCOUNT_UNRESOLVED", codes)

    @patch("app.repositories.accounts.list_accounts")
    @patch("app.repositories.accounts.list_categories")
    @patch("app.repositories.ingestion.lock_ingestion_request")
    @patch("app.repositories.ingestion.update_ingestion_request_status")
    def test_revise_alias_resolution_in_natural_language(
        self,
        mock_update_status,
        mock_lock_request,
        mock_list_categories,
        mock_list_accounts
    ):
        """
        Test that account aliases are loaded and resolved correctly in revise flow (clarification 1).
        """
        req_id = uuid4()
        initial_draft = {
            "occurred_on": "2026-08-20",
            "merchant": "咖啡",
            "original_amount": "30.00",
            "original_currency": "CNY",
            "from_account": None,
            "category": {"id": str(self.cat_dining["id"]), "name": self.cat_dining["name"]},
            "payment_mode": "one_off",
            "total_periods": None
        }

        mock_lock_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "draft_payload": initial_draft,
            "response_payload": None
        }
        mock_list_accounts.return_value = self.active_accounts
        mock_list_categories.return_value = self.active_categories

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (uuid4(), self.acc_credit["id"], "招行卡", "招行卡", "active")
        ]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_gemini = MockGeminiService()
        mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            from_account="招行卡"
        ))

        res = revise_ingestion_request(
            conn=mock_conn,
            request_id=req_id,
            device=self.device,
            correction_note="用招行卡付的",
            gemini_service=mock_gemini
        )

        draft = res["draft"]
        self.assertIsNotNone(draft["from_account"])
        self.assertEqual(draft["from_account"]["id"], str(self.acc_credit["id"]))
        self.assertEqual(res["warnings"], [])

    @patch("app.repositories.accounts.list_accounts")
    @patch("app.repositories.accounts.list_categories")
    @patch("app.repositories.ingestion.lock_ingestion_request")
    @patch("app.repositories.ingestion.update_ingestion_request_status")
    def test_structured_fields_override_natural_language_interpretation(
        self,
        mock_update_status,
        mock_lock_request,
        mock_list_categories,
        mock_list_accounts
    ):
        """
        When both natural-language interpretation and structured_fields are present,
        structured_fields take precedence.
        """
        req_id = uuid4()
        initial_draft = {
            "occurred_on": "2026-08-20",
            "merchant": "初始商户",
            "original_amount": "50.00",
            "original_currency": "CNY",
            "from_account": None,
            "category": None,
            "payment_mode": "one_off",
            "total_periods": None
        }

        mock_lock_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "draft_payload": initial_draft,
            "response_payload": None
        }
        mock_list_accounts.return_value = self.active_accounts
        mock_list_categories.return_value = self.active_categories

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_gemini = MockGeminiService()
        mock_gemini.set_next_revision_result(ExpenseRevisionResult(
            merchant="AI Extracted Merchant",
            original_amount=Decimal("80.00")
        ))

        res = revise_ingestion_request(
            conn=mock_conn,
            request_id=req_id,
            device=self.device,
            correction_note="金额80元",
            structured_fields={
                "merchant": "Caller Overridden Merchant",
                "original_amount": "99.00"
            },
            gemini_service=mock_gemini
        )

        draft = res["draft"]
        # Structured fields won!
        self.assertEqual(draft["merchant"], "Caller Overridden Merchant")
        self.assertEqual(draft["original_amount"], "99.00")

    @patch("app.repositories.accounts.list_accounts")
    @patch("app.repositories.accounts.list_categories")
    @patch("app.repositories.ingestion.lock_ingestion_request")
    @patch("app.repositories.ingestion.update_ingestion_request_status")
    def test_display_summary_never_renders_none_tokens(
        self,
        mock_update_status,
        mock_lock_request,
        mock_list_categories,
        mock_list_accounts
    ):
        """
        When amount/currency/merchant/account/category are missing, display_summary
        renders sensible unknown placeholders and never 'None None · 未知商户'.
        """
        req_id = uuid4()
        initial_draft = {
            "occurred_on": None,
            "merchant": None,
            "original_amount": None,
            "original_currency": None,
            "from_account": None,
            "category": None,
            "payment_mode": None,
            "total_periods": None
        }

        mock_lock_request.return_value = {
            "id": req_id,
            "device_id": self.device_id,
            "status": "needs_confirmation",
            "draft_payload": initial_draft,
            "response_payload": None
        }
        mock_list_accounts.return_value = self.active_accounts
        mock_list_categories.return_value = self.active_categories

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_gemini = MockGeminiService()
        mock_gemini.set_next_revision_result(ExpenseRevisionResult())

        res = revise_ingestion_request(
            conn=mock_conn,
            request_id=req_id,
            device=self.device,
            correction_note="some note",
            gemini_service=mock_gemini
        )

        summary = res["display_summary"]
        self.assertNotIn("None", summary)
        self.assertIn("未知金额", summary)
        self.assertIn("未知商户", summary)
        self.assertIn("未知账户", summary)
        self.assertIn("未分类", summary)


if __name__ == "__main__":
    unittest.main()
