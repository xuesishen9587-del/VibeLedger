"""
VibeLedger Core Ledger Service (Phase 2).

All functions in this module are transaction-scoped service primitives designed for composability.
The caller or workflow owns the database transaction boundary:
    with transaction(conn):
        ...

The ledger service primitives do NOT perform independent commits or rollbacks.
If any step in a composite workflow (e.g. transfer + fee) fails, the caller's transaction
context manager will automatically roll back all mutations, leaving the database state untouched.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List, Union
from uuid import UUID, uuid4
from datetime import date, datetime, timezone
import psycopg2

from app.domain.money import parse_decimal, validate_currency_code, quantize_money, validate_fx_rate
from app.domain import transactions as domain_tx
from app.repositories import accounts as accounts_repo
from app.repositories import transactions as tx_repo
from app.repositories import audit as audit_repo

# --- 1. Expense ---

def record_expense(
    conn,
    household_id: UUID,
    from_account_id: UUID,
    amount: Union[str, int, Decimal],
    currency: str,
    category_id: UUID,
    occurred_on: date,
    occurred_at: Optional[datetime] = None,
    merchant: Optional[str] = None,
    remarks: Optional[str] = None,
    source: str = "system",
    created_by_user_id: Optional[UUID] = None,
    created_by_device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    transaction_id: Optional[UUID] = None,
    account_leg_status: Optional[str] = None,
    original_amount: Optional[Union[str, int, Decimal]] = None,
    original_currency: Optional[str] = None,
    effective_fx_rate: Optional[Union[str, int, Decimal]] = None
) -> Dict[str, Any]:
    if occurred_on is None or not isinstance(occurred_on, date):
        raise domain_tx.InvalidTransactionShapeError("occurred_on is a required business date.")

    curr = validate_currency_code(currency)
    dec_amount = quantize_money(parse_decimal(amount), curr)
    if dec_amount <= 0:
        raise domain_tx.InvalidAmountError(f"Expense amount must be strictly positive. Given: {amount}")

    orig_amt = parse_decimal(original_amount) if original_amount is not None else dec_amount
    orig_curr = validate_currency_code(original_currency) if original_currency is not None else curr
    fx_rate = parse_decimal(effective_fx_rate) if effective_fx_rate is not None else None
    leg_status = account_leg_status or "authoritative"

    # 1. Lock account state
    locked_states = accounts_repo.lock_account_states(conn, [from_account_id])
    if from_account_id not in locked_states:
        raise domain_tx.AccountNotFoundError(from_account_id)
    acc_state = locked_states[from_account_id]

    # 2. Validate account metadata
    account = accounts_repo.get_account(conn, from_account_id)
    if not account:
        raise domain_tx.AccountNotFoundError(from_account_id)
    if account["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Account {from_account_id} does not belong to household {household_id}.")
    if account["status"] != "active":
        raise domain_tx.AccountInactiveError(from_account_id)
    if account["currency"] != curr:
        raise domain_tx.CurrencyMismatchError(f"Expense currency {curr} does not match account currency {account['currency']}.")

    # 3. Validate category metadata
    category = accounts_repo.get_category(conn, category_id)
    if not category:
        raise domain_tx.CategoryNotFoundError(category_id)
    if category["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Category {category_id} does not belong to household {household_id}.")
    if category["category_type"] != "expense":
        raise domain_tx.CategoryMismatchError(f"Expense transaction requires an expense-type category. Given: {category['category_type']}.")
    if category["status"] != "active":
        raise domain_tx.CategoryMismatchError(f"Category {category_id} is inactive.")

    # 4. Compute universal signed projection
    new_balance = acc_state["ledger_balance"] - dec_amount

    # 5. Insert transaction record
    tx_id = transaction_id or uuid4()
    tx_dict = {
        "id": tx_id,
        "household_id": household_id,
        "transaction_type": "expense",
        "occurred_on": occurred_on,
        "occurred_at": occurred_at,
        "posted_on": None,
        "from_account_id": from_account_id,
        "to_account_id": None,
        "original_amount": orig_amt,
        "original_currency": orig_curr,
        "from_amount": dec_amount,
        "from_currency": curr,
        "to_amount": None,
        "to_currency": None,
        "effective_fx_rate": fx_rate,
        "account_leg_status": leg_status,
        "reporting_amount": None,
        "reporting_currency": None,
        "reporting_fx_rate": None,
        "reporting_fx_locked_at": None,
        "category_id": category_id,
        "merchant": merchant,
        "merchant_normalized": merchant.strip().lower() if merchant else None,
        "remarks": remarks,
        "source": source,
        "status": "committed",
        "verification_status": "unverified",
        "confidence": None,
        "source_request_id": source_request_id,
        "statement_batch_id": None,
        "created_by_user_id": created_by_user_id,
        "created_by_device_id": created_by_device_id,
        "row_version": 0,
        "deleted_at": None,
        "deleted_by_user_id": None,
        "delete_reason": None
    }
    tx_repo.insert_transaction(conn, tx_dict)

    # 6. Update projection
    tx_time = occurred_at or datetime.now(timezone.utc)
    accounts_repo.update_account_state_projection(conn, from_account_id, new_balance, last_transaction_at=tx_time)

    # 7. Append audit event
    actor_type = "device" if created_by_device_id else ("user" if created_by_user_id else "system")
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type=actor_type,
        entity_type="transaction",
        entity_id=tx_id,
        action="create",
        actor_user_id=created_by_user_id,
        actor_device_id=created_by_device_id,
        request_id=source_request_id,
        after_data={
            "transaction_type": "expense",
            "from_account_id": str(from_account_id),
            "amount": str(dec_amount),
            "currency": curr,
            "category_id": str(category_id),
            "occurred_on": str(occurred_on)
        }
    )

    return tx_repo.get_transaction(conn, tx_id)


# --- 2. Cash Income ---

def record_cash_income(
    conn,
    household_id: UUID,
    to_account_id: UUID,
    amount: Union[str, int, Decimal],
    currency: str,
    category_id: UUID,
    occurred_on: date,
    occurred_at: Optional[datetime] = None,
    merchant: Optional[str] = None,
    remarks: Optional[str] = None,
    source: str = "system",
    created_by_user_id: Optional[UUID] = None,
    created_by_device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    transaction_id: Optional[UUID] = None
) -> Dict[str, Any]:
    if occurred_on is None or not isinstance(occurred_on, date):
        raise domain_tx.InvalidTransactionShapeError("occurred_on is a required business date.")

    curr = validate_currency_code(currency)
    dec_amount = quantize_money(parse_decimal(amount), curr)
    if dec_amount <= 0:
        raise domain_tx.InvalidAmountError(f"Cash income amount must be strictly positive. Given: {amount}")

    # 1. Lock account state
    locked_states = accounts_repo.lock_account_states(conn, [to_account_id])
    if to_account_id not in locked_states:
        raise domain_tx.AccountNotFoundError(to_account_id)
    acc_state = locked_states[to_account_id]

    # 2. Validate account metadata
    account = accounts_repo.get_account(conn, to_account_id)
    if not account:
        raise domain_tx.AccountNotFoundError(to_account_id)
    if account["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Account {to_account_id} does not belong to household {household_id}.")
    if account["status"] != "active":
        raise domain_tx.AccountInactiveError(to_account_id)
    if account["currency"] != curr:
        raise domain_tx.CurrencyMismatchError(f"Income currency {curr} does not match account currency {account['currency']}.")

    # 3. Validate category metadata
    category = accounts_repo.get_category(conn, category_id)
    if not category:
        raise domain_tx.CategoryNotFoundError(category_id)
    if category["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Category {category_id} does not belong to household {household_id}.")
    if category["category_type"] != "income":
        raise domain_tx.CategoryMismatchError(f"Cash income transaction requires an income-type category. Given: {category['category_type']}.")
    if category["status"] != "active":
        raise domain_tx.CategoryMismatchError(f"Category {category_id} is inactive.")

    # 4. Compute universal signed projection
    new_balance = acc_state["ledger_balance"] + dec_amount

    # 5. Insert transaction record
    tx_id = transaction_id or uuid4()
    tx_dict = {
        "id": tx_id,
        "household_id": household_id,
        "transaction_type": "cash_income",
        "occurred_on": occurred_on,
        "occurred_at": occurred_at,
        "posted_on": None,
        "from_account_id": None,
        "to_account_id": to_account_id,
        "original_amount": dec_amount,
        "original_currency": curr,
        "from_amount": None,
        "from_currency": None,
        "to_amount": dec_amount,
        "to_currency": curr,
        "effective_fx_rate": None,
        "account_leg_status": "authoritative",
        "reporting_amount": None,
        "reporting_currency": None,
        "reporting_fx_rate": None,
        "reporting_fx_locked_at": None,
        "category_id": category_id,
        "merchant": merchant,
        "merchant_normalized": merchant.strip().lower() if merchant else None,
        "remarks": remarks,
        "source": source,
        "status": "committed",
        "verification_status": "unverified",
        "confidence": None,
        "source_request_id": source_request_id,
        "statement_batch_id": None,
        "created_by_user_id": created_by_user_id,
        "created_by_device_id": created_by_device_id,
        "row_version": 0,
        "deleted_at": None,
        "deleted_by_user_id": None,
        "delete_reason": None
    }
    tx_repo.insert_transaction(conn, tx_dict)

    # 6. Update projection
    tx_time = occurred_at or datetime.now(timezone.utc)
    accounts_repo.update_account_state_projection(conn, to_account_id, new_balance, last_transaction_at=tx_time)

    # 7. Append audit event
    actor_type = "device" if created_by_device_id else ("user" if created_by_user_id else "system")
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type=actor_type,
        entity_type="transaction",
        entity_id=tx_id,
        action="create",
        actor_user_id=created_by_user_id,
        actor_device_id=created_by_device_id,
        request_id=source_request_id,
        after_data={
            "transaction_type": "cash_income",
            "to_account_id": str(to_account_id),
            "amount": str(dec_amount),
            "currency": curr,
            "category_id": str(category_id),
            "occurred_on": str(occurred_on)
        }
    )

    return tx_repo.get_transaction(conn, tx_id)


# --- 3. Fee ---

def record_fee(
    conn,
    household_id: UUID,
    from_account_id: UUID,
    amount: Union[str, int, Decimal],
    currency: str,
    category_id: UUID,
    occurred_on: date,
    occurred_at: Optional[datetime] = None,
    merchant: Optional[str] = None,
    remarks: Optional[str] = None,
    source: str = "system",
    created_by_user_id: Optional[UUID] = None,
    created_by_device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    transaction_id: Optional[UUID] = None
) -> Dict[str, Any]:
    if occurred_on is None or not isinstance(occurred_on, date):
        raise domain_tx.InvalidTransactionShapeError("occurred_on is a required business date.")

    curr = validate_currency_code(currency)
    dec_amount = quantize_money(parse_decimal(amount), curr)
    if dec_amount <= 0:
        raise domain_tx.InvalidAmountError(f"Fee amount must be strictly positive. Given: {amount}")

    # 1. Lock account state
    locked_states = accounts_repo.lock_account_states(conn, [from_account_id])
    if from_account_id not in locked_states:
        raise domain_tx.AccountNotFoundError(from_account_id)
    acc_state = locked_states[from_account_id]

    # 2. Validate account metadata
    account = accounts_repo.get_account(conn, from_account_id)
    if not account:
        raise domain_tx.AccountNotFoundError(from_account_id)
    if account["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Account {from_account_id} does not belong to household {household_id}.")
    if account["status"] != "active":
        raise domain_tx.AccountInactiveError(from_account_id)
    if account["currency"] != curr:
        raise domain_tx.CurrencyMismatchError(f"Fee currency {curr} does not match account currency {account['currency']}.")

    # 3. Validate category metadata
    category = accounts_repo.get_category(conn, category_id)
    if not category:
        raise domain_tx.CategoryNotFoundError(category_id)
    if category["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Category {category_id} does not belong to household {household_id}.")
    if category["category_type"] != "expense":
        raise domain_tx.CategoryMismatchError(f"Fee transaction requires an expense-type category. Given: {category['category_type']}.")
    if category["status"] != "active":
        raise domain_tx.CategoryMismatchError(f"Category {category_id} is inactive.")

    # 4. Compute universal signed projection
    new_balance = acc_state["ledger_balance"] - dec_amount

    # 5. Insert transaction record
    tx_id = transaction_id or uuid4()
    tx_dict = {
        "id": tx_id,
        "household_id": household_id,
        "transaction_type": "fee",
        "occurred_on": occurred_on,
        "occurred_at": occurred_at,
        "posted_on": None,
        "from_account_id": from_account_id,
        "to_account_id": None,
        "original_amount": dec_amount,
        "original_currency": curr,
        "from_amount": dec_amount,
        "from_currency": curr,
        "to_amount": None,
        "to_currency": None,
        "effective_fx_rate": None,
        "account_leg_status": "authoritative",
        "reporting_amount": None,
        "reporting_currency": None,
        "reporting_fx_rate": None,
        "reporting_fx_locked_at": None,
        "category_id": category_id,
        "merchant": merchant,
        "merchant_normalized": merchant.strip().lower() if merchant else None,
        "remarks": remarks,
        "source": source,
        "status": "committed",
        "verification_status": "unverified",
        "confidence": None,
        "source_request_id": source_request_id,
        "statement_batch_id": None,
        "created_by_user_id": created_by_user_id,
        "created_by_device_id": created_by_device_id,
        "row_version": 0,
        "deleted_at": None,
        "deleted_by_user_id": None,
        "delete_reason": None
    }
    tx_repo.insert_transaction(conn, tx_dict)

    # 6. Update projection
    tx_time = occurred_at or datetime.now(timezone.utc)
    accounts_repo.update_account_state_projection(conn, from_account_id, new_balance, last_transaction_at=tx_time)

    # 7. Append audit event
    actor_type = "device" if created_by_device_id else ("user" if created_by_user_id else "system")
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type=actor_type,
        entity_type="transaction",
        entity_id=tx_id,
        action="create",
        actor_user_id=created_by_user_id,
        actor_device_id=created_by_device_id,
        request_id=source_request_id,
        after_data={
            "transaction_type": "fee",
            "from_account_id": str(from_account_id),
            "amount": str(dec_amount),
            "currency": curr,
            "category_id": str(category_id),
            "occurred_on": str(occurred_on)
        }
    )

    return tx_repo.get_transaction(conn, tx_id)


# --- 4. Transfer ---

def record_transfer(
    conn,
    household_id: UUID,
    from_account_id: UUID,
    to_account_id: UUID,
    from_amount: Union[str, int, Decimal],
    from_currency: str,
    to_amount: Optional[Union[str, int, Decimal]] = None,
    to_currency: Optional[str] = None,
    occurred_on: Optional[date] = None,
    occurred_at: Optional[datetime] = None,
    remarks: Optional[str] = None,
    fee_amount: Optional[Union[str, int, Decimal]] = None,
    fee_currency: Optional[str] = None,
    fee_category_id: Optional[UUID] = None,
    source: str = "system",
    created_by_user_id: Optional[UUID] = None,
    created_by_device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    transaction_id: Optional[UUID] = None
) -> Dict[str, Any]:
    if occurred_on is None or not isinstance(occurred_on, date):
        raise domain_tx.InvalidTransactionShapeError("occurred_on is a required business date.")

    if from_account_id == to_account_id:
        raise domain_tx.SameAccountTransferError("Source and destination accounts in a transfer must be distinct.")

    from_curr = validate_currency_code(from_currency)
    dec_from_amount = quantize_money(parse_decimal(from_amount), from_curr)
    if dec_from_amount <= 0:
        raise domain_tx.InvalidAmountError(f"Transfer from_amount must be strictly positive. Given: {from_amount}")

    to_curr = validate_currency_code(to_currency) if to_currency else from_curr

    # Handle same-currency vs cross-currency transfer
    if from_curr == to_curr:
        if to_amount is not None:
            dec_to_amount = quantize_money(parse_decimal(to_amount), to_curr)
            if dec_to_amount != dec_from_amount:
                raise domain_tx.InvalidAmountError(
                    f"Same-currency transfer amounts must match: from_amount={dec_from_amount}, to_amount={dec_to_amount}"
                )
        else:
            dec_to_amount = dec_from_amount
        fx_rate = Decimal("1.000000000000")
    else:
        # Cross-currency transfer requires both explicit real legs
        if to_amount is None:
            raise domain_tx.CrossCurrencyMissingLegError("Cross-currency transfer requires explicit to_amount leg.")
        dec_to_amount = quantize_money(parse_decimal(to_amount), to_curr)
        if dec_to_amount <= 0:
            raise domain_tx.InvalidAmountError(f"Cross-currency to_amount must be strictly positive. Given: {to_amount}")
        
        # effective_fx_rate = from_amount / to_amount
        raw_fx = dec_from_amount / dec_to_amount
        fx_rate = validate_fx_rate(raw_fx.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP))

    # Deterministic lock ordering on accounts by UUID
    locked_states = accounts_repo.lock_account_states(conn, [from_account_id, to_account_id])
    if from_account_id not in locked_states:
        raise domain_tx.AccountNotFoundError(from_account_id)
    if to_account_id not in locked_states:
        raise domain_tx.AccountNotFoundError(to_account_id)

    from_acc_state = locked_states[from_account_id]
    to_acc_state = locked_states[to_account_id]

    from_account = accounts_repo.get_account(conn, from_account_id)
    to_account = accounts_repo.get_account(conn, to_account_id)

    if from_account["household_id"] != household_id or to_account["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError("Both accounts in a transfer must belong to the same household.")
    if from_account["status"] != "active" or to_account["status"] != "active":
        raise domain_tx.AccountInactiveError(from_account_id if from_account["status"] != "active" else to_account_id)
    if from_account["currency"] != from_curr:
        raise domain_tx.CurrencyMismatchError(f"from_currency {from_curr} does not match from_account currency {from_account['currency']}.")
    if to_account["currency"] != to_curr:
        raise domain_tx.CurrencyMismatchError(f"to_currency {to_curr} does not match to_account currency {to_account['currency']}.")

    # Calculate universal projections
    new_from_balance = from_acc_state["ledger_balance"] - dec_from_amount
    new_to_balance = to_acc_state["ledger_balance"] + dec_to_amount

    tx_id = transaction_id or uuid4()
    tx_dict = {
        "id": tx_id,
        "household_id": household_id,
        "transaction_type": "transfer",
        "occurred_on": occurred_on,
        "occurred_at": occurred_at,
        "posted_on": None,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "original_amount": dec_from_amount,
        "original_currency": from_curr,
        "from_amount": dec_from_amount,
        "from_currency": from_curr,
        "to_amount": dec_to_amount,
        "to_currency": to_curr,
        "effective_fx_rate": fx_rate,
        "account_leg_status": "authoritative",
        "reporting_amount": None,
        "reporting_currency": None,
        "reporting_fx_rate": None,
        "reporting_fx_locked_at": None,
        "category_id": None,
        "merchant": None,
        "merchant_normalized": None,
        "remarks": remarks,
        "source": source,
        "status": "committed",
        "verification_status": "unverified",
        "confidence": None,
        "source_request_id": source_request_id,
        "statement_batch_id": None,
        "created_by_user_id": created_by_user_id,
        "created_by_device_id": created_by_device_id,
        "row_version": 0,
        "deleted_at": None,
        "deleted_by_user_id": None,
        "delete_reason": None
    }
    tx_repo.insert_transaction(conn, tx_dict)

    tx_time = occurred_at or datetime.now(timezone.utc)
    accounts_repo.update_account_state_projection(conn, from_account_id, new_from_balance, last_transaction_at=tx_time)
    accounts_repo.update_account_state_projection(conn, to_account_id, new_to_balance, last_transaction_at=tx_time)

    actor_type = "device" if created_by_device_id else ("user" if created_by_user_id else "system")
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type=actor_type,
        entity_type="transaction",
        entity_id=tx_id,
        action="create",
        actor_user_id=created_by_user_id,
        actor_device_id=created_by_device_id,
        request_id=source_request_id,
        after_data={
            "transaction_type": "transfer",
            "from_account_id": str(from_account_id),
            "to_account_id": str(to_account_id),
            "from_amount": str(dec_from_amount),
            "from_currency": from_curr,
            "to_amount": str(dec_to_amount),
            "to_currency": to_curr,
            "effective_fx_rate": str(fx_rate),
            "occurred_on": str(occurred_on)
        }
    )

    # If transfer includes a fee, create 1 separate fee transaction atomically
    fee_tx = None
    if fee_amount is not None:
        fee_curr = fee_currency or from_curr
        if not fee_category_id:
            raise domain_tx.CategoryMismatchError("Transfer fee requires an expense category.")
        fee_tx = record_fee(
            conn=conn,
            household_id=household_id,
            from_account_id=from_account_id,
            amount=fee_amount,
            currency=fee_curr,
            category_id=fee_category_id,
            occurred_on=occurred_on,
            occurred_at=occurred_at,
            merchant=None,
            remarks=f"Fee for transfer {tx_id}",
            source=source,
            created_by_user_id=created_by_user_id,
            created_by_device_id=created_by_device_id,
            source_request_id=source_request_id
        )

    res = tx_repo.get_transaction(conn, tx_id)
    if fee_tx:
        res["fee_transaction"] = fee_tx
    return res


# --- 5. Refund ---

def record_refund(
    conn,
    household_id: UUID,
    original_expense_id: UUID,
    to_account_id: UUID,
    amount: Union[str, int, Decimal],
    currency: str,
    occurred_on: date,
    occurred_at: Optional[datetime] = None,
    category_id: Optional[UUID] = None,
    merchant: Optional[str] = None,
    remarks: Optional[str] = None,
    source: str = "system",
    created_by_user_id: Optional[UUID] = None,
    created_by_device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    transaction_id: Optional[UUID] = None
) -> Dict[str, Any]:
    if occurred_on is None or not isinstance(occurred_on, date):
        raise domain_tx.InvalidTransactionShapeError("occurred_on is a required business date.")

    curr = validate_currency_code(currency)
    dec_amount = quantize_money(parse_decimal(amount), curr)
    if dec_amount <= 0:
        raise domain_tx.InvalidAmountError(f"Refund amount must be strictly positive. Given: {amount}")

    # 1. Lock & validate original expense transaction FOR UPDATE first (Lock order: transaction -> account_state)
    orig_tx = tx_repo.lock_transaction(conn, original_expense_id)
    if not orig_tx:
        raise domain_tx.TransactionNotFoundError(original_expense_id)
    if orig_tx["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Original transaction does not belong to household {household_id}.")
    if orig_tx["transaction_type"] != "expense":
        raise domain_tx.InvalidTransactionShapeError(f"Refund target must be an expense transaction. Given: {orig_tx['transaction_type']}.")
    if orig_tx["status"] != "committed":
        raise domain_tx.TransactionAlreadyVoidedError(original_expense_id)

    # 2. Determine authoritative refundable leg currency and enforce currency safety
    orig_refundable_currency = orig_tx["from_currency"] or orig_tx["original_currency"]
    if curr != orig_refundable_currency:
        raise domain_tx.CurrencyMismatchError(
            f"Refund currency {curr} does not match original expense currency {orig_refundable_currency}."
        )

    # 3. Lock affected refund destination account_state
    locked_states = accounts_repo.lock_account_states(conn, [to_account_id])
    if to_account_id not in locked_states:
        raise domain_tx.AccountNotFoundError(to_account_id)
    to_acc_state = locked_states[to_account_id]

    to_account = accounts_repo.get_account(conn, to_account_id)
    if not to_account:
        raise domain_tx.AccountNotFoundError(to_account_id)
    if to_account["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Account {to_account_id} does not belong to household {household_id}.")
    if to_account["status"] != "active":
        raise domain_tx.AccountInactiveError(to_account_id)
    if to_account["currency"] != curr:
        raise domain_tx.CurrencyMismatchError(
            f"Refund currency {curr} does not match destination account currency {to_account['currency']}."
        )

    # 4. Validate category metadata (inherit original expense category if omitted; strictly validate if explicitly provided)
    if category_id is None:
        final_category_id = orig_tx["category_id"]
    else:
        category = accounts_repo.get_category(conn, category_id)
        if not category:
            raise domain_tx.CategoryNotFoundError(category_id)
        if category["household_id"] != household_id:
            raise domain_tx.HouseholdMismatchError(f"Category {category_id} does not belong to household {household_id}.")
        if category["category_type"] != "expense":
            raise domain_tx.CategoryMismatchError(f"Refund transaction requires an expense-type category. Given: {category['category_type']}.")
        if category["status"] != "active":
            raise domain_tx.CategoryMismatchError(f"Category {category_id} is inactive.")
        final_category_id = category_id

    # 5. Check cumulative non-voided refunds limit in the same currency
    active_refunds = tx_repo.get_active_refunds_for_expense(conn, original_expense_id)
    for r in active_refunds:
        r_curr = r["to_currency"] or r["original_currency"]
        if r_curr != orig_refundable_currency:
            raise domain_tx.CurrencyMismatchError(
                f"Active refund {r['id']} has currency {r_curr} different from original expense {orig_refundable_currency}."
            )
    already_refunded = sum((r["to_amount"] for r in active_refunds), Decimal("0"))
    refundable_limit = orig_tx["from_amount"] if orig_tx["from_amount"] is not None else orig_tx["original_amount"]

    if already_refunded + dec_amount > refundable_limit:
        raise domain_tx.RefundExceedsOriginalError(
            f"Refund amount {dec_amount} exceeds remaining refundable limit {refundable_limit - already_refunded}. "
            f"Original: {refundable_limit}, Already refunded: {already_refunded}"
        )

    # 6. Compute universal signed projection
    new_balance = to_acc_state["ledger_balance"] + dec_amount

    # 7. Insert refund transaction
    refund_tx_id = transaction_id or uuid4()
    tx_dict = {
        "id": refund_tx_id,
        "household_id": household_id,
        "transaction_type": "refund",
        "occurred_on": occurred_on,
        "occurred_at": occurred_at,
        "posted_on": None,
        "from_account_id": None,
        "to_account_id": to_account_id,
        "original_amount": dec_amount,
        "original_currency": curr,
        "from_amount": None,
        "from_currency": None,
        "to_amount": dec_amount,
        "to_currency": curr,
        "effective_fx_rate": None,
        "account_leg_status": "authoritative",
        "reporting_amount": None,
        "reporting_currency": None,
        "reporting_fx_rate": None,
        "reporting_fx_locked_at": None,
        "category_id": final_category_id,
        "merchant": merchant or orig_tx.get("merchant"),
        "merchant_normalized": (merchant or orig_tx.get("merchant") or "").strip().lower() or None,
        "remarks": remarks,
        "source": source,
        "status": "committed",
        "verification_status": "unverified",
        "confidence": None,
        "source_request_id": source_request_id,
        "statement_batch_id": None,
        "created_by_user_id": created_by_user_id,
        "created_by_device_id": created_by_device_id,
        "row_version": 0,
        "deleted_at": None,
        "deleted_by_user_id": None,
        "delete_reason": None
    }
    tx_repo.insert_transaction(conn, tx_dict)

    # 8. Create transaction_links record linking refund to original expense
    link_id = uuid4()
    tx_repo.create_transaction_link(
        conn=conn,
        link_id=link_id,
        source_transaction_id=refund_tx_id,
        target_transaction_id=original_expense_id,
        relation_type="refund_of"
    )

    # 9. Update projection
    tx_time = occurred_at or datetime.now(timezone.utc)
    accounts_repo.update_account_state_projection(conn, to_account_id, new_balance, last_transaction_at=tx_time)

    # 10. Append audit event
    actor_type = "device" if created_by_device_id else ("user" if created_by_user_id else "system")
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type=actor_type,
        entity_type="transaction",
        entity_id=refund_tx_id,
        action="create",
        actor_user_id=created_by_user_id,
        actor_device_id=created_by_device_id,
        request_id=source_request_id,
        after_data={
            "transaction_type": "refund",
            "original_expense_id": str(original_expense_id),
            "to_account_id": str(to_account_id),
            "amount": str(dec_amount),
            "currency": curr,
            "link_id": str(link_id),
            "occurred_on": str(occurred_on)
        }
    )

    return tx_repo.get_transaction(conn, refund_tx_id)


# --- 6. Opening Balance Baseline ---

def record_opening_balance(
    conn,
    household_id: UUID,
    account_id: UUID,
    amount: Union[str, int, Decimal],
    currency: str,
    occurred_on: date,
    is_positive: bool = True,
    occurred_at: Optional[datetime] = None,
    remarks: Optional[str] = None,
    source: str = "system",
    created_by_user_id: Optional[UUID] = None,
    created_by_device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    transaction_id: Optional[UUID] = None
) -> Dict[str, Any]:
    if occurred_on is None or not isinstance(occurred_on, date):
        raise domain_tx.InvalidTransactionShapeError("occurred_on is a required business date.")

    curr = validate_currency_code(currency)
    dec_amount = quantize_money(parse_decimal(amount), curr)
    if dec_amount <= 0:
        raise domain_tx.InvalidAmountError(f"Opening balance amount must be strictly positive. Given: {amount}")

    locked_states = accounts_repo.lock_account_states(conn, [account_id])
    if account_id not in locked_states:
        raise domain_tx.AccountNotFoundError(account_id)
    acc_state = locked_states[account_id]

    account = accounts_repo.get_account(conn, account_id)
    if not account:
        raise domain_tx.AccountNotFoundError(account_id)
    if account["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Account {account_id} does not belong to household {household_id}.")
    if account["status"] != "active":
        raise domain_tx.AccountInactiveError(account_id)
    if account["currency"] != curr:
        raise domain_tx.CurrencyMismatchError(f"Opening balance currency {curr} does not match account currency {account['currency']}.")

    # Positive baseline uses to_account; negative baseline uses from_account
    from_acc_id = None if is_positive else account_id
    to_acc_id = account_id if is_positive else None
    from_amt = None if is_positive else dec_amount
    to_amt = dec_amount if is_positive else None
    from_curr = None if is_positive else curr
    to_curr = curr if is_positive else None

    new_balance = acc_state["ledger_balance"] + (dec_amount if is_positive else -dec_amount)

    tx_id = transaction_id or uuid4()
    tx_dict = {
        "id": tx_id,
        "household_id": household_id,
        "transaction_type": "opening_balance",
        "occurred_on": occurred_on,
        "occurred_at": occurred_at,
        "posted_on": None,
        "from_account_id": from_acc_id,
        "to_account_id": to_acc_id,
        "original_amount": dec_amount,
        "original_currency": curr,
        "from_amount": from_amt,
        "from_currency": from_curr,
        "to_amount": to_amt,
        "to_currency": to_curr,
        "effective_fx_rate": None,
        "account_leg_status": "authoritative",
        "reporting_amount": None,
        "reporting_currency": None,
        "reporting_fx_rate": None,
        "reporting_fx_locked_at": None,
        "category_id": None,
        "merchant": None,
        "merchant_normalized": None,
        "remarks": remarks or "Opening baseline",
        "source": source,
        "status": "committed",
        "verification_status": "unverified",
        "confidence": None,
        "source_request_id": source_request_id,
        "statement_batch_id": None,
        "created_by_user_id": created_by_user_id,
        "created_by_device_id": created_by_device_id,
        "row_version": 0,
        "deleted_at": None,
        "deleted_by_user_id": None,
        "delete_reason": None
    }
    tx_repo.insert_transaction(conn, tx_dict)

    tx_time = occurred_at or datetime.now(timezone.utc)
    # Establish initialized_at
    accounts_repo.update_account_state_projection(
        conn=conn,
        account_id=account_id,
        new_balance=new_balance,
        last_transaction_at=tx_time,
        initialized_at=tx_time
    )

    actor_type = "device" if created_by_device_id else ("user" if created_by_user_id else "system")
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type=actor_type,
        entity_type="transaction",
        entity_id=tx_id,
        action="create",
        actor_user_id=created_by_user_id,
        actor_device_id=created_by_device_id,
        request_id=source_request_id,
        after_data={
            "transaction_type": "opening_balance",
            "account_id": str(account_id),
            "amount": str(dec_amount),
            "is_positive": is_positive,
            "currency": curr,
            "occurred_on": str(occurred_on)
        }
    )

    return tx_repo.get_transaction(conn, tx_id)


# --- 7. Reconciliation Adjustment Primitive ---

def record_reconciliation_adjustment(
    conn,
    household_id: UUID,
    account_id: UUID,
    amount: Union[str, int, Decimal],
    currency: str,
    occurred_on: date,
    is_positive: bool = True,
    occurred_at: Optional[datetime] = None,
    statement_batch_id: Optional[UUID] = None,
    remarks: Optional[str] = None,
    source: str = "reconciliation",
    created_by_user_id: Optional[UUID] = None,
    created_by_device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    transaction_id: Optional[UUID] = None
) -> Dict[str, Any]:
    if occurred_on is None or not isinstance(occurred_on, date):
        raise domain_tx.InvalidTransactionShapeError("occurred_on is a required business date.")

    curr = validate_currency_code(currency)
    dec_amount = quantize_money(parse_decimal(amount), curr)
    if dec_amount <= 0:
        raise domain_tx.InvalidAmountError(f"Adjustment amount must be strictly positive. Given: {amount}")

    locked_states = accounts_repo.lock_account_states(conn, [account_id])
    if account_id not in locked_states:
        raise domain_tx.AccountNotFoundError(account_id)
    acc_state = locked_states[account_id]

    account = accounts_repo.get_account(conn, account_id)
    if not account:
        raise domain_tx.AccountNotFoundError(account_id)
    if account["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Account {account_id} does not belong to household {household_id}.")
    if account["status"] != "active":
        raise domain_tx.AccountInactiveError(account_id)
    if account["currency"] != curr:
        raise domain_tx.CurrencyMismatchError(f"Adjustment currency {curr} does not match account currency {account['currency']}.")

    from_acc_id = None if is_positive else account_id
    to_acc_id = account_id if is_positive else None
    from_amt = None if is_positive else dec_amount
    to_amt = dec_amount if is_positive else None
    from_curr = None if is_positive else curr
    to_curr = curr if is_positive else None

    new_balance = acc_state["ledger_balance"] + (dec_amount if is_positive else -dec_amount)

    tx_id = transaction_id or uuid4()
    tx_dict = {
        "id": tx_id,
        "household_id": household_id,
        "transaction_type": "reconciliation_adjustment",
        "occurred_on": occurred_on,
        "occurred_at": occurred_at,
        "posted_on": None,
        "from_account_id": from_acc_id,
        "to_account_id": to_acc_id,
        "original_amount": dec_amount,
        "original_currency": curr,
        "from_amount": from_amt,
        "from_currency": from_curr,
        "to_amount": to_amt,
        "to_currency": to_curr,
        "effective_fx_rate": None,
        "account_leg_status": "authoritative",
        "reporting_amount": None,
        "reporting_currency": None,
        "reporting_fx_rate": None,
        "reporting_fx_locked_at": None,
        "category_id": None,
        "merchant": None,
        "merchant_normalized": None,
        "remarks": remarks or "Reconciliation adjustment",
        "source": source,
        "status": "committed",
        "verification_status": "unverified",
        "confidence": None,
        "source_request_id": source_request_id,
        "statement_batch_id": statement_batch_id,
        "created_by_user_id": created_by_user_id,
        "created_by_device_id": created_by_device_id,
        "row_version": 0,
        "deleted_at": None,
        "deleted_by_user_id": None,
        "delete_reason": None
    }
    tx_repo.insert_transaction(conn, tx_dict)

    tx_time = occurred_at or datetime.now(timezone.utc)
    accounts_repo.update_account_state_projection(conn, account_id, new_balance, last_transaction_at=tx_time)

    actor_type = "device" if created_by_device_id else ("user" if created_by_user_id else "system")
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type=actor_type,
        entity_type="transaction",
        entity_id=tx_id,
        action="create",
        actor_user_id=created_by_user_id,
        actor_device_id=created_by_device_id,
        request_id=source_request_id,
        after_data={
            "transaction_type": "reconciliation_adjustment",
            "account_id": str(account_id),
            "amount": str(dec_amount),
            "is_positive": is_positive,
            "currency": curr,
            "occurred_on": str(occurred_on)
        }
    )

    return tx_repo.get_transaction(conn, tx_id)


# --- 8. Void / Soft Delete ---

def void_transaction(
    conn,
    household_id: UUID,
    transaction_id: UUID,
    delete_reason: str,
    deleted_by_user_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Atomically voids a committed transaction and reverses its exact original projection effect once.
    """
    if not delete_reason or not delete_reason.strip():
        raise domain_tx.InvalidTransactionShapeError("delete_reason is mandatory for voiding a transaction.")

    # 1. Lock transaction row
    tx = tx_repo.lock_transaction(conn, transaction_id)
    if not tx:
        raise domain_tx.TransactionNotFoundError(transaction_id)
    if tx["household_id"] != household_id:
        raise domain_tx.HouseholdMismatchError(f"Transaction {transaction_id} does not belong to household {household_id}.")
    if tx["status"] == "voided":
        raise domain_tx.TransactionAlreadyVoidedError(transaction_id)

    # 2. Collect affected account IDs and lock them in deterministic sorted UUID order
    affected_account_ids = [
        aid for aid in [tx["from_account_id"], tx["to_account_id"]] if aid is not None
    ]
    locked_states = accounts_repo.lock_account_states(conn, affected_account_ids)

    # 3. Calculate exact reverse projection:
    # Original: from_account -= from_amount  => Reverse: from_account += from_amount
    # Original: to_account += to_amount      => Reverse: to_account -= to_amount
    if tx["from_account_id"] is not None:
        from_state = locked_states[tx["from_account_id"]]
        from_amt = tx["from_amount"] if tx["from_amount"] is not None else tx["original_amount"]
        new_from_balance = from_state["ledger_balance"] + from_amt
        accounts_repo.update_account_state_projection(conn, tx["from_account_id"], new_from_balance)

    if tx["to_account_id"] is not None:
        to_state = locked_states[tx["to_account_id"]]
        to_amt = tx["to_amount"] if tx["to_amount"] is not None else tx["original_amount"]
        new_to_balance = to_state["ledger_balance"] - to_amt
        accounts_repo.update_account_state_projection(conn, tx["to_account_id"], new_to_balance)

    # 4. Mark transaction row voided
    tx_repo.mark_transaction_voided(
        conn=conn,
        transaction_id=transaction_id,
        delete_reason=delete_reason.strip(),
        deleted_by_user_id=deleted_by_user_id
    )

    # 5. Append immutable audit event
    actor_type = "user" if deleted_by_user_id else "system"
    audit_repo.insert_audit_event(
        conn=conn,
        household_id=household_id,
        actor_type=actor_type,
        entity_type="transaction",
        entity_id=transaction_id,
        action="void",
        actor_user_id=deleted_by_user_id,
        before_data={"status": "committed"},
        after_data={
            "status": "voided",
            "delete_reason": delete_reason.strip(),
            "deleted_by_user_id": str(deleted_by_user_id) if deleted_by_user_id else None
        }
    )

    return tx_repo.get_transaction(conn, transaction_id)
