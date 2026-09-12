import unittest
from streamlit.testing.v1 import AppTest


SCRIPT='''
import streamlit as st
from api_client import TimeoutError
from statement_page import render
class Client:
    def request(self,method,path,**kw):
        if method=="PATCH":
            st.session_state["edit"]=kw["json_data"]
            return {"status":"needs_confirmation"}
        if method=="POST":
            calls=st.session_state.setdefault("save_calls",[])
            calls.append(kw)
            if len(calls)==1:
                raise TimeoutError()
            st.session_state["finished"]=True
            return {"request_id":"draft","status":"committed","counts":{"create":0,"link":0,"skip":0},"snapshots":[{"id":"saved-old"}]}
        if path.endswith("/accounts"):
            return {"items":[{"id":"account","name":"Wallet","statement_import_enabled":True,"currency":"CNY","row_version":2,"latest_snapshot":{"id":"head"}}]}
        if path.endswith("/categories") or path.endswith("/transactions") or path.endswith("/spending-schedules"):
            return {"items":[],"next_cursor":None}
        if path.endswith("/snapshots"):
            params=kw["params"]
            if params.get("snapshot_id"):
                return {"items":[] if st.session_state.get("unavailable") else [{"id":params["snapshot_id"],"as_of":"2026-02-28T00:00:00+00:00","balance":"100.00","currency":"CNY"}]}
            page=int(params.get("cursor",0))
            return {"items":[{"id":str(i),"as_of":"2026-02-28T00:00:00+00:00","balance":str(i),"currency":"CNY"} for i in range(page*50,(page+1)*50)],"next_cursor":str(page+1) if page<4 else None}
        if path.endswith("/ingestion-requests"):
            if st.session_state.get("finished"):
                return {"items":[],"next_cursor":None}
            balance={"row_id":"balance","selected":True,"balance":"100.00","as_of":"2026-02-28T00:00:00+00:00","time_basis":"explicit","reuse_snapshot_id":"saved-old"}
            return {"next_cursor":None,"items":[{"request_id":"draft","row_version":3,"warnings":[],"draft":{
                "account_id":"account","period_start":"2026-02-01","period_end":"2026-02-28","acknowledge_partial":False,"confirm_account_identity":False,"lines":[],"balance":balance}}]}
        raise AssertionError(path)
render(Client(),review_only=True)
'''


class StatementBalanceReviewTest(unittest.TestCase):
    def test_saved_reuse_choice_is_restored_outside_loaded_page(self):
        app=AppTest.from_string(SCRIPT).run()
        self.assertEqual(len(app.exception),0)
        reuse=next(s for s in app.selectbox if s.label.startswith("复用已记录余额"))
        self.assertEqual(reuse.value,"saved-old")
        next(b for b in app.button if b.label=="保存账单预览修正").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.session_state["edit"]["balance"]["reuse_snapshot_id"],"saved-old")
        self.assertEqual(app.session_state["edit"]["balance"]["expected_latest_snapshot_id"],"head")

    def test_later_snapshot_pages_can_be_selected_and_saved(self):
        app=AppTest.from_string(SCRIPT).run()
        for _ in range(4):
            next(b for b in app.button if b.label=="加载更多余额观察").click().run()
        self.assertEqual(len(app.exception),0)
        next(s for s in app.selectbox if s.label.startswith("复用已记录余额")).select("249")
        next(b for b in app.button if b.label=="保存账单预览修正").click().run()
        self.assertEqual(app.session_state["edit"]["balance"]["reuse_snapshot_id"],"249")

    def test_unavailable_saved_choice_is_visible_and_never_becomes_new_observation(self):
        app=AppTest.from_string(SCRIPT)
        app.session_state["unavailable"]=True
        app.run()
        self.assertEqual(len(app.exception),0)
        self.assertTrue(any("原复用余额已作废" in w.value for w in app.warning))
        next(b for b in app.button if b.label=="保存账单预览修正").click().run()
        self.assertEqual(app.session_state["edit"]["balance"]["reuse_snapshot_id"],"saved-old")

    def test_unknown_save_retry_reuses_command_and_restores_success_summary(self):
        app=AppTest.from_string(SCRIPT).run()
        next(b for b in app.button if b.label=="保存选中支出与余额").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertTrue(app.session_state["_statement_actions"])
        app.run()
        next(b for b in app.button if b.label=="重试账单保存").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.session_state["_statement_actions"],{})
        calls=app.session_state["save_calls"]
        self.assertEqual(calls[0],calls[1])
        self.assertTrue(any("余额 1 条" in s.value for s in app.success))
        self.assertNotIn("statement_snapshot_targets_draft",app.session_state)
