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

def list_installment_plans(conn, household_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, credit_account_id, purchase_occurred_on, merchant,
                   original_amount, original_currency, account_principal_amount, account_currency,
                   total_periods, first_statement_month, status, source_request_id,
                   created_at, updated_at
            FROM installment_plans
            WHERE household_id = %s
            ORDER BY purchase_occurred_on DESC, created_at DESC;
            """,
            (household_id,)
        )
        rows = cur.fetchall()
        return [{
            "id": r[0],
            "household_id": r[1],
            "credit_account_id": r[2],
            "purchase_occurred_on": r[3],
            "merchant": r[4],
            "original_amount": r[5],
            "original_currency": r[6],
            "account_principal_amount": r[7],
            "account_currency": r[8],
            "total_periods": r[9],
            "first_statement_month": r[10],
            "status": r[11],
            "source_request_id": r[12],
            "created_at": r[13],
            "updated_at": r[14]
        } for r in rows]

def update_installment_period_billed(
    conn,
    period_id: UUID,
    expense_transaction_id: UUID,
    statement_line_id: Optional[UUID] = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE installment_periods
            SET status = 'billed',
                expense_transaction_id = %s,
                statement_line_id = %s,
                updated_at = now()
            WHERE id = %s;
            """,
            (expense_transaction_id, statement_line_id, period_id)
        )

def update_installment_period_billed_atomic(
    conn,
    period_id: UUID,
    expense_transaction_id: UUID,
    statement_line_id: Optional[UUID] = None
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE installment_periods
            SET status = 'billed',
                expense_transaction_id = %s,
                statement_line_id = %s,
                updated_at = now()
            WHERE id = %s AND status = 'scheduled'
            RETURNING id;
            """,
            (expense_transaction_id, statement_line_id, period_id)
        )
        row = cur.fetchone()
        return row is not None


def update_installment_plan_status(
    conn,
    plan_id: UUID,
    status: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE installment_plans
            SET status = %s,
                updated_at = now()
            WHERE id = %s;
            """,
            (status, plan_id)
        )


def update_installment_plan_first_statement_month_and_status(
    conn,
    plan_id: UUID,
    status: str,
    first_statement_month: date
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE installment_plans
            SET status = %s,
                first_statement_month = COALESCE(first_statement_month, %s),
                updated_at = now()
            WHERE id = %s;
            """,
            (status, first_statement_month, plan_id)
        )


def populate_scheduled_period_recognition_months(
    conn,
    plan_id: UUID,
    first_statement_month: date
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, period_no FROM installment_periods
            WHERE plan_id = %s AND recognition_month IS NULL;
            """,
            (plan_id,)
        )
        periods = cur.fetchall()
        for pid, pno in periods:
            tot_m = first_statement_month.year * 12 + (first_statement_month.month - 1) + (pno - 1)
            rec_y = tot_m // 12
            rec_m = (tot_m % 12) + 1
            rec_date = date(rec_y, rec_m, 1)
            cur.execute(
                """
                UPDATE installment_periods
                SET recognition_month = %s, updated_at = now()
                WHERE id = %s;
                """,
                (rec_date, pid)
            )


def check_and_update_plan_completion(conn, plan_id: UUID) -> bool:
    """
    Checks if all periods for an installment plan are billed (no scheduled periods remain).
    If so, transitions the plan status to 'completed'.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM installment_periods
            WHERE plan_id = %s AND status = 'scheduled';
            """,
            (plan_id,)
        )
        row = cur.fetchone()
        remaining_scheduled = row[0] if row else 0
        if remaining_scheduled == 0:
            cur.execute(
                """
                UPDATE installment_plans
                SET status = 'completed', updated_at = now()
                WHERE id = %s AND status != 'completed';
                """,
                (plan_id,)
            )
            return True
        return False


