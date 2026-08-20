from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import date, datetime
from decimal import Decimal

def insert_transaction(conn, tx: Dict[str, Any]) -> None:
    """
    Inserts a committed financial transaction into the transactions table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (
                id, household_id, transaction_type, occurred_on, occurred_at, posted_on,
                from_account_id, to_account_id, original_amount, original_currency,
                from_amount, from_currency, to_amount, to_currency, effective_fx_rate,
                account_leg_status, reporting_amount, reporting_currency, reporting_fx_rate, reporting_fx_locked_at,
                category_id, merchant, merchant_normalized, remarks,
                source, status, verification_status, confidence,
                source_request_id, statement_batch_id, created_by_user_id, created_by_device_id,
                row_version, deleted_at, deleted_by_user_id, delete_reason
            ) VALUES (
                %(id)s, %(household_id)s, %(transaction_type)s, %(occurred_on)s, %(occurred_at)s, %(posted_on)s,
                %(from_account_id)s, %(to_account_id)s, %(original_amount)s, %(original_currency)s,
                %(from_amount)s, %(from_currency)s, %(to_amount)s, %(to_currency)s, %(effective_fx_rate)s,
                %(account_leg_status)s, %(reporting_amount)s, %(reporting_currency)s, %(reporting_fx_rate)s, %(reporting_fx_locked_at)s,
                %(category_id)s, %(merchant)s, %(merchant_normalized)s, %(remarks)s,
                %(source)s, %(status)s, %(verification_status)s, %(confidence)s,
                %(source_request_id)s, %(statement_batch_id)s, %(created_by_user_id)s, %(created_by_device_id)s,
                %(row_version)s, %(deleted_at)s, %(deleted_by_user_id)s, %(delete_reason)s
            );
            """,
            {
                "id": tx.get("id"),
                "household_id": tx.get("household_id"),
                "transaction_type": tx.get("transaction_type"),
                "occurred_on": tx.get("occurred_on"),
                "occurred_at": tx.get("occurred_at"),
                "posted_on": tx.get("posted_on"),
                "from_account_id": tx.get("from_account_id"),
                "to_account_id": tx.get("to_account_id"),
                "original_amount": tx.get("original_amount"),
                "original_currency": tx.get("original_currency"),
                "from_amount": tx.get("from_amount"),
                "from_currency": tx.get("from_currency"),
                "to_amount": tx.get("to_amount"),
                "to_currency": tx.get("to_currency"),
                "effective_fx_rate": tx.get("effective_fx_rate"),
                "account_leg_status": tx.get("account_leg_status"),
                "reporting_amount": tx.get("reporting_amount"),
                "reporting_currency": tx.get("reporting_currency"),
                "reporting_fx_rate": tx.get("reporting_fx_rate"),
                "reporting_fx_locked_at": tx.get("reporting_fx_locked_at"),
                "category_id": tx.get("category_id"),
                "merchant": tx.get("merchant"),
                "merchant_normalized": tx.get("merchant_normalized"),
                "remarks": tx.get("remarks"),
                "source": tx.get("source", "system"),
                "status": tx.get("status", "committed"),
                "verification_status": tx.get("verification_status", "unverified"),
                "confidence": tx.get("confidence"),
                "source_request_id": tx.get("source_request_id"),
                "statement_batch_id": tx.get("statement_batch_id"),
                "created_by_user_id": tx.get("created_by_user_id"),
                "created_by_device_id": tx.get("created_by_device_id"),
                "row_version": tx.get("row_version", 0),
                "deleted_at": tx.get("deleted_at"),
                "deleted_by_user_id": tx.get("deleted_by_user_id"),
                "delete_reason": tx.get("delete_reason")
            }
        )

