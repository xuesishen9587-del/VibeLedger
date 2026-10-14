from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import TestCase
from unittest.mock import MagicMock, patch
from uuid import uuid4
from app.auth.context import AuthContext
from app.services import expense_capture, expense_review
from app.services.gemini_service import ExpenseExtractionResult


class ExpenseReviewTests(TestCase):
    def setUp(self):
        self.actor = AuthContext("device", uuid4(), uuid4(), "member", uuid4())
        self.category = {"id": uuid4(), "name": "Other", "is_fallback": True}
        self.draft = {"intent": "expense", "original_amount": "12.50", "original_currency": "CNY",
            "occurred_on": "2026-01-02", "merchant": "Test Store", "from_account": None,
            "category": {"id": str(self.category["id"]), "name": "Other"}, "payment_mode": "one_off",
            "field_confidence": {k: .99 for k in ("amount", "currency", "date", "intent", "category", "account")}}

    def validate(self, draft):
        with patch.object(expense_capture, "references", return_value=([], [self.category])), \
             patch.object(expense_capture.schema, "get_household", return_value={"timezone": "Asia/Singapore"}), \
             patch.object(expense_capture.repo, "duplicate_candidates", return_value=[]):
            return expense_capture.validate_draft(MagicMock(), self.actor, draft, {})

    def test_low_ai_confidence_can_be_confirmed_when_facts_are_valid(self):
        for name in ("amount", "currency", "date", "intent"):
            draft = {**self.draft, "field_confidence": {**self.draft["field_confidence"], name: .4}}
            validated, _, blocked = self.validate(draft)
            self.assertTrue(blocked)
            self.assertTrue(validated["confirmation_ready"])
            self.assertEqual(expense_review.presentation(validated)["next_action"], "confirm")

    def test_missing_invalid_facts_require_supplement_not_confirmation(self):
        for changes in ({"original_amount": None}, {"original_amount": "-12.50"}, {"original_currency": None},
                        {"occurred_on": None}, {"occurred_on": "2999-01-01"}, {"intent": "unknown"}, {"payment_mode": None}):
            with self.subTest(changes=changes):
                validated, _, blocked = self.validate({**self.draft, **changes})
                self.assertTrue(blocked)
                self.assertFalse(validated["confirmation_ready"])
                self.assertEqual(expense_review.presentation(validated)["next_action"], "supplement")

    def test_unknown_account_is_optional_and_not_guessed(self):
        draft, fields, blocked = self.validate(self.draft)
        self.assertFalse(blocked)
        self.assertTrue(draft["confirmation_ready"])
        self.assertIsNone(fields["account_id"])
        view = expense_review.presentation(draft)
        self.assertIn("支付账户：未识别", view["display_summary"])
        self.assertIn("可输入银行名称和卡尾号", view["supplement_prompt"])

    def test_summary_has_all_six_fields_and_does_not_expose_technical_ids(self):
        draft, _, _ = self.validate(self.draft)
        view = expense_review.presentation({**draft, "remarks": "Test note"})
        for label in ("金额：12.50 CNY", "商户：Test Store", "分类：Other", "支付账户：未识别", "日期：2026-01-02", "分期：非分期", "备注：Test note"):
            self.assertIn(label, view["display_summary"])
        self.assertNotIn(str(self.category["id"]), view["display_summary"])
        self.assertEqual(set(view), {"display_summary", "confirmation_ready", "next_action", "supplement_prompt"})

    def test_installment_always_recommends_web_without_changing_confirm_rules(self):
        for action, ready in ((None, False), ("record_full_purchase", True)):
            draft, _, _ = self.validate({**self.draft, "payment_mode": "installment", "total_periods": 12, "action": action})
            self.assertEqual(draft["confirmation_ready"], ready)
            view = expense_review.presentation(draft)
            self.assertEqual(view["next_action"], "review_in_web")
            self.assertEqual(view["supplement_prompt"], "检测到分期，请在网页端处理。")
            self.assertIn("分期：12期", view["display_summary"])

    def test_note_becomes_remarks_but_is_not_a_user_correction(self):
        result = ExpenseExtractionResult(original_amount=Decimal("12.50"), original_currency="CNY")
        for note, expected in ((None, None), ("", None), ("  ", None), ("User note", "User note")):
            draft = expense_capture.initial_draft(result, datetime(2026, 1, 2, tzinfo=timezone.utc),
                {"timezone": "Asia/Singapore"}, [], [], note=note)
            self.assertEqual(draft["remarks"], expected)
            self.assertEqual(draft["user_fields"], [])

    def test_legacy_view_does_not_invent_readiness(self):
        self.assertEqual(expense_review.presentation(self.draft)["next_action"], "recover")
        self.assertFalse(expense_review.presentation(self.draft)["confirmation_ready"])
