from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from app.domain.money import parse_decimal, quantize_money, validate_minor_units
from app.domain.transactions import (
    TransactionNotFoundError,
    TransactionAlreadyVoidedError,
    HouseholdMismatchError,
    RowVersionConflictError,
    InvalidTransactionShapeError,
    InvalidAmountError,
    CategoryNotFoundError,
    CategoryMismatchError,
    RefundExceedsOriginalError
)
import app.repositories.transactions as tx_repo
import app.repositories.accounts as accounts_repo
import app.repositories.categories as categories_repo
import app.repositories.audit as audit_repo


def _validate_correction_fields(
    conn,
    household_id: UUID,
    tx: Dict[str, Any],
    changes: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Strictly validates proposed correction changes against domain and transaction-shape invariants.
    Rejects unsupported keys, validates amount positivity, minor unit precision, category validity,
    and enforces transaction-type specific leg rules.
    """
    allowed_keys = {"occurred_on", "category_id", "merchant", "remarks", "from_amount", "to_amount"}
    unknown_keys = set(changes.keys()) - allowed_keys
    if unknown_keys:
        raise InvalidTransactionShapeError(f"Unsupported correction field(s): {', '.join(sorted(unknown_keys))}")

    tx_type = tx.get("transaction_type")

    # 1. Engine-owned / system transactions reject generic amount modifications
    if tx_type in ("reconciliation_adjustment", "opening_balance", "investment_pnl"):
        if "from_amount" in changes or "to_amount" in changes:
            raise InvalidTransactionShapeError(
                f"Direct manual amount correction is not permitted for '{tx_type}' transactions."
            )

    validated_changes: Dict[str, Any] = {}

    # 2. Date
    if "occurred_on" in changes and changes["occurred_on"] is not None:
        val = changes["occurred_on"]
        if isinstance(val, str):
            try:
                validated_changes["occurred_on"] = date.fromisoformat(val)
            except Exception:
                raise InvalidTransactionShapeError(f"Invalid occurred_on date format: {val}")
        elif isinstance(val, date):
            validated_changes["occurred_on"] = val
        else:
            raise InvalidTransactionShapeError("occurred_on must be a date or ISO string.")

    # 3. Category
    if "category_id" in changes and changes["category_id"] is not None:
        cat_id_val = changes["category_id"]
        try:
            cat_uuid = UUID(str(cat_id_val))
        except (ValueError, TypeError):
            raise InvalidTransactionShapeError(f"Invalid category UUID: {cat_id_val}")

        cat = categories_repo.get_category(conn, cat_uuid, household_id)
        if not cat or cat["household_id"] != household_id:
            raise CategoryNotFoundError(cat_uuid)
        if cat.get("status") != "active":
            raise CategoryMismatchError(f"Category {cat_uuid} is inactive.")

        if tx_type == "expense" and cat.get("category_type") != "expense":
            raise CategoryMismatchError(f"Category type '{cat.get('category_type')}' is incompatible with expense transaction.")
        elif tx_type == "cash_income" and cat.get("category_type") != "income":
            raise CategoryMismatchError(f"Category type '{cat.get('category_type')}' is incompatible with income transaction.")

        validated_changes["category_id"] = cat_uuid

    # 4. Merchant & Remarks
    if "merchant" in changes:
        m_val = changes["merchant"]
        validated_changes["merchant"] = str(m_val).strip() if m_val is not None else None
    if "remarks" in changes:
        r_val = changes["remarks"]
        validated_changes["remarks"] = str(r_val).strip() if r_val is not None else None

    # 5. Transaction-Type Specific Leg & Amount Invariants
    from_curr = tx.get("from_currency") or tx.get("original_currency", "CNY")
    to_curr = tx.get("to_currency") or tx.get("original_currency", "CNY")

    if tx_type in ("expense", "fee"):
        if "to_amount" in changes and changes["to_amount"] is not None:
            raise InvalidTransactionShapeError(f"Transaction of type '{tx_type}' cannot have a to_amount leg.")

    elif tx_type == "cash_income":
        if "from_amount" in changes and changes["from_amount"] is not None:
            raise InvalidTransactionShapeError("Transaction of type 'cash_income' cannot have a from_amount leg.")

    elif tx_type == "refund":
        if "from_amount" in changes and changes["from_amount"] is not None:
            raise InvalidTransactionShapeError("Transaction of type 'refund' cannot have a from_amount leg.")

    # Validate and parse amounts
    if "from_amount" in changes and changes["from_amount"] is not None:
        try:
            dec_from = parse_decimal(changes["from_amount"])
        except Exception:
            raise InvalidAmountError("Malformed decimal for from_amount.")
        if dec_from <= Decimal("0.00"):
            raise InvalidAmountError("from_amount must be strictly positive.")
        try:
            validate_minor_units(dec_from, from_curr)
        except ValueError as ve:
            raise InvalidAmountError(str(ve))
        validated_changes["from_amount"] = dec_from

    if "to_amount" in changes and changes["to_amount"] is not None:
        try:
            dec_to = parse_decimal(changes["to_amount"])
        except Exception:
            raise InvalidAmountError("Malformed decimal for to_amount.")
        if dec_to <= Decimal("0.00"):
            raise InvalidAmountError("to_amount must be strictly positive.")
        try:
            validate_minor_units(dec_to, to_curr)
        except ValueError as ve:
            raise InvalidAmountError(str(ve))
        validated_changes["to_amount"] = dec_to

    # 6. Transfer Consistency
    if tx_type == "transfer":
        is_same_currency = from_curr.upper() == to_curr.upper()
        if is_same_currency:
            if "from_amount" in validated_changes and "to_amount" in validated_changes:
                if validated_changes["from_amount"] != validated_changes["to_amount"]:
                    raise InvalidTransactionShapeError("Same-currency transfer must have equal from_amount and to_amount.")
            elif "from_amount" in validated_changes:
                validated_changes["to_amount"] = validated_changes["from_amount"]
            elif "to_amount" in validated_changes:
                validated_changes["from_amount"] = validated_changes["to_amount"]

    # 7. Refund Bounds Validation
    if tx_type == "refund" and "to_amount" in validated_changes:
        new_ref_amt = validated_changes["to_amount"]
        # Find original expense link
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_transaction_id 
                FROM transaction_links 
                WHERE target_transaction_id = %s AND relation_type = 'refund';
                """,
                (tx["id"],)
            )
            link_row = cur.fetchone()

        if link_row:
            orig_expense_id = link_row[0]
            orig_tx = tx_repo.get_transaction(conn, orig_expense_id)
            if orig_tx:
                orig_total = parse_decimal(orig_tx["from_amount"] if orig_tx.get("from_amount") is not None else orig_tx["original_amount"])
                active_refunds = tx_repo.get_active_refunds_for_expense(conn, orig_expense_id)
                other_refunds_total = sum(
                    parse_decimal(r["to_amount"] if r.get("to_amount") is not None else r["original_amount"])
                    for r in active_refunds
                    if str(r["id"]) != str(tx["id"])
                )
                if other_refunds_total + new_ref_amt > orig_total:
                    raise RefundExceedsOriginalError(
                        f"Refund amount {new_ref_amt} exceeds remaining refundable amount {orig_total - other_refunds_total}."
                    )

    return validated_changes


