from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, Depends

from app.api.deps import get_db_connection, get_authenticated_device
from app.domain.money import quantize_money
from app.domain.transactions import AccountResourceNotFoundError, AccountTypeMismatchError
import app.repositories.accounts as accounts_repo
import app.repositories.credit_cards as credit_cards_repo

router = APIRouter(prefix="/api/v1/credit-cards", tags=["Credit Cards"])

@router.get("/{account_id}/state", summary="Get Credit Card Account State")
def get_credit_card_state(
    account_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves the latest authoritative credit card statement state.
    """
    household_id = device["household_id"]
    acc = accounts_repo.get_account(conn, account_id)
    if not acc or acc["household_id"] != household_id:
        raise AccountResourceNotFoundError(account_id)

    if acc["account_type"] != "credit":
        raise AccountTypeMismatchError(f"Account {account_id} is '{acc['account_type']}', not a credit card account.")

    snap = credit_cards_repo.get_latest_credit_card_snapshot(conn, account_id, household_id)
    curr = acc["currency"]

    if snap:
        latest_snapshot = {
            "as_of": snap["as_of"].isoformat(),
            "statement_balance": f"{quantize_money(snap['statement_balance'], curr):.2f}" if snap.get("statement_balance") is not None else None,
            "remaining_statement_due": f"{quantize_money(snap['remaining_statement_due'], curr):.2f}" if snap.get("remaining_statement_due") is not None else None,
            "unbilled_balance": f"{quantize_money(snap['unbilled_balance'], curr):.2f}" if snap.get("unbilled_balance") is not None else None,
            "current_outstanding": f"{quantize_money(snap['current_outstanding'], curr):.2f}" if snap.get("current_outstanding") is not None else None
        }
    else:
        latest_snapshot = None

    return {
        "account_id": str(account_id),
        "currency": curr,
        "latest_snapshot": latest_snapshot
    }
