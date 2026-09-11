import json
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID

CANONICAL_ACTIONS = {
    'create', 'update', 'void', 'replace', 'close', 'reopen',
    'confirm_flows', 'fill_reporting_fx', 'acknowledge_metadata',
    'skip_period', 'link_statement', 'pause_schedule',
    'resume_schedule', 'cancel_schedule'
}

def insert_audit_event(
    conn,
    household_id: UUID,
    actor_type: str,
    entity_type: str,
    entity_id: UUID,
    action: str,
    actor_user_id: Optional[UUID] = None,
    actor_device_id: Optional[UUID] = None,
    source_request_id: Optional[UUID] = None,
    request_id: Optional[UUID] = None,  # Backward compatibility alias
    before_data: Optional[Dict[str, Any]] = None,
    after_data: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,  # Backward compatibility ignored
    reconciliation_batch_id: Optional[UUID] = None,  # Backward compatibility ignored
) -> None:
    """
    Inserts an immutable audit event matching the simplified schema.
    """
    req_id = source_request_id or request_id
    if action not in CANONICAL_ACTIONS:
        raise ValueError(f"Invalid audit action '{action}'. Must be one of {sorted(CANONICAL_ACTIONS)}")

    before_json = json.dumps(before_data, default=str) if before_data is not None else None
    after_json = json.dumps(after_data, default=str) if after_data is not None else None
    
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_events (
                household_id, actor_type, actor_user_id, actor_device_id, source_request_id,
                entity_type, entity_id, action, before_data, after_data, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                household_id, actor_type, actor_user_id, actor_device_id, req_id,
                entity_type, entity_id, action,
                before_json, after_json, reason
            )
        )

def list_audit_events_for_entity(conn, entity_type: str, entity_id: UUID, household_id: UUID) -> List[Dict[str, Any]]:
    query = """
        SELECT id, household_id, actor_type, actor_user_id, actor_device_id, source_request_id,
               entity_type, entity_id, action, before_data, after_data, reason, created_at
        FROM audit_events
        WHERE household_id = %s AND entity_type = %s AND entity_id = %s
        ORDER BY id DESC;
    """
    with conn.cursor() as cur:
        cur.execute(query, (household_id, entity_type, entity_id))
        rows = cur.fetchall()
        events = []
        for r in rows:
            events.append({
                "id": r[0],
                "household_id": r[1],
                "actor_type": r[2],
                "actor_user_id": r[3],
                "actor_device_id": r[4],
                "source_request_id": r[5],
                "request_id": r[5],
                "entity_type": r[6],
                "entity_id": r[7],
                "action": r[8],
                "before_data": r[9],
                "after_data": r[10],
                "reason": r[11],
                "created_at": r[12]
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
        SELECT id, household_id, actor_type, actor_user_id, actor_device_id, source_request_id,
               entity_type, entity_id, action, before_data, after_data, reason, created_at
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
            "source_request_id": r[5],
            "request_id": r[5],
            "entity_type": r[6],
            "entity_id": r[7],
            "action": r[8],
            "before_data": r[9],
            "after_data": r[10],
            "reason": r[11],
            "created_at": r[12]
        })

    next_cursor = str(actual_rows[-1][0]) if has_more and actual_rows else None
    return events, next_cursor

def get_entity_history(
    conn,
    household_id: UUID,
    entity_type: str,
    entity_id: UUID,
    limit: int = 50,
    cursor: Optional[str] = None
) -> Optional[Tuple[List[Dict[str, Any]], Optional[str]]]:
    """
    Retrieves append-only change history for a specific entity within the household.
    Returns None if the entity does not exist in the authenticated household (caller should raise 404).
    """
    table_map = {
        "account": "accounts",
        "category": "categories",
        "account_alias": "account_aliases",
        "transaction": "transactions",
        "spending_schedule": "spending_schedules",
        "schedule_occurrence": "schedule_occurrences",
        "account_snapshot": "account_snapshots",
        "statement_line": "statement_lines",
    }
    table = table_map.get(entity_type)
    if table:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {table} WHERE household_id = %s AND id = %s LIMIT 1;", (household_id, str(entity_id)))
            if not cur.fetchone():
                return None

    return list_audit_events_with_filters(
        conn=conn,
        household_id=household_id,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        cursor=cursor
    )
