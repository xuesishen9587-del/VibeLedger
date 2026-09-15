from datetime import date
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from tests.support.db_helper import BaseDbTestCase
from tests.integration import test_s3_balances_db as balances_setup, test_s2_spending_db as spending_setup
from app.repositories import simplified_schema as schema, spending as repo


class TestS4InvestmentGainsDb(BaseDbTestCase):
    observation=balances_setup.TestS3BalancesDb.observation
    save=balances_setup.TestS3BalancesDb.save

    def seed_test_data(self):
        spending_setup.TestS2SpendingDb.seed_test_data(self)
        self.account=schema.create_account(self.conn,self.hh,"Funds","brokerage total","investment","CNY",opened_on=date(2026,1,1))
        self.conn.commit()
        self.opening=self.save(self.observation("100000.00",as_of="2026-02-01T12:00:00+08:00")).json()["snapshots"][0]
        self.closing=self.save(self.observation("160000.00",as_of="2026-02-20T12:00:00+08:00")).json()["snapshots"][0]

    def body(self,**kw):
        return {"opening_snapshot_id":self.opening["id"],"closing_snapshot_id":self.closing["id"],
            "contributions_amount":"50000.00","withdrawals_amount":"0.00","expected_version":None,**kw}

    def put(self,body=None,key=None):
        return self.client.put("/api/v1/investment-period-inputs",json=body or self.body(),headers={"Idempotency-Key":key or str(uuid4())})

    def report(self,**params):
        result=self.client.get("/api/v1/reports/investments",params=params)
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def test_estimate_confirm_replay_void_reactivate_and_history(self):
        self.assertEqual(self.report()["items"][0]["gain"],"60000.00")
        key=str(uuid4())
        result=self.put(key=key)
        self.assertEqual(result.status_code,201,result.text)
        self.assertEqual(self.put(key=key).json(),result.json())
        item=self.report()["items"][0]
        self.assertEqual((item["gain"],item["gain_status"]),("10000.00","user_confirmed"))
        self.assertEqual(self.client.get("/api/v1/review",params={"section":"investment"}).json()["counts"]["unusual_investment_estimates"],0)
        void=self.client.post(f"/api/v1/investment-period-inputs/{result.json()['id']}/void",json={"expected_version":0,"reason":"Incomplete totals"},headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(void.status_code,200,void.text)
        self.assertEqual(self.report()["items"][0]["gain_status"],"estimated")
        self.assertEqual(self.put().status_code,409)
        active=self.put(self.body(expected_version=1,contributions_amount="0.00"))
        self.assertEqual(active.status_code,200,active.text)
        self.assertEqual(active.json()["id"],result.json()["id"])
        self.assertFalse(self.report()["items"][0]["needs_review"])
        events=self.client.get("/api/v1/history",params={"entity_type":"investment_period_input","entity_id":result.json()["id"]})
        self.assertEqual(len(events.json()["items"]),3)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],0)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM account_snapshots")[0]["n"],2)

    def test_insert_between_pair_invalidates_confirmation_and_stale_write(self):
        self.assertEqual(self.put().status_code,201)
        self.assertEqual(self.save(self.observation("130000.00",as_of="2026-02-10T12:00:00+08:00")).status_code,201)
        report=self.report()
        self.assertEqual(len(report["historical_inputs"]),1)
        self.assertEqual([r["gain_status"] for r in report["items"]],["estimated","estimated"])
        self.assertTrue(all(r["reason"]=="PAIR_CHANGED" for r in report["items"]))
        self.assertEqual(self.put(self.body(expected_version=0)).status_code,409)
        page=self.client.get("/api/v1/review",params={"section":"investment","limit":1}).json()
        self.assertEqual(page["counts"]["unusual_investment_estimates"],2)
        second=self.client.get("/api/v1/review",params={"section":"investment","limit":1,"cursor":page["next_cursor"]}).json()
        self.assertNotEqual(page["items"][0]["id"],second["items"][0]["id"])

    def test_concurrent_create_only_one_assertion_and_changed_body_rejected(self):
        body=self.body()
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.put(body).status_code,range(2)))
        self.assertEqual(sorted(results),[201,409])
        key=str(uuid4())
        result=self.put(self.body(expected_version=0),key)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.put(self.body(expected_version=1),key).status_code,409)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM investment_period_inputs")[0]["n"],1)

    def test_atomic_audit_failure_leaves_no_input_or_committed_receipt(self):
        key=str(uuid4())
        with patch("app.services.investment_gains.history",side_effect=RuntimeError("audit failed")),self.assertRaises(RuntimeError):
            self.put(key=key)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM investment_period_inputs")[0]["n"],0)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM ingestion_requests WHERE idempotency_key=%s",(key,))[0]["n"],0)

    def test_invalid_foreign_nonconsecutive_and_device_writes(self):
        for values in ({"contributions_amount":"-1"},{"withdrawals_amount":"NaN"},{"contributions_amount":"0.001"},
            {"opening_snapshot_id":self.closing["id"],"closing_snapshot_id":self.opening["id"]}):
            self.assertEqual(self.put(self.body(**values)).status_code,422)
        self.assertEqual(self.put(self.body(opening_snapshot_id=str(uuid4()))).status_code,404)
        self.assertEqual(self.client.get("/api/v1/reports/investments",params={"account_id":str(uuid4())}).status_code,404)
        from app.auth.context import AuthContext
        self.actor=AuthContext("device",self.user,self.hh,"owner",device_id=uuid4())
        self.assertEqual(self.put().status_code,403)

    def test_threshold_settings_version_and_history(self):
        settings=self.client.get("/api/v1/review").json()
        body={"expected_version":settings["settings_row_version"],"investment_review_change_ratio":"0.7"}
        result=self.client.patch("/api/v1/household-settings",json=body,headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(result.status_code,200,result.text)
        self.assertFalse(self.report()["items"][0]["needs_review"])
        self.assertEqual(self.client.patch("/api/v1/household-settings",json=body,headers={"Idempotency-Key":str(uuid4())}).status_code,409)
        history=self.client.get("/api/v1/history",params={"entity_type":"household","entity_id":str(self.hh)})
        self.assertEqual(len(history.json()["items"]),1)
        for value in ("NaN","0","1.01","0.12345"):
            body.update(expected_version=result.json()["row_version"],investment_review_change_ratio=value)
            self.assertEqual(self.client.patch("/api/v1/household-settings",json=body,headers={"Idempotency-Key":str(uuid4())}).status_code,422)

    def test_partial_native_currency_range_never_uses_fx(self):
        usd=schema.create_account(self.conn,self.hh,"USD funds","dollar portfolio","investment","USD",opened_on=date(2026,1,1))
        self.conn.commit()
        self.save(self.observation("100.00",usd,as_of="2026-02-01T12:00:00+08:00"))
        self.save(self.observation("105.00",usd,as_of="2026-02-20T12:00:00+08:00"))
        totals={r["currency"]:r for r in self.report()["native_currency_totals"]}
        self.assertEqual(totals["USD"]["combined_gain"],"5.00")
        result=self.report(**{"from":"2026-02-02","to":"2026-02-20"})
        self.assertEqual(result["items"],[])
        self.assertEqual(len(result["excluded_boundary_intervals"]),2)
        self.assertTrue(all(r["combined_gain"] is None for r in result["native_currency_totals"]))
        self.assertFalse(result["coverage"]["complete"])

    def test_correction_replaces_review_pair_and_preserves_old_history(self):
        self.assertEqual(self.put().status_code,201)
        result=self.client.post(f"/api/v1/snapshots/{self.closing['id']}/correct",json={"expected_version":0,
            "expected_latest_snapshot_id":self.closing["id"],"expected_account_version":0,"reason":"Wrong closing value", "balance":"150000.00",
            "currency":"CNY","as_of":self.closing["as_of"],"time_basis":"explicit"},headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(result.status_code,200,result.text)
        row=self.report()["items"][0]
        self.assertEqual(row["gain"],"50000.00")
        self.assertEqual(row["reason"],"PAIR_CHANGED")
        self.assertNotEqual(row["closing_snapshot_id"],self.closing["id"])
        self.assertEqual(self.put(self.body(expected_version=0)).status_code,404)

    def test_voiding_inserted_observation_does_not_resurrect_old_confirmation(self):
        confirmed=self.put().json()
        middle=self.save(self.observation("130000.00",as_of="2026-02-10T12:00:00+08:00")).json()["snapshots"][0]
        result=self.client.post(f"/api/v1/snapshots/{middle['id']}/void",json={"expected_version":0,
            "expected_latest_snapshot_id":self.closing["id"],"expected_account_version":0,"reason":"Remove incorrect middle value"},headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(result.status_code,200,result.text)
        item=self.report()["items"][0]
        self.assertEqual(item["input_id"],confirmed["id"])
        self.assertEqual((item["gain_status"],item["reason"]),("estimated","PAIR_CHANGED"))
        self.assertEqual(self.put(self.body(expected_version=0)).status_code,200)
        self.assertEqual(self.report()["items"][0]["gain_status"],"user_confirmed")

    def test_split_racing_confirmation_never_leaves_confirmed_subintervals(self):
        observation=self.observation("130000.00",as_of="2026-02-10T12:00:00+08:00")
        with ThreadPoolExecutor(2) as pool:
            confirm=pool.submit(self.put)
            split=pool.submit(self.save,observation)
            self.assertIn(confirm.result().status_code,(201,409))
            self.assertEqual(split.result().status_code,201)
        result=self.report()
        self.assertEqual(len(result["items"]),2)
        self.assertTrue(all(r["gain_status"]=="estimated" for r in result["items"]))

    def test_other_household_cannot_read_edit_void_or_view_input_history(self):
        result=self.put().json()
        from app.auth.context import AuthContext
        other_hh,other_user=uuid4(),uuid4()
        schema.create_household(self.conn,"Other",household_id=other_hh)
        schema.create_user(self.conn,auth_subject=str(other_user),email=f"{other_user}@example.test",display_name="Other",user_id=other_user)
        schema.add_household_member(self.conn,other_hh,other_user,"owner")
        self.conn.commit()
        self.actor=AuthContext("browser",other_user,other_hh,"owner")
        self.assertEqual(self.report()["items"],[])
        self.assertEqual(self.put(self.body(expected_version=0)).status_code,404)
        void=self.client.post(f"/api/v1/investment-period-inputs/{result['id']}/void",json={"expected_version":0,"reason":"Foreign"},headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(void.status_code,404)
        history=self.client.get("/api/v1/history",params={"entity_type":"investment_period_input","entity_id":result["id"]})
        self.assertEqual(history.status_code,404)
