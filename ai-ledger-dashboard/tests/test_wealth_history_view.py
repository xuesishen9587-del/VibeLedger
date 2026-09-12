import unittest
from streamlit.testing.v1 import AppTest
from wealth_history_view import history_figure,history_rows


def point(day,complete,known="100.00"):
    return {"as_of":f"2026-02-{day:02d}T00:00:00+08:00","reporting_currency":"CNY","setup_required":False,
        "total_assets":known if complete else None,"total_liabilities":"0.00" if complete else None,
        "net_worth":known if complete else None,"known_assets":known,"known_liabilities":"0.00","known_net_worth":known,
        "coverage":{"complete":complete,"missing_account_ids":[] if complete else ["unknown"],"missing_fx_currencies":[],
        "stale_account_ids":[],"oldest_observation_at":None,"newest_observation_at":None}}


class WealthHistoryTest(unittest.TestCase):
    def test_partial_history_remains_separate_and_zero_is_preserved(self):
        points=[point(1,True,"0.00"),point(2,False),point(3,True,"150.00")]
        figure=history_figure(points,"CNY")
        self.assertEqual(list(figure.data[0].y),[0.0,None,150.0])
        self.assertEqual(list(figure.data[1].y),[None,100.0,None])
        self.assertTrue(all(trace.line.shape=="hv" and trace.connectgaps is False for trace in figure.data))
        rows=history_rows(points)
        self.assertIsNone(rows[1]["净资产"])
        self.assertEqual(rows[1]["已知净资产"],"100.00")
        self.assertEqual(rows[1]["缺余额账户数"],1)

    def test_chart_loads_on_request_and_keeps_partial_coverage_visible(self):
        script='''
import streamlit as st
from wealth_history_view import render
from tests.test_wealth_history_view import point
class Client:
    def request(self,method,path,**kwargs):
        assert method=="GET" and path=="/api/v1/reports/wealth-history"
        st.session_state["history_query"]=kwargs["params"]
        return {"points":[point(1,True),point(2,False)]}
render(Client())
'''
        app=AppTest.from_string(script).run()
        self.assertEqual(len(app.exception),0)
        self.assertNotIn("history_query",app.session_state)
        app.button[0].click().run()
        self.assertEqual(len(app.exception),0)
        self.assertTrue(any("缺少余额或汇率" in w.value for w in app.warning))
        self.assertEqual(len(app.get("plotly_chart")),1)
        self.assertEqual(len(app.dataframe),1)
