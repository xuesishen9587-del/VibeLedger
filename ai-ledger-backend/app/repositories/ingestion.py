import json
from typing import Optional, Dict, Any
from uuid import UUID

def create_ingestion_request(
    conn,
    request_id: UUID,
    device_id: UUID,
    idempotency_key: str,
    request_kind: str,
    request_hash: bytes,
    status: str = 'received',
    draft_payload: Optional[Dict[str, Any]] = None
) -> None:
    draft_json = json.dumps(draft_payload) if draft_payload is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_requests (
                id, device_id, idempotency_key, request_kind, request_hash, status, draft_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s);
            """,
            (request_id, device_id, idempotency_key, request_kind, request_hash, status, draft_json)
        )

def get_ingestion_request(conn, request_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, device_id, idempotency_key, request_kind, request_hash, status, 
                   draft_payload, response_payload, failure_code, created_at, updated_at, committed_at
            FROM ingestion_requests
            WHERE id = %s;
            """,
            (request_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "device_id": row[1],
            "idempotency_key": row[2],
            "request_kind": row[3],
            "request_hash": row[4],
            "status": row[5],
            "draft_payload": row[6],
            "response_payload": row[7],
            "failure_code": row[8],
            "created_at": row[9],
            "updated_at": row[10],
            "committed_at": row[11]
        }

def get_by_device_and_key(conn, device_id: UUID, idempotency_key: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, device_id, idempotency_key, request_kind, request_hash, status, 
                   draft_payload, response_payload, failure_code, created_at, updated_at, committed_at
            FROM ingestion_requests
            WHERE device_id = %s AND idempotency_key = %s;
            """,
            (device_id, idempotency_key)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "device_id": row[1],
            "idempotency_key": row[2],
            "request_kind": row[3],
            "request_hash": row[4],
            "status": row[5],
            "draft_payload": row[6],
            "response_payload": row[7],
            "failure_code": row[8],
            "created_at": row[9],
            "updated_at": row[10],
            "committed_at": row[11]
        }

def update_ingestion_request_status(
    conn,
    request_id: UUID,
    status: str,
    response_payload: Optional[Dict[str, Any]] = None,
    failure_code: Optional[str] = None,
    committed_at: Optional[Any] = None
) -> None:
    resp_json = json.dumps(response_payload) if response_payload is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ingestion_requests
            SET status = %s,
                response_payload = COALESCE(%s, response_payload),
                failure_code = COALESCE(%s, failure_code),
                committed_at = COALESCE(%s, committed_at),
                updated_at = now()
            WHERE id = %s;
            """,
            (status, resp_json, failure_code, committed_at, request_id)
        )