def _map_transaction_row(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "household_id": row[1],
        "transaction_type": row[2],
        "occurred_on": row[3],
        "occurred_at": row[4],
        "posted_on": row[5],
        "from_account_id": row[6],
        "to_account_id": row[7],
        "original_amount": row[8],
        "original_currency": row[9],
        "from_amount": row[10],
        "from_currency": row[11],
        "to_amount": row[12],
        "to_currency": row[13],
        "effective_fx_rate": row[14],
        "account_leg_status": row[15],
        "reporting_amount": row[16],
        "reporting_currency": row[17],
        "reporting_fx_rate": row[18],
        "reporting_fx_locked_at": row[19],
        "category_id": row[20],
        "merchant": row[21],
        "merchant_normalized": row[22],
        "remarks": row[23],
        "source": row[24],
        "status": row[25],
        "verification_status": row[26],
        "confidence": row[27],
        "source_request_id": row[28],
        "statement_batch_id": row[29],
        "created_by_user_id": row[30],
        "created_by_device_id": row[31],
        "row_version": row[32],
        "created_at": row[33],
        "updated_at": row[34],
        "deleted_at": row[35],
        "deleted_by_user_id": row[36],
        "delete_reason": row[37]
    }

TX_TABLE_COLUMNS = """
    t.id, t.household_id, t.transaction_type, t.occurred_on, t.occurred_at, t.posted_on,
    t.from_account_id, t.to_account_id, t.original_amount, t.original_currency,
    t.from_amount, t.from_currency, t.to_amount, t.to_currency, t.effective_fx_rate,
    t.account_leg_status, t.reporting_amount, t.reporting_currency, t.reporting_fx_rate, t.reporting_fx_locked_at,
    t.category_id, t.merchant, t.merchant_normalized, t.remarks,
    t.source, t.status, t.verification_status, t.confidence,
    t.source_request_id, t.statement_batch_id, t.created_by_user_id, t.created_by_device_id,
    t.row_version, t.created_at, t.updated_at, t.deleted_at, t.deleted_by_user_id, t.delete_reason
"""

def get_transaction(conn, transaction_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {TX_TABLE_COLUMNS} FROM transactions t WHERE t.id = %s;",
            (transaction_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_transaction_row(row)

def lock_transaction(conn, transaction_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Locks and retrieves a transaction row (SELECT ... FOR UPDATE).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {TX_TABLE_COLUMNS} FROM transactions t WHERE t.id = %s FOR UPDATE;",
            (transaction_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_transaction_row(row)

def create_transaction_link(
    conn,
    link_id: UUID,
    source_transaction_id: UUID,
    target_transaction_id: UUID,
    relation_type: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transaction_links (id, source_transaction_id, target_transaction_id, relation_type)
            VALUES (%s, %s, %s, %s);
            """,
            (link_id, source_transaction_id, target_transaction_id, relation_type)
        )

def list_transaction_links_for_source(conn, source_transaction_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_transaction_id, target_transaction_id, relation_type, created_at
            FROM transaction_links
            WHERE source_transaction_id = %s;
            """,
            (source_transaction_id,)
        )
        rows = cur.fetchall()
        return [{
            "id": r[0],
            "source_transaction_id": r[1],
            "target_transaction_id": r[2],
            "relation_type": r[3],
            "created_at": r[4]
        } for r in rows]

def get_active_refunds_for_expense(conn, expense_transaction_id: UUID) -> List[Dict[str, Any]]:
    """
    Retrieves all committed, non-voided refund transactions linked to the given expense.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {TX_TABLE_COLUMNS}
            FROM transactions t
            JOIN transaction_links tl ON tl.source_transaction_id = t.id
            WHERE tl.target_transaction_id = %s
              AND tl.relation_type = 'refund_of'
              AND t.status = 'committed'
              AND t.deleted_at IS NULL;
            """,
            (expense_transaction_id,)
        )
        rows = cur.fetchall()
        return [_map_transaction_row(r) for r in rows]

def mark_transaction_voided(
    conn,
    transaction_id: UUID,
    delete_reason: str,
    deleted_by_user_id: Optional[UUID] = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transactions
            SET status = 'voided',
                deleted_at = now(),
                delete_reason = %s,
                deleted_by_user_id = %s,
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %s;
            """,
            (delete_reason, deleted_by_user_id, transaction_id)
        )
