import unittest
from streamlit.testing.v1 import AppTest


class WealthPageTest(unittest.TestCase):
    def test_incomplete_wealth_and_atomic_manual_save(self):
        script='''
import streamlit as st
from wealth_page import render
class Client:
    def request(self,method,path,**kw):
        if method=="POST":
            st.session_state["saved"]=kw["json_data"]
            return {"status":"committed"}
        if path.endswith("/wealth"):
            return {"setup_required":False,"reporting_currency":"CNY","known_assets":"100.00","known_liabilities":"0.00","known_net_worth":"100.00",
                "accounts":[],"risk_buckets":[],"coverage":{"complete":False,"missing_account_ids":["account"],"missing_fx_currencies":[],"stale_account_ids":[],"stale_fx_currencies":[],"oldest_observation_at":None,"newest_observation_at":None}}
        if path.endswith("/accounts"):
            return {"items":[{"id":"account","name":"Wallet","currency":"CNY","account_type":"cash","row_version":2,"latest_snapshot":{"id":"head"}}]}
        if path.endswith("/ingestion-requests"):
            return {"items":[],"next_cursor":None}
        if path.endswith("/snapshots"):
            return {"items":[],"next_cursor":None,"expected_account_version":2,"expected_latest_snapshot_id":"head"}
        raise AssertionError(path)
render(Client())
'''
        app=AppTest.from_string(script).run()
        self.assertEqual(len(app.exception),0)
        self.assertTrue(any("不是完整家庭财富" in w.value for w in app.warning))
        self.assertEqual(next(m for m in app.metric if m.label=="已知净资产").value,"100.00 CNY")
        app.multiselect[0].select("account").run()
        next(t for t in app.text_input if "余额（可为零）" in t.label).set_value("0.00")
        next(b for b in app.button if b.label=="保存本次全部余额").click().run()
        self.assertEqual(len(app.exception),0)
        row=app.session_state["saved"]["observations"][0]
        self.assertEqual(row["balance"],"0.00")
        self.assertEqual(row["expected_latest_snapshot_id"],"head")
        self.assertEqual(row["expected_account_version"],2)

    def test_statement_preview_does_not_write_without_save(self):
        script='''
import streamlit as st
from statement_page import render
class Client:
    def request(self,method,path,**kw):
        if method!="GET":
            raise AssertionError("unexpected write")
        if path.endswith("/accounts"):
            return {"items":[]}
        if path.endswith("/categories") or path.endswith("/transactions") or path.endswith("/spending-schedules"):
            return {"items":[]}
        if path.endswith("/ingestion-requests"):
            return {"next_cursor":None,"items":[{"request_id":"draft","row_version":2,"warnings":[{"message":"Review partial coverage"}],
                "draft":{"account_id":"account","period_start":"2026-02-01","period_end":"2026-02-28","acknowledge_partial":False,"confirm_account_identity":False,"lines":[],"balance":None}}]}
        raise AssertionError(path)
render(Client(),review_only=True)
'''
        app=AppTest.from_string(script).run()
        self.assertEqual(len(app.exception),0)
        self.assertTrue(any(w.value=="Review partial coverage" for w in app.warning))
