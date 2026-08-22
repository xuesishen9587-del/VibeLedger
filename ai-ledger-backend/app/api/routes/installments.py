from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, Depends

from app.api.deps import get_db_connection, get_authenticated_device
from app.domain.money import quantize_money
from app.domain.transactions import InstallmentPlanResourceNotFoundError
import app.repositories.installments as installments_repo

router = APIRouter(prefix="/api/v1/installments", tags=["Installments"])

@router.get("", summary="List Installment Plans")
def list_installment_plans(
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Lists all installment plans belonging to the authenticated household.
    """
    plans = installments_repo.list_installment_plans(conn, device["household_id"])
    return {
        "items": [
            {
                "id": str(p["id"]),
                "credit_account_id": str(p["credit_account_id"]),
                "purchase_occurred_on": p["purchase_occurred_on"].isoformat(),
                "merchant": p["merchant"],
                "original_amount": f"{quantize_money(p['original_amount'], p['original_currency']):.2f}",
                "original_currency": p["original_currency"],
                "account_principal_amount": f"{quantize_money(p['account_principal_amount'], p['account_currency']):.2f}",
                "account_currency": p["account_currency"],
                "total_periods": p["total_periods"],
                "status": p["status"],
                "created_at": p["created_at"].isoformat() if p.get("created_at") else None
            }
            for p in plans
        ]
    }

@router.get("/{plan_id}", summary="Get Installment Plan Details")
def get_installment_plan(
    plan_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves an installment plan and its scheduled recognition periods.
    Scheduled recognition periods are NOT financial transactions.
    """
    plan = installments_repo.get_installment_plan(conn, plan_id)
    if not plan or plan["household_id"] != device["household_id"]:
        raise InstallmentPlanResourceNotFoundError(plan_id)

    periods = installments_repo.list_periods_for_plan(conn, plan_id)
    return {
        "id": str(plan["id"]),
        "credit_account_id": str(plan["credit_account_id"]),
        "purchase_occurred_on": plan["purchase_occurred_on"].isoformat(),
        "merchant": plan["merchant"],
        "original_amount": f"{quantize_money(plan['original_amount'], plan['original_currency']):.2f}",
        "original_currency": plan["original_currency"],
        "account_principal_amount": f"{quantize_money(plan['account_principal_amount'], plan['account_currency']):.2f}",
        "account_currency": plan["account_currency"],
        "total_periods": plan["total_periods"],
        "status": plan["status"],
        "periods": [
            {
                "period_no": per["period_no"],
                "recognition_month": per["recognition_month"].isoformat() if per.get("recognition_month") else None,
                "scheduled_amount": f"{quantize_money(per['scheduled_amount'], per['currency']):.2f}",
                "currency": per["currency"],
                "status": per["status"],
                "expense_transaction_id": str(per["expense_transaction_id"]) if per.get("expense_transaction_id") else None
            }
            for per in periods
        ]
    }
