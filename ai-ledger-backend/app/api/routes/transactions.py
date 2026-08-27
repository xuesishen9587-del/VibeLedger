from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import date, datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_actor
from app.db import transaction
from app.domain.money import quantize_money, parse_decimal
from app.domain.transactions import (
    TransactionResourceNotFoundError,
    TransactionNotFoundError,
    TransactionAlreadyVoidedError,
    HouseholdMismatchError,
    RowVersionConflictError,
    InvalidTransactionShapeError,
    CategoryNotFoundError,
    CategoryMismatchError
)
import app.repositories.transactions as transactions_repo
import app.repositories.accounts as accounts_repo
import app.repositories.categories as categories_repo
import app.repositories.audit as audit_repo
import app.services.ledger_service as ledger_service

router = APIRouter(prefix="/api/v1/transactions", tags=["Transactions"])


class VoidTransactionRequest(BaseModel):
    expected_version: Optional[int] = Field(None, description="Expected row version for optimistic concurrency")
    delete_reason: str = Field(..., min_length=1, description="Mandatory reason for voiding transaction")


class CorrectionPreviewRequest(BaseModel):
    occurred_on: Optional[date] = Field(None, description="Corrected occurrence date")
    category_id: Optional[UUID] = Field(None, description="Corrected category UUID")
    merchant: Optional[str] = Field(None, description="Corrected merchant name")
    remarks: Optional[str] = Field(None, description="Corrected remarks")
    from_amount: Optional[Decimal] = Field(None, gt=0, description="Corrected debit amount")
    to_amount: Optional[Decimal] = Field(None, gt=0, description="Corrected credit amount")


class CorrectionCommitRequest(BaseModel):
    expected_version: int = Field(..., description="Expected row version for optimistic concurrency")
    changes: Dict[str, Any] = Field(..., description="Dictionary of fields to correct")
    reason: Optional[str] = Field(None, description="Optional explanation for correction")


class RefundCreateRequest(BaseModel):
    occurred_on: date = Field(..., description="Refund transaction date")
    amount: Decimal = Field(..., gt=0, description="Refund amount in refund currency")
    currency: str = Field(..., min_length=3, max_length=3, description="Refund currency code")
    to_account_id: UUID = Field(..., description="Account receiving the refund")
    remarks: Optional[str] = Field(None, description="Optional refund remarks")


def _format_transaction(tx: Dict[str, Any]) -> Dict[str, Any]:
    orig_curr = tx["original_currency"]
    from_curr = tx.get("from_currency")
    to_curr = tx.get("to_currency")
    rep_curr = tx.get("reporting_currency")

    from_amt = tx.get("from_amount")
    to_amt = tx.get("to_amount")
    orig_amt = tx.get("original_amount")
    rep_amt = tx.get("reporting_amount")
    fx_rate = tx.get("effective_fx_rate")

    formatted = {
        "id": str(tx["id"]),
        "transaction_type": tx["transaction_type"],
        "occurred_on": tx["occurred_on"].isoformat() if tx.get("occurred_on") else None,
        "posted_on": tx["posted_on"].isoformat() if tx.get("posted_on") else None,
        "merchant": tx.get("merchant"),
        "remarks": tx.get("remarks"),
        "original_amount": f"{quantize_money(orig_amt, orig_curr):.2f}" if orig_amt is not None else None,
        "original_currency": orig_curr,
        "from_amount": f"{quantize_money(from_amt, from_curr):.2f}" if from_amt is not None and from_curr else (f"{from_amt:.2f}" if from_amt is not None else None),
        "from_currency": from_curr,
        "to_amount": f"{quantize_money(to_amt, to_curr):.2f}" if to_amt is not None and to_curr else (f"{to_amt:.2f}" if to_amt is not None else None),
        "to_currency": to_curr,
        "effective_fx_rate": f"{fx_rate:.12f}" if fx_rate is not None else None,
        "account_leg_status": tx.get("account_leg_status"),
        "from_account": tx.get("from_account"),
        "to_account": tx.get("to_account"),
        "category": tx.get("category"),
        "verification_status": tx.get("verification_status"),
        "status": tx.get("status"),
        "row_version": tx.get("row_version", 0),
        "reporting_amount": f"{quantize_money(rep_amt, rep_curr):.2f}" if rep_amt is not None and rep_curr else (f"{rep_amt:.2f}" if rep_amt is not None else None),
        "reporting_currency": rep_curr,
        "created_at": tx["created_at"].isoformat() if tx.get("created_at") else None,
        "deleted_at": tx["deleted_at"].isoformat() if tx.get("deleted_at") else None,
        "delete_reason": tx.get("delete_reason")
    }
    if "links" in tx:
        formatted["links"] = [
            {
                "id": str(l["id"]),
                "source_transaction_id": str(l["source_transaction_id"]),
                "target_transaction_id": str(l["target_transaction_id"]),
                "relation_type": l["relation_type"],
                "created_at": l["created_at"].isoformat() if l.get("created_at") else None
            }
            for l in tx["links"]
        ]
    return formatted


