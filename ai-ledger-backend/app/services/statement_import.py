"""One statement preview and atomic financial application; no reconciliation engine."""
import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import HTTPException
from app.domain.spending import fail, money, business_date
from app.domain.statement_import import StatementExtraction
from app.repositories import spending as repo, simplified_schema as schema
from app.services import capture_receipts as receipts, balance_service as balances, balance_capture
from app.services import spending_service as spending, spending_schedules as schedules
from app.services.expense_capture import references, match
from app.services.durable_commands import compute_command_hash
from app.services.statement_document import MAX_BYTES, PARSER_VERSION
from app.services.spending_reports import prime_quote


def account_for_import(conn,actor,identity):
    account=schema.get_account(conn,actor.household_id,identity)
    if not account:
        fail("ACCOUNT_NOT_FOUND","Account not found.",404)
    if not actor.is_browser:
        fail("FORBIDDEN","Statement imports require Dashboard access.",403)
    if not account["statement_import_enabled"] or account["status"]!="active":
        fail("STATEMENT_IMPORT_DISABLED","Enable statement import on an active account.",409)
    return account


def provider_matches(conn,hh,account_id,provider_id):
    if not provider_id:
        return []
    return repo.rows(conn,"SELECT DISTINCT t.* FROM statement_lines l JOIN ingestion_requests r ON r.id=l.request_id AND r.household_id=l.household_id "
        "JOIN transactions t ON t.id=l.applied_transaction_id AND t.household_id=l.household_id WHERE l.household_id=%s AND l.account_id=%s "
        "AND r.status='committed' AND l.extracted_payload->>'provider_transaction_id'=%s",(hh,account_id,provider_id))


def identity_agrees(line,target):
    return (str(target["occurred_on"])==str(line.get("occurred_on")) and str(target["original_currency"])==line.get("original_currency")
        and target["transaction_type"]==line.get("transaction_type") and target["original_amount"]==money(line.get("original_amount"),line.get("original_currency")))


def validate_balance(conn,actor,balance):
    if not balance.get("reuse_snapshot_id"):
        balances.validate(conn,actor.household_id,balance)
        return None
    from app.domain.balances import signed_money, instant
    head=balances.head_fields(conn,actor.household_id,balance["account_id"])
    if any(str(head[k])!=str(balance.get(k)) for k in head):
        fail("BALANCE_CHANGED","Review the latest account balance before linking.",409)
    rows=repo.rows(conn,"SELECT * FROM account_snapshots WHERE household_id=%s AND account_id=%s AND id=%s AND status='active'",
        (actor.household_id,balance["account_id"],balance["reuse_snapshot_id"]))
    if not rows:
        fail("SNAPSHOT_NOT_FOUND","Observation not found.",404)
    target=rows[0]
    if target["currency"]!=balance["currency"] or target["as_of"]!=instant(balance["as_of"]) or target["balance"]!=signed_money(balance["balance"],balance["currency"]):
        fail("SNAPSHOT_TIME_CONFLICT","Only an identical observation may be reused; otherwise correct or exclude it.",409)
    return target


