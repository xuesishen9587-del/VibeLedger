import hashlib
import json
import time
from datetime import datetime, time as daytime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from app.domain.balance_capture import BalanceExtraction
from app.domain.balances import instant, signed_money
from app.domain.spending import fail, MINOR_UNITS
from app.domain.capture_images import decode_image
from app.repositories import simplified_schema as schema
from app.services import capture_receipts as receipts, balance_service as balances
from app.services.expense_capture import references, match, dependency_failure
from app.services.durable_commands import compute_command_hash


def prepare(extracted, captured, household, accounts, heads):
    extracted = BalanceExtraction.model_validate(extracted).model_dump(mode="json")
    rows, evidence = [], []
    seen = set()
    tz = ZoneInfo(household["timezone"])
    for item in extracted["rows"]:
        if item["row_id"] in seen:
            raise ValueError("Duplicate extraction row ID")
        seen.add(item["row_id"])
        if item["meaning"] == "total":
            evidence.append({"code":"TOTAL_NOT_BALANCE","message":"A displayed total is cross-check evidence, not an additional account balance."})
            continue
        account = match(item["account"] or item["label"], accounts)
        confident = all(v>=.85 for v in item["confidence"].values())
        if item["confidence"]["account"] < .85:
            account = None
        balance = item["amount"]
        try:
            amount = signed_money(balance,item["currency"])
            if item["meaning"] in ("debt","overpayment"):
                if amount<0:
                    raise ValueError()
                balance = str(-amount if item["meaning"]=="debt" else amount)
        except (HTTPException, ValueError):
            confident = False
            balance = None
        timestamp, basis = None, "explicit"
        try:
            if item["as_of"] and len(item["as_of"]) == 10:
                day = datetime.fromisoformat(item["as_of"]).date()
                if day == captured.astimezone(tz).date() and item["current_screen"]:
                    timestamp, basis = captured, "capture"
                else:
                    timestamp, basis = datetime.combine(day,daytime.max,tz), "date_only"
            elif item["as_of"]:
                timestamp = instant(item["as_of"])
            elif item["current_screen"]:
                timestamp, basis = captured, "capture"
        except (ValueError, HTTPException):
            confident = False
        valid_meaning = item["meaning"] in ("asset","debt","overpayment")
        if account and account["account_type"] == "credit":
            valid_meaning = item["meaning"]=="overpayment" or (item["meaning"]=="debt" and item["debt_scope"] in ("total_debt","outstanding_principal"))
        if item["meaning"]=="debt" and item["debt_scope"] not in ("total_debt","outstanding_principal"):
            valid_meaning = False
        if not valid_meaning:
            balance = None
        row = {"row_id":item["row_id"],"selected":True,"label":item["label"],
            "account_id":str(account["id"]) if account else None,"balance":balance,"currency":item["currency"],
            "as_of":timestamp.isoformat() if timestamp else None,"time_basis":basis,
            **(heads[str(account["id"])] if account else {"expected_account_version":None,"expected_latest_snapshot_id":None}),
            "requires_review":not confident or not valid_meaning or item["approximate"] or item["overlap_uncertain"],
            "display_unit":item["display_unit"],"exclusion_reason":None}
        rows.append(row)
    return {"rows":rows,"totals":extracted["totals"],"evidence":evidence,"acknowledge_evidence":False}


def validate(conn, actor, proposed):
    draft=json.loads(json.dumps(proposed,default=str))
    warnings=list(draft.get("evidence",[]))
    selected=[r for r in draft["rows"] if r["selected"]]
    blocked=not selected
    if not selected:
        warnings.append({"code":"NO_SELECTED_BALANCES","message":"Select at least one account balance, or cancel this capture."})
    ids=[r.get("account_id") for r in selected]
    if len(ids)!=len(set(ids)):
        blocked=True
        warnings.append({"code":"DUPLICATE_ACCOUNT","message":"Select at most one balance for each account."})
    for row in selected:
        try:
            if not row.get("account_id") or row.get("expected_account_version") is None or not row.get("time_basis"):
                fail("BALANCE_IDENTITY_UNCERTAIN","Choose a recognized account and its current version.")
            balances.validate(conn,actor.household_id,row)
            if row.get("requires_review"):
                fail("BALANCE_EVIDENCE_UNCERTAIN","Review the amount, time, account scope and debt meaning.")
        except HTTPException as exc:
            blocked=True
            error=exc.detail.get("error",{}) if isinstance(exc.detail,dict) else {}
            warnings.append({"code":error.get("code","INVALID_BALANCE"),"message":error.get("message","Correct this balance."),"row_id":row["row_id"]})
            if error.get("code")=="BALANCE_CHANGED":
                row.update(balances.head_fields(conn,actor.household_id,row["account_id"]))
    lookup={r["row_id"]:r for r in selected}
    all_row_ids={r["row_id"] for r in draft["rows"]}
    for total in draft["totals"]:
        covered=total["covered_row_ids"]
        if total["scope"]=="incomplete":
            warnings.append({"code":"TOTAL_NOT_COMPARABLE","message":"The total does not cover exactly the selected known rows."})
            continue
        try:
            if total["scope"]!="complete" or not covered or len(set(covered))!=len(covered):
                raise ValueError()
            if not set(covered)<=all_row_ids:
                raise ValueError()
            if not set(covered)<=lookup.keys():
                warnings.append({"code":"TOTAL_NOT_COMPARABLE","message":"The total includes explicitly excluded rows."})
                continue
            components=[lookup[key] for key in covered]
            if any(r["currency"]!=total["currency"] for r in components):
                raise ValueError()
            target=signed_money(total["amount"],total["currency"])
            difference=abs(sum((signed_money(r["balance"],r["currency"]) for r in components),Decimal(0))-target)
            tolerance=Decimal(0)
            if total["explicitly_rounded"]:
                units=[Decimal(total["display_unit"]),*(Decimal(r["display_unit"]) for r in components)]
                if any(not u.is_finite() or u<=0 for u in units):
                    raise ValueError()
                tolerance=sum(units)/2
            if difference>tolerance:
                raise ValueError()
            if difference:
                warnings.append({"code":"DISPLAY_ROUNDING","message":"The total agrees within explicitly displayed rounding units."})
        except (HTTPException, ValueError, ArithmeticError, TypeError):
            warnings.append({"code":"TOTAL_REQUIRES_REVIEW","message":"Review overlapping scope or a total that does not match its components."})
            blocked |= not draft.get("acknowledge_evidence",False)
    draft["warnings"]=warnings
    return draft,blocked


