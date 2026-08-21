import json
from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime

def _map_ingestion_row(row) -> Dict[str, Any]:
    draft_data = row[6]
    if isinstance(draft_data, str):
        try:
            draft_data = json.loads(draft_data)
        except Exception:
            pass

    resp_data = row[7]
    if isinstance(resp_data, str):
        try:
            resp_data = json.loads(resp_data)
        except Exception:
            pass

    return {
        "id": row[0],
        "device_id": row[1],
        "idempotency_key": row[2],
        "request_kind": row[3],
        "request_hash": bytes(row[4]) if row[4] is not None else None,
        "status": row[5],
        "draft_payload": draft_data,
        "response_payload": resp_data,
        "failure_code": row[8],
        "captured_at": row[9],
        "client_version": row[10],
        "created_at": row[11],
        "updated_at": row[12],
        "committed_at": row[13]
    }

INGESTION_COLUMNS = """
    id, device_id, idempotency_key, request_kind, request_hash, status,
    draft_payload, response_payload, failure_code, captured_at, client_version,
    created_at, updated_at, committed_at
"""

def create_ingestion_request(
    conn,
    request_id: UUID,
    device_id: UUID,
    idempotency_key: str,
    request_kind: str,
    request_hash: bytes,
    status: str = "received",
    captured_at: Optional[datetime] = None,
    client_version: Optional[str] = None,
    draft_payload: Optional[Dict[str, Any]] = None,
    response_payload: Optional[Dict[str, Any]] = None
) -> bool:
    draft_json = json.dumps(draft_payload) if draft_payload is not None else None
    resp_json = json.dumps(response_payload) if response_payload is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_requests (
                id, device_id, idempotency_key, request_kind, request_hash, status,
                captured_at, client_version, draft_payload, response_payload, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (device_id, idempotency_key) DO NOTHING;
            """,
            (
                request_id, device_id, idempotency_key, request_kind, request_hash, status,
                captured_at, client_version, draft_json, resp_json
            )
        )
        return cur.rowcount > 0

def get_ingestion_request(conn, request_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {INGESTION_COLUMNS}
            FROM ingestion_requests
            WHERE id = %s;
            """,
            (request_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_ingestion_row(row)

def get_by_device_and_key(conn, device_id: UUID, idempotency_key: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {INGESTION_COLUMNS}
            FROM ingestion_requests
            WHERE device_id = %s AND idempotency_key = %s;
            """,
            (device_id, idempotency_key)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_ingestion_row(row)

def lock_ingestion_request(conn, request_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Locks the ingestion_requests row FOR UPDATE.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {INGESTION_COLUMNS}
            FROM ingestion_requests
            WHERE id = %s
            FOR UPDATE;
            """,
            (request_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_ingestion_row(row)

def lock_by_device_and_key(conn, device_id: UUID, idempotency_key: str) -> Optional[Dict[str, Any]]:
    """
    Locks the ingestion_requests row FOR UPDATE by device_id and idempotency_key.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {INGESTION_COLUMNS}
            FROM ingestion_requests
            WHERE device_id = %s AND idempotency_key = %s
            FOR UPDATE;
            """,
            (device_id, idempotency_key)
        )
        row = cur.fetchone()
        if not row:
            return None
        return _map_ingestion_row(row)

def update_ingestion_request_status(
    conn,
    request_id: UUID,
    status: str,
    response_payload: Optional[Dict[str, Any]] = None,
    draft_payload: Optional[Dict[str, Any]] = None,
    failure_code: Optional[str] = None,
    committed_at: Optional[Any] = None
) -> None:
    resp_json = json.dumps(response_payload) if response_payload is not None else None
    draft_json = json.dumps(draft_payload) if draft_payload is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ingestion_requests
            SET status = %s,
                response_payload = COALESCE(%s, response_payload),
                draft_payload = COALESCE(%s, draft_payload),
                failure_code = %s,
                committed_at = COALESCE(%s, committed_at),
                updated_at = now()
            WHERE id = %s;
            """,
            (status, resp_json, draft_json, failure_code, committed_at, request_id)
        )