def prepare(conn,actor,row,data,account,categories,head):
    page_count=data.pop("actual_page_count")
    extraction=StatementExtraction.model_validate(data).model_dump(mode="json")
    selected=match(extraction["account_hint"],[account])
    identity_ok=bool(selected and extraction["account_currency"]==account["currency"] and extraction["account_confidence"]>=.85)
    partial=not extraction["complete"] or set(extraction["processed_pages"])!=set(range(1,page_count+1)) or extraction["expected_line_count"]!=len(extraction["lines"])
    fallback=next(c for c in categories if c["is_fallback"])
    lines=[]
    for number,extracted in enumerate(extraction["lines"],1):
        identity=uuid4()
        repo.rows(conn,"INSERT INTO statement_lines(id,household_id,request_id,account_id,row_no,extracted_payload) VALUES(%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
            (identity,actor.household_id,row["id"],account["id"],number,json.dumps(extracted)))
        category=match(extracted["category"],categories)
        uncertain=category is None or extracted["confidence"]["category"]<.85
        category=fallback if uncertain else category
        excluded=extracted["kind"] in ("transfer","repayment","income","opening_balance","investment_trade")
        line={"row_id":str(identity),"row_no":number,"action":"skip" if excluded else "create",
            "occurred_on":extracted["occurred_on"],"original_amount":extracted["amount"],"original_currency":extracted["currency"],
            "merchant":extracted["merchant"],"transaction_type":"refund" if extracted["kind"]=="refund" else "expense" if extracted["kind"] in ("expense","fee") else None,
            "category_id":str(category["id"]),"category_uncertain":uncertain,"remarks":None,
            "provider_transaction_id":extracted["provider_transaction_id"],"reason":"Not spending: "+extracted["kind"] if excluded else None,
            "requires_review":any(extracted["confidence"][k]<.85 for k in ("amount","currency","date","intent")),"duplicate_ids":[]}
        targets=provider_matches(conn,actor.household_id,account["id"],line["provider_transaction_id"])
        if len(targets)==1 and targets[0]["status"]=="committed" and not excluded:
            try:
                agrees=identity_agrees(line,targets[0])
            except HTTPException:
                agrees=False
            if agrees:
                line.update(action="link_existing",transaction_id=str(targets[0]["id"]),expected_transaction_version=targets[0]["row_version"])
        lines.append(line)
    balance=None
    if extraction["closing_balance"]:
        item=extraction["closing_balance"]
        # Statements never substitute upload time for a missing closing date.
        item["current_screen"]=False
        proposed=balance_capture.prepare({"rows":[item]},datetime.now(timezone.utc),schema.get_household(conn,actor.household_id),[account],{str(account["id"]):head})
        if proposed["rows"]:
            balance=proposed["rows"][0]
            if balance.get("account_id")!=str(account["id"]) or balance.get("requires_review"):
                balance["selected"]=False
                balance["exclusion_reason"]="Closing balance scope or identity needs review."
    return {"lines":lines,"balance":balance,"account_id":str(account["id"]),"period_start":extraction["period_start"],
        "period_end":extraction["period_end"],"partial":partial,"acknowledge_partial":False,"identity_ok":identity_ok,
        "confirm_account_identity":False,"actual_page_count":page_count,"warnings":[]}


