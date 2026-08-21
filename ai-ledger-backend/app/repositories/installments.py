from typing import Optional, Dict, Any, List
from uuid import UUID
from decimal import Decimal
from datetime import date

def create_installment_plan(
    conn,
    plan_id: UUID,
    household_id: UUID,
    credit_account_id: UUID,
    purchase_occurred_on: date,
    original_amount: Decimal,
    original_currency: str,
    account_currency: str,
    total_periods: int,
    merchant: Optional[str] = None,
    account_principal_amount: Optional[Decimal] = None,
    first_statement_month: Optional[date] = None,
    status: str = "pending_first_bill",
    source_request_id: Optional[UUID] = None
) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO installment_plans (
                id, household_id, credit_account_id, purchase_occurred_on, merchant,
                original_amount, original_currency, account_principal_amount, account_currency,
                total_periods, first_statement_month, status, source_request_id,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                now(), now()
            )
            RETURNING id, household_id, credit_account_id, purchase_occurred_on, merchant,
                      original_amount, original_currency, account_principal_amount, account_currency,
                      total_periods, first_statement_month, status, source_request_id,
                      created_at, updated_at;
            """,
            (
                plan_id, household_id, credit_account_id, purchase_occurred_on, merchant,
                original_amount, original_currency, account_principal_amount, account_currency,
                total_periods, first_statement_month, status, source_request_id
            )
        )
        row = cur.fetchone()
        return {
            "id": row[0],
            "household_id": row[1],
            "credit_account_id": row[2],
            "purchase_occurred_on": row[3],
            "merchant": row[4],
            "original_amount": row[5],
            "original_currency": row[6],
            "account_principal_amount": row[7],
            "account_currency": row[8],
            "total_periods": row[9],
            "first_statement_month": row[10],
            "status": row[11],
            "source_request_id": row[12],
            "created_at": row[13],
            "updated_at": row[14]
        }

def create_installment_period(
    conn,
    period_id: UUID,
    plan_id: UUID,
    period_no: int,
    scheduled_amount: Decimal,
    currency: str,
    recognition_month: Optional[date] = None,
    status: str = "scheduled"
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO installment_periods (
                id, plan_id, period_no, recognition_month, scheduled_amount, currency, status,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now());
            """,
            (period_id, plan_id, period_no, recognition_month, scheduled_amount, currency, status)
        )

def get_installment_plan(conn, plan_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, credit_account_id, purchase_occurred_on, merchant,
                   original_amount, original_currency, account_principal_amount, account_currency,
                   total_periods, first_statement_month, status, source_request_id,
                   created_at, updated_at
            FROM installment_plans
            WHERE id = %s;
            """,
            (plan_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "credit_account_id": row[2],
            "purchase_occurred_on": row[3],
            "merchant": row[4],
            "original_amount": row[5],
            "original_currency": row[6],
            "account_principal_amount": row[7],
            "account_currency": row[8],
            "total_periods": row[9],
            "first_statement_month": row[10],
            "status": row[11],
            "source_request_id": row[12],
            "created_at": row[13],
            "updated_at": row[14]
        }

def get_plan_by_source_request_id(conn, source_request_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, credit_account_id, purchase_occurred_on, merchant,
                   original_amount, original_currency, account_principal_amount, account_currency,
                   total_periods, first_statement_month, status, source_request_id,
                   created_at, updated_at
            FROM installment_plans
            WHERE source_request_id = %s;
            """,
            (source_request_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "credit_account_id": row[2],
            "purchase_occurred_on": row[3],
            "merchant": row[4],
            "original_amount": row[5],
            "original_currency": row[6],
            "account_principal_amount": row[7],
            "account_currency": row[8],
            "total_periods": row[9],
            "first_statement_month": row[10],
            "status": row[11],
            "source_request_id": row[12],
            "created_at": row[13],
            "updated_at": row[14]
        }

def list_periods_for_plan(conn, plan_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, plan_id, period_no, recognition_month, scheduled_amount, currency, status,
                   statement_line_id, expense_transaction_id, created_at, updated_at
            FROM installment_periods
            WHERE plan_id = %s
            ORDER BY period_no ASC;
            """,
            (plan_id,)
        )
        rows = cur.fetchall()
        return [{
            "id": r[0],
            "plan_id": r[1],
            "period_no": r[2],
            "recognition_month": r[3],
            "scheduled_amount": r[4],
            "currency": r[5],
            "status": r[6],
            "statement_line_id": r[7],
            "expense_transaction_id": r[8],
            "created_at": r[9],
            "updated_at": r[10]
        } for r in rows]
