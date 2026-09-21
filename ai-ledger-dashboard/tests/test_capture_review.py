import unittest
from streamlit.testing.v1 import AppTest


class CaptureReviewTest(unittest.TestCase):
    def test_edit_requires_current_version_and_confirm_is_separate(self):
        script = '''
import streamlit as st
from capture_review import render
class Client:
    def request(self, method, path, **kwargs):
        return {"items": [] if st.session_state.get("confirmed") else [{
            "request_id":"capture", "row_version":st.session_state.get("version", 2),
            "warnings":[], "draft":{"intent":"expense", "payment_mode":"one_off",
            "original_amount":"10.00", "original_currency":"CNY", "occurred_on":"2026-01-01",
            "merchant":"Lunch", "from_account":None, "category":None}}], "next_cursor":None}
def save(slot, method, path, body):
    st.session_state["last_save"] = (method, path, body)
    if path.endswith("/confirm"):
        st.session_state["confirmed"] = True
    else:
        st.session_state["version"] = 3
    return {"status":"committed" if path.endswith("/confirm") else "needs_confirmation"}
render(Client(), save, {None:"未知", "wallet":"Wallet"}, [{"id":"other", "name":"Other"}])
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)
        next(w for w in app.selectbox if w.label == "截图付款账户").select("wallet")
        next(w for w in app.button if w.label == "保存草稿修正").click().run()
        self.assertEqual(len(app.exception), 0)
        method, path, body = app.session_state["last_save"]
        self.assertEqual((method, path), ("PATCH", "/api/v1/ingestion-requests/capture/draft"))
        self.assertEqual(body["expected_version"], 2)
        self.assertEqual(body["from_account_id"], "wallet")
        self.assertNotIn("confirmed", app.session_state)
        next(w for w in app.button if w.label == "确认保存这笔支出").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["last_save"][2], {"expected_version": 3})
        self.assertTrue(app.session_state["confirmed"])

    def test_report_missing_fx_stays_partial_and_auto_refresh_is_bounded(self):
        script = '''
import streamlit as st
from spending_report_view import render
from spending_controller import SpendingActions
class Client:
    def request(self, method, path, **kwargs):
        if method == "POST":
            st.session_state["refresh_count"] = st.session_state.get("refresh_count",0) + 1
            return {"filled":0,"pending":1}
        report = {"reporting_currency":"CNY", "missing_conversion_count":1,
            "schedules_current_through":"2026-09-09", "native_currency_totals":[],
            "category":[], "merchant":[], "month":[]}
        for field in ("gross_expenses", "refunds", "net_spending", "recorded_income"):
            report[field] = None
            report["known_" + field] = "12.00"
        return report
client=Client()
render(client, SpendingActions(st.session_state.setdefault("actions", {}), client))
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["refresh_count"], 1)
        self.assertTrue(any("合计尚不完整" in w.value for w in app.warning))
        self.assertEqual(next(m for m in app.metric if m.label == "已折算净支出").value, "12.00 CNY")
        app.run()
        self.assertEqual(app.session_state["refresh_count"], 1)
        next(b for b in app.button if b.label == "补齐缺失汇率").click().run()
        self.assertEqual(app.session_state["refresh_count"], 2)