def validate(conn,actor,receipt,proposed,confirming=False):
    draft=json.loads(json.dumps(proposed,default=str))
    account=account_for_import(conn,actor,receipt["statement_account_id"])
    household=schema.get_household(conn,actor.household_id)
    warnings=[]
    def warn(code,message,row_id=None):
        warnings.append({"code":code,"message":message,"row_id":row_id})
    if not draft["identity_ok"] and not draft["confirm_account_identity"]:
        warn("STATEMENT_ACCOUNT_MISMATCH","Confirm the document belongs to the selected account and currency.")
    if draft["partial"] and not draft["acknowledge_partial"]:
        warn("PARTIAL_STATEMENT","Explicitly acknowledge that document coverage is incomplete.")
    try:
        start=business_date(draft["period_start"],household)
        end=business_date(draft["period_end"],household)
        if start>end:
            raise ValueError()
    except (HTTPException,ValueError,TypeError):
        start=end=None
        warn("STATEMENT_PERIOD_REQUIRED","Correct the statement period bounds.")
    provider_targets={}
    for line in draft["lines"]:
        rid=line["row_id"]
        if line["action"]=="skip":
            if not (line.get("reason") or "").strip():
                warn("SKIP_REASON_REQUIRED","State why this line is excluded.",rid)
            continue
        try:
            prior=provider_matches(conn,actor.household_id,account["id"],line.get("provider_transaction_id"))
            if line["action"]=="link_existing":
                if not line.get("transaction_id") or line.get("expected_transaction_version") is None:
                    fail("IMPORT_CHANGED","Select the target expense and its current version.",409)
                target=spending.require_record(conn,actor.household_id,line["transaction_id"],line["expected_transaction_version"])
                if target["status"]!="committed":
                    fail("IMPORT_CHANGED","This transaction was voided. Skip the statement line or review the original record.",409)
                if target["transaction_type"] not in ("expense","refund"):
                    fail("INVALID_STATEMENT_LINK","Select an expense or refund.")
                if prior and {str(t["id"]) for t in prior}!={str(target["id"])}:
                    fail("IMPORT_CHANGED","This provider ID already identifies a different transaction.",409)
                if line.get("provider_transaction_id"):
                    pid=line["provider_transaction_id"]
                    if pid in provider_targets and provider_targets[pid]!=target["id"]:
                        fail("IMPORT_CHANGED","Rows sharing a provider ID must share a target.",409)
                    provider_targets[pid]=target["id"]
                continue
            if prior:
                fail("IMPORT_CHANGED","This provider ID is already imported. Link the existing record or skip.",409)
            money(line.get("original_amount"),line.get("original_currency"))
            day=business_date(line.get("occurred_on"),household)
            if start and not start<=day<=end:
                fail("STATEMENT_DATE_OUTSIDE_PERIOD","Business date lies outside the reviewed statement period.")
            if line.get("requires_review") or line.get("transaction_type") not in ("expense","refund"):
                fail("STATEMENT_LINE_UNCERTAIN","Correct the amount, currency, business date and spending intent.")
            if line["transaction_type"]=="refund" and not (line.get("remarks") or "").strip():
                fail("REFUND_NOTE_REQUIRED","Provide a category and note for the unlinked refund.")
            category=schema.get_category(conn,actor.household_id,line.get("category_id")) if line.get("category_id") else None
            if not category or category["status"]!="active" or category["category_type"]!="expense":
                fail("INVALID_CATEGORY","Select an active expense category.")
            candidates=repo.rows(conn,"SELECT id FROM transactions WHERE household_id=%s AND status='committed' "
                "AND transaction_type=%s AND occurred_on=%s AND original_amount=%s AND original_currency=%s "
                "AND merchant_normalized IS NOT DISTINCT FROM %s AND (account_id=%s OR account_id IS NULL)",
                (actor.household_id,line["transaction_type"],day,line["original_amount"],line["original_currency"],
                 " ".join(line["merchant"].casefold().split()) if line.get("merchant") else None,account["id"]))
            ids=sorted(str(c["id"]) for c in candidates)
            if line["action"]=="create" and ids and (not line.get("explicit_create") or bool(set(ids)-set(line.get("duplicate_ids",[])))):
                warn("POSSIBLE_DUPLICATE","Choose link, skip, or explicitly create another purchase.",rid)
            line["duplicate_ids"]=ids
        except HTTPException as exc:
            detail=exc.detail.get("error",{}) if isinstance(exc.detail,dict) else {}
            warn(detail.get("code","INVALID_STATEMENT_LINE"),detail.get("message","Review this line."),rid)
    balance=draft.get("balance")
    if balance and balance["selected"]:
        try:
            if str(balance.get("account_id"))!=str(account["id"]):
                fail("STATEMENT_ACCOUNT_MISMATCH","Closing balance must belong to the selected account.")
            if balance.get("requires_review"):
                fail("BALANCE_EVIDENCE_UNCERTAIN","Review the closing balance scope and date.")
            validate_balance(conn,actor,balance)
        except HTTPException as exc:
            detail=exc.detail.get("error",{}) if isinstance(exc.detail,dict) else {}
            warn(detail.get("code","INVALID_BALANCE"),detail.get("message","Review or exclude the closing balance."))
            if detail.get("code")=="BALANCE_CHANGED":
                balance.update(balances.head_fields(conn,actor.household_id,account["id"]))
    if not draft["lines"] and not (balance and balance["selected"]):
        warn("EMPTY_IMPORT","Select a line action or a closing balance.")
    draft["warnings"]=warnings
    return draft, bool(warnings)


