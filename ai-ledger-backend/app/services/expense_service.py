import io
import json
import base64
import hashlib
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID, uuid4
from datetime import datetime, date, timezone
from decimal import Decimal
from PIL import Image

from app.domain.money import parse_decimal, validate_currency_code, quantize_money
from app.domain.installments import calculate_installment_schedule
from app.domain.transactions import (
    LedgerDomainError,
    AccountNotFoundError,
    AccountInactiveError,
    CategoryNotFoundError,
    CurrencyMismatchError,
    InvalidTransactionShapeError,
    InvalidImagePayloadError,
    IdempotencyKeyReuseError,
    RequestNotFoundError,
    HouseholdMismatchError,
    InvalidRequestStateError,
    InvalidPaymentModeError,
    InvalidInstallmentPeriodsError
)
from app.config import get_settings
import app.repositories.ingestion as ingestion_repo
import app.repositories.accounts as accounts_repo
import app.repositories.installments as installments_repo
import app.repositories.audit as audit_repo
import app.services.ledger_service as ledger_service
from app.services.gemini_service import GeminiService, ExpenseExtractionResult
from app.services.reference_fx_service import ReferenceFxService

FIELD_CONFIDENCE_THRESHOLD = 0.85

def validate_image_payload(image_payload: Optional[Dict[str, Any]], max_bytes: Optional[int] = None) -> Tuple[bytes, str]:
    """
    Validates base64 image input, decoded payload size, MIME type, magic bytes, and actual decoder verify.
    Raises InvalidImagePayloadError on any validation failure before calling AI.
    """
    settings = get_settings()
    limit = max_bytes or settings.MAX_EXPENSE_IMAGE_BYTES

    if not image_payload or not isinstance(image_payload, dict):
        raise InvalidImagePayloadError("Image object is required.")

    b64_str = image_payload.get("base64")
    if not b64_str or not isinstance(b64_str, str) or not b64_str.strip():
        raise InvalidImagePayloadError("Base64 image data is empty or missing.")

    try:
        image_bytes = base64.b64decode(b64_str, validate=True)
    except Exception as e:
        raise InvalidImagePayloadError(f"Malformed base64 image data: {e}")

    if len(image_bytes) == 0:
        raise InvalidImagePayloadError("Decoded image data is empty.")

    if len(image_bytes) > limit:
        raise InvalidImagePayloadError(f"Decoded image size {len(image_bytes)} exceeds maximum limit of {limit} bytes.")

    mime_type = (image_payload.get("mime_type") or "").strip().lower()
    if mime_type not in ("image/jpeg", "image/png"):
        raise InvalidImagePayloadError(f"Unsupported image MIME type: '{mime_type}'. Supported formats are image/jpeg and image/png.")

    # 1. Verify magic bytes format
    if mime_type == "image/jpeg":
        if not image_bytes.startswith(b"\xff\xd8\xff"):
            raise InvalidImagePayloadError("Image data does not match declared JPEG format.")
    elif mime_type == "image/png":
        if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise InvalidImagePayloadError("Image data does not match declared PNG format.")

    # 2. Real image decode & verify using Pillow (catches corrupted payloads with valid headers)
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.verify()
            expected_format = "JPEG" if mime_type == "image/jpeg" else "PNG"
            if img.format != expected_format:
                raise InvalidImagePayloadError(f"Decoded image format '{img.format}' does not match declared '{expected_format}'.")
    except InvalidImagePayloadError:
        raise
    except Exception as e:
        raise InvalidImagePayloadError(f"Corrupted or unparseable image payload: {e}")

    return image_bytes, mime_type

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
    Resolves candidate string against active expense categories.
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
    Enforces idempotency, performs image validation, AI extraction, deterministic validation,
    and atomically commits one-off expense, foreign card expense, or installment plan.
    """
    device_id = device["device_id"]
    household_id = device["household_id"]
    user_id = device["user_id"]
    idempotency_key = payload.get("idempotency_key", "").strip()

    if not idempotency_key:
        raise InvalidTransactionShapeError("idempotency_key is required.")

    # 1. Validate image BEFORE calling Gemini or inserting rows
    if image_bytes is None:
        img_payload = payload.get("image")
        img_data, mime_type = validate_image_payload(img_payload)
    else:
        img_data = image_bytes
        mime_type = payload.get("image", {}).get("mime_type", "image/jpeg")

    # 2. Check and parse captured_at (must be timezone-aware)
    captured_at = payload.get("captured_at")
    if captured_at is None:
        raise InvalidTransactionShapeError("captured_at is required.")
    if isinstance(captured_at, str):
        try:
            captured_at = datetime.fromisoformat(captured_at)
        except Exception:
            raise InvalidTransactionShapeError("captured_at must be an ISO 8601 string with timezone.")
    if not isinstance(captured_at, datetime) or captured_at.tzinfo is None:
        raise InvalidTransactionShapeError("captured_at must be a timezone-aware datetime.")

    req_hash = compute_request_hash(payload)

    # 3. Check existing request for idempotency & concurrency lock
    existing = ingestion_repo.lock_by_device_and_key(conn, device_id, idempotency_key)
    if existing:
        if existing["request_hash"] != req_hash:
            raise IdempotencyKeyReuseError("This idempotency key was already used for different content.")

        if existing["status"] == "committed":
            return existing["response_payload"]
        elif existing["status"] == "needs_confirmation":
            return existing["response_payload"]
        elif existing["status"] == "rejected":
            return existing["response_payload"]
        elif existing["status"] == "failed":
            return existing["response_payload"]
        elif existing["status"] in ("received", "processing"):
            request_id = existing["id"]
    else:
        request_id = uuid4()
        inserted = ingestion_repo.create_ingestion_request(
            conn=conn,
            request_id=request_id,
            device_id=device_id,
            idempotency_key=idempotency_key,
            request_kind="expense",
            request_hash=req_hash,
            status="processing",
            captured_at=captured_at,
            client_version=payload.get("client_version")
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
    if not existing:
        pass

    # 4. Fetch household active accounts, aliases, and categories for extraction & validation
    accounts = accounts_repo.list_accounts(conn, household_id)
    active_accounts = [a for a in accounts if a["status"] == "active"]

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

    # Enrich active accounts with their active aliases for Gemini prompt (household-scoped)
    for a in active_accounts:
        a["aliases"] = [al["alias_text"] for al in all_aliases if al["account_id"] == a["id"]]

    categories = accounts_repo.list_categories(conn, household_id)
    active_expense_categories = [c for c in categories if c["category_type"] == "expense" and c["status"] == "active"]

    # 5. AI Expense-only extraction
    extracted: ExpenseExtractionResult = gemini_service.extract_expense(
        image_bytes=img_data,
        mime_type=mime_type,
        note=payload.get("note"),
        accounts=active_accounts,
        categories=active_expense_categories,
        captured_at=captured_at
    )

    # 6. Field-level confidence decision model: missing confidence defaults conservatively to 0.0
    field_conf = extracted.field_confidence or {}
    date_conf = field_conf.get("date", 0.0)
    if extracted.occurred_on is not None and date_conf >= FIELD_CONFIDENCE_THRESHOLD:
        occurred_on = extracted.occurred_on
    else:
        occurred_on = captured_at.date()

    merchant = extracted.merchant.strip() if extracted.merchant else None

    # Validate payment_mode (MUST NOT default to one_off)
    raw_pm = extracted.payment_mode
    if not raw_pm or not isinstance(raw_pm, str) or raw_pm.strip().lower() not in ("one_off", "installment"):
        pm = "unknown"
    else:
        pm = raw_pm.strip().lower()

    # Deterministic account and category resolution
    resolved_acc, acc_warn = _resolve_account(extracted.from_account, active_accounts, all_aliases)
    resolved_cat, cat_warn = _resolve_category(extracted.category, active_expense_categories)

    warnings: List[Dict[str, str]] = []

    # Payment mode validation
    if pm not in ("one_off", "installment"):
        warnings.append({
            "code": "INVALID_PAYMENT_MODE",
            "message": f"未识别或缺失支付模式: '{raw_pm}'，请手动确认。"
        })

    # Account confidence & resolution
    acc_conf = field_conf.get("account", 0.0)
    if acc_warn:
        warnings.append({
            "code": acc_warn,
            "message": "支付账户识别置信度较低或未匹配到唯一账户。" if acc_warn == "LOW_ACCOUNT_CONFIDENCE" else "未能识别支付账户，请手动选择。"
        })
    elif acc_conf < FIELD_CONFIDENCE_THRESHOLD:
        warnings.append({
            "code": "LOW_ACCOUNT_CONFIDENCE",
            "message": "支付账户识别置信度不足，请手动确认。"
        })

    # Currency validation (NO SILENT CNY DEFAULT)
    raw_curr = extracted.original_currency
    curr_conf = field_conf.get("currency", 0.0)
    quantized_amt = None
    curr = None

    if not raw_curr or not isinstance(raw_curr, str) or not raw_curr.strip():
        warnings.append({
            "code": "CURRENCY_UNCLEAR",
            "message": "未能识别有效币种，请手动确认。"
        })
    else:
        try:
            curr = validate_currency_code(raw_curr.strip())
            if curr_conf < FIELD_CONFIDENCE_THRESHOLD:
                warnings.append({
                    "code": "LOW_CURRENCY_CONFIDENCE",
                    "message": "币种识别置信度较低，请手动确认。"
                })
        except Exception:
            curr = None
            warnings.append({
                "code": "CURRENCY_UNCLEAR",
                "message": f"无效的币种代码: '{raw_curr}'。"
            })

    # Amount validation
    amt = extracted.total_amount if pm == "installment" else extracted.original_amount
    amt_conf = field_conf.get("amount", 0.0)
    if amt is None:
        warnings.append({
            "code": "AMOUNT_UNCLEAR",
            "message": "未能识别消费金额。"
        })
    elif amt_conf < FIELD_CONFIDENCE_THRESHOLD:
        warnings.append({
            "code": "LOW_AMOUNT_CONFIDENCE",
            "message": "金额识别置信度较低，请手动确认。"
        })
    else:
        try:
            dec_amt = parse_decimal(amt)
            if dec_amt <= 0:
                raise ValueError("Amount must be positive.")
            if curr:
                quantized_amt = quantize_money(dec_amt, curr)
        except Exception:
            warnings.append({
                "code": "AMOUNT_UNCLEAR",
                "message": "消费金额格式无效或必须为正数。"
            })

    # Category validation (Category is optional for installments)
    cat_conf = field_conf.get("category", 0.0)
    if pm != "installment":
        if cat_warn:
            warnings.append({
                "code": cat_warn,
                "message": "未能识别消费分类，请手动选择。"
            })
        elif cat_conf < FIELD_CONFIDENCE_THRESHOLD:
            warnings.append({
                "code": "LOW_CATEGORY_CONFIDENCE",
                "message": "消费分类识别置信度较低，请手动确认。"
            })

    # Branch C: Installment validation
    total_periods = None
    if pm == "installment":
        if resolved_acc and resolved_acc["account_type"] != "credit":
            warnings.append({
                "code": "NON_CREDIT_INSTALLMENT_ACCOUNT",
                "message": f"分期计划仅支持信用卡账户，'{resolved_acc['name']}' 不是信用卡账户。"
            })

        periods_conf = field_conf.get("total_periods", 0.0)
        raw_periods = extracted.total_periods
        if raw_periods is None or not isinstance(raw_periods, int) or raw_periods < 2 or raw_periods > 120 or periods_conf < FIELD_CONFIDENCE_THRESHOLD:
            warnings.append({
                "code": "INVALID_INSTALLMENT_PERIODS",
                "message": "分期期数缺失或无效（支持 2-120 期），请手动确认。"
            })
        else:
            total_periods = raw_periods

    # Note: Merchant novelty alone NEVER forces confirmation.

    # 7. Check if request branches to Branch D: needs_confirmation
    if warnings or quantized_amt is None or curr is None or resolved_acc is None or (pm != "installment" and resolved_cat is None) or pm not in ("one_off", "installment"):
        draft_payload = {
            "occurred_on": occurred_on.isoformat() if occurred_on else None,
            "merchant": merchant,
            "original_amount": str(quantized_amt) if quantized_amt is not None else (str(amt) if amt is not None else None),
            "original_currency": curr or raw_curr,
            "from_account": {"id": str(resolved_acc["id"]), "name": resolved_acc["name"]} if resolved_acc else None,
            "category": {"id": str(resolved_cat["id"]), "name": resolved_cat["name"]} if resolved_cat else None,
            "payment_mode": raw_pm,  # Keep extracted/raw payment mode, DO NOT rewrite to one_off
            "total_periods": total_periods or extracted.total_periods,
            "remarks": payload.get("note")
        }

        acc_name_disp = resolved_acc["name"] if resolved_acc else "未知账户"
        cat_name_disp = resolved_cat["name"] if resolved_cat else "未分类"
        amt_disp = f"{draft_payload['original_amount']} {draft_payload['original_currency'] or ''}".strip()
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

    # 8. Automated Execution: Branch C (Installment) vs Branch B (Foreign Credit Card) vs Branch A (One-off)
    if pm == "installment":
        plan_id = uuid4()
        schedules = calculate_installment_schedule(quantized_amt, curr, total_periods)

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

    # Branch B: Foreign Credit Card Expense
    is_foreign_card = (resolved_acc["account_type"] == "credit" and curr != resolved_acc["currency"])
    if is_foreign_card:
        fx_service = reference_fx_service or ReferenceFxService()
        est_from_amount, fx_rate = fx_service.estimate_settlement(
            original_amount=quantized_amt,
            original_currency=curr,
            account_currency=resolved_acc["currency"],
            as_of=occurred_on
        )

        tx = ledger_service.record_expense(
            conn=conn,
            household_id=household_id,
            from_account_id=resolved_acc["id"],
            amount=est_from_amount,
            currency=resolved_acc["currency"],
            category_id=resolved_cat["id"],
            occurred_on=occurred_on,
            merchant=merchant,
            remarks=payload.get("note"),
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
        # Branch A: Standard One-Off Expense
        if curr != resolved_acc["currency"]:
            raise CurrencyMismatchError(
                f"Expense currency {curr} does not match debit account {resolved_acc['name']} ({resolved_acc['currency']})."
            )

        tx = ledger_service.record_expense(
            conn=conn,
            household_id=household_id,
            from_account_id=resolved_acc["id"],
            amount=quantized_amt,
            currency=curr,
            category_id=resolved_cat["id"],
            occurred_on=occurred_on,
            merchant=merchant,
            remarks=payload.get("note"),
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
        if row["response_payload"]:
            return row["response_payload"]
        return {"status": "committed", "request_id": str(request_id)}

    if row["status"] != "needs_confirmation":
        raise InvalidRequestStateError(f"Cannot confirm request in status '{row['status']}'.")

    draft = row["draft_payload"] or {}
    occurred_on_str = draft.get("occurred_on")
    occurred_on = date.fromisoformat(occurred_on_str) if occurred_on_str else datetime.now(timezone.utc).date()
    merchant = draft.get("merchant")
    original_amount_str = draft.get("original_amount")
    currency = draft.get("original_currency")

    if not original_amount_str:
        raise InvalidTransactionShapeError("Draft is missing valid amount.")
    if not currency:
        raise InvalidTransactionShapeError("Draft is missing valid currency.")

    curr = validate_currency_code(currency)
    dec_amount = quantize_money(parse_decimal(original_amount_str), curr)

    # Re-validate account against current DB state
    acc_info = draft.get("from_account") or {}
    acc_id = UUID(acc_info["id"]) if isinstance(acc_info.get("id"), str) else acc_info.get("id")
    if not acc_id:
        raise AccountNotFoundError("Account is not resolved in draft.")
    account = accounts_repo.get_account(conn, acc_id)
    if not account:
        raise AccountNotFoundError(acc_id)
    if account["household_id"] != household_id:
        raise HouseholdMismatchError(f"Account {acc_id} does not belong to household.")
    if account["status"] != "active":
        raise AccountInactiveError(acc_id)

    payment_mode = draft.get("payment_mode")
    if not payment_mode or payment_mode not in ("one_off", "installment"):
        raise InvalidPaymentModeError(f"Cannot confirm request with unresolved or invalid payment_mode: '{payment_mode}'.")

    # Branch C: Installment
    if payment_mode == "installment":
        if account["account_type"] != "credit":
            raise LedgerDomainError(f"Account {account['name']} is not a credit account for installments.", code="NON_CREDIT_INSTALLMENT_ACCOUNT")

        raw_periods = draft.get("total_periods")
        if raw_periods is None:
            raise InvalidInstallmentPeriodsError("Installment total_periods is missing in draft.")
        try:
            total_periods = int(raw_periods)
        except (ValueError, TypeError):
            raise InvalidInstallmentPeriodsError(f"Invalid installment total_periods: {raw_periods}")

        if total_periods < 2 or total_periods > 120:
            raise InvalidInstallmentPeriodsError(f"Installment total_periods must be between 2 and 120. Given: {total_periods}")

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

    # Branch A / B: One-off
    cat_info = draft.get("category") or {}
    cat_id = UUID(cat_info["id"]) if isinstance(cat_info.get("id"), str) else cat_info.get("id")
    if not cat_id:
        raise CategoryNotFoundError("Category is not resolved in draft.")
    category = accounts_repo.get_category(conn, cat_id)
    if not category:
        raise CategoryNotFoundError(cat_id)
    if category["household_id"] != household_id:
        raise HouseholdMismatchError(f"Category {cat_id} does not belong to household.")
    if category["status"] != "active":
        raise CategoryNotFoundError(f"Category {cat_id} is inactive.")

    is_foreign_card = (account["account_type"] == "credit" and curr != account["currency"])
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

def recompute_draft_warnings(
    draft: Dict[str, Any],
    active_accounts: List[Dict[str, Any]],
    active_expense_categories: List[Dict[str, Any]]
) -> List[Dict[str, str]]:
    """
    Recomputes validation warnings from the current state of an ingestion draft.
    warnings=[] is only possible when all required draft fields and their
    deterministic relationships are valid enough for explicit Confirm.
    """
    warnings: List[Dict[str, str]] = []

    # 1. Payment mode validation
    pm = draft.get("payment_mode")
    if not pm or not isinstance(pm, str) or pm.strip().lower() not in ("one_off", "installment"):
        warnings.append({
            "code": "INVALID_PAYMENT_MODE",
            "message": f"未识别或缺失支付模式: '{pm}'，请手动确认。"
        })
        normalized_pm = None
    else:
        normalized_pm = pm.strip().lower()

    # 2. Account resolution and validation
    acc_info = draft.get("from_account")
    matched_acc = None
    if not acc_info or not isinstance(acc_info, dict) or not acc_info.get("id"):
        warnings.append({
            "code": "ACCOUNT_UNRESOLVED",
            "message": "未能识别支付账户，请手动选择。"
        })
    else:
        try:
            target_id = UUID(str(acc_info["id"]))
            matched_acc = next((a for a in active_accounts if a["id"] == target_id), None)
            if not matched_acc:
                warnings.append({
                    "code": "ACCOUNT_UNRESOLVED",
                    "message": "支付账户无效或未激活，请重新选择。"
                })
        except Exception:
            warnings.append({
                "code": "ACCOUNT_UNRESOLVED",
                "message": "支付账户格式无效，请手动选择。"
            })

    # 3. Currency validation
    raw_curr = draft.get("original_currency")
    valid_curr = None
    if not raw_curr or not isinstance(raw_curr, str) or not raw_curr.strip():
        warnings.append({
            "code": "CURRENCY_UNCLEAR",
            "message": "未能识别有效币种，请手动确认。"
        })
    else:
        try:
            valid_curr = validate_currency_code(raw_curr.strip().upper())
        except Exception:
            warnings.append({
                "code": "CURRENCY_UNCLEAR",
                "message": f"无效的币种代码: '{raw_curr}'。"
            })

    # 4. Amount validation
    raw_amt = draft.get("original_amount")
    if raw_amt is None or str(raw_amt).strip() == "":
        warnings.append({
            "code": "AMOUNT_UNCLEAR",
            "message": "未能识别消费金额。"
        })
    else:
        try:
            dec_amt = parse_decimal(str(raw_amt))
            if dec_amt <= 0:
                warnings.append({
                    "code": "AMOUNT_UNCLEAR",
                    "message": "消费金额格式无效或必须为正数。"
                })
        except Exception:
            warnings.append({
                "code": "AMOUNT_UNCLEAR",
                "message": "消费金额格式无效或必须为正数。"
            })

    # 5. Installment-specific validations
    if normalized_pm == "installment":
        if matched_acc and matched_acc.get("account_type") != "credit":
            warnings.append({
                "code": "NON_CREDIT_INSTALLMENT_ACCOUNT",
                "message": f"分期计划仅支持信用卡账户，'{matched_acc['name']}' 不是信用卡账户。"
            })

        raw_periods = draft.get("total_periods")
        if raw_periods is None:
            warnings.append({
                "code": "INVALID_INSTALLMENT_PERIODS",
                "message": "分期期数缺失或无效（支持 2-120 期），请手动确认。"
            })
        else:
            try:
                t_periods = int(raw_periods)
                if t_periods < 2 or t_periods > 120:
                    warnings.append({
                        "code": "INVALID_INSTALLMENT_PERIODS",
                        "message": "分期期数缺失或无效（支持 2-120 期），请手动确认。"
                    })
            except (ValueError, TypeError):
                warnings.append({
                    "code": "INVALID_INSTALLMENT_PERIODS",
                    "message": "分期期数缺失或无效（支持 2-120 期），请手动确认。"
                })

    # 6. One-off specific validations (also applies when payment_mode is missing/unresolved)
    if normalized_pm != "installment":
        cat_info = draft.get("category")
        if not cat_info or not isinstance(cat_info, dict) or not cat_info.get("id"):
            warnings.append({
                "code": "CATEGORY_UNRESOLVED",
                "message": "未能识别消费分类，请手动选择。"
            })
        else:
            try:
                target_cat_id = UUID(str(cat_info["id"]))
                matched_cat = next((c for c in active_expense_categories if c["id"] == target_cat_id), None)
                if not matched_cat:
                    warnings.append({
                        "code": "CATEGORY_UNRESOLVED",
                        "message": "消费分类无效或未激活，请手动选择。"
                    })
            except Exception:
                warnings.append({
                    "code": "CATEGORY_UNRESOLVED",
                    "message": "消费分类格式无效，请手动选择。"
                })

        # Deterministic Confirm compatibility: Non-credit account cannot pay mismatched currency
        if matched_acc and valid_curr:
            if matched_acc.get("account_type") != "credit" and valid_curr != matched_acc.get("currency"):
                warnings.append({
                    "code": "CURRENCY_MISMATCH",
                    "message": f"非信用卡账户 '{matched_acc['name']}' ({matched_acc['currency']}) 不支持外币消费 ({valid_curr})。"
                })

    return warnings


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
    Enforces strict state-machine checks and validates supplied entity IDs.
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

    if row["status"] == "rejected":
        raise InvalidRequestStateError("Cannot revise a rejected request.")

    if row["status"] == "failed":
        raise InvalidRequestStateError("Cannot revise a failed request.")

    if row["status"] in ("received", "processing"):
        raise InvalidRequestStateError("Request is still processing.")

    if row["status"] != "needs_confirmation":
        raise InvalidRequestStateError(f"Cannot revise request in status '{row['status']}'.")

    draft = dict(row["draft_payload"] or {})

    # Load active household accounts and categories
    accounts = accounts_repo.list_accounts(conn, household_id)
    active_accounts = [a for a in accounts if a["status"] == "active"]

    # Explicitly load active household-scoped account aliases
    all_aliases = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT aa.id, aa.account_id, aa.alias_text, aa.normalized_alias, aa.status
            FROM account_aliases aa
            JOIN accounts a ON a.id = aa.account_id
            WHERE a.household_id = %s
              AND a.status = 'active'
              AND aa.status = 'active'
              AND aa.deleted_at IS NULL;
            """,
            (household_id,)
        )
        for r in cur.fetchall():
            all_aliases.append({
                "id": r[0],
                "account_id": r[1],
                "alias_text": r[2],
                "normalized_alias": r[3],
                "status": r[4]
            })

    # Enrich active accounts with their active aliases for Gemini revision prompt
    for a in active_accounts:
        a["aliases"] = [al["alias_text"] for al in all_aliases if al["account_id"] == a["id"]]

    categories = accounts_repo.list_categories(conn, household_id)
    active_expense_categories = [c for c in categories if c["category_type"] == "expense" and c["status"] == "active"]

    # 1. Natural Language Revision via Gemini
    if correction_note and correction_note.strip():
        svc = gemini_service or GeminiService()
        revised = svc.revise_expense_draft(
            current_draft=draft,
            correction_note=correction_note.strip(),
            accounts=active_accounts,
            categories=active_expense_categories
        )

        # Conservative merge: null/missing revision field leaves current draft value unchanged
        if revised.occurred_on is not None:
            draft["occurred_on"] = revised.occurred_on.isoformat()

        if revised.merchant is not None and revised.merchant.strip():
            draft["merchant"] = revised.merchant.strip()

        if revised.original_amount is not None:
            draft["original_amount"] = str(revised.original_amount)

        if revised.original_currency is not None and revised.original_currency.strip():
            c_candidate = revised.original_currency.strip().upper()
            try:
                draft["original_currency"] = validate_currency_code(c_candidate)
            except Exception:
                draft["original_currency"] = c_candidate

        if revised.from_account is not None and revised.from_account.strip():
            resolved_acc, _ = _resolve_account(revised.from_account, active_accounts, all_aliases)
            if resolved_acc:
                draft["from_account"] = {"id": str(resolved_acc["id"]), "name": resolved_acc["name"]}
            else:
                # Explicit correction attempted but unresolved: clear draft field to null and warn
                draft["from_account"] = None

        if revised.category is not None and revised.category.strip():
            resolved_cat, _ = _resolve_category(revised.category, active_expense_categories)
            if resolved_cat:
                draft["category"] = {"id": str(resolved_cat["id"]), "name": resolved_cat["name"]}
            else:
                # Explicit correction attempted but unresolved: clear draft field to null and warn
                draft["category"] = None

        if revised.payment_mode is not None and revised.payment_mode.strip():
            draft["payment_mode"] = revised.payment_mode.strip().lower()

        if revised.total_periods is not None:
            draft["total_periods"] = revised.total_periods

    # 2. Structured Fields Revision (precedence over natural-language interpretation)
    if structured_fields:
        if "merchant" in structured_fields and structured_fields["merchant"] is not None:
            draft["merchant"] = structured_fields["merchant"]
        if "occurred_on" in structured_fields and structured_fields["occurred_on"] is not None:
            draft["occurred_on"] = structured_fields["occurred_on"]
        if "original_amount" in structured_fields and structured_fields["original_amount"] is not None:
            draft["original_amount"] = str(structured_fields["original_amount"])
        if "original_currency" in structured_fields and structured_fields["original_currency"] is not None:
            raw_curr = str(structured_fields["original_currency"]).strip().upper()
            try:
                draft["original_currency"] = validate_currency_code(raw_curr)
            except Exception:
                draft["original_currency"] = raw_curr
        if "from_account_id" in structured_fields and structured_fields["from_account_id"] is not None:
            target_acc_id = UUID(structured_fields["from_account_id"]) if isinstance(structured_fields["from_account_id"], str) else structured_fields["from_account_id"]
            acc = next((a for a in active_accounts if a["id"] == target_acc_id), None)
            if not acc:
                raise AccountNotFoundError(f"Account {target_acc_id} not found or inactive for household.")
            draft["from_account"] = {"id": str(acc["id"]), "name": acc["name"]}
        if "category_id" in structured_fields and structured_fields["category_id"] is not None:
            target_cat_id = UUID(structured_fields["category_id"]) if isinstance(structured_fields["category_id"], str) else structured_fields["category_id"]
            cat = next((c for c in active_expense_categories if c["id"] == target_cat_id), None)
            if not cat:
                raise CategoryNotFoundError(f"Expense category {target_cat_id} not found or inactive.")
            draft["category"] = {"id": str(cat["id"]), "name": cat["name"]}
        if "payment_mode" in structured_fields and structured_fields["payment_mode"] is not None:
            p_mode = structured_fields["payment_mode"]
            if p_mode not in ("one_off", "installment"):
                raise InvalidPaymentModeError(f"Invalid payment_mode '{p_mode}'. Must be 'one_off' or 'installment'.")
            draft["payment_mode"] = p_mode
        if "total_periods" in structured_fields and structured_fields["total_periods"] is not None:
            try:
                t_periods = int(structured_fields["total_periods"])
                if t_periods < 2 or t_periods > 120:
                    raise InvalidInstallmentPeriodsError(f"total_periods must be between 2 and 120. Given: {t_periods}")
                draft["total_periods"] = t_periods
            except (ValueError, TypeError):
                raise InvalidInstallmentPeriodsError(f"Invalid total_periods: {structured_fields['total_periods']}")

    # 3. Recompute warnings from FINAL revised draft
    warnings = recompute_draft_warnings(draft, active_accounts, active_expense_categories)

    # 4. Format display summary cleanly without "None" tokens
    acc_name_disp = draft.get("from_account", {}).get("name") if draft.get("from_account") else "未知账户"
    cat_name_disp = draft.get("category", {}).get("name") if draft.get("category") else "未分类"
    amt = draft.get("original_amount")
    curr = draft.get("original_currency")
    if amt and curr:
        amt_disp = f"{amt} {curr}"
    elif amt:
        amt_disp = f"{amt} (币种未知)"
    elif curr:
        amt_disp = f"未知金额 ({curr})"
    else:
        amt_disp = "未知金额"
    merchant_disp = draft.get("merchant") or "未知商户"
    display_summary = f"⚠️ 请确认 (已修订)\n{amt_disp} · {merchant_disp}\n{acc_name_disp} · {cat_name_disp}"

    response_payload = {
        "status": "needs_confirmation",
        "request_id": str(request_id),
        "draft": draft,
        "warnings": warnings,
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
    Produces zero financial transactions, balance changes, or installment plans.
    """
    device_id = device["device_id"]

    row = ingestion_repo.lock_ingestion_request(conn, request_id)
    if not row:
        raise RequestNotFoundError(f"Ingestion request {request_id} not found.")

    if row["device_id"] != device_id:
        raise HouseholdMismatchError("Ingestion request does not belong to the authenticated device.")

    if row["status"] == "committed":
        raise InvalidRequestStateError("Cannot reject an already committed ingestion request.")

    if row["status"] == "rejected":
        return row["response_payload"]

    if row["status"] not in ("needs_confirmation", "received", "processing"):
        raise InvalidRequestStateError(f"Cannot reject request in status '{row['status']}'.")

    response_payload = {
        "status": "rejected",
        "request_id": str(request_id),
        "display_summary": f"已放弃记账: {reason or '用户取消'}"
    }

    ingestion_repo.update_ingestion_request_status(
        conn=conn,
        request_id=request_id,
        status="rejected",
        response_payload=response_payload
    )
    return response_payload
