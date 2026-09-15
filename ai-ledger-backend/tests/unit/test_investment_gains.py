import unittest
from datetime import datetime, timezone, date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4
import app.auth
from app.domain.investment_gains import interval, flow_amount, PeriodInput
from app.services.investment_gains import report, review_page
from fastapi import HTTPException
from pydantic import ValidationError


def snapshot(value, day):
    return {"id":uuid4(),"balance":Decimal(value),"as_of":datetime(2026,2,day,12,tzinfo=timezone.utc),"time_basis":"explicit"}


class InvestmentGainsTest(unittest.TestCase):
    def setUp(self):
        self.account={"id":uuid4(),"name":"Funds","currency":"CNY","opened_on":date(2026,1,1),"closed_on":None}
        self.opening=snapshot("100000",1)
        self.closing=snapshot("160000",20)

    def value(self,opening,closing,inputs=None):
        return interval(self.account,snapshot(opening,1),snapshot(closing,20),inputs,Decimal(".2"))

    def test_estimated_and_confirmed_examples(self):
        self.assertEqual(self.value("100000","160000")["gain"],"60000.00")
        inputs={"id":uuid4(),"status":"active","row_version":0,"confirmed_by_user_id":uuid4(),
            "confirmed_at":datetime.now(timezone.utc),"contributions_amount":Decimal(50000),"withdrawals_amount":Decimal(0)}
        result=self.value("100000","160000",inputs)
        self.assertEqual(result["gain"],"10000.00")
        self.assertEqual(result["gain_status"],"user_confirmed")
        self.assertFalse(result["needs_review"])
        inputs.update(contributions_amount=Decimal(0),withdrawals_amount=Decimal(25000))
        self.assertEqual(self.value("100000","80000",inputs)["gain"],"5000.00")
        inputs["status"]="voided"
        self.assertEqual(self.value("100000","80000",inputs)["gain"],"-20000.00")

    def test_exact_threshold_both_signs_and_zero(self):
        for opening,closing,expected in (("100","120",True),("100","80",True),("100","119.99",False),
            ("100","80.01",False),("0","0",False),("0",".01",True),("-100","-80",True),("100000","105000",False)):
            with self.subTest(opening=opening,closing=closing):
                self.assertEqual(self.value(opening,closing)["needs_review"],expected)

    def test_invalid_flow_precision_and_missing_explicit_totals(self):
        for value in ("NaN","Infinity","-1","1.001",1.2):
            with self.subTest(value=value),self.assertRaises(HTTPException):
                flow_amount(value,"CNY")
        self.assertEqual(flow_amount("0","JPY"),0)
        with self.assertRaises(ValidationError):
            PeriodInput(opening_snapshot_id=uuid4(),closing_snapshot_id=uuid4(),contributions_amount="0",expected_version=None)

    def make_report(self,start=None,end=None,inputs=None,rows=None):
        observations=rows if rows is not None else [self.opening,self.closing]
        for row in observations:
            row["account_id"]=self.account["id"]
        with patch("app.services.investment_gains.schema.get_household",return_value={"timezone":"Asia/Singapore","investment_review_change_ratio":Decimal(".2")}),patch(
            "app.services.investment_gains.repo.rows",side_effect=[[self.account],observations,inputs or []]):
            return report(None,uuid4(),start=start,end=end)

    def test_range_does_not_prorate_crossing_interval(self):
        result=self.make_report(date(2026,2,2),date(2026,2,20))
        self.assertEqual(result["items"],[])
        self.assertEqual(len(result["excluded_boundary_intervals"]),1)
        self.assertIsNone(result["native_currency_totals"][0]["combined_gain"])
        self.assertFalse(result["coverage"]["complete"])
        result=self.make_report(date(2026,2,1),date(2026,2,20))
        self.assertEqual(result["items"][0]["gain"],"60000.00")
        self.assertEqual(result["native_currency_totals"][0]["gain_status"],"estimated")
        self.assertIsNone(result["first_observations"][0]["gain"])

    def test_first_or_missing_observation_is_unavailable(self):
        for rows,reason in (([],"MISSING_OBSERVATIONS"),([self.opening],"FIRST_OBSERVATION")):
            result=self.make_report(rows=rows)
            self.assertEqual(result["unavailable"][0]["reason"],reason)
            self.assertIsNone(result["native_currency_totals"][0]["combined_gain"])

    def test_split_pair_excludes_old_assertion_and_returns_new_review_ids(self):
        middle=snapshot("130000",10)
        old={"id":uuid4(),"account_id":self.account["id"],"opening_snapshot_id":self.opening["id"],"closing_snapshot_id":self.closing["id"],
            "opening_as_of":self.opening["as_of"],"closing_as_of":self.closing["as_of"],"status":"active"}
        result=self.make_report(inputs=[old],rows=[self.opening,middle,self.closing])
        self.assertEqual(len(result["historical_inputs"]),1)
        self.assertTrue(all(r["reason"]=="PAIR_CHANGED" and r["gain_status"]=="estimated" for r in result["items"]))
        page,count=review_page(result,None,1)
        self.assertEqual(count,2)
        self.assertIsNotNone(page["next_cursor"])
        second,_=review_page(result,page["next_cursor"],1)
        self.assertNotEqual(page["items"][0]["id"],second["items"][0]["id"])
        with self.assertRaises(HTTPException):
            review_page(result,"bad",50)

    def test_voided_split_uses_audit_order_not_transaction_start_time(self):
        middle=snapshot("130000",10)
        middle.update(status="voided",observation_revision=12)
        saved={"id":uuid4(),"account_id":self.account["id"],"opening_snapshot_id":self.opening["id"],"closing_snapshot_id":self.closing["id"],
            "opening_as_of":self.opening["as_of"],"closing_as_of":self.closing["as_of"],"status":"active","row_version":0,
            "contributions_amount":Decimal(50000),"withdrawals_amount":Decimal(0),"confirmed_by_user_id":uuid4(),
            "confirmed_at":datetime.now(timezone.utc),"confirmation_revision":11}
        result=self.make_report(inputs=[saved],rows=[self.opening,middle,self.closing])
        self.assertEqual(result["items"][0]["gain_status"],"estimated")
        self.assertEqual(result["items"][0]["reason"],"PAIR_CHANGED")
        saved["confirmation_revision"]=13
        result=self.make_report(inputs=[saved],rows=[self.opening,middle,self.closing])
        self.assertEqual(result["items"][0]["gain_status"],"user_confirmed")
        self.assertEqual(result["historical_inputs"],[])

    def test_mixed_subtotals_remain_estimated(self):
        middle=snapshot("130000",10)
        saved={"id":uuid4(),"account_id":self.account["id"],"opening_snapshot_id":self.opening["id"],"closing_snapshot_id":middle["id"],
            "opening_as_of":self.opening["as_of"],"closing_as_of":middle["as_of"],"status":"active","row_version":0,
            "contributions_amount":Decimal(20000),"withdrawals_amount":Decimal(0),"confirmed_by_user_id":uuid4(),
            "confirmed_at":datetime.now(timezone.utc)}
        totals=self.make_report(inputs=[saved],rows=[self.opening,middle,self.closing])["native_currency_totals"][0]
        self.assertEqual((totals["confirmed_gain_subtotal"],totals["estimated_gain_subtotal"],totals["combined_gain"]),("10000.00","30000.00","40000.00"))
        self.assertEqual(totals["gain_status"],"estimated")
        self.assertEqual((totals["confirmed_interval_count"],totals["estimated_interval_count"]),(1,1))