def upload(factory,actor,account_id,key,content,password,parser):
    start=time.monotonic()
    if not content or len(content)>MAX_BYTES or not content.startswith(b"%PDF-"):
        fail("STATEMENT_LIMIT_EXCEEDED","Upload a PDF no larger than 20 MiB.")
    digest=hashlib.sha256(content).hexdigest()
    operation=f"POST /api/v1/accounts/{account_id}/statement-imports"
    request_hash=compute_command_hash(operation,{"account_id":str(account_id),"document_sha256":digest})
    with receipts.session(factory) as conn:
        account=account_for_import(conn,actor,account_id)
        row,new=receipts.reserve(conn,actor,key,request_hash,request_kind="statement",operation=operation)
        if not new:
            return receipts.response(row)
        schema.acquire_household_finance_lock(conn,actor.household_id)
        others=repo.rows(conn,"SELECT id FROM ingestion_requests WHERE household_id=%s AND statement_account_id=%s AND document_sha256=%s "
            "AND status IN ('processing','needs_confirmation','committed')",(actor.household_id,account_id,digest))
        if others:
            code="STATEMENT_ALREADY_UPLOADED"
            result={"error":{"code":code,"message":"Open the existing import for this document.","retryable":False,"details":{"request_id":str(others[0]["id"])}}}
            return receipts.response(receipts.terminal(conn,actor,row,"rejected",result,409,code))
        repo.rows(conn,"UPDATE ingestion_requests SET statement_account_id=%s,document_sha256=%s,parser_version=%s WHERE household_id=%s AND id=%s RETURNING id",(account_id,digest,PARSER_VERSION,actor.household_id,row["id"]))
        accounts,categories=references(conn,actor.household_id)
        account=next(a for a in accounts if a["id"]==account["id"])
        head=balances.head_fields(conn,actor.household_id,account_id)
    try:
        parsed=parser.parse(content,password,account,categories)
        if time.monotonic()-start>=120:
            raise TimeoutError()
    except Exception as exc:
        code="STATEMENT_PARSE_FAILED"
        status=503
        if isinstance(exc,HTTPException) and isinstance(exc.detail,dict):
            code=exc.detail.get("error",{}).get("code",code)
            status=exc.status_code
        with receipts.session(factory) as conn:
            current=receipts.get(conn,actor,row["id"],lock=True)
            if current["status"]!="processing":
                return receipts.response(current)
            return receipts.response(receipts.terminal(conn,actor,current,"failed",{"error":{"code":code,"message":"Statement parsing failed; the document and password were discarded.","retryable":status>=500,"details":{"request_id":str(row["id"])}}},status,code))
    finally:
        content=password=None
    with receipts.session(factory) as conn:
        current=receipts.get(conn,actor,row["id"],lock=True)
        if current["status"]!="processing":
            return receipts.response(current)
        schema.acquire_household_finance_lock(conn,actor.household_id)
        receipts.authorize(conn,actor)
        proposed=prepare(conn,actor,current,parsed,account,categories,head)
        draft,_=validate(conn,actor,current,proposed)
        return receipts.response(receipts.draft(conn,actor,current,draft))


def revise(factory,actor,identity,data):
    with receipts.session(factory) as conn:
        row=receipts.get(conn,actor,identity,lock=True)
        account_for_import(conn,actor,row["statement_account_id"])
        receipts.version(row,actor,data["expected_version"])
        if row["status"]!="needs_confirmation" or row["request_kind"]!="statement":
            fail("INVALID_REQUEST_STATE","A pending statement preview is required.",409)
        schema.acquire_household_finance_lock(conn,actor.household_id)
        receipts.authorize(conn,actor)
        draft=row["draft_payload"]
        prior={r["row_id"]:r for r in draft["lines"]}
        ids=[r["row_id"] for r in data["lines"]]
        if set(ids)!=set(prior) or len(ids)!=len(set(ids)):
            fail("INVALID_STATEMENT_ROWS","Preserve every row ID and explicitly skip excluded lines.")
        revised=[]
        for line in data["lines"]:
            previous=prior[line["row_id"]]
            confirmed = line.get("confirm_facts",False)
            edited={**previous,**line,"requires_review":False if confirmed else previous["requires_review"],
                    "explicit_create":line["action"]=="create" and (confirmed or previous.get("explicit_create",False))}
            if "category_id" in line:
                edited["category_uncertain"]=False
            revised.append(edited)
        draft["lines"]=revised
        if "balance" in data:
            if draft.get("balance") and data["balance"] is None:
                fail("INVALID_BALANCE_ROWS","Explicitly deselect the closing balance.")
            if data["balance"]:
                if not draft.get("balance") or data["balance"]["row_id"]!=draft["balance"]["row_id"]:
                    fail("INVALID_BALANCE_ROWS","Preserve the closing balance row ID.")
                draft["balance"]={**draft["balance"],**data["balance"],"requires_review":False}
        for field in ("period_start","period_end","acknowledge_partial","confirm_account_identity"):
            if field in data:
                draft[field]=data[field]
        draft,_=validate(conn,actor,row,draft)
        return receipts.response(receipts.draft(conn,actor,row,draft))


