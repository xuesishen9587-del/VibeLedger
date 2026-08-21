import json
import hashlib
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID, uuid4
from decimal import Decimal
from datetime import date, datetime, timezone

from app.domain.money import parse_decimal, validate_currency_code, quantize_money
from app.domain.transactions import (
    LedgerDomainError,
    IdempotencyKeyReuseError,
    RequestNotFoundError,
    AmbiguousAccountError,
    CategoryNotFoundError,
    AccountNotFoundError,
    AccountInactiveError,
    CategoryMismatchError,
    InvalidTransactionShapeError,
    HouseholdMismatchError
)
from app.domain.installments import calculate_installment_schedule
import app.repositories.accounts as accounts_repo
import app.repositories.transactions as tx_repo
import app.repositories.ingestion as ingestion_repo
import app.repositories.installments as installments_repo
import app.repositories.audit as audit_repo
import app.services.ledger_service as ledger_service
from app.services.reference_fx_service import ReferenceFxService
from app.services.gemini_service import GeminiService, ExpenseExtractionResult

def compute_request_hash(payload: Dict[str, Any]) -> bytes:
    """
    Computes a deterministic SHA-256 digest of normalized client request content.
    Includes idempotency_key, captured_at, client_version, image mime_type, image base64, and note.
    """
    normalized = {
        "idempotency_key": payload.get("idempotency_key", "").strip(),
        "captured_at": str(payload.get("captured_at", "")).strip(),
        "client_version": str(payload.get("client_version", "")).strip(),
        "image_mime": payload.get("image", {}).get("mime_type", "").strip().lower(),
        "image_base64": payload.get("image", {}).get("base64", "").strip(),
        "note": (payload.get("note") or "").strip()
    }
    canonical_json = json.dumps(normalized, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(canonical_json).digest()

def _resolve_account(
    candidate_name: Optional[str],
    accounts: List[Dict[str, Any]],
    aliases: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Resolves candidate string against household active accounts and aliases.
    Returns: (resolved_account_dict, warning_code).
    """
    if not candidate_name or not candidate_name.strip():
        return None, "ACCOUNT_UNRESOLVED"

    cand = candidate_name.strip().lower()
    matches = []

    # 1. Exact name match
    for acc in accounts:
        if acc["name"].strip().lower() == cand:
            matches.append(acc)

    # 2. Alias match
    if not matches:
        matched_acc_ids = set()
        for al in aliases:
            if al["normalized_alias"] == cand or al["alias_text"].strip().lower() == cand:
                matched_acc_ids.add(al["account_id"])
        for acc in accounts:
            if acc["id"] in matched_acc_ids and acc not in matches:
                matches.append(acc)

    # 3. Substring match fallback if still no matches
    if not matches:
        for acc in accounts:
            if cand in acc["name"].strip().lower() or acc["name"].strip().lower() in cand:
                matches.append(acc)

    if len(matches) == 1:
        return matches[0], None
    elif len(matches) > 1:
        return None, "MULTIPLE_ACCOUNT_CANDIDATES"
    else:
        return None, "ACCOUNT_UNRESOLVED"

def _resolve_category(
    candidate_name: Optional[str],
    categories: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Resolves candidate string against household active expense categories.
    Returns: (resolved_category_dict, warning_code).
    """
    if not candidate_name or not candidate_name.strip():
        return None, "CATEGORY_UNRESOLVED"

    cand = candidate_name.strip().lower()
    matches = []

    for cat in categories:
        if cat["category_type"] == "expense" and cat["status"] == "active":
            if cat["name"].strip().lower() == cand:
                matches.append(cat)

    if not matches:
        for cat in categories:
            if cat["category_type"] == "expense" and cat["status"] == "active":
                if cand in cat["name"].strip().lower() or cat["name"].strip().lower() in cand:
                    matches.append(cat)

    if len(matches) == 1:
        return matches[0], None
    else:
        return None, "CATEGORY_UNRESOLVED"

def process_expense_request(
    conn,
    device: Dict[str, Any],
    payload: Dict[str, Any],
    gemini_service: GeminiService,
    reference_fx_service: Optional[ReferenceFxService] = None,
    image_bytes: Optional[bytes] = None
) -> Dict[str, Any]:
    """
    Primary workflow orchestrator for POST /api/v1/expenses.
    Enforces idempotency, performs AI extraction, deterministic validation,
    and atomically commits one-off expense, foreign card expense, or installment plan.
    """
    device_id = device["device_id"]
    household_id = device["household_id"]
    user_id = device["user_id"]
    idempotency_key = payload.get("idempotency_key", "").strip()

    if not idempotency_key:
        raise InvalidTransactionShapeError("idempotency_key is required.")

    req_hash = compute_request_hash(payload)

    # 1. Check existing request for idempotency & concurrency lock
    existing = ingestion_repo.lock_by_device_and_key(conn, device_id, idempotency_key)
    if existing:
        if existing["request_hash"] != req_hash:
            raise IdempotencyKeyReuseError("This idempotency key was already used for different content.")

        if existing["status"] == "committed":
            return existing["response_payload"]
        elif existing["status"] == "needs_confirmation":
            return existing["response_payload"]
        elif existing["status"] == "rejected":
            return {"status": "rejected", "request_id": str(existing["id"])}
        elif existing["status"] in ("received", "processing"):
            if existing["response_payload"]:
                return existing["response_payload"]

    request_id = uuid4()
    captured_at = payload.get("captured_at")
    client_version = payload.get("client_version")
    note = payload.get("note")

    # Insert initial ingestion_request row if not existing
    if not existing:
        inserted = ingestion_repo.create_ingestion_request(
            conn=conn,
            request_id=request_id,
            device_id=device_id,
            idempotency_key=idempotency_key,
            request_kind="expense",
            request_hash=req_hash,
            status="processing",
            captured_at=captured_at,
            client_version=client_version
        )
        if not inserted:
            # Another concurrent request inserted this key first -> lock it
            existing = ingestion_repo.lock_by_device_and_key(conn, device_id, idempotency_key)
            if existing:
                if existing["request_hash"] != req_hash:
                    raise IdempotencyKeyReuseError("This idempotency key was already used for different content.")
                if existing["response_payload"]:
                    return existing["response_payload"]
                request_id = existing["id"]
    else:
        request_id = existing["id"]

    # 2. Fetch household active accounts and categories for extraction & validation
    accounts = accounts_repo.list_accounts_for_household(conn, household_id)
    active_accounts = [a for a in accounts if a["status"] == "active"]

    # Load aliases for active accounts
    all_aliases = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, account_id, alias_text, normalized_alias, status
            FROM account_aliases
            WHERE status = 'active' AND deleted_at IS NULL;
            """
        )
        for r in cur.fetchall():
            all_aliases.append({
                "id": r[0],
                "account_id": r[1],
                "alias_text": r[2],
                "normalized_alias": r[3],
                "status": r[4]
            })

    categories = accounts_repo.list_categories_for_household(conn, household_id)
    active_expense_categories = [c for c in categories if c["category_type"] == "expense" and c["status"] == "active"]

    # 3. AI Expense-only extraction
    img_data = image_bytes
    if not img_data and payload.get("image", {}).get("base64"):
        import base64
        img_data = base64.b64decode(payload["image"]["base64"])
    mime_type = payload.get("image", {}).get("mime_type", "image/jpeg")

    extracted: ExpenseExtractionResult = gemini_service.extract_expense(
        image_bytes=img_data or b"",
        mime_type=mime_type,
        note=note,
        accounts=active_accounts,
        categories=active_expense_categories,
        captured_at=captured_at
    )

    # 4. Deterministic Validation & Resolution
    occurred_on = extracted.occurred_on or (datetime.now(timezone.utc).date())
    merchant = extracted.merchant.strip() if extracted.merchant else None
    confidence = extracted.confidence

    resolved_acc, acc_warn = _resolve_account(extracted.from_account, active_accounts, all_aliases)
    resolved_cat, cat_warn = _resolve_category(extracted.category, active_expense_categories)

    warnings: List[Dict[str, str]] = []
    if acc_warn:
        warnings.append({
            "code": acc_warn,
            "message": "支付账户识别置信度较低或未匹配到唯一账户。" if acc_warn == "LOW_ACCOUNT_CONFIDENCE" else "未能识别支付账户，请手动选择。"
        })
    if cat_warn:
        warnings.append({
            "code": cat_warn,
            "message": "未能明确支出分类，请手动确认。"
        })

    # Validate amount
    amt = extracted.total_amount if extracted.payment_mode == "installment" else extracted.original_amount
    curr = extracted.original_currency or "CNY"
    try:
        if amt is None:
            raise ValueError("Amount is missing.")
        dec_amt = parse_decimal(amt)
        curr = validate_currency_code(curr)
        quantized_amt = quantize_money(dec_amt, curr)
        if quantized_amt <= 0:
            raise ValueError("Amount must be positive.")
    except Exception:
        quantized_amt = None
        warnings.append({
            "code": "AMOUNT_UNCLEAR",
            "message": "未能识别有效金额或币种。"
        })

    # Check overall confidence threshold (force confirmation if low confidence or critical unresolved fields)
    needs_confirm = (
        len(warnings) > 0
        or resolved_acc is None
        or resolved_cat is None
        or quantized_amt is None
        or confidence < 0.85
    )

    # 5. Handle Branch D: Needs Confirmation
    if needs_confirm:
        draft_payload = {
            "occurred_on": str(occurred_on),
            "merchant": merchant,
            "original_amount": str(quantized_amt) if quantized_amt else (str(amt) if amt else None),
            "original_currency": curr,
            "from_account": {
                "id": str(resolved_acc["id"]),
                "name": resolved_acc["name"]
            } if resolved_acc else None,
            "category": {
                "id": str(resolved_cat["id"]),
                "name": resolved_cat["name"]
            } if resolved_cat else None,
            "payment_mode": extracted.payment_mode,
            "total_periods": extracted.total_periods if extracted.payment_mode == "installment" else None
        }

        acc_name_disp = resolved_acc["name"] if resolved_acc else "未知账户"
        cat_name_disp = resolved_cat["name"] if resolved_cat else "未分类"
        amt_disp = f"{quantized_amt} {curr}" if quantized_amt else "金额待定"
        display_summary = f"⚠️ 请确认\n{amt_disp} · {merchant or '未知商户'}\n{acc_name_disp} · {cat_name_disp}"

        response_payload = {
            "status": "needs_confirmation",
            "request_id": str(request_id),
            "draft": draft_payload,
            "warnings": warnings,
            "display_summary": display_summary
        }

        ingestion_repo.update_ingestion_request_status(
            conn=conn,
            request_id=request_id,
            status="needs_confirmation",
            response_payload=response_payload,
            draft_payload=draft_payload
        )
        return response_payload

    # --- High Confidence & Fully Validated Paths ---

    # 6. Branch C: Installment Plan Capture
    if extracted.payment_mode == "installment":
        total_periods = extracted.total_periods or 12
        if total_periods < 2 or total_periods > 120:
            total_periods = 12

        plan_id = uuid4()
        schedules = calculate_installment_schedule(quantized_amt, curr, total_periods)

        # Insert installment plan
        plan = installments_repo.create_installment_plan(
            conn=conn,
            plan_id=plan_id,
            household_id=household_id,
            credit_account_id=resolved_acc["id"],
            purchase_occurred_on=occurred_on,
            original_amount=quantized_amt,
            original_currency=curr,
            account_currency=resolved_acc["currency"],
            total_periods=total_periods,
            merchant=merchant,
            status="pending_first_bill",
            source_request_id=request_id
        )

        # Insert schedule periods
        for idx, period_amt in enumerate(schedules, start=1):
            installments_repo.create_installment_period(
                conn=conn,
                period_id=uuid4(),
                plan_id=plan_id,
                period_no=idx,
                scheduled_amount=period_amt,
                currency=curr,
                status="scheduled"
            )

        # Audit event for plan creation
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device",
            entity_type="installment_plan",
            entity_id=plan_id,
            action="create",
            actor_user_id=user_id,
            actor_device_id=device_id,
            request_id=request_id,
            after_data={
                "total_amount": str(quantized_amt),
                "currency": curr,
                "total_periods": total_periods,
                "credit_account_id": str(resolved_acc["id"]),
                "merchant": merchant
            }
        )

        curr_symbol = "¥" if curr == "CNY" else f"{curr} "
        display_summary = (
            f"{curr_symbol}{quantized_amt} · {merchant or '分期消费'} "
            f"({total_periods}期分期计划已建立，待首期账单确认)\n"
            f"{resolved_acc['name']}\n{occurred_on}"
        )

        response_payload = {
            "status": "committed",
            "request_id": str(request_id),
            "installment_plan_id": str(plan_id),
            "plan_status": "pending_first_bill",
            "payment_mode": "installment",
            "total_amount": str(quantized_amt),
            "currency": curr,
            "total_periods": total_periods,
            "merchant": merchant,
            "display_summary": display_summary
        }

        ingestion_repo.update_ingestion_request_status(
            conn=conn,
            request_id=request_id,
            status="committed",
            response_payload=response_payload,
            committed_at=datetime.now(timezone.utc)
        )
        return response_payload

    # 7. Branch A & B: One-off Expense (Same Currency vs Foreign Credit Card)
    is_foreign_card = (resolved_acc["currency"] != curr)

    if is_foreign_card:
        # Foreign currency card expense estimation rule
        if resolved_acc["account_type"] != "credit":
            # Non-credit card foreign expense cannot estimate leg on Shortcut
            raise LedgerDomainError(
                f"Foreign currency expense on non-credit account {resolved_acc['name']} is not supported for auto-estimation.",
                code="CURRENCY_MISMATCH"
            )

        fx_service = reference_fx_service or ReferenceFxService()
        est_from_amount, fx_rate = fx_service.estimate_settlement(
            original_amount=quantized_amt,
            original_currency=curr,
            account_currency=resolved_acc["currency"],
            as_of=occurred_on
        )

        # Record expense with estimated settlement leg
        tx = ledger_service.record_expense(
            conn=conn,
            household_id=household_id,
            from_account_id=resolved_acc["id"],
            amount=est_from_amount,
            currency=resolved_acc["currency"],
            category_id=resolved_cat["id"],
            occurred_on=occurred_on,
            merchant=merchant,
            remarks=note,
            source="shortcut",
            created_by_user_id=user_id,
            created_by_device_id=device_id,
            source_request_id=request_id,
            account_leg_status="estimated",
            original_amount=quantized_amt,
            original_currency=curr,
            effective_fx_rate=fx_rate
        )

        card_sym = "$" if resolved_acc["currency"] == "USD" else f"{resolved_acc['currency']} "
        display_summary = (
            f"{quantized_amt} {curr} (est. {card_sym}{est_from_amount}) · {merchant or '消费'}\n"
            f"{resolved_acc['name']} · {resolved_cat['name']}\n{occurred_on}"
        )

        response_payload = {
            "status": "committed",
            "request_id": str(request_id),
            "transaction_id": str(tx["id"]),
            "payment_mode": "one_off",
            "original_amount": str(quantized_amt),
            "original_currency": curr,
            "from_amount": str(est_from_amount),
            "from_currency": resolved_acc["currency"],
            "account_leg_status": "estimated",
            "display_summary": display_summary
        }
    else:
        # Standard one-off expense (same currency)
        tx = ledger_service.record_expense(
            conn=conn,
            household_id=household_id,
            from_account_id=resolved_acc["id"],
            amount=quantized_amt,
            currency=curr,
            category_id=resolved_cat["id"],
            occurred_on=occurred_on,
            merchant=merchant,
            remarks=note,
            source="shortcut",
            created_by_user_id=user_id,
            created_by_device_id=device_id,
            source_request_id=request_id,
            account_leg_status="authoritative"
        )

        curr_symbol = "¥" if curr == "CNY" else f"{curr} "
        display_summary = (
            f"{curr_symbol}{quantized_amt} · {merchant or '消费'}\n"
            f"{resolved_acc['name']} · {resolved_cat['name']}\n{occurred_on}"
        )

        response_payload = {
            "status": "committed",
            "request_id": str(request_id),
            "transaction_id": str(tx["id"]),
            "payment_mode": "one_off",
            "display_summary": display_summary
        }

    ingestion_repo.update_ingestion_request_status(
        conn=conn,
        request_id=request_id,
        status="committed",
        response_payload=response_payload,
        committed_at=datetime.now(timezone.utc)
    )

    return response_payload

def get_by_idempotency_key(
    conn,
    device_id: UUID,
    idempotency_key: str
) -> Dict[str, Any]:
    """
    Resolves client idempotency key inside the authenticated device scope.
    """
    row = ingestion_repo.get_by_device_and_key(conn, device_id, idempotency_key)
    if not row:
        raise RequestNotFoundError("The request was not received by the server.")

    if row["response_payload"]:
        return row["response_payload"]

    return {
        "status": row["status"],
        "request_id": str(row["id"]),
        "draft": row["draft_payload"],
        "display_summary": f"Request status: {row['status']}"
    }

def confirm_ingestion_request(
    conn,
    request_id: UUID,
    device: Dict[str, Any],
    reference_fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Confirms a needs_confirmation draft request.
    Re-validates draft against current DB accounts/categories and commits atomically.
    """
    device_id = device["device_id"]
    household_id = device["household_id"]
    user_id = device["user_id"]

    row = ingestion_repo.lock_ingestion_request(conn, request_id)
    if not row:
        raise RequestNotFoundError(f"Ingestion request {request_id} not found.")

    if row["device_id"] != device_id:
        raise HouseholdMismatchError("Ingestion request does not belong to the authenticated device.")

    # Idempotent replay if already committed
    if row["status"] == "committed":
        return row["response_payload"]

    if row["status"] != "needs_confirmation":
        raise LedgerDomainError(f"Cannot confirm request in status {row['status']}.", code="INVALID_REQUEST_STATE")

    draft = row["draft_payload"] or {}
    occurred_on_str = draft.get("occurred_on")
    occurred_on = date.fromisoformat(occurred_on_str) if occurred_on_str else datetime.now(timezone.utc).date()
    merchant = draft.get("merchant")
    original_amount_str = draft.get("original_amount")
    currency = draft.get("original_currency", "CNY")

    if not original_amount_str:
        raise InvalidTransactionShapeError("Draft is missing valid amount.")
    curr = validate_currency_code(currency)
    dec_amount = quantize_money(parse_decimal(original_amount_str), curr)

    # Re-validate account
    acc_info = draft.get("from_account") or {}
    acc_id = UUID(acc_info["id"]) if isinstance(acc_info.get("id"), str) else acc_info.get("id")
    if not acc_id:
        raise AccountNotFoundError("Account is not resolved.")
    account = accounts_repo.get_account(conn, acc_id)
    if not account:
        raise AccountNotFoundError(acc_id)
    if account["household_id"] != household_id:
        raise HouseholdMismatchError(f"Account {acc_id} does not belong to household.")
    if account["status"] != "active":
        raise AccountInactiveError(acc_id)

    # Branch C: Installment
    payment_mode = draft.get("payment_mode", "one_off")
    if payment_mode == "installment":
        total_periods = int(draft.get("total_periods") or 12)
        plan_id = uuid4()
        schedules = calculate_installment_schedule(dec_amount, curr, total_periods)

        plan = installments_repo.create_installment_plan(
            conn=conn,
            plan_id=plan_id,
            household_id=household_id,
            credit_account_id=acc_id,
            purchase_occurred_on=occurred_on,
            original_amount=dec_amount,
            original_currency=curr,
            account_currency=account["currency"],
            total_periods=total_periods,
            merchant=merchant,
            status="pending_first_bill",
            source_request_id=request_id
        )

        for idx, period_amt in enumerate(schedules, start=1):
            installments_repo.create_installment_period(
                conn=conn,
                period_id=uuid4(),
                plan_id=plan_id,
                period_no=idx,
                scheduled_amount=period_amt,
                currency=curr,
                status="scheduled"
            )

        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device",
            entity_type="installment_plan",
            entity_id=plan_id,
            action="create",
            actor_user_id=user_id,
            actor_device_id=device_id,
            request_id=request_id,
            after_data={
                "total_amount": str(dec_amount),
                "currency": curr,
                "total_periods": total_periods,
                "credit_account_id": str(acc_id),
                "merchant": merchant
            }
        )

        curr_symbol = "¥" if curr == "CNY" else f"{curr} "
        display_summary = (
            f"{curr_symbol}{dec_amount} · {merchant or '分期消费'} "
            f"({total_periods}期分期计划已建立，待首期账单确认)\n"
            f"{account['name']}\n{occurred_on}"
        )

        response_payload = {
            "status": "committed",
            "request_id": str(request_id),
            "installment_plan_id": str(plan_id),
            "plan_status": "pending_first_bill",
            "payment_mode": "installment",
            "total_amount": str(dec_amount),
            "currency": curr,
            "total_periods": total_periods,
            "merchant": merchant,
            "display_summary": display_summary
        }

        ingestion_repo.update_ingestion_request_status(
            conn=conn,
            request_id=request_id,
            status="committed",
            response_payload=response_payload,
            committed_at=datetime.now(timezone.utc)
        )
        return response_payload

    # Re-validate category for one-off expense
    cat_info = draft.get("category") or {}
    cat_id = UUID(cat_info["id"]) if isinstance(cat_info.get("id"), str) else cat_info.get("id")
    if not cat_id:
        raise CategoryNotFoundError("Category is not resolved.")
    category = accounts_repo.get_category(conn, cat_id)
    if not category:
        raise CategoryNotFoundError(cat_id)
    if category["household_id"] != household_id:
        raise HouseholdMismatchError(f"Category {cat_id} does not belong to household.")
    if category["category_type"] != "expense":
        raise CategoryMismatchError(f"Category {cat_id} is not an expense category.")
    if category["status"] != "active":
        raise CategoryMismatchError(f"Category {cat_id} is inactive.")

    is_foreign_card = (account["currency"] != curr)
    if is_foreign_card:
        if account["account_type"] != "credit":
            raise LedgerDomainError(
                f"Foreign currency expense on non-credit account {account['name']} is not supported.",
                code="CURRENCY_MISMATCH"
            )
        fx_service = reference_fx_service or ReferenceFxService()
        est_from_amount, fx_rate = fx_service.estimate_settlement(
            original_amount=dec_amount,
            original_currency=curr,
            account_currency=account["currency"],
            as_of=occurred_on
        )

        tx = ledger_service.record_expense(
            conn=conn,
            household_id=household_id,
            from_account_id=acc_id,
            amount=est_from_amount,
            currency=account["currency"],
            category_id=cat_id,
            occurred_on=occurred_on,
            merchant=merchant,
            remarks=draft.get("remarks"),
            source="shortcut",
            created_by_user_id=user_id,
            created_by_device_id=device_id,
            source_request_id=request_id,
            account_leg_status="estimated",
            original_amount=dec_amount,
            original_currency=curr,
            effective_fx_rate=fx_rate
        )

        card_sym = "$" if account["currency"] == "USD" else f"{account['currency']} "
        display_summary = (
            f"{dec_amount} {curr} (est. {card_sym}{est_from_amount}) · {merchant or '消费'}\n"
            f"{account['name']} · {category['name']}\n{occurred_on}"
        )

        response_payload = {
            "status": "committed",
            "request_id": str(request_id),
            "transaction_id": str(tx["id"]),
            "payment_mode": "one_off",
            "original_amount": str(dec_amount),
            "original_currency": curr,
            "from_amount": str(est_from_amount),
            "from_currency": account["currency"],
            "account_leg_status": "estimated",
            "display_summary": display_summary
        }
    else:
        tx = ledger_service.record_expense(
            conn=conn,
            household_id=household_id,
            from_account_id=acc_id,
            amount=dec_amount,
            currency=curr,
            category_id=cat_id,
            occurred_on=occurred_on,
            merchant=merchant,
            remarks=draft.get("remarks"),
            source="shortcut",
            created_by_user_id=user_id,
            created_by_device_id=device_id,
            source_request_id=request_id,
            account_leg_status="authoritative"
        )

        curr_symbol = "¥" if curr == "CNY" else f"{curr} "
        display_summary = (
            f"{curr_symbol}{dec_amount} · {merchant or '消费'}\n"
            f"{account['name']} · {category['name']}\n{occurred_on}"
        )

        response_payload = {
            "status": "committed",
            "request_id": str(request_id),
            "transaction_id": str(tx["id"]),
            "payment_mode": "one_off",
            "display_summary": display_summary
        }

    ingestion_repo.update_ingestion_request_status(
        conn=conn,
        request_id=request_id,
        status="committed",
        response_payload=response_payload,
        committed_at=datetime.now(timezone.utc)
    )
    return response_payload

def revise_ingestion_request(
    conn,
    request_id: UUID,
    device: Dict[str, Any],
    correction_note: Optional[str] = None,
    structured_fields: Optional[Dict[str, Any]] = None,
    gemini_service: Optional[GeminiService] = None,
    reference_fx_service: Optional[ReferenceFxService] = None
) -> Dict[str, Any]:
    """
    Revises a pending draft request using natural-language correction note or structured edits.
    Maintains the exact same request_id and idempotency key identity.
    """
    device_id = device["device_id"]
    household_id = device["household_id"]

    row = ingestion_repo.lock_ingestion_request(conn, request_id)
    if not row:
        raise RequestNotFoundError(f"Ingestion request {request_id} not found.")

    if row["device_id"] != device_id:
        raise HouseholdMismatchError("Ingestion request does not belong to the authenticated device.")

    if row["status"] == "committed":
        return row["response_payload"]

    draft = dict(row["draft_payload"] or {})
    accounts = accounts_repo.list_accounts_for_household(conn, household_id)
    active_accounts = [a for a in accounts if a["status"] == "active"]
    categories = accounts_repo.list_categories_for_household(conn, household_id)
    active_expense_categories = [c for c in categories if c["category_type"] == "expense" and c["status"] == "active"]

    if structured_fields:
        if "merchant" in structured_fields:
            draft["merchant"] = structured_fields["merchant"]
        if "occurred_on" in structured_fields:
            draft["occurred_on"] = structured_fields["occurred_on"]
        if "original_amount" in structured_fields:
            draft["original_amount"] = structured_fields["original_amount"]
        if "original_currency" in structured_fields:
            draft["original_currency"] = structured_fields["original_currency"]
        if "from_account_id" in structured_fields:
            target_acc_id = UUID(structured_fields["from_account_id"]) if isinstance(structured_fields["from_account_id"], str) else structured_fields["from_account_id"]
            acc = next((a for a in active_accounts if a["id"] == target_acc_id), None)
            if acc:
                draft["from_account"] = {"id": str(acc["id"]), "name": acc["name"]}
        if "category_id" in structured_fields:
            target_cat_id = UUID(structured_fields["category_id"]) if isinstance(structured_fields["category_id"], str) else structured_fields["category_id"]
            cat = next((c for c in active_expense_categories if c["id"] == target_cat_id), None)
            if cat:
                draft["category"] = {"id": str(cat["id"]), "name": cat["name"]}

    if correction_note:
        note_lower = correction_note.lower()
        # Look for account mentions in correction note
        for acc in active_accounts:
            if acc["name"].lower() in note_lower:
                draft["from_account"] = {"id": str(acc["id"]), "name": acc["name"]}
                break
        # Look for category mentions
        for cat in active_expense_categories:
            if cat["name"].lower() in note_lower:
                draft["category"] = {"id": str(cat["id"]), "name": cat["name"]}
                break

    # Save revised draft
    acc_name_disp = draft.get("from_account", {}).get("name") if draft.get("from_account") else "未知账户"
    cat_name_disp = draft.get("category", {}).get("name") if draft.get("category") else "未分类"
    amt_disp = f"{draft.get('original_amount')} {draft.get('original_currency', 'CNY')}"
    display_summary = f"⚠️ 请确认 (已修订)\n{amt_disp} · {draft.get('merchant') or '未知商户'}\n{acc_name_disp} · {cat_name_disp}"

    response_payload = {
        "status": "needs_confirmation",
        "request_id": str(request_id),
        "draft": draft,
        "warnings": [],
        "display_summary": display_summary
    }

    ingestion_repo.update_ingestion_request_status(
        conn=conn,
        request_id=request_id,
        status="needs_confirmation",
        response_payload=response_payload,
        draft_payload=draft
    )
    return response_payload

def reject_ingestion_request(
    conn,
    request_id: UUID,
    device: Dict[str, Any],
    reason: Optional[str] = None
) -> Dict[str, Any]:
    """
    Rejects a pending or confirmable ingestion request.
    Rejection produces no financial transactions, no balance mutations, and no installment plans.
    """
    device_id = device["device_id"]
    row = ingestion_repo.lock_ingestion_request(conn, request_id)
    if not row:
        raise RequestNotFoundError(f"Ingestion request {request_id} not found.")

    if row["device_id"] != device_id:
        raise HouseholdMismatchError("Ingestion request does not belong to the authenticated device.")

    if row["status"] == "committed":
        raise LedgerDomainError("Cannot reject an already committed transaction.", code="CANNOT_REJECT_COMMITTED")

    ingestion_repo.update_ingestion_request_status(
        conn=conn,
        request_id=request_id,
        status="rejected",
        failure_code=reason or "User rejected draft"
    )

    return {
        "status": "rejected",
        "request_id": str(request_id)
    }
