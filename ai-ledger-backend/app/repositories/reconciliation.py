import json
from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import datetime, date
from decimal import Decimal

# --- Reconciliation Batches ---

def create_reconciliation_batch(
    conn,
    batch_id: UUID,
    household_id: UUID,
    account_id: UUID,
    batch_type: str,
    status: str,
    currency: str,
    authoritative_balance: Optional[Decimal] = None,
    statement_balance: Optional[Decimal] = None,
    current_outstanding: Optional[Decimal] = None,
    unbilled_balance: Optional[Decimal] = None,
    residual_amount: Optional[Decimal] = None,
    adjustment_amount: Optional[Decimal] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    source_request_id: Optional[UUID] = None,
    created_by_user_id: Optional[UUID] = None,
    row_version: int = 0,
    committed_at: Optional[datetime] = None
) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reconciliation_batches (
                id, household_id, account_id, batch_type, status, currency,
                authoritative_balance, statement_balance, current_outstanding, unbilled_balance,
                residual_amount, adjustment_amount,
                period_start, period_end, source_request_id, created_by_user_id,
                row_version, committed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, household_id, account_id, batch_type, status, currency,
                      authoritative_balance, statement_balance, current_outstanding,
                      unbilled_balance, residual_amount, adjustment_amount,
                      matched_count, created_count, pending_count, parser_version,
                      engine_version, source_request_id, created_by_user_id,
                      row_version, failure_code, failure_detail, period_start, period_end,
                      created_at, updated_at, committed_at;
            """,
            (
                batch_id, household_id, account_id, batch_type, status, currency,
                authoritative_balance, statement_balance, current_outstanding, unbilled_balance,
                residual_amount, adjustment_amount,
                period_start, period_end, source_request_id, created_by_user_id,
                row_version, committed_at
            )
        )
        row = cur.fetchone()
        return _map_batch_row(row)

def get_reconciliation_batch(conn, batch_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, account_id, batch_type, status, currency,
                   authoritative_balance, statement_balance, current_outstanding,
                   unbilled_balance, residual_amount, adjustment_amount,
                   matched_count, created_count, pending_count, parser_version,
                   engine_version, source_request_id, created_by_user_id,
                   row_version, failure_code, failure_detail, period_start, period_end,
                   created_at, updated_at, committed_at
            FROM reconciliation_batches
            WHERE id = %s;
            """,
            (batch_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_batch_row(row)

def lock_reconciliation_batch(conn, batch_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, account_id, batch_type, status, currency,
                   authoritative_balance, statement_balance, current_outstanding,
                   unbilled_balance, residual_amount, adjustment_amount,
                   matched_count, created_count, pending_count, parser_version,
                   engine_version, source_request_id, created_by_user_id,
                   row_version, failure_code, failure_detail, period_start, period_end,
                   created_at, updated_at, committed_at
            FROM reconciliation_batches
            WHERE id = %s
            FOR UPDATE;
            """,
            (batch_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_batch_row(row)

def update_reconciliation_batch(
    conn,
    batch_id: UUID,
    status: str,
    residual_amount: Optional[Decimal] = None,
    adjustment_amount: Optional[Decimal] = None,
    committed_at: Optional[datetime] = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reconciliation_batches
            SET status = %s,
                residual_amount = COALESCE(%s, residual_amount),
                adjustment_amount = COALESCE(%s, adjustment_amount),
                committed_at = COALESCE(%s, committed_at),
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %s;
            """,
            (status, residual_amount, adjustment_amount, committed_at, batch_id)
        )

def update_reconciliation_batch_stats(
    conn,
    batch_id: UUID,
    status: str,
    matched_count: int,
    created_count: int,
    pending_count: int,
    residual_amount: Optional[Decimal] = None,
    adjustment_amount: Optional[Decimal] = None,
    engine_version: Optional[str] = "v1.0.0",
    committed_at: Optional[datetime] = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reconciliation_batches
            SET status = %s,
                matched_count = %s,
                created_count = %s,
                pending_count = %s,
                residual_amount = COALESCE(%s, residual_amount),
                adjustment_amount = COALESCE(%s, adjustment_amount),
                engine_version = COALESCE(%s, engine_version),
                committed_at = COALESCE(%s, committed_at),
                row_version = row_version + 1,
                updated_at = now()
            WHERE id = %s;
            """,
            (
                status, matched_count, created_count, pending_count,
                residual_amount, adjustment_amount, engine_version, committed_at, batch_id
            )
        )

# --- Statement Lines ---

def create_statement_line(
    conn,
    line_id: UUID,
    batch_id: UUID,
    description_raw: str,
    amount: Decimal,
    currency: str,
    direction: str,
    line_type: str,
    description_normalized: Optional[str] = None,
    transaction_on: Optional[date] = None,
    posted_on: Optional[date] = None,
    match_status: str = "unmatched",
    matched_transaction_id: Optional[UUID] = None,
    confidence: Optional[Decimal] = None,
    source_page_no: Optional[int] = None,
    source_row_no: Optional[int] = None,
    line_fingerprint: Optional[bytes] = None
) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO statement_lines (
                id, batch_id, description_raw, description_normalized,
                amount, currency, direction, line_type,
                transaction_on, posted_on, match_status, matched_transaction_id,
                confidence, source_page_no, source_row_no, line_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, batch_id, source_page_no, source_row_no, transaction_on, posted_on,
                      description_raw, description_normalized, amount, currency, direction, line_type,
                      match_status, matched_transaction_id, confidence, line_fingerprint, created_at;
            """,
            (
                line_id, batch_id, description_raw, description_normalized,
                amount, currency, direction, line_type,
                transaction_on, posted_on, match_status, matched_transaction_id,
                confidence, source_page_no, source_row_no, line_fingerprint
            )
        )
        row = cur.fetchone()
        return _map_statement_line_row(row)

def list_statement_lines_for_batch(conn, batch_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, batch_id, source_page_no, source_row_no, transaction_on, posted_on,
                   description_raw, description_normalized, amount, currency, direction, line_type,
                   match_status, matched_transaction_id, confidence, line_fingerprint, created_at
            FROM statement_lines
            WHERE batch_id = %s
            ORDER BY COALESCE(source_page_no, 0) ASC, COALESCE(source_row_no, 0) ASC, created_at ASC;
            """,
            (batch_id,)
        )
        rows = cur.fetchall()
        return [_map_statement_line_row(r) for r in rows]

def update_statement_line_status(
    conn,
    line_id: UUID,
    match_status: str,
    matched_transaction_id: Optional[UUID] = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE statement_lines
            SET match_status = %s,
                matched_transaction_id = %s
            WHERE id = %s;
            """,
            (match_status, matched_transaction_id, line_id)
        )

# --- Candidates ---

def create_reconciliation_candidate(
    conn,
    candidate_id: UUID,
    batch_id: UUID,
    candidate_type: str,
    status: str,
    payload: Dict[str, Any],
    statement_line_id: Optional[UUID] = None,
    target_transaction_id: Optional[UUID] = None,
    applied_transaction_id: Optional[UUID] = None,
    confidence: Optional[Decimal] = None,
    reason_code: Optional[str] = None,
    reason_detail: Optional[str] = None
) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reconciliation_candidates (
                id, batch_id, statement_line_id, candidate_type, status,
                target_transaction_id, applied_transaction_id, payload, confidence, reason_code, reason_detail
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, batch_id, statement_line_id, candidate_type, status,
                      target_transaction_id, payload, confidence, reason_code,
                      reason_detail, resolved_by_user_id, resolved_at,
                      applied_transaction_id, created_at, updated_at;
            """,
            (
                candidate_id, batch_id, statement_line_id, candidate_type, status,
                target_transaction_id, applied_transaction_id, json.dumps(payload), confidence, reason_code, reason_detail
            )
        )
        row = cur.fetchone()
        return _map_candidate_row(row)


def list_candidates_for_batch(conn, batch_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, batch_id, statement_line_id, candidate_type, status,
                   target_transaction_id, payload, confidence, reason_code,
                   reason_detail, resolved_by_user_id, resolved_at,
                   applied_transaction_id, created_at, updated_at
            FROM reconciliation_candidates
            WHERE batch_id = %s
            ORDER BY created_at ASC;
            """,
            (batch_id,)
        )
        rows = cur.fetchall()
        return [_map_candidate_row(r) for r in rows]

def update_candidate_status(
    conn,
    candidate_id: UUID,
    status: str,
    applied_transaction_id: Optional[UUID] = None,
    resolved_by_user_id: Optional[UUID] = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reconciliation_candidates
            SET status = %s,
                applied_transaction_id = COALESCE(%s, applied_transaction_id),
                resolved_by_user_id = COALESCE(%s, resolved_by_user_id),
                resolved_at = now(),
                updated_at = now()
            WHERE id = %s;
            """,
            (status, applied_transaction_id, resolved_by_user_id, candidate_id)
        )

def update_candidate_applied(
    conn,
    candidate_id: UUID,
    status: str,
    payload: Optional[Dict[str, Any]] = None,
    applied_transaction_id: Optional[UUID] = None,
    resolved_by_user_id: Optional[UUID] = None
) -> None:
    payload_json = json.dumps(payload) if payload is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reconciliation_candidates
            SET status = %s,
                payload = COALESCE(%s::jsonb, payload),
                applied_transaction_id = %s,
                resolved_by_user_id = COALESCE(%s, resolved_by_user_id),
                resolved_at = now(),
                updated_at = now()
            WHERE id = %s;
            """,
            (status, payload_json, applied_transaction_id, resolved_by_user_id, candidate_id)
        )


def update_reconciliation_candidate_full(
    conn,
    candidate_id: UUID,
    candidate_type: str,
    status: str,
    payload: Dict[str, Any],
    target_transaction_id: Optional[UUID] = None,
    applied_transaction_id: Optional[UUID] = None,
    confidence: Optional[Decimal] = None,
    reason_code: Optional[str] = None,
    reason_detail: Optional[str] = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reconciliation_candidates
            SET candidate_type = %s,
                status = %s,
                payload = %s::jsonb,
                target_transaction_id = %s,
                applied_transaction_id = %s,
                confidence = COALESCE(%s, confidence),
                reason_code = %s,
                reason_detail = %s,
                updated_at = now()
            WHERE id = %s;
            """,
            (
                candidate_type, status, json.dumps(payload), target_transaction_id,
                applied_transaction_id, confidence, reason_code, reason_detail, candidate_id
            )
        )


# --- Row Mappers ---

def _map_batch_row(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "household_id": row[1],
        "account_id": row[2],
        "batch_type": row[3],
        "status": row[4],
        "currency": row[5],
        "authoritative_balance": row[6],
        "statement_balance": row[7],
        "current_outstanding": row[8],
        "unbilled_balance": row[9],
        "residual_amount": row[10],
        "adjustment_amount": row[11],
        "matched_count": row[12],
        "created_count": row[13],
        "pending_count": row[14],
        "parser_version": row[15],
        "engine_version": row[16],
        "source_request_id": row[17],
        "created_by_user_id": row[18],
        "row_version": row[19],
        "failure_code": row[20],
        "failure_detail": row[21],
        "period_start": row[22],
        "period_end": row[23],
        "created_at": row[24],
        "updated_at": row[25],
        "committed_at": row[26]
    }

def _map_statement_line_row(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "batch_id": row[1],
        "source_page_no": row[2],
        "source_row_no": row[3],
        "transaction_on": row[4],
        "posted_on": row[5],
        "description_raw": row[6],
        "description_normalized": row[7],
        "amount": row[8],
        "currency": row[9],
        "direction": row[10],
        "line_type": row[11],
        "match_status": row[12],
        "matched_transaction_id": row[13],
        "confidence": row[14],
        "line_fingerprint": bytes(row[15]) if row[15] is not None else None,
        "created_at": row[16]
    }

def _map_candidate_row(row) -> Dict[str, Any]:
    payload_val = row[6]
    if isinstance(payload_val, str):
        try:
            payload_val = json.loads(payload_val)
        except Exception:
            pass
    return {
        "id": row[0],
        "batch_id": row[1],
        "statement_line_id": row[2],
        "candidate_type": row[3],
        "status": row[4],
        "target_transaction_id": row[5],
        "payload": payload_val,
        "confidence": row[7],
        "reason_code": row[8],
        "reason_detail": row[9],
        "resolved_by_user_id": row[10],
        "resolved_at": row[11],
        "applied_transaction_id": row[12],
        "created_at": row[13],
        "updated_at": row[14]
    }
