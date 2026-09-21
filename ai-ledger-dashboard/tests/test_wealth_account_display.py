import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from wealth_page import account_rows


def account(name, age, balance="0.00", **changes):
    result={"name":name,"balance":balance,"currency":"CNY","as_of":"2026-01-01T00:00:00Z",
        "converted_amount":balance,"fx_as_of":"2026-01-01","age_days":age,
        "needs_update":age is not None and age>30,"very_stale":age is not None and age>90,
        "account_type":"cash","balance_scope":"Wallet only"}
    result.update(changes)
    return result


class WealthAccountDisplayTest(unittest.TestCase):
    def test_missing_zero_and_freshness_boundaries_remain_distinct(self):
        rows=account_rows([account("Missing",None,None),account("Zero",30),account("Old",31),
            account("Ninety",90),account("Very old",91)])
        self.assertEqual([r["余额状态"] for r in rows],
            ["尚未记录余额","已记录","超过 30 天，建议更新","超过 30 天，建议更新","超过 90 天未更新"])
        self.assertIsNone(rows[0]["原币余额"])
        self.assertEqual(rows[1]["原币余额"],"0.00")
        self.assertEqual(rows[1]["覆盖范围"],"Wallet only")

    def test_surplus_is_visible_even_without_fx_and_debt_is_not_surplus(self):
        rows=account_rows([account("Card",1,"200.00",account_type="credit",currency="USD",converted_amount=None,fx_as_of=None),
            account("Debt",1,"-200.00",account_type="credit"),account("Zero",1,account_type="credit"),
            account("Cash",1,"200.00")])
        self.assertIn("信用卡溢缴款",rows[0]["说明"])
        self.assertIsNone(rows[0]["折算金额"])
        self.assertEqual([r["说明"] for r in rows[1:]],["","",""])

    def test_very_stale_warning_names_accounts_without_changing_totals(self):
        report={"setup_required":False,"reporting_currency":"CNY","total_assets":"100.00",
            "total_liabilities":"0.00","net_worth":"100.00","accounts":[account("Old bank",91,"100.00")],
            "risk_buckets":[],"coverage":{"complete":True,"stale_account_ids":["old"],
                "stale_fx_currencies":[],"missing_fx_currencies":[],"oldest_observation_at":"2026-01-01",
                "newest_observation_at":"2026-01-01"}}
        def request(self,method,path,**kw):
            if method!="GET":
                raise AssertionError("Display must not write")
            if path.endswith("/wealth"):
                return report
            return {"items":[],"next_cursor":None}
        script='''
from api_client import ApiClient
from wealth_page import render
render(ApiClient())
'''
        with patch("api_client.ApiClient.request",request):
            app=AppTest.from_string(script).run()
        self.assertEqual(len(app.exception),0)
        self.assertTrue(any("超过 90 天" in w.value and "Old bank" in w.value for w in app.warning))
        self.assertEqual(next(m.value for m in app.metric if m.label=="净资产"),"100.00 CNY")
        self.assertEqual(app.dataframe[0].value.iloc[0]["余额状态"],"超过 90 天未更新")
