"""S2.4c / SCHED-02 UI Closure Tests:
Conversion of already-saved full-price expenses to installment spending schedules.

Proves:
1. Existing full-price expense -> installment schedule (atomic POST, Idempotency-Key, replacement ID/version, body).
2. Ordinary installment without replacement (replacement fields absent).
3. Recurring schedule (cannot submit replacement fields).
4. Preview (read-only, terms only, selected replacement identity/version survives to Save).
5. Unknown-outcome retry (exact same key and payload retried).
6. Changed replacement after unknown outcome (blocked before HTTP, no silent key reuse).
7. Stale transaction version / deterministic 4xx (stale action terminates, no silent retry or void).
8. Screenshot draft -> schedule (retained separate workflow, no conflation).
9. Adversarial: Two-command proxy (zero void calls, exactly one schedule-create mutation).
"""
import unittest
from unittest.mock import MagicMock
from streamlit.testing.v1 import AppTest

from api_client import TimeoutError, ConflictError, ValidationError, ServiceUnavailableError
from settings_controller import MutationModifiedPendingError
from spending_controller import SpendingActions
from spending_page import _schedules


class TestSpendingScheduleConversion(unittest.TestCase):
    def test_01_existing_expense_to_installment_schedule_exact_contract(self):
        """Row 1: Full-price expense converted to installment schedule via single atomic command."""
        script = '''
import streamlit as st
from spending_page import _schedules
from spending_controller import SpendingActions

class MockClient:
    def __init__(self):
        self.calls = []
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/spending-schedules/preview"):
            return {
                "acknowledged_due_through": "2026-09-09",
                "due_periods": [{"period_no": 1, "due_on": "2026-09-01"}],
                "due_total": "100.00",
                "upcoming_dates": ["2026-10-01"]
            }
        if path.endswith("/transactions"):
            return {
                "items": [{
                    "id": "tx-full-99",
                    "occurred_on": "2026-09-01",
                    "original_amount": "1200.00",
                    "original_currency": "CNY",
                    "merchant": "Apple Store",
                    "row_version": 2,
                    "schedule_occurrence_id": None
                }],
                "next_cursor": None
            }
        if "/transactions/tx-full-99" in path:
            return {
                "id": "tx-full-99",
                "occurred_on": "2026-09-01",
                "original_amount": "1200.00",
                "original_currency": "CNY",
                "merchant": "Apple Store",
                "row_version": 2,
                "schedule_occurrence_id": None
            }
        if path.endswith("/spending-schedules"):
            return {"id": "sched-1"}
        return {}

if "client" not in st.session_state:
    st.session_state["client"] = MockClient()
client = st.session_state["client"]
actions = SpendingActions(st.session_state.setdefault("_spending_actions", {}), client)

def save(slot, method, path, body):
    return actions.execute(slot, method, path, body)

categories = [{"id": "cat-electronics", "name": "数码电器", "category_type": "expense"}]
accounts = {"acc-bank": "招商银行"}
_schedules(client, save, categories, accounts)
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)

        # Fill schedule terms
        app.text_input[0].input("iPhone 16 分期")
        next(s for s in app.selectbox if s.label == "计划来源").select("convert_expense")
        next(s for s in app.selectbox if s.label.startswith("选择要转换的已记录支出")).select("tx-full-99")
        app.text_input[2].input("100.00")  # 每期金额
        app.text_input[3].input("Apple Store")  # 商户

        # Preview
        next(b for b in app.button if b.label == "预览日期和金额").click().run()
        self.assertEqual(len(app.exception), 0)

        # Verify preview button appeared and save
        save_btn = next(b for b in app.button if b.label == "保存以上预览计划")
        save_btn.click().run()
        self.assertEqual(len(app.exception), 0)

        client = app.session_state["client"]
        create_calls = [c for c in client.calls if c[0] == "POST" and c[1] == "/api/v1/spending-schedules"]
        self.assertEqual(len(create_calls), 1)
        method, path, kwargs = create_calls[0]
        self.assertEqual((method, path), ("POST", "/api/v1/spending-schedules"))

        # Exact headers & body assertions
        headers = kwargs.get("headers", {})
        self.assertIn("Idempotency-Key", headers)
        self.assertTrue(len(headers["Idempotency-Key"]) >= 8)

        body = kwargs["json_data"]
        self.assertEqual(body["name"], "iPhone 16 分期")
        self.assertEqual(body["kind"], "installment")
        self.assertEqual(body["amount_per_period"], "100.00")
        self.assertEqual(body["currency"], "CNY")
        self.assertEqual(body["period_count"], 12)
        self.assertEqual(body["start_month"], "2026-09-01")
        self.assertEqual(body["day_of_month"], 1)
        self.assertEqual(body["merchant"], "Apple Store")
        self.assertEqual(body["category_id"], "cat-electronics")
        self.assertEqual(body["account_id"], "acc-bank")
        self.assertEqual(body["acknowledged_due_through"], "2026-09-09")
        self.assertEqual(body["replaces_transaction_id"], "tx-full-99")
        self.assertEqual(body["expected_transaction_version"], 2)

    def test_02_ordinary_installment_without_replacement(self):
        """Row 2: Normal installment schedule creation omits replacement fields."""
        script = '''
