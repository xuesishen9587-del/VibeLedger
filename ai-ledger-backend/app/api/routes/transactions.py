from typing import Optional, Dict, Any
from uuid import UUID
from datetime import date
from fastapi import APIRouter, Depends, Query
from decimal import Decimal

from app.api.deps import get_db_connection, get_authenticated_device
from app.domain.money import quantize_money
from app.domain.transactions import TransactionResourceNotFoundError
import app.repositories.transactions as transactions_repo

router = APIRouter(prefix="/api/v1/transactions", tags=["Transactions"])

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
        "reporting_amount": f"{quantize_money(rep_amt, rep_curr):.2f}" if rep_amt is not None and rep_curr else (f"{rep_amt:.2f}" if rep_amt is not None else None),
        "reporting_currency": rep_curr,
        "created_at": tx["created_at"].isoformat() if tx.get("created_at") else None
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
    device: Dict[str, Any] = Depends(get_authenticated_device),
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
    device: Dict[str, Any] = Depends(get_authenticated_device),
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
