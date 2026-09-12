import unittest
from streamlit.testing.v1 import AppTest


class SpendingPageTest(unittest.TestCase):
    def test_saved_metadata_review_renders_and_corrects_without_reingestion(self):
        script = '''
import streamlit as st
from spending_page import render
class Client:
    def request(self, method, path, **kwargs):
        if method == "PATCH":
            st.session_state["saved_patch"] = kwargs["json_data"]
            st.session_state["resolved"] = True
            return {"id": "tx"}
        if path.endswith("/materialize"):
            return {"schedules_current_through": "2026-09-08"}
        if path.endswith("/accounts"):
            return {"items": [{"id":"account", "name":"Wallet"}]}
        if path.endswith("/categories"):
            return {"items": [{"id":"other", "name":"Other", "category_type":"expense"}]}
        if path.endswith("/review"):
            return {"counts":{"transactions":1,"missing_account":1,"category_uncertain":1,"schedule_occurrences":0,"drafts":0}}
        if path.endswith("/transactions"):
            row={"id":"tx","occurred_on":"2026-01-01","original_amount":"10.00","original_currency":"CNY","merchant":"Lunch","account_id":None,"category_id":"other","account_review_acknowledged":False,"row_version":0,"review_reasons":["MISSING_ACCOUNT","CATEGORY_UNCERTAIN"]}
            return {"items": [] if st.session_state.get("resolved") else [row], "next_cursor":None}
        raise AssertionError(path)
render(Client(), review_only=True)
'''
        app = AppTest.from_string(script).run()
        self.assertEqual(len(app.exception), 0)
        confirm = next(c for c in app.checkbox if c.label == "确认当前分类（包括 Other）")
        confirm.check()
        next(b for b in app.button if b.label == "保存修正").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["saved_patch"], {"expected_version": 0, "category_id": "other"})
