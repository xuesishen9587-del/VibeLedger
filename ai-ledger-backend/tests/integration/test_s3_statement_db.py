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

    def test_acknowledgement_clears_uncertainty_but_preserves_validation_and_atomic_import(self):
        confidence={k:.2 for k in ("amount","currency","date","intent","category")}
        draft=self.upload(self.extraction([
            self.line(merchant="Low confidence",confidence=confidence),
            self.line(merchant="Missing category",confidence=confidence),
            self.line(merchant="Unknown intent",kind="unknown"),
            self.line(merchant="Missing date",occurred_on=None),
            self.line(merchant="Invalid amount",amount="0"),
        ])).json()
        lines=[{"row_id":r["row_id"],"action":r["action"]} for r in draft["draft"]["lines"]]
        lines[1]["category_id"]=None
        reviewed=self.edit(draft,lines)
        by_row={w["row_id"]:w["code"] for w in reviewed["warnings"]}
        self.assertNotIn(lines[0]["row_id"],by_row)
        self.assertEqual(by_row[lines[1]["row_id"]],"INVALID_CATEGORY")
        self.assertEqual(by_row[lines[2]["row_id"]],"INVALID_TRANSACTION_TYPE")
        self.assertIn(lines[3]["row_id"],by_row)
        self.assertIn(lines[4]["row_id"],by_row)
        blocked=self.confirm(reviewed).json()
        self.assertEqual(blocked["status"],"needs_confirmation")
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],0)
        lines[1]["category_id"]=draft["draft"]["lines"][0]["category_id"]
        lines[2]["transaction_type"]="expense"
        lines[3]["occurred_on"]="2026-02-03"
        lines[4]["original_amount"]="12.00"
        fixed=self.edit(blocked,lines)
        self.assertEqual(fixed["warnings"],[])
        result=self.confirm(fixed).json()
        self.assertEqual(result["status"],"committed")
        self.assertEqual(result["counts"]["create"],5)
        self.assertEqual(self.confirm(fixed).json(),result)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],5)

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
        # Extracted refunds now have an honest source note. Explicitly removing
        # that note must still preserve the existing unlinked-refund guard.
        draft=self.edit(draft,[{"row_id":r["row_id"],"action":r["action"],"remarks":None} for r in draft["draft"]["lines"]])
        self.assertEqual(self.confirm(draft).json()["status"],"needs_confirmation")
        current=self.client.get(f"/api/v1/ingestion-requests/{draft['request_id']}").json()
        lines=[{"row_id":r["row_id"],"action":r["action"],**({"remarks":"Prior purchase refund"} if r["transaction_type"]=="refund" else {})} for r in current["draft"]["lines"]]
        edited=self.edit(current,lines)
        result=self.confirm(edited)
        self.assertEqual(result.json()["status"],"committed",result.text)
        self.assertEqual(result.json()["counts"],{"create":2,"link":0,"skip":1})

    def test_opposite_statement_signs_import_without_per_row_edits(self):
        from decimal import Decimal
        for sign in ("-", "+"):
            draft=self.upload(self.extraction([
                self.line(amount=sign+"100.00",merchant="Purchase "+sign),
                self.line(amount=sign+"20.00",kind="refund",merchant="Refund "+sign),
                self.line(amount=sign+"5.00",kind="fee",merchant="Fee "+sign),
                self.line(amount=sign+"300.00",kind="repayment")])).json()
            self.assertEqual(draft["warnings"],[])
            result=self.confirm(draft)
            self.assertEqual(result.json()["status"],"committed",result.text)
            self.assertEqual(result.json()["counts"],{"create":3,"link":0,"skip":1})
            self.assertEqual(self.confirm(draft).json(),result.json())
        records=repo.rows(self.conn,"SELECT * FROM transactions")
        self.assertEqual(len(records),6)
        self.assertEqual(sum(r["original_amount"]*(-1 if r["transaction_type"]=="refund" else 1) for r in records),Decimal("170.00"))

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

    def test_explicit_legacy_amount_repair_preserves_evidence_edits_and_uncertainty(self):
        import json
        draft=self.upload(self.extraction([
            self.line(amount="-12.00"),
            self.line(amount="-23.00",merchant="Uncertain",confidence={k:.2 for k in ("amount","currency","date","intent","category")}),
            self.line(amount="-34.00",merchant="Edited"),
            self.line(amount="-45.00",kind="unknown",merchant="Unknown")])).json()
        legacy=draft["draft"]
        for line,amount in zip(legacy["lines"],["-12.00","-23.00","8.00","-45.00"]):
            line["original_amount"]=amount
            line.pop("kind",None)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE ingestion_requests SET draft_payload=%s::jsonb WHERE id=%s",
                (json.dumps(legacy),draft["request_id"]))
        self.conn.commit()
        evidence=repo.rows(self.conn,"SELECT id,extracted_payload FROM statement_lines ORDER BY row_no")
        path=f"/api/v1/ingestion-requests/{draft['request_id']}"
        self.assertEqual(self.client.get(path).json()["draft"]["lines"][0]["original_amount"],"-12.00")
        body={"expected_version":draft["row_version"],"normalize_display_amounts":True,
            "lines":[{"row_id":r["row_id"],"action":r["action"]} for r in legacy["lines"]]}
        repaired=self.client.patch(path+"/draft",json=body)
        self.assertEqual(repaired.status_code,200,repaired.text)
        lines=repaired.json()["draft"]["lines"]
        self.assertEqual([r["original_amount"] for r in lines],["12.00","23.00","8.00","-45.00"])
        self.assertFalse(lines[0]["requires_review"])
        self.assertTrue(lines[1]["requires_review"])
        self.assertTrue(lines[1]["category_uncertain"])
        self.assertEqual(evidence,repo.rows(self.conn,"SELECT id,extracted_payload FROM statement_lines ORDER BY row_no"))
        self.assertEqual(self.client.patch(path+"/draft",json=body).status_code,409)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],0)

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

    def test_identical_provider_rows_share_one_transaction_and_replay_once(self):
        draft=self.upload(self.extraction([
            self.line(provider_transaction_id="shared-id",amount="12"),
            self.line(provider_transaction_id="shared-id",amount="12.00",merchant="Page overlap"),
            self.line(provider_transaction_id="shared-id",amount="+12.0"),
        ])).json()
        self.assertEqual(draft["warnings"],[])
        saved=self.confirm(draft).json()
        self.assertEqual(saved["counts"],{"create":1,"link":2,"skip":0})
        self.assertEqual(len({r["transaction_id"] for r in saved["lines"]}),1)
        self.assertEqual(self.confirm(draft).json(),saved)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],1)

    def test_conflicting_provider_facts_are_all_actionable_before_apply(self):
        for changes in ({"amount":"15.00"},{"occurred_on":"2026-02-04"},{"kind":"refund"},{"currency":"USD"}):
            with self.subTest(changes=changes):
                pid=str(uuid4())
                draft=self.upload(self.extraction([self.line(merchant="Unrelated"),
                    self.line(provider_transaction_id=pid),self.line(provider_transaction_id=pid,**changes)])).json()
                expected={r["row_id"] for r in draft["draft"]["lines"][1:]}
                self.assertEqual({w["row_id"] for w in draft["warnings"] if w["code"]=="STATEMENT_PROVIDER_CONFLICT"},expected)
                with patch("app.services.statement_import.spending.create_record") as apply:
                    blocked=self.confirm(draft).json()
                    apply.assert_not_called()
                self.assertEqual(blocked["status"],"needs_confirmation")
                self.assertEqual({w["row_id"] for w in blocked["warnings"]},expected)
                self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],0)
                self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM statement_lines WHERE final_action IS NOT NULL")[0]["n"],0)

    def test_88_rows_period_correction_then_provider_correction_commits_once(self):
        lines=[self.line(merchant=f"Purchase {i}",provider_transaction_id="repeat" if i<2 else None,
            amount="15.00" if i==1 else "12.00") for i in range(86)]
        lines += [self.line(kind="refund"),self.line(kind="repayment")]
        draft=self.upload(self.extraction(lines,period_start="2026-02-10")).json()
        self.assertIn("STATEMENT_DATE_OUTSIDE_PERIOD",{w["code"] for w in draft["warnings"]})
        fixed_period=self.edit(draft,period_start="2026-02-01")
        ids={r["row_id"] for r in fixed_period["draft"]["lines"][:2]}
        self.assertEqual({w["row_id"] for w in fixed_period["warnings"]},ids)
        with patch("app.services.statement_import.spending.create_record") as apply:
            blocked=self.confirm(fixed_period).json()
            apply.assert_not_called()
        edits=[{"row_id":r["row_id"],"action":r["action"]} for r in blocked["draft"]["lines"]]
        edits[1]["original_amount"]="12.00"
        corrected=self.edit(blocked,edits)
        self.assertEqual(corrected["warnings"],[])
        saved=self.confirm(corrected).json()
        self.assertEqual(saved["counts"],{"create":86,"link":1,"skip":1})
        self.assertEqual(self.confirm(corrected).json(),saved)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],86)

    def test_provider_conflict_can_be_resolved_by_skipping_incorrect_row(self):
        draft=self.upload(self.extraction([self.line(provider_transaction_id="same"),
            self.line(provider_transaction_id="same",amount="15.00")])).json()
        lines=[{"row_id":r["row_id"],"action":"create"} for r in draft["draft"]["lines"]]
        lines[1].update(action="skip",reason="Duplicate extraction with wrong amount")
        fixed=self.edit(draft,lines)
        self.assertEqual(fixed["warnings"],[])
        self.assertEqual(self.confirm(fixed).json()["counts"],{"create":1,"link":0,"skip":1})

    def test_provider_create_and_explicit_link_are_order_independent(self):
        original=self.confirm(self.upload().json()).json()["lines"][0]["transaction_id"]
        for link_index in (0,1):
            draft=self.upload(self.extraction([self.line(provider_transaction_id=str(link_index))]*2)).json()
            lines=[{"row_id":r["row_id"],"action":"create"} for r in draft["draft"]["lines"]]
            lines[link_index].update(action="link_existing",transaction_id=original,expected_transaction_version=0)
            fixed=self.edit(draft,lines)
            self.assertEqual(fixed["warnings"],[])
            saved=self.confirm(fixed).json()
            self.assertEqual(saved["counts"],{"create":0,"link":2,"skip":0})
            self.assertEqual({r["transaction_id"] for r in saved["lines"]},{original})
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM transactions")[0]["n"],1)

    def test_same_provider_cannot_link_different_targets_even_with_identical_facts(self):
        saved=self.confirm(self.upload(self.extraction([self.line(),self.line()])).json()).json()
        draft=self.upload(self.extraction([self.line(provider_transaction_id="ambiguous")]*2)).json()
        lines=[{"row_id":r["row_id"],"action":"link_existing","transaction_id":t["transaction_id"],"expected_transaction_version":0}
            for r,t in zip(draft["draft"]["lines"],saved["lines"])]
        blocked=self.edit(draft,lines)
        self.assertEqual({w["row_id"] for w in blocked["warnings"]},{r["row_id"] for r in lines})
        self.assertTrue(all(w["code"]=="STATEMENT_PROVIDER_CONFLICT" for w in blocked["warnings"]))
        lines[1]["transaction_id"]=lines[0]["transaction_id"]
        fixed=self.edit(blocked,lines)
        self.assertEqual(fixed["warnings"],[])
        self.assertEqual(self.confirm(fixed).json()["counts"]["link"],2)

    def test_apply_exception_retains_row_and_rolls_back_links_transactions_and_audit(self):
        from app.domain.spending import fail
        from app.services.balance_service import history
        draft=self.upload(self.extraction([self.line(provider_transaction_id="repeat")]*2)).json()
        def injected(conn,actor,receipt_id,result,*args,**kw):
            if result["id"]==draft["draft"]["lines"][1]["row_id"]:
                fail("IMPORT_CHANGED","Late validation failure",409)
            return history(conn,actor,receipt_id,result,*args,**kw)
        with patch("app.services.statement_import.balances.history",side_effect=injected):
            blocked=self.confirm(draft).json()
        self.assertEqual(blocked["warnings"][0]["row_id"],draft["draft"]["lines"][1]["row_id"])
        self.assertEqual(blocked["draft"]["lines"],draft["draft"]["lines"])
        for table in ("transactions","audit_events"):
            self.assertEqual(repo.rows(self.conn,f"SELECT count(*) n FROM {table} WHERE source_request_id=%s",(draft["request_id"],))[0]["n"],0)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM statement_lines WHERE final_action IS NOT NULL")[0]["n"],0)
        fixed=self.edit(blocked)
        saved=self.confirm(fixed).json()
        self.assertEqual(saved["counts"],{"create":1,"link":1,"skip":0})
        self.assertEqual(self.confirm(fixed).json(),saved)

    def test_unselected_schedule_is_a_row_blocker_before_apply(self):
        draft=self.upload().json()
        fixed=self.edit(draft,[{"row_id":draft["draft"]["lines"][0]["row_id"],"action":"use_schedule_period"}])
        self.assertEqual(fixed["warnings"][0]["code"],"INVALID_SCHEDULE")
        self.assertEqual(fixed["warnings"][0]["row_id"],draft["draft"]["lines"][0]["row_id"])

    def test_late_balance_failure_is_global_and_rolls_back_every_transaction(self):
        from app.domain.spending import fail
        closing={"row_id":"closing","label":"Wallet","amount":"100.00","currency":"CNY","meaning":"asset","as_of":"2026-02-28",
            "current_screen":False,"overlap_uncertain":False,"confidence":{k:.99 for k in ("amount","currency","account","scope","date")}}
        draft=self.upload(self.extraction(closing_balance=closing)).json()
        self.assertTrue(draft["draft"]["balance"]["selected"])
        def changed(*args,**kwargs):
            fail("BALANCE_CHANGED","Refresh or exclude the closing balance.",409)
        with patch("app.services.statement_import.balances.create_record",side_effect=changed):
            blocked=self.confirm(draft).json()
        self.assertIsNone(blocked["warnings"][0]["row_id"])
        self.assertEqual(blocked["warnings"][0]["code"],"BALANCE_CHANGED")
        for table in ("transactions","account_snapshots"):
            self.assertEqual(repo.rows(self.conn,f"SELECT count(*) n FROM {table}")[0]["n"],0)
        self.assertEqual(repo.rows(self.conn,"SELECT count(*) n FROM statement_lines WHERE final_action IS NOT NULL")[0]["n"],0)
        fixed=self.edit(blocked)
        saved=self.confirm(fixed).json()
        self.assertEqual(saved["status"],"committed")
        self.assertEqual(self.confirm(fixed).json(),saved)
