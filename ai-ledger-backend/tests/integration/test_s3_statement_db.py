from uuid import uuid4
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from tests.support.db_helper import BaseDbTestCase
from tests.integration import test_s3_balances_db as setup
from app.api.routes.statements import get_statement_parser
from app.repositories import spending as repo, simplified_schema as schema


class TestS3StatementDb(BaseDbTestCase):
    def seed_test_data(self):
        setup.TestS3BalancesDb.seed_test_data(self)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE accounts SET statement_import_enabled=true WHERE id=%s",(self.account["id"],))
        self.conn.commit()
        self.parser=Mock()
        self.client.app.dependency_overrides[get_statement_parser]=lambda:self.parser

    def line(self,**kw):
        return {"occurred_on":"2026-02-03","amount":"12.00","currency":"CNY","merchant":"Cafe","kind":"expense","category":"Other",
            "confidence":{k:.99 for k in ("amount","currency","date","intent","category")},**kw}

    def extraction(self,lines=None,**kw):
        return {"account_hint":"Wallet","account_currency":"CNY","account_confidence":.99,"period_start":"2026-02-01","period_end":"2026-02-28",
            "lines":[self.line()] if lines is None else lines,"processed_pages":[1],"actual_page_count":1,"expected_line_count":len(lines) if lines is not None else 1,"complete":True,**kw}

    def upload(self,data=None,key=None,document=None):
        self.parser.parse.return_value=data or self.extraction()
        return self.client.post(f"/api/v1/accounts/{self.account['id']}/statement-imports",headers={"Idempotency-Key":key or str(uuid4())},
            files={"file":("test.pdf",document or b"%PDF-"+str(uuid4()).encode(),"application/pdf")},data={"password":"PRIVATE-PASSWORD"})

    def confirm(self,draft):
        return self.client.post(f"/api/v1/ingestion-requests/{draft['request_id']}/confirm",json={"expected_version":draft["row_version"]})

    def edit(self,draft,lines=None,**kw):
        lines=lines if lines is not None else [{"row_id":r["row_id"],"action":r["action"]} for r in draft["draft"]["lines"]]
        lines=[{**r,"confirm_facts":True} for r in lines]
        response=self.client.patch(f"/api/v1/ingestion-requests/{draft['request_id']}/draft",json={"expected_version":draft["row_version"],"lines":lines,**kw})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_preview_then_atomic_save_and_repeat_file(self):
        document=b"%PDF-repeat-test"
        result=self.upload(document=document)
        self.assertEqual(result.status_code,200,result.text)
        draft=result.json()
        self.assertEqual(draft["status"],"needs_confirmation")
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],0)
        saved=self.confirm(draft)
        self.assertEqual(saved.json()["status"],"committed",saved.text)
        self.assertEqual(saved.json()["counts"],{"create":1,"link":0,"skip":0})
        self.assertEqual(saved.json(),self.confirm(draft).json())
        duplicate=self.upload(document=document)
        self.assertEqual(duplicate.status_code,409,duplicate.text)
        self.assertEqual(duplicate.json()["error"]["details"]["request_id"],draft["request_id"])
        self.assertEqual(self.parser.parse.call_count,1)
        receipt=schema.get_ingestion_request(self.conn,self.hh,draft["request_id"])
        self.assertNotIn("PRIVATE-PASSWORD",str(receipt))

    def test_partial_wrong_account_and_missing_business_date_require_review(self):
        draft=self.upload(self.extraction([self.line(occurred_on=None,posted_on="2026-02-03")],account_hint="Another bank",complete=False)).json()
        result=self.confirm(draft).json()
        self.assertEqual(result["status"],"needs_confirmation")
        codes={r["code"] for r in result["warnings"]}
        self.assertTrue({"PARTIAL_STATEMENT","STATEMENT_ACCOUNT_MISMATCH"}<=codes)
        line=result["draft"]["lines"][0]
        fixed=self.edit(result,[{"row_id":line["row_id"],"action":"create","occurred_on":"2026-02-03"}],acknowledge_partial=True,confirm_account_identity=True)
        self.assertEqual(self.confirm(fixed).json()["status"],"committed")

    def test_provider_id_overlap_links_and_equal_purchases_preserve_multiplicity(self):
        first=self.upload(self.extraction([self.line(provider_transaction_id="bank-1")])).json()
        saved=self.confirm(first).json()
        second=self.upload(self.extraction([self.line(provider_transaction_id="bank-1")])).json()
        self.assertEqual(second["draft"]["lines"][0]["action"],"link_existing")
        linked=self.confirm(second).json()
        self.assertEqual(linked["lines"][0]["transaction_id"],saved["lines"][0]["transaction_id"])
        multiple=self.upload(self.extraction([self.line(merchant="Different"),self.line(merchant="Different")])).json()
        self.assertEqual(self.confirm(multiple).json()["counts"]["create"],2)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],3)

    def test_concurrent_imports_warn_instead_of_duplicate(self):
        first=self.upload().json()
        second=self.upload().json()
        with ThreadPoolExecutor(2) as pool:
            result=list(pool.map(lambda d:self.confirm(d).json(),[first,second]))
        self.assertEqual(sorted(r["status"] for r in result),["committed","needs_confirmation"])
        pending=next(r for r in result if r["status"]=="needs_confirmation")
        edited=self.edit(pending)
        self.assertEqual(self.confirm(edited).json()["status"],"committed")
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],2)

    def test_last_row_failure_rolls_back_all_financial_outcomes(self):
        draft=self.upload(self.extraction([self.line(),self.line(merchant="Second")])).json()
        from app.services.spending_service import create_record
        calls=[]
        def injected(*args,**kw):
            calls.append(1)
            if len(calls)==2:
                raise RuntimeError("SQL failure")
            return create_record(*args,**kw)
        with patch("app.services.statement_import.spending.create_record",side_effect=injected),self.assertRaises(RuntimeError):
            self.confirm(draft)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],0)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM statement_lines WHERE final_action IS NOT NULL")[0]["n"],0)
        self.assertEqual(schema.get_ingestion_request(self.conn,self.hh,draft["request_id"])["status"],"needs_confirmation")

    def test_skipped_nonspending_and_unlinked_refund_require_note(self):
        draft=self.upload(self.extraction([self.line(kind="transfer"),self.line(kind="refund"),self.line(kind="fee",merchant="Fee")])).json()
        self.assertEqual(self.confirm(draft).json()["status"],"needs_confirmation")
        current=self.client.get(f"/api/v1/ingestion-requests/{draft['request_id']}").json()
        lines=[{"row_id":r["row_id"],"action":r["action"],**({"remarks":"Prior purchase refund"} if r["transaction_type"]=="refund" else {})} for r in current["draft"]["lines"]]
        edited=self.edit(current,lines)
        result=self.confirm(edited)
        self.assertEqual(result.json()["status"],"committed",result.text)
        self.assertEqual(result.json()["counts"],{"create":2,"link":0,"skip":1})

    def test_balance_only_import_and_explicit_identical_observation_reuse(self):
        closing={"row_id":"closing","label":"Wallet","amount":"100.00","currency":"CNY","meaning":"asset","as_of":"2026-02-28",
            "current_screen":False,"overlap_uncertain":False,"confidence":{k:.99 for k in ("amount","currency","account","scope","date")}}
        draft=self.upload(self.extraction([],closing_balance=closing)).json()
        saved=self.confirm(draft).json()
        self.assertEqual(saved["status"],"committed",saved)
        self.assertEqual(len(saved["snapshots"]),1)
        second=self.upload(self.extraction([],closing_balance=closing)).json()
        self.assertTrue(second["warnings"])
        balance=second["draft"]["balance"]
        fields=("row_id","selected","account_id","balance","currency","as_of","time_basis","expected_latest_snapshot_id","expected_account_version")
        balance={k:balance[k] for k in fields}
        balance["reuse_snapshot_id"]=saved["snapshots"][0]["id"]
        fixed=self.edit(second,balance=balance)
        reused=self.confirm(fixed).json()
        self.assertTrue(reused["snapshots"][0]["reused"],reused)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM account_snapshots")[0]["n"],1)

    def test_statement_binds_due_schedule_once_with_statement_provenance(self):
        from datetime import date
        from app.services.spending_schedules import run_due
        from app.db import get_connection
        with patch("app.services.spending_schedules.local_today",return_value=date(2026,2,1)):
            response=self.client.post("/api/v1/spending-schedules",json={"name":"Plan","kind":"installment","amount_per_period":"12.00","currency":"CNY",
                "period_count":2,"start_month":"2026-02-01","day_of_month":3,"merchant":"Cafe","category_id":str(self.category),
                "account_id":str(self.account["id"]),"acknowledged_due_through":"2026-02-01"},headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(response.status_code,201,response.text)
        plan=response.json()
        draft=self.upload().json()
        edited=self.edit(draft,[{"row_id":draft["draft"]["lines"][0]["row_id"],"action":"use_schedule_period",
            "schedule_id":plan["id"],"period_no":1,"expected_schedule_version":plan["row_version"]}])
        saved=self.confirm(edited)
        self.assertEqual(saved.json()["status"],"committed",saved.text)
        with patch("app.services.spending_schedules.local_today",return_value=date(2026,2,3)):
            self.assertEqual(run_due(lambda:get_connection(self.test_schema))["processed"],0)
        transactions=repo.rows(self.conn,"SELECT * FROM transactions")
        self.assertEqual(len(transactions),1)
        self.assertEqual(transactions[0]["source"],"statement")

    def test_editing_other_page_does_not_confirm_unseen_uncertain_rows(self):
        row=self.line(confidence={"amount":.2,"currency":.99,"date":.99,"intent":.99,"category":.99})
        draft=self.upload(self.extraction([row])).json()
        response=self.client.patch(f"/api/v1/ingestion-requests/{draft['request_id']}/draft",json={"expected_version":draft["row_version"],
            "lines":[{"row_id":draft["draft"]["lines"][0]["row_id"],"action":"create"}]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()["draft"]["lines"][0]["requires_review"])
        self.assertEqual(self.confirm(response.json()).json()["status"],"needs_confirmation")

    def test_voided_provider_evidence_cannot_auto_link_or_be_recreated(self):
        extraction=self.extraction([self.line(provider_transaction_id="voided-bank-id")])
        original=self.confirm(self.upload(extraction).json()).json()["lines"][0]["transaction_id"]
        response=self.client.post(f"/api/v1/transactions/{original}/void",
            json={"expected_version":0,"delete_reason":"Not a household expense"},
            headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(response.status_code,200,response.text)
        draft=self.upload(extraction).json()
        self.assertEqual(draft["draft"]["lines"][0]["action"],"create")
        self.assertIn("IMPORT_CHANGED",{w["code"] for w in draft["warnings"]})
        self.assertEqual(self.confirm(draft).json()["status"],"needs_confirmation")
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],1)

    def test_explicit_link_to_voided_target_is_blocked_but_skip_is_allowed(self):
        original=self.confirm(self.upload().json()).json()["lines"][0]["transaction_id"]
        self.client.post(f"/api/v1/transactions/{original}/void",
            json={"expected_version":0,"delete_reason":"Excluded"},headers={"Idempotency-Key":str(uuid4())})
        draft=self.upload().json()
        row_id=draft["draft"]["lines"][0]["row_id"]
        edited=self.edit(draft,[{"row_id":row_id,"action":"link_existing","transaction_id":original,"expected_transaction_version":1}])
        blocked=self.confirm(edited).json()
        self.assertEqual(blocked["status"],"needs_confirmation")
        self.assertIn("IMPORT_CHANGED",{w["code"] for w in blocked["warnings"]})
        skipped=self.edit(blocked,[{"row_id":row_id,"action":"skip","reason":"Original deliberately voided"}])
        result=self.confirm(skipped).json()
        self.assertEqual(result["counts"],{"create":0,"link":0,"skip":1})
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions WHERE status='committed'")[0]["n"],0)

    def test_changed_or_voided_link_target_blocks_the_whole_import(self):
        original=self.confirm(self.upload(self.extraction([self.line(provider_transaction_id="edited-bank-id")])).json()).json()["lines"][0]["transaction_id"]
        draft=self.upload(self.extraction([self.line(merchant="New purchase"),self.line(provider_transaction_id="edited-bank-id")])).json()
        response=self.client.patch(f"/api/v1/transactions/{original}",
            json={"expected_version":0,"original_amount":"13.00"},headers={"Idempotency-Key":str(uuid4())})
        self.assertEqual(response.status_code,200,response.text)
        blocked=self.confirm(draft).json()
        self.assertEqual(blocked["status"],"needs_confirmation")
        self.assertIn("ROW_VERSION_CONFLICT",{w["code"] for w in blocked["warnings"]})
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],1)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM statement_lines WHERE request_id=%s AND final_action IS NOT NULL",(draft["request_id"],))[0]["n"],0)

    def test_cancel_during_pdf_parse_prevents_evidence_and_financial_writes(self):
        parsing,release=Event(),Event()
        def parse(*args):
            parsing.set()
            if not release.wait(10):
                raise AssertionError("Cancellation test did not release parser")
            return self.extraction()
        self.parser.parse.side_effect=parse
        key=str(uuid4())
        with ThreadPoolExecutor(1) as pool:
            pending=pool.submit(self.upload,key=key)
            try:
                self.assertTrue(parsing.wait(10))
                cancelled=self.client.post(f"/api/v1/ingestion-requests/by-key/{key}/cancel")
                self.assertEqual(cancelled.status_code,200,cancelled.text)
                self.assertEqual(cancelled.json()["status"],"rejected")
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=10).json(),cancelled.json())
        for table in ("statement_lines","transactions","account_snapshots"):
            self.assertEqual(repo.rows(self.conn,f"SELECT count(*) n FROM {table}")[0]["n"],0)

    def test_conflicting_provider_rows_rollback_earlier_rows(self):
        draft=self.upload(self.extraction([self.line(provider_transaction_id="shared-id"),
            self.line(provider_transaction_id="shared-id",amount="15.00")])).json()
        blocked=self.confirm(draft).json()
        self.assertEqual(blocked["status"],"needs_confirmation")
        self.assertIn("IMPORT_CHANGED",{w["code"] for w in blocked["warnings"]})
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],0)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM statement_lines WHERE final_action IS NOT NULL")[0]["n"],0)
