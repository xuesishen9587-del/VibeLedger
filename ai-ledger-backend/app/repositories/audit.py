import json
from typing import Optional, Dict, Any, List
from uuid import UUID

def insert_audit_event(
    conn,
    household_id: UUID,
    actor_type: str,
    actor_user_id: Optional[UUID] = None,
    actor_device_id: Optional[UUID] = None,
    request_id: Optional[UUID] = None,
    reconciliation_batch_id: Optional[UUID] = None,
    entity_type: str = "",
    entity_id: UUID = None,
    action: str = "",
    before_data: Optional[Dict[str, Any]] = None,
    after_data: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> None:
    before_json = json.dumps(before_data) if before_data is not None else None
    after_json = json.dumps(after_data) if after_data is not None else None
    meta_json = json.dumps(metadata) if metadata is not None else None
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_events (
                household_id, actor_type, actor_user_id, actor_device_id, request_id, 
                reconciliation_batch_id, entity_type, entity_id, action, 
                before_data, after_data, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                household_id, actor_type, actor_user_id, actor_device_id, request_id,
                reconciliation_batch_id, entity_type, entity_id, action,
                before_json, after_json, meta_json
            )
        )

def list_audit_events_for_entity(conn, entity_type: str, entity_id: UUID) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, actor_type, actor_user_id, actor_device_id, request_id,
                   reconciliation_batch_id, entity_type, entity_id, action,
                   before_data, after_data, metadata, created_at
            FROM audit_events
            WHERE entity_type = %s AND entity_id = %s
            ORDER BY created_at DESC;
            """,
            (entity_type, entity_id)
        )
        rows = cur.fetchall()
        events = []
        for r in rows:
            events.append({
                "id": r[0],
                "household_id": r[1],
                "actor_type": r[2],
                "actor_user_id": r[3],
                "actor_device_id": r[4],
                "request_id": r[5],
                "reconciliation_batch_id": r[6],
                "entity_type": r[7],
                "entity_id": r[8],
                "action": r[9],
                "before_data": r[10],
                "after_data": r[11],
                "metadata": r[12],
                "created_at": r[13]
            })
        return events
