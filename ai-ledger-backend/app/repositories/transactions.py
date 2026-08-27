from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID
from datetime import date, datetime
from decimal import Decimal
from app.domain.transactions import InvalidCursorError, LedgerDomainError

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
                "account_leg_status": tx.get("account_leg_status", "authoritative"),
                "reporting_amount": tx.get("reporting_amount"),
                "reporting_currency": tx.get("reporting_currency"),
                "reporting_fx_rate": tx.get("reporting_fx_rate"),
                "reporting_fx_locked_at": tx.get("reporting_fx_locked_at"),
                "category_id": tx.get("category_id"),
                "merchant": tx.get("merchant"),
                "merchant_normalized": tx.get("merchant_normalized") or (tx["merchant"].strip().lower() if tx.get("merchant") else None),
                "remarks": tx.get("remarks"),
                "source": tx.get("source", "shortcut"),
                "status": tx.get("status", "committed"),
                "verification_status": tx.get("verification_status", "unconfirmed"),
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

def create_transaction(
    conn,
    tx_id: UUID,
    household_id: UUID,
    transaction_type: str,
    occurred_on: date,
    original_amount: Decimal,
    original_currency: str,
    from_amount: Optional[Decimal] = None,
    from_currency: Optional[str] = None,
    to_amount: Optional[Decimal] = None,
    to_currency: Optional[str] = None,
    from_account_id: Optional[UUID] = None,
    to_account_id: Optional[UUID] = None,
    category_id: Optional[UUID] = None,
    merchant: Optional[str] = None,
    effective_fx_rate: Optional[Decimal] = None,
    account_leg_status: Optional[str] = "authoritative",
    reporting_amount: Optional[Decimal] = None,
    reporting_currency: Optional[str] = None,
    reporting_fx_rate: Optional[Decimal] = None,
    reporting_fx_locked_at: Optional[datetime] = None,
    source: str = "shortcut",
    status: str = "committed",
    verification_status: str = "unverified",
    statement_batch_id: Optional[UUID] = None,
    posted_on: Optional[date] = None,
    remarks: Optional[str] = None
) -> None:

    insert_transaction(conn, {
        "id": tx_id,
        "household_id": household_id,
        "transaction_type": transaction_type,
        "occurred_on": occurred_on,
        "posted_on": posted_on,
        "original_amount": original_amount,
        "original_currency": original_currency,
        "from_amount": from_amount,
        "from_currency": from_currency,
        "to_amount": to_amount,
        "to_currency": to_currency,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "category_id": category_id,
        "merchant": merchant,
        "remarks": remarks,
        "effective_fx_rate": effective_fx_rate,
        "account_leg_status": account_leg_status,
        "reporting_amount": reporting_amount,
        "reporting_currency": reporting_currency,
        "reporting_fx_rate": reporting_fx_rate,
        "reporting_fx_locked_at": reporting_fx_locked_at,
        "source": source,
        "status": status,
        "verification_status": verification_status,
        "statement_batch_id": statement_batch_id
    })


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

get_links_for_transaction = list_transaction_links_for_source

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

