from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import date, datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, ConfigDict

from app.api.deps import get_db_connection, get_authenticated_actor
from app.db import transaction
from app.domain.money import quantize_money
from app.domain.transactions import (
    TransactionResourceNotFoundError,
    TransactionNotFoundError,
    TransactionAlreadyVoidedError,
    HouseholdMismatchError,
    RowVersionConflictError
)
import app.repositories.transactions as transactions_repo
import app.services.ledger_service as ledger_service
import app.services.transaction_service as tx_service

router = APIRouter(prefix="/api/v1/transactions", tags=["Transactions"])


class VoidTransactionRequest(BaseModel):
    expected_version: int = Field(..., description="Expected row version for optimistic concurrency (required)")
    delete_reason: str = Field(..., min_length=1, description="Mandatory reason for voiding transaction")


class TransactionCorrectionChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurred_on: Optional[date] = Field(None, description="Corrected occurrence date")
    category_id: Optional[UUID] = Field(None, description="Corrected category UUID")
    merchant: Optional[str] = Field(None, description="Corrected merchant name")
    remarks: Optional[str] = Field(None, description="Corrected remarks")
    from_amount: Optional[Decimal] = Field(None, gt=0, description="Corrected debit amount")
    to_amount: Optional[Decimal] = Field(None, gt=0, description="Corrected credit amount")


class CorrectionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurred_on: Optional[date] = Field(None, description="Corrected occurrence date")
    category_id: Optional[UUID] = Field(None, description="Corrected category UUID")
    merchant: Optional[str] = Field(None, description="Corrected merchant name")
    remarks: Optional[str] = Field(None, description="Corrected remarks")
    from_amount: Optional[Decimal] = Field(None, gt=0, description="Corrected debit amount")
    to_amount: Optional[Decimal] = Field(None, gt=0, description="Corrected credit amount")


class CorrectionCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., description="Expected row version for optimistic concurrency")
    changes: TransactionCorrectionChanges = Field(..., description="Validated fields to correct")
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
    Requires mandatory expected_version for optimistic concurrency.
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

        if tx["row_version"] != payload.expected_version:
            raise RowVersionConflictError("Transaction was modified concurrently. Reload before voiding.")

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
    changes_dict = payload.model_dump(exclude_unset=True)
    return tx_service.preview_transaction_correction(
        conn=conn,
        household_id=device["household_id"],
        transaction_id=transaction_id,
        changes=changes_dict
    )


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
    changes_dict = payload.changes.model_dump(exclude_unset=True)
    with transaction(conn):
        updated_tx = tx_service.commit_transaction_correction(
            conn=conn,
            household_id=device["household_id"],
            transaction_id=transaction_id,
            expected_version=payload.expected_version,
            changes=changes_dict,
            reason=payload.reason,
            actor_user_id=device.get("user_id"),
            actor_device_id=device.get("device_id")
        )
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