def finalize(conn,actor,row,proposed):
    draft,blocked=validate(conn,actor,proposed)
    if blocked:
        return receipts.response(receipts.draft(conn,actor,row,draft))
    saved=balances.save_rows(conn,actor,row["id"],[r for r in draft["rows"] if r["selected"]],source="screenshot")
    # Keep sanitized selections/exclusions for history; never store source image/model output.
    schema.update_request_draft(conn,actor.household_id,row["id"],row["row_version"],draft,actor.actor_scope)
    row=schema.get_ingestion_request(conn,actor.household_id,row["id"])
    return receipts.response(receipts.terminal(conn,actor,row,"committed",{"status":"committed","request_id":str(row["id"]),
        "snapshots":saved,"display_summary":f"Saved {len(saved)} balance observations.","warnings":draft["warnings"]}))


def process(factory,actor,payload,model):
    start=time.monotonic()
    raw,mime=decode_image(payload["image"])
    captured=payload["captured_at"]
    if captured>datetime.now(timezone.utc):
        fail("INVALID_DATE","Capture cannot be in the future.")
    digest=hashlib.sha256(raw).hexdigest()
    operation="POST /api/v1/balance-captures"
    fingerprint=compute_command_hash(operation,{"image_sha256":digest,"mime_type":mime,"captured_at":captured.astimezone(timezone.utc).isoformat(),"note":payload.get("note"),"client_version":payload.get("client_version")})
    with receipts.session(factory) as conn:
        row,new=receipts.reserve(conn,actor,payload["idempotency_key"],fingerprint,digest,captured,payload.get("client_version"),request_kind="balance_capture",operation=operation)
        if not new:
            return receipts.response(row)
        accounts,_=references(conn,actor.household_id)
        household=schema.get_household(conn,actor.household_id)
        heads={str(a["id"]):balances.head_fields(conn,actor.household_id,a["id"]) for a in accounts}
    try:
        result=model.extract_balances(raw,mime,payload.get("note"),accounts,captured)
        proposed=prepare(result,captured,household,accounts,heads)
        if time.monotonic()-start>=45:
            raise TimeoutError()
    except Exception:
        return dependency_failure(factory,actor,row["id"])
    finally:
        raw=None
    with receipts.session(factory) as conn:
        row=receipts.get(conn,actor,row["id"],lock=True)
        if row["status"]!="processing":
            return receipts.response(row)
        schema.acquire_household_finance_lock(conn,actor.household_id)
        receipts.authorize(conn,actor)
        return finalize(conn,actor,row,proposed)


def confirm(factory,actor,identity,version=None):
    with receipts.session(factory) as conn:
        row=receipts.get(conn,actor,identity,lock=True)
        if row["status"] in receipts.TERMINAL:
            return receipts.response(row)
        receipts.version(row,actor,version)
        if row["request_kind"]!="balance_capture" or row["status"]!="needs_confirmation":
            fail("INVALID_REQUEST_STATE","A balance draft is required.",409)
        schema.acquire_household_finance_lock(conn,actor.household_id)
        receipts.authorize(conn,actor)
        return finalize(conn,actor,row,row["draft_payload"])


def revise(factory,actor,identity,payload):
    with receipts.session(factory) as conn:
        row=receipts.get(conn,actor,identity,lock=True)
        receipts.version(row,actor,payload["expected_version"])
        if row["request_kind"]!="balance_capture" or row["status"]!="needs_confirmation":
            fail("INVALID_REQUEST_STATE","A balance draft is required.",409)
        schema.acquire_household_finance_lock(conn,actor.household_id)
        receipts.authorize(conn,actor)
        draft=row["draft_payload"]
        prior={r["row_id"]:r for r in draft["rows"]}
        ids=[r["row_id"] for r in payload["rows"]]
        if set(ids)!=set(prior) or len(ids)!=len(set(ids)):
            fail("INVALID_BALANCE_ROWS","Keep every row ID; explicitly deselect excluded rows.")
        draft["rows"]=[{**prior[r["row_id"]],**r,"requires_review":False} for r in payload["rows"]]
        draft["acknowledge_evidence"]=payload.get("acknowledge_evidence",False)
        draft,_=validate(conn,actor,draft)
        return receipts.response(receipts.draft(conn,actor,row,draft))