def preview_transaction_correction(
    conn,
    household_id: UUID,
    transaction_id: UUID,
    changes: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Calculates projected balance impacts and requirements for correcting a transaction. Read-only.
    """
    tx = tx_repo.get_transaction(conn, transaction_id)
    if not tx:
        raise TransactionNotFoundError(transaction_id)
    if tx["household_id"] != household_id:
        raise HouseholdMismatchError()
    if tx["status"] == "voided" or tx.get("deleted_at") is not None:
        raise TransactionAlreadyVoidedError(transaction_id)

    validated_changes = _validate_correction_fields(conn, household_id, tx, changes)

    proposed_changes_resp: Dict[str, Any] = {}
    if "occurred_on" in validated_changes:
        proposed_changes_resp["occurred_on"] = validated_changes["occurred_on"].isoformat()
    if "category_id" in validated_changes:
        proposed_changes_resp["category_id"] = str(validated_changes["category_id"])
    if "merchant" in validated_changes:
        proposed_changes_resp["merchant"] = validated_changes["merchant"]
    if "remarks" in validated_changes:
        proposed_changes_resp["remarks"] = validated_changes["remarks"]
    if "from_amount" in validated_changes:
        proposed_changes_resp["from_amount"] = f"{validated_changes['from_amount']:.2f}"
    if "to_amount" in validated_changes:
        proposed_changes_resp["to_amount"] = f"{validated_changes['to_amount']:.2f}"

    account_state_deltas: List[Dict[str, Any]] = []

    # Calculate delta for from_account
    if "from_amount" in validated_changes and tx.get("from_account_id"):
        from_acc = accounts_repo.get_account(conn, tx["from_account_id"])
        state = accounts_repo.get_account_state(conn, tx["from_account_id"])
        curr_bal = Decimal(str(state["ledger_balance"])) if state else Decimal("0.00")
        old_amt = Decimal(str(tx["from_amount"] if tx.get("from_amount") is not None else tx["original_amount"]))
        new_amt = validated_changes["from_amount"]
        delta = old_amt - new_amt
        projected = curr_bal + delta
        curr_code = from_acc["currency"] if from_acc else tx["original_currency"]
        account_state_deltas.append({
            "account_id": str(tx["from_account_id"]),
            "account_name": from_acc["name"] if from_acc else "Debit Account",
            "current_balance": f"{quantize_money(curr_bal, curr_code):.2f}",
            "delta": f"{quantize_money(delta, curr_code):.2f}",
            "projected_balance": f"{quantize_money(projected, curr_code):.2f}"
        })

    # Calculate delta for to_account
    if "to_amount" in validated_changes and tx.get("to_account_id"):
        to_acc = accounts_repo.get_account(conn, tx["to_account_id"])
        state = accounts_repo.get_account_state(conn, tx["to_account_id"])
        curr_bal = Decimal(str(state["ledger_balance"])) if state else Decimal("0.00")
        old_amt = Decimal(str(tx["to_amount"] if tx.get("to_amount") is not None else tx["original_amount"]))
        new_amt = validated_changes["to_amount"]
        delta = new_amt - old_amt
        projected = curr_bal + delta
        curr_code = to_acc["currency"] if to_acc else tx["original_currency"]
        account_state_deltas.append({
            "account_id": str(tx["to_account_id"]),
            "account_name": to_acc["name"] if to_acc else "Credit Account",
            "current_balance": f"{quantize_money(curr_bal, curr_code):.2f}",
            "delta": f"{quantize_money(delta, curr_code):.2f}",
            "projected_balance": f"{quantize_money(projected, curr_code):.2f}"
        })

    is_stmt_confirmed = tx.get("verification_status") == "statement_confirmed"
    requires_confirmation = is_stmt_confirmed or len(account_state_deltas) > 0

    return {
        "transaction_id": str(transaction_id),
        "expected_version": tx["row_version"],
        "is_statement_confirmed": is_stmt_confirmed,
        "proposed_changes": proposed_changes_resp,
        "account_state_deltas": account_state_deltas,
        "requires_confirmation": requires_confirmation
    }


def commit_transaction_correction(
    conn,
    household_id: UUID,
    transaction_id: UUID,
    expected_version: int,
    changes: Dict[str, Any],
    reason: Optional[str] = None,
    actor_user_id: Optional[UUID] = None,
    actor_device_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Atomically commits explicit correction to a transaction and reconciles account balances.
    Maintains all dependent fields (original_amount, reporting_amount, effective_fx_rate).
    """
    # 1. Lock transaction row
    tx = tx_repo.lock_transaction(conn, transaction_id)
    if not tx:
        raise TransactionNotFoundError(transaction_id)
    if tx["household_id"] != household_id:
        raise HouseholdMismatchError()
    if tx["status"] == "voided" or tx.get("deleted_at") is not None:
        raise TransactionAlreadyVoidedError(transaction_id)

    # 2. Optimistic concurrency check
    if tx["row_version"] != expected_version:
        raise RowVersionConflictError("Transaction was modified concurrently. Reload before correcting.")

    # 3. Domain validation
    validated_changes = _validate_correction_fields(conn, household_id, tx, changes)
    tx_type = tx.get("transaction_type")

    before_data = {
        "occurred_on": tx["occurred_on"].isoformat() if tx.get("occurred_on") else None,
        "category_id": str(tx["category_id"]) if tx.get("category_id") else None,
        "merchant": tx.get("merchant"),
        "remarks": tx.get("remarks"),
        "from_amount": str(tx["from_amount"]) if tx.get("from_amount") is not None else None,
        "to_amount": str(tx["to_amount"]) if tx.get("to_amount") is not None else None,
        "original_amount": str(tx["original_amount"]) if tx.get("original_amount") is not None else None,
        "effective_fx_rate": str(tx["effective_fx_rate"]) if tx.get("effective_fx_rate") is not None else None
    }

    # 4. Account balance deltas with deterministic lock ordering
    affected_accs: List[UUID] = []
    if "from_amount" in validated_changes and tx.get("from_account_id"):
        affected_accs.append(tx["from_account_id"])
    if "to_amount" in validated_changes and tx.get("to_account_id"):
        affected_accs.append(tx["to_account_id"])

    if affected_accs:
        locked_states = accounts_repo.lock_account_states(conn, sorted(list(set(affected_accs))))
        if "from_amount" in validated_changes and tx.get("from_account_id"):
            old_amt = Decimal(str(tx["from_amount"] if tx.get("from_amount") is not None else tx["original_amount"]))
            new_amt = validated_changes["from_amount"]
            delta = old_amt - new_amt
            new_bal = locked_states[tx["from_account_id"]]["ledger_balance"] + delta
            accounts_repo.update_account_state_projection(conn, tx["from_account_id"], new_bal)

        if "to_amount" in validated_changes and tx.get("to_account_id"):
            old_amt = Decimal(str(tx["to_amount"] if tx.get("to_amount") is not None else tx["original_amount"]))
            new_amt = validated_changes["to_amount"]
            delta = new_amt - old_amt
            new_bal = locked_states[tx["to_account_id"]]["ledger_balance"] + delta
            accounts_repo.update_account_state_projection(conn, tx["to_account_id"], new_bal)

    # 5. Compute dependent fields (original_amount, reporting_amount, effective_fx_rate)
    new_from = validated_changes.get("from_amount") if "from_amount" in validated_changes else tx.get("from_amount")
    new_to = validated_changes.get("to_amount") if "to_amount" in validated_changes else tx.get("to_amount")
    new_orig = tx.get("original_amount")
    new_fx_rate = tx.get("effective_fx_rate")
    new_rep_amount = tx.get("reporting_amount")

    household = accounts_repo.get_household(conn, household_id)
    rep_curr = household["reporting_currency"]

    # Determine active financial leg and currency
    financial_leg: Optional[Decimal] = None
    leg_curr: Optional[str] = None

    if tx_type in ("expense", "fee") and new_from is not None:
        financial_leg = new_from
        leg_curr = tx.get("from_currency")
        if tx.get("from_currency") == tx.get("original_currency") or tx.get("original_currency") is None:
            new_orig = new_from
        else:
            # Foreign card transaction: preserve original foreign currency amount
            new_orig = tx.get("original_amount")
    elif tx_type in ("cash_income", "refund") and new_to is not None:
        financial_leg = new_to
        leg_curr = tx.get("to_currency")
        if tx.get("to_currency") == tx.get("original_currency") or tx.get("original_currency") is None:
            new_orig = new_to
        else:
            new_orig = tx.get("original_amount")
    elif tx_type == "transfer":
        financial_leg = new_from
        leg_curr = tx.get("from_currency")
        if new_from is not None and new_to is not None:
            if tx.get("from_currency") == tx.get("to_currency"):
                new_fx_rate = Decimal("1.000000000000")
                new_orig = new_from
            else:
                new_fx_rate = (new_from / new_to).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)

    # Recompute reporting_amount deterministically preserving frozen reporting FX rate
    if financial_leg is not None and leg_curr is not None:
        if leg_curr == rep_curr:
            new_rep_amount = quantize_money(financial_leg, rep_curr)
        else:
            if tx.get("reporting_fx_rate") is not None:
                frozen_rate = parse_decimal(tx["reporting_fx_rate"])
                new_rep_amount = quantize_money(financial_leg * frozen_rate, rep_curr)
            elif tx.get("reporting_amount") is not None or tx.get("verification_status") == "statement_confirmed":
                raise domain_tx.InvalidTransactionShapeError(
                    "Cannot deterministically recompute reporting amount: missing frozen reporting FX rate."
                )
            else:
                new_rep_amount = None

    # 6. Execute repository update
    merchant_raw = validated_changes.get("merchant") if "merchant" in validated_changes else tx.get("merchant")
    merchant_norm = merchant_raw.strip().lower() if merchant_raw else None

    tx_repo.update_transaction_fields(
        conn=conn,
        transaction_id=transaction_id,
        occurred_on=validated_changes.get("occurred_on") if "occurred_on" in validated_changes else tx["occurred_on"],
        category_id=validated_changes.get("category_id") if "category_id" in validated_changes else tx.get("category_id"),
        merchant=merchant_raw,
        merchant_normalized=merchant_norm,
        remarks=validated_changes.get("remarks") if "remarks" in validated_changes else tx.get("remarks"),
        from_amount=new_from,
        to_amount=new_to,
        original_amount=new_orig,
        reporting_amount=new_rep_amount,
        effective_fx_rate=new_fx_rate
    )

    # 7. Append immutable audit event
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type="user" if actor_user_id else "system",
        entity_type="transaction",
        entity_id=transaction_id,
        action="update",
        actor_user_id=actor_user_id,
        actor_device_id=actor_device_id,
        before_data=before_data,
        after_data={
            "changes": changes,
            "reason": reason
        }
    )

    return tx_repo.get_transaction_detail(conn, transaction_id, household_id)