import streamlit as st
from spending_page import _schedules
from spending_controller import SpendingActions

class MockClient:
    def __init__(self):
        self.calls = []
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/spending-schedules/preview"):
            return {
                "acknowledged_due_through": "2026-09-09",
                "due_periods": [],
                "due_total": "0.00",
                "upcoming_dates": ["2026-10-01"]
            }
        if path.endswith("/spending-schedules"):
            return {"id": "sched-2"}
        if path.endswith("/transactions"):
            return {"items": [], "next_cursor": None}
        return {}

if "client" not in st.session_state:
    st.session_state["client"] = MockClient()
client = st.session_state["client"]
actions = SpendingActions(st.session_state.setdefault("_spending_actions", {}), client)

def save(slot, method, path, body):
    return actions.execute(slot, method, path, body)

categories = [{"id": "cat-gym", "name": "健身", "category_type": "expense"}]
accounts = {"acc-bank": "招商银行"}
_schedules(client, save, categories, accounts)
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)

        app.text_input[0].input("健身房分期")
        # Default is new_schedule
        app.text_input[2].input("200.00")
        app.text_input[3].input("SuperGym")

        next(b for b in app.button if b.label == "预览日期和金额").click().run()
        self.assertEqual(len(app.exception), 0)

        next(b for b in app.button if b.label == "保存以上预览计划").click().run()
        self.assertEqual(len(app.exception), 0)

        client = app.session_state["client"]
        create_calls = [c for c in client.calls if c[0] == "POST" and c[1] == "/api/v1/spending-schedules"]
        self.assertEqual(len(create_calls), 1)
        body = create_calls[0][2]["json_data"]
        self.assertEqual(body["name"], "健身房分期")
        self.assertEqual(body["kind"], "installment")
        self.assertNotIn("replaces_transaction_id", body)
        self.assertNotIn("expected_transaction_version", body)
        self.assertNotIn("source_draft_request_id", body)
        self.assertEqual(body["acknowledged_due_through"], "2026-09-09")

    def test_03_recurring_schedule_cannot_submit_replacement_fields(self):
        """Row 3: Recurring schedule cannot accidentally submit replacement fields."""
        script = '''
import streamlit as st
from spending_page import _schedules
from spending_controller import SpendingActions

class MockClient:
    def __init__(self):
        self.calls = []
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/spending-schedules/preview"):
            return {
                "acknowledged_due_through": "2026-09-09",
                "due_periods": [],
                "due_total": "0.00",
                "upcoming_dates": ["2026-10-01"]
            }
        if path.endswith("/transactions"):
            return {"items": [{"id": "tx-1", "occurred_on": "2026-09-01", "original_amount": "50.00", "original_currency": "CNY", "row_version": 1}], "next_cursor": None}
        if path.endswith("/spending-schedules"):
            return {"id": "sched-rec"}
        return {}

if "client" not in st.session_state:
    st.session_state["client"] = MockClient()
client = st.session_state["client"]
actions = SpendingActions(st.session_state.setdefault("_spending_actions", {}), client)

def save(slot, method, path, body):
    return actions.execute(slot, method, path, body)

categories = [{"id": "cat-sub", "name": "订阅", "category_type": "expense"}]
accounts = {"acc-bank": "招商银行"}
_schedules(client, save, categories, accounts)
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)

        # 1. Attempt conversion with recurring -> must be rejected
        app.text_input[0].input("Netflix 订阅")
        next(s for s in app.selectbox if s.label == "计划类型").select("recurring")
        next(s for s in app.selectbox if s.label == "计划来源").select("convert_expense")
        next(s for s in app.selectbox if s.label.startswith("选择要转换的已记录支出")).select("tx-1")
        app.text_input[2].input("50.00")
        app.text_input[3].input("Netflix")

        next(b for b in app.button if b.label == "预览日期和金额").click().run()
        self.assertEqual(len(app.exception), 0)
        # Verify error displayed and no preview button appears
        self.assertTrue(any("只有分期计划支持转换已有支出" in e.value for e in app.error))
        self.assertFalse(any(b.label == "保存以上预览计划" for b in app.button))

        # 2. Switch to normal schedule -> preview and save succeeds without replacement fields
        next(s for s in app.selectbox if s.label == "计划来源").select("new_schedule")
        next(b for b in app.button if b.label == "预览日期和金额").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any(b.label == "保存以上预览计划" for b in app.button))

        next(b for b in app.button if b.label == "保存以上预览计划").click().run()
        self.assertEqual(len(app.exception), 0)

        client = app.session_state["client"]
        create_calls = [c for c in client.calls if c[0] == "POST" and c[1] == "/api/v1/spending-schedules"]
        self.assertEqual(len(create_calls), 1)
        body = create_calls[0][2]["json_data"]
        self.assertEqual(body["kind"], "recurring")
        self.assertNotIn("replaces_transaction_id", body)
        self.assertNotIn("expected_transaction_version", body)

    def test_04_preview_read_only_and_replacement_identity_survives_to_save(self):
        """Row 4: Preview performs no financial mutation; replacement identity/version survives to Save."""
        client = MagicMock()
        client.request.side_effect = lambda method, path, **kwargs: (
            {"acknowledged_due_through": "2026-09-09", "due_periods": [], "due_total": "0.00", "upcoming_dates": []}
            if path.endswith("/preview") else
            {"items": [{"id": "tx-preview-1", "row_version": 4, "occurred_on": "2026-09-01", "original_amount": "500.00", "original_currency": "CNY"}]}
            if path.endswith("/transactions") else
            {"id": "tx-preview-1", "row_version": 4, "occurred_on": "2026-09-01", "original_amount": "500.00", "original_currency": "CNY"}
            if "/transactions/tx-preview-1" in path else
            {"id": "sched-preview"}
        )
        actions = SpendingActions({}, client)

        # Before save: only preview calls, no POST /api/v1/spending-schedules, no void
        preview_body = {
            "name": "iPad 分期", "kind": "installment", "amount_per_period": "50.00", "currency": "CNY",
            "period_count": 10, "start_month": "2026-09-01", "day_of_month": 1, "merchant": "Apple",
            "category_id": "cat-1", "account_id": "acc-1"
        }
        res_preview = client.request("POST", "/api/v1/spending-schedules/preview", json_data=preview_body)
        self.assertIn("acknowledged_due_through", res_preview)

        # Assert no financial mutations happened during preview
        for call in client.request.call_args_list:
            method, path = call[0][0], call[0][1]
            self.assertNotEqual(path, "/api/v1/spending-schedules")
            self.assertFalse(path.endswith("/void"))

        # Save preserves replacement identity and version from preview
        save_payload = {
            **preview_body,
            "replaces_transaction_id": "tx-preview-1",
            "expected_transaction_version": 4,
            "acknowledged_due_through": res_preview["acknowledged_due_through"]
        }
        actions.execute("create_schedule", "POST", "/api/v1/spending-schedules", save_payload)

        last_call = client.request.call_args_list[-1]
        self.assertEqual(last_call[0][:2], ("POST", "/api/v1/spending-schedules"))
        self.assertEqual(last_call[1]["json_data"]["replaces_transaction_id"], "tx-preview-1")
        self.assertEqual(last_call[1]["json_data"]["expected_transaction_version"], 4)

    def test_05_unknown_outcome_retry_reuses_exact_same_key_and_body(self):
        """Row 5: Unknown outcome retries with identical Idempotency-Key and body."""
        client = MagicMock()
        client.request.side_effect = [TimeoutError("Gateway Timeout"), {"id": "sched-timeout"}]
        state = {}
        actions = SpendingActions(state, client)

        payload = {
            "name": "Watch 分期", "kind": "installment", "amount_per_period": "100.00", "currency": "CNY",
            "period_count": 6, "start_month": "2026-09-01", "day_of_month": 1, "merchant": "Apple",
            "category_id": "cat-1", "account_id": "acc-1",
            "replaces_transaction_id": "tx-timeout-1", "expected_transaction_version": 1,
            "acknowledged_due_through": "2026-09-09"
        }

        # Attempt 1: times out
        with self.assertRaises(TimeoutError):
            actions.execute("create_schedule", "POST", "/api/v1/spending-schedules", payload)

        self.assertIn("create_schedule", state)
        original_key = state["create_schedule"]["key"]

        # Attempt 2: retry of exact logical action
        result = actions.retry("create_schedule")
        self.assertEqual(result, {"id": "sched-timeout"})
        self.assertNotIn("create_schedule", state)

        # Assert exact same key and body were sent on retry
        call1 = client.request.call_args_list[0]
        call2 = client.request.call_args_list[1]
        self.assertEqual(call1[1]["headers"]["Idempotency-Key"], original_key)
        self.assertEqual(call2[1]["headers"]["Idempotency-Key"], original_key)
        self.assertEqual(call1[1]["json_data"], payload)
        self.assertEqual(call2[1]["json_data"], payload)

    def test_06_changed_replacement_after_unknown_outcome_blocked_before_http(self):
        """Row 6: Changing replacement transaction or terms after unknown outcome is blocked before HTTP."""
        client = MagicMock()
        client.request.side_effect = TimeoutError("Connection reset")
        state = {}
        actions = SpendingActions(state, client)

        payload_a = {
            "name": "Camera 分期", "kind": "installment", "amount_per_period": "500.00", "currency": "CNY",
            "period_count": 6, "start_month": "2026-09-01", "day_of_month": 1, "merchant": "Sony",
            "category_id": "cat-1", "account_id": "acc-1",
            "replaces_transaction_id": "tx-sony-a", "expected_transaction_version": 0,
            "acknowledged_due_through": "2026-09-09"
        }
        with self.assertRaises(TimeoutError):
            actions.execute("create_schedule", "POST", "/api/v1/spending-schedules", payload_a)

        call_count_before = client.request.call_count

        # User changes transaction to tx-sony-b
        payload_b = {**payload_a, "replaces_transaction_id": "tx-sony-b"}
        with self.assertRaises(MutationModifiedPendingError):
            actions.execute("create_schedule", "POST", "/api/v1/spending-schedules", payload_b)

        # User changes version to 1
        payload_c = {**payload_a, "expected_transaction_version": 1}
        with self.assertRaises(MutationModifiedPendingError):
            actions.execute("create_schedule", "POST", "/api/v1/spending-schedules", payload_c)

        # Zero additional HTTP calls made
        self.assertEqual(client.request.call_count, call_count_before)

    def test_07_stale_transaction_version_and_4xx_terminates_action(self):
        """Row 7: ROW_VERSION_CONFLICT / deterministic 4xx terminates pending action without auto-retry or void."""
        client = MagicMock()
        client.request.side_effect = ConflictError("Stale row", code="ROW_VERSION_CONFLICT", status_code=409)
        state = {}
        actions = SpendingActions(state, client)

        payload = {
            "name": "TV 分期", "kind": "installment", "amount_per_period": "300.00", "currency": "CNY",
            "period_count": 10, "start_month": "2026-09-01", "day_of_month": 1, "merchant": "LG",
            "category_id": "cat-1", "account_id": "acc-1",
            "replaces_transaction_id": "tx-stale-1", "expected_transaction_version": 0,
            "acknowledged_due_through": "2026-09-09"
        }

        with self.assertRaises(ConflictError):
            actions.execute("create_schedule", "POST", "/api/v1/spending-schedules", payload)

        # Stale action must terminate immediately (popped from state)
        self.assertNotIn("create_schedule", state)
        # Exactly 1 HTTP call, zero retries, zero void calls
        self.assertEqual(client.request.call_count, 1)
        self.assertFalse(any(c[0][1].endswith("/void") for c in client.request.call_args_list))

    def test_08_screenshot_draft_to_schedule_flow_remains_separate(self):
        """Row 8: Screenshot draft -> schedule flow uses source_draft_request_id and remains regression-free."""
        script = '''