def update_transaction_statement_confirmed(
    conn,
    transaction_id: UUID,
    posted_on: Optional[date] = None,
    account_leg_status: Optional[str] = None,
    from_amount: Optional[Decimal] = None,
    from_currency: Optional[str] = None,
    to_amount: Optional[Decimal] = None,
    to_currency: Optional[str] = None,
    reporting_amount: Optional[Decimal] = None,
    reporting_currency: Optional[str] = None,
    reporting_fx_rate: Optional[Decimal] = None,
    reporting_fx_locked_at: Optional[datetime] = None,
    statement_batch_id: Optional[UUID] = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transactions
            SET verification_status = 'statement_confirmed',
                posted_on = COALESCE(%s, posted_on),
                account_leg_status = COALESCE(%s, account_leg_status),
                from_amount = COALESCE(%s, from_amount),
                from_currency = COALESCE(%s, from_currency),
                to_amount = COALESCE(%s, to_amount),
                to_currency = COALESCE(%s, to_currency),
                reporting_amount = COALESCE(%s, reporting_amount),
                reporting_currency = COALESCE(%s, reporting_currency),
                reporting_fx_rate = COALESCE(%s, reporting_fx_rate),
                reporting_fx_locked_at = COALESCE(%s, reporting_fx_locked_at),
                statement_batch_id = COALESCE(%s, statement_batch_id),
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %s;
            """,
            (
                posted_on, account_leg_status, from_amount, from_currency,
                to_amount, to_currency, reporting_amount, reporting_currency,
                reporting_fx_rate, reporting_fx_locked_at, statement_batch_id,
                transaction_id
            )
        )


import base64

def _encode_cursor(occurred_on: date, created_at: datetime, tx_id: UUID) -> str:
    raw = f"{occurred_on.isoformat()}|{created_at.isoformat()}|{str(tx_id)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")

def _decode_cursor(cursor_str: str) -> Tuple[date, datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode("utf-8")).decode("utf-8")
        parts = raw.split("|")
        if len(parts) != 3:
            raise InvalidCursorError()
        return date.fromisoformat(parts[0]), datetime.fromisoformat(parts[1]), UUID(parts[2])
    except LedgerDomainError:
        raise
    except Exception:
        raise InvalidCursorError()


def list_transactions_with_filters(
    conn,
    household_id: UUID,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    account_id: Optional[UUID] = None,
    transaction_type: Optional[str] = None,
    category_id: Optional[UUID] = None,
    currency: Optional[str] = None,
    verification_status: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Lists transactions with rich filters and replay-safe deterministic cursor pagination.
    Sort order: occurred_on DESC, created_at DESC, id DESC.
    """
    clamped_limit = max(1, min(limit, 200))
    query_limit = clamped_limit + 1

    query = """
        SELECT t.id, t.household_id, t.transaction_type, t.occurred_on, t.occurred_at, t.posted_on,
               t.from_account_id, fa.name AS from_account_name,
               t.to_account_id, ta.name AS to_account_name,
               t.original_amount, t.original_currency,
               t.from_amount, t.from_currency, t.to_amount, t.to_currency, t.effective_fx_rate,
               t.account_leg_status, t.reporting_amount, t.reporting_currency, t.reporting_fx_rate,
               t.category_id, c.name AS category_name,
               t.merchant, t.remarks,
               t.source, t.status, t.verification_status, t.confidence,
               t.created_at, t.updated_at, t.deleted_at
        FROM transactions t
        LEFT JOIN accounts fa ON fa.id = t.from_account_id
        LEFT JOIN accounts ta ON ta.id = t.to_account_id
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.household_id = %(household_id)s
    """
    params: Dict[str, Any] = {"household_id": household_id}

    if from_date is not None:
        query += " AND t.occurred_on >= %(from_date)s"
        params["from_date"] = from_date
    if to_date is not None:
        query += " AND t.occurred_on <= %(to_date)s"
        params["to_date"] = to_date
    if account_id is not None:
        query += " AND (t.from_account_id = %(account_id)s OR t.to_account_id = %(account_id)s)"
        params["account_id"] = account_id
    if transaction_type is not None:
        query += " AND t.transaction_type = %(transaction_type)s"
        params["transaction_type"] = transaction_type
    if category_id is not None:
        query += " AND t.category_id = %(category_id)s"
        params["category_id"] = category_id
    if currency is not None:
        query += " AND (t.original_currency = %(currency)s OR t.from_currency = %(currency)s OR t.to_currency = %(currency)s)"
        params["currency"] = currency
    if verification_status is not None:
        query += " AND t.verification_status = %(verification_status)s"
        params["verification_status"] = verification_status

    if cursor is not None:
        c_occurred_on, c_created_at, c_id = _decode_cursor(cursor)
        query += """
            AND (
                t.occurred_on < %(c_occurred_on)s
                OR (t.occurred_on = %(c_occurred_on)s AND t.created_at < %(c_created_at)s)
                OR (t.occurred_on = %(c_occurred_on)s AND t.created_at = %(c_created_at)s AND t.id < %(c_id)s)
            )
        """
        params["c_occurred_on"] = c_occurred_on
        params["c_created_at"] = c_created_at
        params["c_id"] = c_id

    query += " ORDER BY t.occurred_on DESC, t.created_at DESC, t.id DESC LIMIT %(query_limit)s;"
    params["query_limit"] = query_limit

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    has_more = len(rows) > clamped_limit
    effective_rows = rows[:clamped_limit]

    items = []
    for r in effective_rows:
        from_acc = {"id": str(r[6]), "name": r[7]} if r[6] else None
        to_acc = {"id": str(r[8]), "name": r[9]} if r[8] else None
        cat = {"id": str(r[21]), "name": r[22]} if r[21] else None

        items.append({
            "id": r[0],
            "household_id": r[1],
            "transaction_type": r[2],
            "occurred_on": r[3],
            "occurred_at": r[4],
            "posted_on": r[5],
            "from_account": from_acc,
            "to_account": to_acc,
            "original_amount": r[10],
            "original_currency": r[11],
            "from_amount": r[12],
            "from_currency": r[13],
            "to_amount": r[14],
            "to_currency": r[15],
            "effective_fx_rate": r[16],
            "account_leg_status": r[17],
            "reporting_amount": r[18],
            "reporting_currency": r[19],
            "reporting_fx_rate": r[20],
            "category": cat,
            "merchant": r[23],
            "remarks": r[24],
            "source": r[25],
            "status": r[26],
            "verification_status": r[27],
            "confidence": r[28],
            "created_at": r[29],
            "updated_at": r[30],
            "deleted_at": r[31]
        })

    next_cursor = None
    if has_more and effective_rows:
        last_row = effective_rows[-1]
        next_cursor = _encode_cursor(last_row[3], last_row[29], last_row[0])

    return items, next_cursor

def get_transaction_detail(conn, transaction_id: UUID, household_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    query = """
        SELECT t.id, t.household_id, t.transaction_type, t.occurred_on, t.occurred_at, t.posted_on,
               t.from_account_id, fa.name AS from_account_name,
               t.to_account_id, ta.name AS to_account_name,
               t.original_amount, t.original_currency,
               t.from_amount, t.from_currency, t.to_amount, t.to_currency, t.effective_fx_rate,
               t.account_leg_status, t.reporting_amount, t.reporting_currency, t.reporting_fx_rate,
               t.category_id, c.name AS category_name,
               t.merchant, t.remarks,
               t.source, t.status, t.verification_status, t.confidence,
               t.source_request_id, t.statement_batch_id,
               t.created_at, t.updated_at, t.deleted_at, t.row_version
        FROM transactions t
        LEFT JOIN accounts fa ON fa.id = t.from_account_id
        LEFT JOIN accounts ta ON ta.id = t.to_account_id
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.id = %s
    """
    params = [transaction_id]
    if household_id is not None:
        query += " AND t.household_id = %s"
        params.append(household_id)

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        r = cur.fetchone()
        if not r:
            return None

        from_acc = {"id": str(r[6]), "name": r[7]} if r[6] else None
        to_acc = {"id": str(r[8]), "name": r[9]} if r[8] else None
        cat = {"id": str(r[21]), "name": r[22]} if r[21] else None

        # Fetch links
        cur.execute(
            """
            SELECT id, source_transaction_id, target_transaction_id, relation_type, created_at
            FROM transaction_links
            WHERE source_transaction_id = %s OR target_transaction_id = %s;
            """,
            (transaction_id, transaction_id)
        )
        link_rows = cur.fetchall()
        links = [{
            "id": lr[0],
            "source_transaction_id": lr[1],
            "target_transaction_id": lr[2],
            "relation_type": lr[3],
            "created_at": lr[4]
        } for lr in link_rows]

        return {
            "id": r[0],
            "household_id": r[1],
            "transaction_type": r[2],
            "occurred_on": r[3],
            "occurred_at": r[4],
            "posted_on": r[5],
            "from_account": from_acc,
            "to_account": to_acc,
            "original_amount": r[10],
            "original_currency": r[11],
            "from_amount": r[12],
            "from_currency": r[13],
            "to_amount": r[14],
            "to_currency": r[15],
            "effective_fx_rate": r[16],
            "account_leg_status": r[17],
            "reporting_amount": r[18],
            "reporting_currency": r[19],
            "reporting_fx_rate": r[20],
            "category": cat,
            "merchant": r[23],
            "remarks": r[24],
            "source": r[25],
            "status": r[26],
            "verification_status": r[27],
            "confidence": r[28],
            "source_request_id": r[29],
            "statement_batch_id": r[30],
            "created_at": r[31],
            "updated_at": r[32],
            "deleted_at": r[33],
            "row_version": r[34],
            "links": links
        }

def update_transaction_fields(
    conn,
    transaction_id: UUID,
    occurred_on: Optional[date] = None,
    category_id: Optional[UUID] = None,
    merchant: Optional[str] = None,
    merchant_normalized: Optional[str] = None,
    remarks: Optional[str] = None,
    from_amount: Optional[Decimal] = None,
    to_amount: Optional[Decimal] = None,
    reporting_amount: Optional[Decimal] = None
) -> None:
    """
    Updates the specified fields on a transaction and increments row_version.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transactions
            SET occurred_on = COALESCE(%(occurred_on)s, occurred_on),
                category_id = COALESCE(%(category_id)s, category_id),
                merchant = COALESCE(%(merchant)s, merchant),
                merchant_normalized = COALESCE(%(merchant_normalized)s, merchant_normalized),
                remarks = COALESCE(%(remarks)s, remarks),
                from_amount = COALESCE(%(from_amount)s, from_amount),
                to_amount = COALESCE(%(to_amount)s, to_amount),
                reporting_amount = COALESCE(%(reporting_amount)s, reporting_amount),
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %(transaction_id)s;
            """,
            {
                "occurred_on": occurred_on,
                "category_id": category_id,
                "merchant": merchant,
                "merchant_normalized": merchant_normalized,
                "remarks": remarks,
                "from_amount": from_amount,
                "to_amount": to_amount,
                "reporting_amount": reporting_amount,
                "transaction_id": transaction_id
            }
        )

