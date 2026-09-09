"""Durable capture reservations and state transitions; no screenshot storage."""
import json
from contextlib import contextmanager
from uuid import uuid4
from app.db import get_connection
from app.domain.spending import fail
from app.repositories import spending as repo, simplified_schema as schema

TERMINAL = {"committed", "rejected", "failed"}


def connection_factory(auth_connection):
    # Auth has finished. Do not retain its database connection during model I/O.
    with auth_connection.cursor() as cur:
        cur.execute("SELECT current_schema()")
        name = cur.fetchone()[0]
    auth_connection.commit()
    auth_connection.close()
    return lambda: get_connection(name)


@contextmanager
def session(factory):
    conn = factory()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def authorize(conn, actor):
    valid = repo.rows(conn, "SELECT 1 FROM users u JOIN household_members m ON m.user_id=u.id "
        "JOIN households h ON h.id=m.household_id WHERE u.id=%s AND h.id=%s AND u.status='active' "
        "AND h.status='active' AND m.role IN ('owner','member')", (actor.user_id, actor.household_id))
    if not valid:
        fail("FORBIDDEN", "Household access is no longer active.", 403)
    if actor.is_device:
        valid = repo.rows(conn, "SELECT 1 FROM devices WHERE household_id=%s AND id=%s AND user_id=%s "
            "AND status='active' AND revoked_at IS NULL", (actor.household_id, actor.device_id, actor.user_id))
        if not valid:
            fail("DEVICE_REVOKED", "Device access is no longer active.", 401)


def get(conn, actor, identity, lock=False):
    rows = repo.rows(conn, "SELECT * FROM ingestion_requests WHERE household_id=%s AND id=%s "
        "AND (%s OR actor_scope=%s)" + (" FOR UPDATE" if lock else ""),
        (actor.household_id, identity, actor.is_browser, actor.actor_scope))
    if not rows:
        fail("REQUEST_NOT_FOUND", "Request not found.", 404)
    return rows[0]


def response(row, recovery=False):
    if row["status"] in TERMINAL:
        if recovery and row["status"] == "failed":
            return {"status": "failed", "request_id": str(row["id"]), "failure_code": row["failure_code"],
                    "row_version": row["row_version"], "display_summary": "Capture failed. You may capture again with a new key."}, 200
        return row["response_payload"], row["response_http_status"]
    if row["status"] == "processing":
        return {"status": "processing", "request_id": str(row["id"]), "row_version": row["row_version"],
                "display_summary": "Capture is processing. Keep this request key."}, 202
    draft = row["draft_payload"]
    return {"status": "needs_confirmation", "request_id": str(row["id"]), "row_version": row["row_version"],
            "draft": draft, "warnings": draft.get("warnings", []),
            "display_summary": "Please review the expense before saving."}, 200


def reserve(conn, actor, key, request_hash=None, digest=None, captured_at=None, client_version=None, cancel=False):
    authorize(conn, actor)
    inserted = repo.rows(conn, "INSERT INTO ingestion_requests(id,household_id,user_id,device_id,actor_scope,"
        "idempotency_key,request_kind,operation,request_hash,image_sha256,captured_at,client_version,status,last_editor_scope) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'processing',%s) "
        "ON CONFLICT(household_id,actor_scope,idempotency_key) DO NOTHING RETURNING *",
        (uuid4(), actor.household_id, actor.user_id, actor.device_id, actor.actor_scope, key,
         "command" if cancel else "expense", "cancel" if cancel else "POST /api/v1/expenses",
         request_hash, digest, captured_at, client_version, actor.actor_scope))
    if inserted:
        return inserted[0], True
    existing = repo.rows(conn, "SELECT * FROM ingestion_requests WHERE household_id=%s AND actor_scope=%s "
                         "AND idempotency_key=%s FOR UPDATE", (actor.household_id, actor.actor_scope, key))[0]
    if not cancel and existing["request_hash"] is not None and (existing["request_hash"] != request_hash or existing["operation"] != "POST /api/v1/expenses"):
        fail("IDEMPOTENCY_KEY_REUSE", "This key belongs to a different request.", 409)
    return existing, False


def version(row, actor, expected_version):
    if expected_version is not None:
        if row["row_version"] != expected_version:
            fail("DRAFT_CHANGED", "The draft changed. Reload it.", 409)
    elif actor.is_browser:
        fail("INVALID_REQUEST", "Browser draft actions require expected_version.")
    elif row["last_editor_scope"] != actor.actor_scope:
        fail("DRAFT_CHANGED", "This draft was edited in Dashboard. Review it before confirming.", 409)


def draft(conn, actor, row, payload):
    return schema.update_request_draft(conn, actor.household_id, row["id"], row["row_version"],
                                       json.loads(json.dumps(payload, default=str)), actor.actor_scope)


def terminal(conn, actor, row, status, payload, http_status=200, failure_code=None):
    return schema.finalize_ingestion_request(conn, actor.household_id, row["id"], row["row_version"],
                                             status, payload, http_status, failure_code)


def rejected(conn, actor, row, message="Capture cancelled."):
    return terminal(conn, actor, row, "rejected", {"status": "rejected", "request_id": str(row["id"]),
                                                  "display_summary": message})
