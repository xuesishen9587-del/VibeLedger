import base64
import io
from datetime import date
from uuid import uuid4
from unittest.mock import Mock
from PIL import Image
from tests.support.db_helper import BaseDbTestCase
from tests.integration import test_s3_balances_db as setup
from app.api.routes.balance_captures import get_balance_model
from app.repositories import spending as repo, simplified_schema as schema


class TestS3BalanceCaptureDb(BaseDbTestCase):
    def seed_test_data(self):
        setup.TestS3BalancesDb.seed_test_data(self)
        self.model=Mock()
        self.client.app.dependency_overrides[get_balance_model]=lambda:self.model
        buffer=io.BytesIO()
        Image.new("RGB",(3,3)).save(buffer,format="PNG")
        self.image=base64.b64encode(buffer.getvalue()).decode()

    def row(self,**kw):
        return {"row_id":"r1","label":"Wallet","amount":"100.00","currency":"CNY","meaning":"asset",
            "current_screen":True,"overlap_uncertain":False,"confidence":{k:.99 for k in ("amount","currency","account","scope","date")},**kw}

    def capture(self,rows,totals=None,key=None):
        self.model.extract_balances.return_value={"rows":rows,"totals":totals or []}
        return self.client.post("/api/v1/balance-captures",json={"idempotency_key":key or str(uuid4()),
            "captured_at":"2026-02-03T12:00:00+08:00","image":{"mime_type":"image/png","base64":self.image}})

    def test_clear_capture_replays_and_preserves_expense_independence(self):
        key=str(uuid4())
        first=self.capture([self.row()],key=key)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()["status"],"committed")
        self.assertEqual(first.json(),self.capture([self.row()],key=key).json())
        self.assertEqual(self.model.extract_balances.call_count,1)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],0)

    def test_debt_monthly_bill_and_overpayment(self):
        schema.create_account(self.conn,self.hh,"Card","full debt","credit","CNY",opened_on=date(2026,1,1))
        self.conn.commit()
        draft=self.capture([self.row(label="Card",meaning="debt",amount="1000.00",debt_scope="monthly_bill")]).json()
        self.assertEqual(draft["status"],"needs_confirmation")
        self.assertIsNone(draft["draft"]["rows"][0]["balance"])
        result=self.capture([self.row(label="Card",meaning="debt",amount="3000.00",debt_scope="total_debt")]).json()
        self.assertEqual(result["snapshots"][0]["balance"],"-3000.00")
        surplus=self.row(label="Card",meaning="overpayment",amount="200.00",as_of="2026-02-04T12:00:00+08:00")
        result=self.capture([surplus]).json()
        self.assertEqual(result["snapshots"][0]["balance"],"200.00")

    def test_unknown_row_requires_explicit_exclusion_and_whole_set_saved(self):
        draft=self.capture([self.row(),self.row(row_id="r2",label="Unknown",account="Unknown")]).json()
        self.assertEqual(draft["status"],"needs_confirmation")
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM account_snapshots")[0]["n"],0)
        fields=("row_id","selected","account_id","balance","currency","as_of","time_basis","expected_latest_snapshot_id","expected_account_version")
        rows=[{k:r[k] for k in fields} for r in draft["draft"]["rows"]]
        base=f"/api/v1/ingestion-requests/{draft['request_id']}"
        response=self.client.patch(base+"/draft",json={"expected_version":draft["row_version"],"rows":rows[:1]})
        self.assertEqual(response.status_code,422)
        rows[1].update(selected=False,exclusion_reason="Not tracked")
        response=self.client.patch(base+"/draft",json={"expected_version":draft["row_version"],"rows":rows})
        self.assertEqual(response.status_code,200,response.text)
        saved=self.client.post(base+"/confirm",json={"expected_version":response.json()["row_version"]})
        self.assertEqual(saved.json()["status"],"committed",saved.text)
        self.assertEqual(len(saved.json()["snapshots"]),1)

    def test_totals_are_not_balances_and_mismatch_is_reviewed(self):
        total={"amount":"100.00","currency":"CNY","covered_row_ids":["r1"],"scope":"complete"}
        result=self.capture([self.row(),self.row(row_id="total",meaning="total")],[total])
        self.assertEqual(result.json()["status"],"committed",result.text)
        self.assertEqual(len(result.json()["snapshots"]),1)
        total["amount"]="999.00"
        draft=self.capture([self.row(as_of="2026-02-05T12:00:00+08:00")],[total]).json()
        self.assertEqual(draft["status"],"needs_confirmation")
        self.assertTrue(any(w["code"]=="TOTAL_REQUIRES_REVIEW" for w in draft["warnings"]))

    def test_late_configuration_change_refreshes_draft_and_invalid_image_is_private(self):
        def model(*args):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE accounts SET row_version=row_version+1 WHERE id=%s",(self.account["id"],))
            self.conn.commit()
            return {"rows":[self.row()]}
        self.model.extract_balances.side_effect=model
        result=self.capture([self.row()])
        self.assertEqual(result.json()["status"],"needs_confirmation",result.text)
        self.assertEqual(result.json()["draft"]["rows"][0]["expected_account_version"],1)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM account_snapshots")[0]["n"],0)
        invalid=self.client.post("/api/v1/balance-captures",json={"secret":"DO-NOT-ECHO"})
        self.assertEqual(invalid.status_code,422)
        self.assertNotIn("DO-NOT-ECHO",invalid.text)
