import json
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID

def insert_audit_event(
    conn,
    household_id: UUID,
    actor_type: str,
    entity_type: str,
    entity_id: UUID,
    action: str,
    actor_user_id: Optional[UUID] = None,
    actor_device_id: Optional[UUID] = None,
    request_id: Optional[UUID] = None,
    reconciliation_batch_id: Optional[UUID] = None,
    before_data: Optional[Dict[str, Any]] = None,
    after_data: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> None:
    """
    Inserts an immutable audit event. entity_type, entity_id, and action are strictly required.
    """
    before_json = json.dumps(before_data, default=str) if before_data is not None else None
    after_json = json.dumps(after_data, default=str) if after_data is not None else None
    meta_json = json.dumps(metadata, default=str) if metadata is not None else None
    
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

def list_audit_events_with_filters(
    conn,
    household_id: UUID,
    entity_type: Optional[str] = None,
    entity_id: Optional[UUID] = None,
    from_date: Optional[Any] = None,
    to_date: Optional[Any] = None,
    actor_user_id: Optional[UUID] = None,
    limit: int = 50,
    cursor: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Retrieves filtered audit events for a household with deterministic pagination.
    """
    where_clauses = ["household_id = %s"]
    params: List[Any] = [household_id]

    if entity_type:
        where_clauses.append("entity_type = %s")
        params.append(entity_type)

    if entity_id:
        where_clauses.append("entity_id = %s")
        params.append(entity_id)

    if from_date:
        where_clauses.append("created_at >= %s")
        params.append(from_date)

    if to_date:
        where_clauses.append("created_at <= %s")
        params.append(to_date)

    if actor_user_id:
        where_clauses.append("actor_user_id = %s")
        params.append(actor_user_id)

    if cursor:
        try:
            cursor_id = int(cursor)
            where_clauses.append("id < %s")
            params.append(cursor_id)
        except (ValueError, TypeError):
            pass

    where_sql = " AND ".join(where_clauses)
    query = f"""
        SELECT id, household_id, actor_type, actor_user_id, actor_device_id, request_id,
               reconciliation_batch_id, entity_type, entity_id, action,
               before_data, after_data, metadata, created_at
        FROM audit_events
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT %s;
    """
    params.append(limit + 1)

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall()

    events = []
    has_more = len(rows) > limit
    actual_rows = rows[:limit]

    for r in actual_rows:
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

    next_cursor = str(actual_rows[-1][0]) if has_more and actual_rows else None
    return events, next_cursor
