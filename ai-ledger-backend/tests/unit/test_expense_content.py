from datetime import datetime, timezone
from unittest import TestCase
from uuid import uuid4
from pydantic import ValidationError
from app.domain.expense_content import content_fields, extracted_content, command_payload
from app.api.routes.transactions import CreateTransactionRequest, PatchTransactionRequest
from app.api.routes.spending_schedules import CreateSchedule
from app.services.expense_capture import initial_draft
from app.services.expense_review import summary
from app.services.gemini_service import ExpenseExtractionResult, ExpenseExtractionTransportSchema


class ExpenseContentTests(TestCase):
    def extraction(self, **changes):
        return {"expense_content": "西少爷（新辰里店）-美团App-261019112007000001",
                "expense_content_evidence": "西少爷（新辰里店）-美团App-261019112007000001",
                "expense_content_source": "screenshot", "expense_content_confidence": .99, **changes}

    def test_requires_evidence_and_independent_confidence(self):
        for changes in ({"expense_content_confidence": .5}, {"expense_content_evidence": None},
                        {"expense_content_source": None}, {"expense_content": "京东平台商户"}):
            self.assertEqual(extracted_content(self.extraction(**changes)), content_fields(None))
        self.assertEqual(extracted_content(self.extraction())["expense_content"], "西少爷（新辰里店）")

    def test_overlong_optional_model_content_does_not_discard_financial_facts(self):
        parsed = ExpenseExtractionTransportSchema(original_amount="34.60", expense_content="商品" * 200,
                                                  expense_content_evidence="商品" * 500)
        self.assertEqual(str(parsed.original_amount), "34.60")
        self.assertIsNone(parsed.expense_content)
        self.assertIsNone(parsed.expense_content_evidence)

    def test_item_counts_must_appear_in_evidence(self):
        data = self.extraction(expense_content="牛奶、纸巾等3件商品", expense_content_evidence="牛奶、纸巾")
        self.assertEqual(extracted_content(data)["expense_content"], "牛奶、纸巾")
        data["expense_content_evidence"] = "共3件：牛奶、纸巾、洗手液"
        self.assertEqual(extracted_content(data)["expense_content"], "牛奶、纸巾等3件商品")
        data["expense_content_evidence"] = "共13件：牛奶、纸巾"
        self.assertEqual(extracted_content(data)["expense_content"], "牛奶、纸巾")
        data["expense_content"] = "牛奶3件"
        self.assertEqual(extracted_content(data)["expense_content"], "牛奶")

    def test_note_source_requires_quote_from_actual_note(self):
        data = self.extraction(expense_content="家用路由器", expense_content_source="user_note",
                               expense_content_evidence="买了家用路由器")
        self.assertIsNone(extracted_content(data, note="给家里买东西")["expense_content"])
        result = ExpenseExtractionResult(**data)
        draft = initial_draft(result, datetime.now(timezone.utc), {"timezone": "Asia/Shanghai"},
                              [], [], note="买了家用路由器，放书房")
        self.assertEqual(draft["expense_content"], "家用路由器")
        self.assertEqual(draft["expense_content_source"], "user_note")
        self.assertEqual(draft["remarks"], "买了家用路由器，放书房")

    def test_manual_content_is_not_machine_cleaned_and_clear_resets_source(self):
        self.assertEqual(content_fields(" iPhone 16 Pro 256GB "),
                         {"expense_content": "iPhone 16 Pro 256GB", "expense_content_source": "manual"})
        self.assertEqual(content_fields(" ", "screenshot"),
                         {"expense_content": None, "expense_content_source": None})

    def test_legacy_create_payload_is_byte_semantically_unchanged(self):
        old = {"transaction_type": "expense", "occurred_on": "2026-01-01", "original_amount": "1.00",
               "original_currency": "CNY", "category_id": str(uuid4()), "account_id": None,
               "merchant": None, "remarks": None, "refund_of_transaction_id": None,
               "account_review_acknowledged": False}
        self.assertEqual(command_payload(CreateTransactionRequest(**old)), old)
        explicit = command_payload(CreateTransactionRequest(**old, expense_content=None))
        self.assertIn("expense_content", explicit)
        with self.assertRaises(ValidationError):
            CreateTransactionRequest(**old, expense_content_source="screenshot")

    def test_legacy_schedule_hash_shape_and_nullable_patch(self):
        old = dict(name="会员", kind="recurring", amount_per_period="25.00", currency="CNY",
                   period_count=None, start_month="2026-01-01", day_of_month=1, merchant="视频平台",
                   category_id=str(uuid4()), account_id=None, acknowledged_due_through="2026-01-01",
                   replaces_transaction_id=None, expected_transaction_version=None,
                   source_draft_request_id=None, expected_draft_version=None)
        self.assertEqual(command_payload(CreateSchedule(**old)), old)
        self.assertEqual(PatchTransactionRequest(expected_version=0, expense_content=None).model_dump(exclude_unset=True),
                         {"expected_version": 0, "expense_content": None})

    def test_mobile_summary_separates_all_three_fields(self):
        text = summary(dict(expense_content="牛奶、纸巾", merchant="京东", remarks="周末采购"), saved=True)
        self.assertIn("消费内容：牛奶、纸巾\n商户：京东", text)
        self.assertIn("备注：周末采购", text)
        self.assertIn("用途待补充", summary({}, saved=True))
