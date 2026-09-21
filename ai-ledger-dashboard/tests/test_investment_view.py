import copy
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from api_client import TimeoutError

ITEM={"id":"opening:closing","account_id":"fund","account_name":"Funds","currency":"CNY",
    "opening_snapshot_id":"opening","closing_snapshot_id":"closing","period_start":"2026-02-01T12:00:00+08:00",
    "period_end":"2026-02-20T12:00:00+08:00","opening_time_basis":"explicit","closing_time_basis":"date_only",
    "opening_value":"100000.00","closing_value":"160000.00","gain":"60000.00","gain_status":"estimated",
    "assumption":"zero_flows","needs_review":True,"reason":None,"input_id":None,"input_status":None,"input_version":None,
    "effective_contributions":"0.00","effective_withdrawals":"0.00","notes":None}

class InvestmentViewTest(unittest.TestCase):
    def setUp(self):
        self.item=copy.deepcopy(ITEM)
        self.commands=[]
        self.fail_once=False
        self.params={}

    def request(self,_client,method,path,**kw):
        if method!="GET":
            self.commands.append((method,path,copy.deepcopy(kw)))
            if self.fail_once:
                self.fail_once=False
                raise TimeoutError()
            if method=="PUT":
                self.item.update(input_id="input",input_version=0,input_status="active",gain_status="user_confirmed",needs_review=False,
                    gain="10000.00",effective_contributions=kw["json_data"]["contributions_amount"])
            elif method=="POST":
                self.item.update(input_status="voided",input_version=1,gain_status="estimated",needs_review=True,gain="60000.00")
            return {"id":"input"}
        if path.endswith("/review"):
            return {"items":[self.item] if self.item["needs_review"] else [],"next_cursor":None,
                "counts":{"unusual_investment_estimates":int(self.item["needs_review"])},"investment_review_change_ratio":"0.2000","settings_row_version":3}
        self.params=kw.get("params",{})
        return {"items":[self.item],"coverage":{"complete":False,"gaps":[]},"unavailable":[],"first_observations":[],
            "historical_inputs":[],"excluded_boundary_intervals":[],"native_currency_totals":[{"currency":"CNY","gain_status":self.item["gain_status"],
                "combined_gain":self.item["gain"],"confirmed_gain_subtotal":"0.00","estimated_gain_subtotal":self.item["gain"]}]}

    def app(self,review=False,settings=False):
        function="render_settings" if settings else "render"
        script=f'from api_client import ApiClient\nfrom investment_view import {function}\n{function}(ApiClient()'+(')' if settings else f',review_only={review})')
        return AppTest.from_string(script).run()

    def patch_client(self):
        return patch("api_client.ApiClient.request",lambda client,*a,**kw:self.request(client,*a,**kw))

    def test_explicit_completeness_confirm_then_void(self):
        with self.patch_client():
            app=self.app()
            self.assertEqual(len(app.exception),0)
            self.assertTrue(any("假设没有投入" in info.value for info in app.info))
            next(b for b in app.button if b.label=="保存完整资金确认").click().run()
            self.assertEqual(self.commands,[])
            next(t for t in app.text_input if t.label=="期间投入总额").set_value("50000.00")
            app.checkbox[0].check()
            next(b for b in app.button if b.label=="保存完整资金确认").click().run()
            self.assertEqual(len(app.exception),0)
            body=self.commands[0][2]["json_data"]
            self.assertEqual(body["contributions_amount"],"50000.00")
            self.assertEqual(body["withdrawals_amount"],"0.00")
            self.assertIsNone(body["expected_version"])
            next(t for t in app.text_input if t.label=="撤回确认原因").set_value("Totals were incomplete")
            next(b for b in app.button if b.label=="撤回资金确认，恢复估算").click().run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(self.commands[-1][0:2],("POST","/api/v1/investment-period-inputs/input/void"))
            self.assertTrue(any("假设没有投入" in info.value for info in app.info))

    def test_unknown_result_retry_keeps_exact_body_and_key(self):
        self.fail_once=True
        with self.patch_client():
            app=self.app(review=True)
            app.checkbox[0].check()
            next(b for b in app.button if b.label=="保存完整资金确认").click().run()
            app.run()
            next(b for b in app.button if b.label=="重试资金确认").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(self.commands[0],self.commands[1])
        self.assertEqual(app.session_state["_investment_actions"],{})
        self.assertTrue(any("没有待复核" in info.value for info in app.info))

    def test_range_and_changed_pair_are_explicit(self):
        from datetime import date
        self.item["reason"]="PAIR_CHANGED"
        with self.patch_client():
            app=self.app()
            self.assertTrue(any("原资金确认未沿用" in w.value for w in app.warning))
            app.date_input[0].set_value(date(2026,2,2))
            app.date_input[1].set_value(date(2026,2,20))
            next(b for b in app.button if b.label=="查看投资区间").click().run()
        self.assertEqual(self.params,{"from":"2026-02-02","to":"2026-02-20"})
        self.assertTrue(any("覆盖不完整" in w.value for w in app.warning))

    def test_threshold_percent_converts_without_float_and_sends_version(self):
        with self.patch_client():
            app=self.app(settings=True)
            app.text_input[0].set_value("12.34")
            next(b for b in app.button if b.label=="保存投资复核阈值").click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(self.commands[0][2]["json_data"],{"expected_version":3,"investment_review_change_ratio":"0.1234"})