def confirm(factory,actor,identity,version,provider=None):
    with receipts.session(factory) as conn:
        row=receipts.get(conn,actor,identity)
        if not actor.is_browser:
            fail("FORBIDDEN","Statement imports require Dashboard access.",403)
        if row["status"] in receipts.TERMINAL:
            return receipts.response(row)
        receipts.version(row,actor,version)
        if row["request_kind"]!="statement" or row["status"]!="needs_confirmation":
            fail("INVALID_REQUEST_STATE","A pending statement preview is required.",409)
        if provider:
            attempted=set()
            for line in row["draft_payload"]["lines"]:
                pair=(line.get("original_currency"),line.get("occurred_on"))
                if line["action"] in ("create","use_schedule_period") and pair not in attempted and len(attempted)<5:
                    attempted.add(pair)
                    prime_quote(conn,actor.household_id,line,provider)
    with receipts.session(factory) as conn:
        row=receipts.get(conn,actor,identity,lock=True)
        if row["status"] in receipts.TERMINAL:
            return receipts.response(row)
        receipts.version(row,actor,version)
        schema.acquire_household_finance_lock(conn,actor.household_id)
        receipts.authorize(conn,actor)
        draft,blocked=validate(conn,actor,row,row["draft_payload"],confirming=True)
        if blocked:
            return receipts.response(receipts.draft(conn,actor,row,draft))
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT statement_apply")
        try:
            results=[]
            local_providers={}
            for line in draft["lines"]:
                transaction=None
                action=line["action"]
                pid=line.get("provider_transaction_id")
                if action!="skip" and pid and pid in local_providers:
                    prior=local_providers[pid]
                    if action=="link_existing" and str(prior["id"])!=line.get("transaction_id"):
                        fail("IMPORT_CHANGED","Provider evidence must share one transaction.",409)
                    if action!="link_existing" and not identity_agrees(line,prior):
                        fail("IMPORT_CHANGED","Provider evidence has conflicting financial facts.",409)
                    action="link_existing"
                    line["transaction_id"]=str(prior["id"])
                if action=="link_existing":
                    transaction=repo.get(conn,actor.household_id,line["transaction_id"])
                elif action!="skip":
                    pid=line.get("provider_transaction_id")
                    if pid and pid in local_providers:
                        transaction=local_providers[pid]
                        if not identity_agrees(line,transaction):
                            fail("IMPORT_CHANGED","Conflicting lines share the same provider ID.",409)
                        action="link_existing"
                    else:
                        fields={k:line.get(k) for k in ("transaction_type","occurred_on","original_amount","original_currency","merchant","category_id","remarks")}
                        fields.update(account_id=str(row["statement_account_id"]),payment_mode="one_off" if fields["transaction_type"]=="expense" else None)
                        if action=="use_schedule_period":
                            if fields["transaction_type"]!="expense":
                                fail("INVALID_SCHEDULE","Only expenses bind schedule periods.")
                            transaction=schedules.bind_capture_period(conn,actor,row["id"],{**line,"date_source":"statement"},fields,
                                source="statement",item_key=line["row_id"])
                        else:
                            transaction=spending.create_record(conn,actor,row["id"],fields,source="statement",date_source="statement",item_key=line["row_id"],category_uncertain=line["category_uncertain"])
                        if pid:
                            local_providers[pid]=transaction
                if transaction and pid:
                    local_providers[pid]=transaction
                final_action="skip" if action=="skip" else "link" if action in ("link_existing","use_schedule_period") else "create"
                repo.rows(conn,"UPDATE statement_lines SET applied_transaction_id=%s,final_action=%s,updated_at=now() WHERE household_id=%s AND request_id=%s AND id=%s RETURNING id",(transaction["id"] if transaction else None,final_action,actor.household_id,row["id"],line["row_id"]))
                result={"id":line["row_id"],"action":final_action,"transaction_id":str(transaction["id"]) if transaction else None}
                balances.history(conn,actor,row["id"],result,"link_statement",reason=line.get("reason"),entity="statement_line")
                results.append(result)
            snapshots=[]
            if draft.get("balance") and draft["balance"]["selected"]:
                reused=validate_balance(conn,actor,draft["balance"])
                snapshots=[{**balances.output(reused or balances.create_record(conn,actor,row["id"],draft["balance"],source="statement")),"reused":reused is not None}]
        except HTTPException as exc:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT statement_apply")
            error=exc.detail.get("error",{}) if isinstance(exc.detail,dict) else {}
            draft["warnings"]=[{"code":error.get("code","IMPORT_CHANGED"),"message":error.get("message","Import changed; review before saving.")}]
            return receipts.response(receipts.draft(conn,actor,row,draft))
        repo.rows(conn,"UPDATE ingestion_requests SET period_start=%s,period_end=%s WHERE household_id=%s AND id=%s RETURNING id",(draft["period_start"],draft["period_end"],actor.household_id,row["id"]))
        result={"status":"committed","request_id":str(row["id"]),"lines":results,"snapshots":snapshots,
            "counts":{a:sum(r["action"]==a for r in results) for a in ("create","link","skip")},
            "partial_document":draft["partial"],"display_summary":"Selected statement rows and balance saved."}
        return receipts.response(receipts.terminal(conn,actor,row,"committed",result))