@router.get("", summary="List Transactions (Read-Only)")
def list_transactions(
    from_date: Optional[date] = Query(None, alias="from", description="Start date (YYYY-MM-DD) inclusive"),
    to_date: Optional[date] = Query(None, alias="to", description="End date (YYYY-MM-DD) inclusive"),
    account_id: Optional[UUID] = Query(None, description="Account ID filter (matches from or to account)"),
    transaction_type: Optional[str] = Query(None, description="Transaction type filter"),
    category_id: Optional[UUID] = Query(None, description="Category ID filter"),
    currency: Optional[str] = Query(None, description="Currency filter"),
    verification_status: Optional[str] = Query(None, description="Verification status filter"),
    limit: int = Query(50, ge=1, le=200, description="Items per page (1-200)"),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves transactions for the authenticated household with rich filtering and deterministic cursor pagination.
    """
    items, next_cursor = transactions_repo.list_transactions_with_filters(
        conn=conn,
        household_id=device["household_id"],
        from_date=from_date,
        to_date=to_date,
        account_id=account_id,
        transaction_type=transaction_type,
        category_id=category_id,
        currency=currency,
        verification_status=verification_status,
        limit=limit,
        cursor=cursor
    )
    return {
        "items": [_format_transaction(t) for t in items],
        "next_cursor": next_cursor
    }


@router.get("/{transaction_id}", summary="Get Transaction Details")
def get_transaction(
    transaction_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves detailed transaction metadata including related links and accounts.
    """
    tx = transactions_repo.get_transaction_detail(
        conn=conn,
        transaction_id=transaction_id,
        household_id=device["household_id"]
    )
    if not tx:
        raise TransactionResourceNotFoundError(transaction_id)
    return _format_transaction(tx)


@router.post("/{transaction_id}/void", summary="Void Transaction")
def void_transaction_endpoint(
    transaction_id: UUID,
    payload: VoidTransactionRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Atomically voids a committed transaction and reverses its balance projection.
    """
    household_id = device["household_id"]
    with transaction(conn):
        tx = transactions_repo.lock_transaction(conn, transaction_id)
        if not tx:
            raise TransactionResourceNotFoundError(transaction_id)
        if tx["household_id"] != household_id:
            raise HouseholdMismatchError()
        if tx["status"] == "voided":
            raise TransactionAlreadyVoidedError(transaction_id)

        if payload.expected_version is not None and tx["row_version"] != payload.expected_version:
            raise RowVersionConflictError("Transaction was modified concurrently.")

        voided_tx = ledger_service.void_transaction(
            conn=conn,
            household_id=household_id,
            transaction_id=transaction_id,
            delete_reason=payload.delete_reason,
            deleted_by_user_id=device.get("user_id")
        )

        return {
            "status": "voided",
            "transaction_id": str(transaction_id),
            "deleted_at": voided_tx["deleted_at"].isoformat() if voided_tx.get("deleted_at") else datetime.now().isoformat(),
            "delete_reason": payload.delete_reason,
            "account_balance_restored": True
        }


@router.post("/{transaction_id}/corrections/preview", summary="Preview Transaction Correction")
def preview_transaction_correction_endpoint(
    transaction_id: UUID,
    payload: CorrectionPreviewRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Calculates projected balance impacts and requirements for correcting a transaction. Read-only.
    """
    household_id = device["household_id"]
    tx = transactions_repo.get_transaction(conn, transaction_id)
    if not tx:
        raise TransactionResourceNotFoundError(transaction_id)
    if tx["household_id"] != household_id:
        raise HouseholdMismatchError()
    if tx["status"] == "voided":
        raise TransactionAlreadyVoidedError(transaction_id)

    proposed_changes: Dict[str, Any] = {}
    if payload.occurred_on is not None:
        proposed_changes["occurred_on"] = payload.occurred_on.isoformat()
    if payload.category_id is not None:
        cat = categories_repo.get_category(conn, payload.category_id, household_id)
        if not cat or cat["household_id"] != household_id:
            raise CategoryNotFoundError(payload.category_id)
        proposed_changes["category_id"] = str(payload.category_id)
    if payload.merchant is not None:
        proposed_changes["merchant"] = payload.merchant
    if payload.remarks is not None:
        proposed_changes["remarks"] = payload.remarks
    if payload.from_amount is not None:
        proposed_changes["from_amount"] = f"{payload.from_amount:.2f}"
    if payload.to_amount is not None:
        proposed_changes["to_amount"] = f"{payload.to_amount:.2f}"

    account_state_deltas: List[Dict[str, Any]] = []

    # Calculate delta for from_account if from_amount changed
    if payload.from_amount is not None and tx.get("from_account_id"):
        from_acc = accounts_repo.get_account(conn, tx["from_account_id"])
        state = accounts_repo.get_account_state(conn, tx["from_account_id"])
        curr_bal = Decimal(str(state["ledger_balance"])) if state else Decimal("0.00")
        old_amt = Decimal(str(tx["from_amount"] if tx.get("from_amount") is not None else tx["original_amount"]))
        new_amt = payload.from_amount
        # from_account balance was subtracted by old_amt. If new_amt is bigger, delta is -(new_amt - old_amt) = old_amt - new_amt
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

    # Calculate delta for to_account if to_amount changed
    if payload.to_amount is not None and tx.get("to_account_id"):
        to_acc = accounts_repo.get_account(conn, tx["to_account_id"])
        state = accounts_repo.get_account_state(conn, tx["to_account_id"])
        curr_bal = Decimal(str(state["ledger_balance"])) if state else Decimal("0.00")
        old_amt = Decimal(str(tx["to_amount"] if tx.get("to_amount") is not None else tx["original_amount"]))
        new_amt = payload.to_amount
        # to_account balance was added by old_amt. If new_amt is bigger, delta is +(new_amt - old_amt)
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
        "proposed_changes": proposed_changes,
        "account_state_deltas": account_state_deltas,
        "requires_confirmation": requires_confirmation
    }


@router.post("/{transaction_id}/corrections/commit", summary="Commit Transaction Correction")
def commit_transaction_correction_endpoint(
    transaction_id: UUID,
    payload: CorrectionCommitRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Atomically commits explicit correction to a transaction and reconciles account balances.
    """
    household_id = device["household_id"]
    changes = payload.changes

    with transaction(conn):
        tx = transactions_repo.lock_transaction(conn, transaction_id)
        if not tx:
            raise TransactionResourceNotFoundError(transaction_id)
        if tx["household_id"] != household_id:
            raise HouseholdMismatchError()
        if tx["status"] == "voided":
            raise TransactionAlreadyVoidedError(transaction_id)

        if tx["row_version"] != payload.expected_version:
            raise RowVersionConflictError("Transaction was modified concurrently. Reload before correcting.")

        before_data = {
            "occurred_on": tx["occurred_on"].isoformat() if tx.get("occurred_on") else None,
            "category_id": str(tx["category_id"]) if tx.get("category_id") else None,
            "merchant": tx.get("merchant"),
            "remarks": tx.get("remarks"),
            "from_amount": str(tx["from_amount"]) if tx.get("from_amount") is not None else None,
            "to_amount": str(tx["to_amount"]) if tx.get("to_amount") is not None else None
        }

        # Apply account balance delta if amount changed
        affected_accs: List[UUID] = []
        if "from_amount" in changes and tx.get("from_account_id"):
            affected_accs.append(tx["from_account_id"])
        if "to_amount" in changes and tx.get("to_account_id"):
            affected_accs.append(tx["to_account_id"])

        if affected_accs:
            locked_states = accounts_repo.lock_account_states(conn, sorted(list(set(affected_accs))))
            if "from_amount" in changes and tx.get("from_account_id"):
                old_amt = Decimal(str(tx["from_amount"] if tx.get("from_amount") is not None else tx["original_amount"]))
                new_amt = parse_decimal(changes["from_amount"])
                delta = old_amt - new_amt
                new_bal = locked_states[tx["from_account_id"]]["ledger_balance"] + delta
                accounts_repo.update_account_state_projection(conn, tx["from_account_id"], new_bal)

            if "to_amount" in changes and tx.get("to_account_id"):
                old_amt = Decimal(str(tx["to_amount"] if tx.get("to_amount") is not None else tx["original_amount"]))
                new_amt = parse_decimal(changes["to_amount"])
                delta = new_amt - old_amt
                new_bal = locked_states[tx["to_account_id"]]["ledger_balance"] + delta
                accounts_repo.update_account_state_projection(conn, tx["to_account_id"], new_bal)

        # Update transaction row fields
        new_occurred_on = date.fromisoformat(changes["occurred_on"]) if "occurred_on" in changes and changes["occurred_on"] else tx["occurred_on"]
        new_category_id = UUID(changes["category_id"]) if "category_id" in changes and changes["category_id"] else tx.get("category_id")
        if new_category_id and "category_id" in changes:
            cat = categories_repo.get_category(conn, new_category_id, household_id)
            if not cat or cat["household_id"] != household_id:
                raise CategoryNotFoundError(new_category_id)

        new_merchant = changes["merchant"] if "merchant" in changes else tx.get("merchant")
        new_merchant_norm = new_merchant.strip().lower() if new_merchant else None
        new_remarks = changes["remarks"] if "remarks" in changes else tx.get("remarks")
        new_from_amt = parse_decimal(changes["from_amount"]) if "from_amount" in changes and changes["from_amount"] is not None else tx.get("from_amount")
        new_to_amt = parse_decimal(changes["to_amount"]) if "to_amount" in changes and changes["to_amount"] is not None else tx.get("to_amount")

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE transactions
                SET occurred_on = %s,
                    category_id = %s,
                    merchant = %s,
                    merchant_normalized = %s,
                    remarks = %s,
                    from_amount = %s,
                    to_amount = %s,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE id = %s;
                """,
                (
                    new_occurred_on, new_category_id, new_merchant, new_merchant_norm,
                    new_remarks, new_from_amt, new_to_amt, transaction_id
                )
            )

        # Append audit event
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="user" if device.get("user_id") else "system",
            entity_type="transaction",
            entity_id=transaction_id,
            action="update",
            actor_user_id=device.get("user_id"),
            actor_device_id=device.get("device_id"),
            before_data=before_data,
            after_data={
                "changes": changes,
                "reason": payload.reason
            }
        )

    updated_tx = transactions_repo.get_transaction_detail(conn, transaction_id, household_id)
    return _format_transaction(updated_tx)


@router.post("/{transaction_id}/refunds", summary="Record Refund for Transaction", status_code=status.HTTP_201_CREATED)
def create_refund_endpoint(
    transaction_id: UUID,
    payload: RefundCreateRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Records a refund transaction linked to the original expense transaction.
    """
    household_id = device["household_id"]
    with transaction(conn):
        refund_tx = ledger_service.record_refund(
            conn=conn,
            household_id=household_id,
            original_expense_id=transaction_id,
            occurred_on=payload.occurred_on,
            refund_amount=payload.amount,
            to_account_id=payload.to_account_id,
            currency=payload.currency,
            remarks=payload.remarks,
            created_by_user_id=device.get("user_id"),
            created_by_device_id=device.get("device_id")
        )

    detail = transactions_repo.get_transaction_detail(conn, refund_tx["id"], household_id)
    return _format_transaction(detail)
