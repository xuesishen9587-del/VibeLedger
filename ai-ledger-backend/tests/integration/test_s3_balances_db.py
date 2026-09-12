from datetime import date
from uuid import uuid4
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from tests.support.db_helper import BaseDbTestCase
from tests.integration import test_s2_spending_db as setup
from app.repositories import simplified_schema as schema, spending as repo


class TestS3BalancesDb(BaseDbTestCase):
    def test_snapshot_lookup_is_account_scoped_and_omits_voided_evidence(self):
        original=self.save(self.observation()).json()["snapshots"][0]
        path=f"/api/v1/accounts/{self.account['id']}/snapshots"
        found=self.client.get(path,params={"snapshot_id":original["id"],"limit":1})
        self.assertEqual(found.status_code,200,found.text)
        self.assertEqual([r["id"] for r in found.json()["items"]],[original["id"]])
        self.assertIsNone(found.json()["next_cursor"])
        other=schema.create_account(self.conn,self.hh,"Separate","separate cash","cash","CNY",opened_on=date(2026,1,1))
        self.conn.commit()
        wrong=self.client.get(f"/api/v1/accounts/{other['id']}/snapshots",params={"snapshot_id":original["id"]})
        self.assertEqual(wrong.json()["items"],[])
        missing=self.client.get(f"/api/v1/accounts/{uuid4()}/snapshots",params={"snapshot_id":original["id"]})
        self.assertEqual(missing.status_code,404)
        voided=self.client.post(f"/api/v1/snapshots/{original['id']}/void",json={"expected_version":0,
            "expected_latest_snapshot_id":original["id"],"expected_account_version":0,"reason":"Wrong observation"},
            headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(voided.status_code,200,voided.text)
        self.assertEqual(self.client.get(path,params={"snapshot_id":original["id"]}).json()["items"],[])
        history=self.client.get(path,params={"snapshot_id":original["id"],"include_voided":True})
        self.assertEqual(history.json()["items"][0]["status"],"voided")

    def seed_test_data(self):
        setup.TestS2SpendingDb.seed_test_data(self)
        self.account = schema.create_account(self.conn, self.hh, "Wallet", "wallet only", "cash", "CNY", risk_level="very_low", opened_on=date(2026,1,1))
        self.conn.commit()

    def observation(self, amount="100.00", account=None, **kw):
        account = account or self.account
        from app.services.balance_service import head_fields
        row = {"account_id": str(account["id"]), "balance": amount, "currency": account["currency"],
            "as_of": "2026-02-02T12:00:00+08:00", "time_basis": "explicit",
            **head_fields(self.conn, self.hh, account["id"])}
        self.conn.commit()
        return {**row, **kw}

    def save(self, *rows, key=None):
        return self.client.post("/api/v1/balance-updates", json={"observations": list(rows)}, headers={"Idempotency-Key": key or str(uuid4())})

    def test_zero_replay_and_spending_independence(self):
        key, row = str(uuid4()), self.observation("0.00")
        result = self.save(row, key=key)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json(), self.save(row, key=key).json())
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM transactions")[0]["n"], 0)
        report = self.client.get("/api/v1/reports/wealth").json()
        self.assertEqual(report["net_worth"], "0.00")
        self.assertTrue(all(r["percentage"] is None for r in report["risk_buckets"]))

    def test_multirow_failure_rolls_back_snapshots_and_audit(self):
        second = schema.create_account(self.conn, self.hh, "Bank", "bank", "savings", "CNY", opened_on=date(2026,1,1))
        self.conn.commit()
        rows = [self.observation(), self.observation(account=second)]
        from app.services.balance_service import create_record
        calls = []
        def injected(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("Simulated SQL failure")
            return create_record(*args, **kwargs)
        with patch("app.services.balance_service.create_record", side_effect=injected), self.assertRaises(RuntimeError):
            self.save(*rows)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM account_snapshots")[0]["n"], 0)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) n FROM audit_events WHERE entity_type='account_snapshot'")[0]["n"], 0)

    def test_concurrent_heads_and_backdated_observation(self):
        row = self.observation()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.save(row).status_code, range(2)))
        self.assertEqual(sorted(results), [201,409])
        historical = self.save(self.observation("50.00", as_of="2026-01-02T12:00:00+08:00"))
        self.assertEqual(historical.status_code,201,historical.text)
        report = self.client.get("/api/v1/reports/wealth").json()
        self.assertEqual(report["net_worth"],"100.00")
        old = self.client.get("/api/v1/reports/wealth", params={"as_of":"2026-01-03"}).json()
        self.assertEqual(old["net_worth"],"50.00")

    def test_correction_void_history_and_closure_guard(self):
        original = self.save(self.observation()).json()["snapshots"][0]
        common = {"expected_version":0,"expected_latest_snapshot_id":original["id"],"expected_account_version":0,"reason":"Correct amount"}
        replacement = self.client.post(f"/api/v1/snapshots/{original['id']}/correct", json={**common,
            "balance":"0.00","currency":"CNY","as_of":original["as_of"],"time_basis":"explicit"}, headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(replacement.status_code,200,replacement.text)
        new = replacement.json()
        self.assertEqual(new["replaces_snapshot_id"], original["id"])
        closed = self.client.post(f"/api/v1/accounts/{self.account['id']}/close", json={"expected_version":0,
            "closing_snapshot_id":new["id"],"closed_on":"2026-02-02"},headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(closed.status_code,200,closed.text)
        later=self.save(self.observation("5.00",as_of="2026-02-02T13:00:00+08:00"))
        self.assertEqual(later.status_code,409,later.text)
        body={**common,"expected_latest_snapshot_id":new["id"],"expected_account_version":closed.json()["row_version"]}
        rejected=self.client.post(f"/api/v1/snapshots/{new['id']}/void", json=body,headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(rejected.status_code,409,rejected.text)
        result=self.client.post(f"/api/v1/snapshots/{new['id']}/void", json={**body,"reopen_account":True},headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(result.status_code,200,result.text)
        history=self.client.get("/api/v1/history",params={"entity_type":"account_snapshot","entity_id":original["id"]})
        self.assertEqual(history.status_code,200)
        self.assertEqual(len(history.json()["items"]),2)

    def test_partial_wealth_debt_surplus_risk_and_history_gaps(self):
        credit = schema.create_account(self.conn,self.hh,"Card","total owed","credit","CNY",opened_on=date(2026,1,1))
        unknown = schema.create_account(self.conn,self.hh,"Unknown","separate","investment","CNY",opened_on=date(2026,1,1))
        self.conn.commit()
        result=self.save(self.observation("80000.00"),self.observation("-3000.00",credit))
        self.assertEqual(result.status_code,201,result.text)
        report=self.client.get("/api/v1/reports/wealth").json()
        self.assertEqual(report["known_net_worth"],"77000.00")
        self.assertIsNone(report["net_worth"])
        self.assertIn(str(unknown["id"]),report["coverage"]["missing_account_ids"])
        self.assertEqual(sum(Decimal(r["amount"]) for r in report["risk_buckets"]),Decimal("80000"))
        history=self.client.get("/api/v1/reports/wealth-history",params={"from":"2026-01-01","to":"2026-03-01"})
        self.assertEqual(history.status_code,200,history.text)
        self.assertIsNone(history.json()["points"][0]["net_worth"])

    def test_invalid_values_time_precision_and_foreign_account(self):
        for fields in ({"balance":"NaN"},{"balance":"1.001"},{"currency":"USD"},{"as_of":"2999-01-01T00:00:00Z"}):
            self.assertEqual(self.save(self.observation(**fields)).status_code,422)
        self.assertEqual(self.save(self.observation(account_id=str(uuid4()))).status_code,404)
        self.assertEqual(self.save(self.observation(time_basis="date_only",as_of="2026-02-02T23:59:59.999999+08:00")).status_code,201)
        self.assertEqual(self.save(self.observation()).status_code,409)

    def test_current_stale_fx_is_visible_but_historical_stale_fx_is_missing(self):
        usd=schema.create_account(self.conn,self.hh,"USD","dollars","cash","USD",opened_on=date(2026,1,1))
        self.conn.commit()
        self.assertEqual(self.save(self.observation(),self.observation("10.00",usd)).status_code,201)
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO fx_quotes(from_currency,to_currency,rate_as_of,rate,source) VALUES('USD','CNY','2026-01-01',7.2,'fixture')")
        self.conn.commit()
        current=self.client.get("/api/v1/reports/wealth").json()
        self.assertEqual(current["net_worth"],"172.00")
        self.assertEqual(current["coverage"]["stale_fx_currencies"],["USD"])
        historical=self.client.get("/api/v1/reports/wealth",params={"as_of":"2026-02-03"}).json()
        self.assertIsNone(historical["net_worth"])
        self.assertEqual(historical["coverage"]["missing_fx_currencies"],["USD"])

    def test_history_includes_fx_changes_expiry_and_stale_observation_boundaries(self):
        usd=schema.create_account(self.conn,self.hh,"USD","dollars","cash","USD",opened_on=date(2026,1,1))
        self.conn.commit()
        self.assertEqual(self.save(self.observation(),self.observation("10.00",usd)).status_code,201)
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO fx_quotes(from_currency,to_currency,rate_as_of,rate,source) VALUES "
                "('USD','CNY','2026-01-30',7.2,'fixture'),('USD','CNY','2026-02-03',7.3,'fixture')")
        self.conn.commit()
        response=self.client.get("/api/v1/reports/wealth-history",params={"from":"2026-02-02","to":"2026-03-10"})
        self.assertEqual(response.status_code,200,response.text)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        points={datetime.fromisoformat(p["as_of"]).astimezone(ZoneInfo("Asia/Singapore")).date():p for p in response.json()["points"]}
        self.assertEqual(points[date(2026,2,3)]["net_worth"],"173.00")
        self.assertIsNone(points[date(2026,2,11)]["net_worth"])
        self.assertEqual(points[date(2026,2,11)]["coverage"]["missing_fx_currencies"],["USD"])
        self.assertEqual(len(points[date(2026,3,5)]["coverage"]["stale_account_ids"]),2)