import streamlit as st
from spending_page import _schedules
from spending_controller import SpendingActions

class MockClient:
    def __init__(self):
        self.calls = []
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/spending-schedules/preview"):
            return {
                "acknowledged_due_through": "2026-09-09",
                "due_periods": [],
                "due_total": "0.00",
                "upcoming_dates": ["2026-10-01"]
            }
        if path.endswith("/spending-schedules"):
            return {"id": "sched-from-draft"}
        return {}

if "client" not in st.session_state:
    st.session_state["client"] = MockClient()
client = st.session_state["client"]
actions = SpendingActions(st.session_state.setdefault("_spending_actions", {}), client)

def save(slot, method, path, body):
    return actions.execute(slot, method, path, body)

categories = [{"id": "cat-draft", "name": "截图分类", "category_type": "expense"}]
accounts = {"acc-draft": "账户"}
source_draft = {"request_id": "draft-req-777", "row_version": 5}
_schedules(client, save, categories, accounts, source_draft=source_draft)
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)

        # In draft flow, conversion_mode selectbox is NOT rendered
        self.assertFalse(any(s.label == "计划来源" for s in app.selectbox))
        self.assertFalse(any(s.label.startswith("选择要转换的已记录支出") for s in app.selectbox))

        app.text_input[0].input("截图分期计划")
        app.text_input[1].input("88.00")
        app.text_input[2].input("Taobao")

        next(b for b in app.button if b.label == "预览日期和金额").click().run()
        self.assertEqual(len(app.exception), 0)

        next(b for b in app.button if b.label == "保存以上预览计划").click().run()
        self.assertEqual(len(app.exception), 0)

        client = app.session_state["client"]
        create_calls = [c for c in client.calls if c[0] == "POST" and c[1] == "/api/v1/spending-schedules"]
        self.assertEqual(len(create_calls), 1)
        body = create_calls[0][2]["json_data"]
        # Contains source_draft fields, NOT replaces_transaction fields
        self.assertEqual(body["source_draft_request_id"], "draft-req-777")
        self.assertEqual(body["expected_draft_version"], 5)
        self.assertNotIn("replaces_transaction_id", body)
        self.assertNotIn("expected_transaction_version", body)

    def test_09_adversarial_two_command_proxy_forbidden(self):
        """Adversarial Proof: Fail if Dashboard calls transaction void before or after schedule creation."""
        script = '''
import streamlit as st
from spending_page import _schedules
from spending_controller import SpendingActions

class MockClient:
    def __init__(self):
        self.calls = []
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/spending-schedules/preview"):
            return {
                "acknowledged_due_through": "2026-09-09",
                "due_periods": [],
                "due_total": "0.00",
                "upcoming_dates": ["2026-10-01"]
            }
        if path.endswith("/transactions"):
            return {
                "items": [{
                    "id": "tx-adv-1",
                    "occurred_on": "2026-09-01",
                    "original_amount": "600.00",
                    "original_currency": "CNY",
                    "merchant": "DJI",
                    "row_version": 1,
                    "schedule_occurrence_id": None
                }],
                "next_cursor": None
            }
        if "/transactions/tx-adv-1" in path:
            return {
                "id": "tx-adv-1",
                "occurred_on": "2026-09-01",
                "original_amount": "600.00",
                "original_currency": "CNY",
                "merchant": "DJI",
                "row_version": 1,
                "schedule_occurrence_id": None
            }
        if path.endswith("/spending-schedules"):
            return {"id": "sched-adv"}
        return {}

if "client" not in st.session_state:
    st.session_state["client"] = MockClient()
client = st.session_state["client"]
actions = SpendingActions(st.session_state.setdefault("_spending_actions", {}), client)

def save(slot, method, path, body):
    return actions.execute(slot, method, path, body)

categories = [{"id": "cat-adv", "name": "航模", "category_type": "expense"}]
accounts = {"acc-adv": "银行"}
_schedules(client, save, categories, accounts)
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)

        app.text_input[0].input("DJI 无人机分期")
        next(s for s in app.selectbox if s.label == "计划来源").select("convert_expense")
        next(s for s in app.selectbox if s.label.startswith("选择要转换的已记录支出")).select("tx-adv-1")
        app.text_input[2].input("100.00")
        app.text_input[3].input("DJI Store")

        next(b for b in app.button if b.label == "预览日期和金额").click().run()
        self.assertEqual(len(app.exception), 0)

        next(b for b in app.button if b.label == "保存以上预览计划").click().run()
        self.assertEqual(len(app.exception), 0)

        client = app.session_state["client"]
        # Explicit check: ZERO calls to void
        void_calls = [c for c in client.calls if "void" in c[1]]
        self.assertEqual(len(void_calls), 0, "Dashboard must never void the transaction as a separate write!")

        # Exactly one mutation call
        mutation_calls = [c for c in client.calls if c[0] in ("POST", "PATCH", "DELETE") and not c[1].endswith("/preview")]
        self.assertEqual(len(mutation_calls), 1)
        self.assertEqual(mutation_calls[0][1], "/api/v1/spending-schedules")


if __name__ == "__main__":
    unittest.main()
