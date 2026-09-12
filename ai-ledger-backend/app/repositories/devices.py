import hashlib
import logging
import secrets
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID, uuid4
import psycopg2

from app.domain.auth import AuthError

logger = logging.getLogger(__name__)

def get_device_by_id(conn, device_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Looks up a device by ID, returning metadata without secret token hash.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, user_id, name, platform, status, client_version, created_at, last_seen_at, revoked_at
            FROM devices
            WHERE id = %s;
            """,
            (device_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "device_id": row[0],
            "id": row[0],
            "household_id": row[1],
            "user_id": row[2],
            "device_name": row[3],
            "name": row[3],
            "platform": row[4],
            "status": row[5],
            "client_version": row[6],
            "created_at": row[7],
            "last_seen_at": row[8],
            "revoked_at": row[9],
        }

def get_device_by_token_hash(conn, token_hash: bytes) -> Optional[Dict[str, Any]]:
    """
    Looks up a device by token hash regardless of active/revoked status.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, user_id, name, platform, status, client_version, created_at, last_seen_at, revoked_at
            FROM devices
            WHERE token_hash = %s;
            """,
            (psycopg2.Binary(token_hash),)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "device_id": row[0],
            "id": row[0],
            "household_id": row[1],
            "user_id": row[2],
            "device_name": row[3],
            "name": row[3],
            "platform": row[4],
            "status": row[5],
            "client_version": row[6],
            "created_at": row[7],
            "last_seen_at": row[8],
            "revoked_at": row[9],
        }

def get_active_device_by_token_hash(conn, token_hash: bytes) -> Optional[Dict[str, Any]]:
    """
    Looks up an active device by its SHA-256 token hash and resolves owning user and household context.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id AS device_id, d.household_id, d.user_id, d.name AS device_name, d.platform, d.status, d.client_version,
                   u.display_name AS user_name,
                   hm.role AS household_role, d.last_seen_at
            FROM devices d
            JOIN users u ON u.id = d.user_id
            JOIN household_members hm ON hm.household_id = d.household_id AND hm.user_id = u.id
            JOIN households h ON h.id = d.household_id
            WHERE d.token_hash = %s
              AND d.status = 'active'
              AND d.revoked_at IS NULL
              AND u.status = 'active'
              AND h.status = 'active'
            LIMIT 1;
            """,
            (psycopg2.Binary(token_hash),)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "device_id": row[0],
            "id": row[0],
            "household_id": row[1],
            "user_id": row[2],
            "device_name": row[3],
            "name": row[3],
            "platform": row[4],
            "status": row[5],
            "client_version": row[6],
            "user_name": row[7],
            "household_role": row[8],
            "last_seen_at": row[9],
        }

def list_devices_for_user(conn, user_id: UUID) -> List[Dict[str, Any]]:
    """
    Lists all devices belonging to a specific user.
    CRITICAL: Never returns token_hash or secret tokens.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, user_id, name, platform, status, client_version, created_at, last_seen_at, revoked_at
            FROM devices
            WHERE user_id = %s
            ORDER BY created_at DESC;
            """,
            (user_id,)
        )
        rows = cur.fetchall()
        return [
            {
                "device_id": r[0],
                "id": r[0],
                "household_id": r[1],
                "user_id": r[2],
                "device_name": r[3],
                "name": r[3],
                "platform": r[4],
                "status": r[5],
                "client_version": r[6],
                "created_at": r[7],
                "last_seen_at": r[8],
                "revoked_at": r[9],
            }
            for r in rows
        ]

def create_device_with_token(
    conn,
    user_id: UUID,
    device_name: str,
    platform: str,
    client_version: Optional[str] = None,
    household_id: Optional[UUID] = None,
    max_attempts: int = 3,
) -> Tuple[Dict[str, Any], str]:
    """
    Generates a high-entropy 256-bit secret token, hashes it using SHA-256,
    persists the device record with bounded collision retry, and returns the device record and raw secret token.
    The raw token is returned ONLY ONCE upon creation.
    """
    if household_id is None:
        from app.repositories import household_members as repo_members
        memberships = repo_members.list_active_household_memberships_for_user(conn, user_id)
        if not memberships:
            raise AuthError("User has no active household membership")
        household_id = memberships[0]["household_id"]

    for attempt in range(max_attempts):
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
        device_id = uuid4()

        savepoint_name = f"sp_device_insert_{attempt}_{uuid4().hex[:8]}"
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {savepoint_name};")
            try:
                cur.execute(
                    """
                    INSERT INTO devices (
                        id, household_id, user_id, name, platform, token_hash, status, client_version, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, now())
                    RETURNING id, household_id, user_id, name, platform, status, client_version, created_at, last_seen_at, revoked_at;
                    """,
                    (device_id, household_id, user_id, device_name, platform, psycopg2.Binary(token_hash), client_version)
                )
                row = cur.fetchone()
                cur.execute(f"RELEASE SAVEPOINT {savepoint_name};")
                device_dict = {
                    "device_id": row[0],
                    "id": row[0],
                    "household_id": row[1],
                    "user_id": row[2],
                    "device_name": row[3],
                    "name": row[3],
                    "platform": row[4],
                    "status": row[5],
                    "client_version": row[6],
                    "created_at": row[7],
                    "last_seen_at": row[8],
                    "revoked_at": row[9],
                }
                return device_dict, raw_token
            except psycopg2.IntegrityError as exc:
                cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name};")
                err_str = str(exc).lower()
                diag = getattr(exc, "diag", None)
                constraint_name = (getattr(diag, "constraint_name", "") or "").lower()
                if "token_hash" in constraint_name or "token_hash" in err_str or "devices_token_hash" in err_str:
                    logger.warning(
                        "Device token hash collision detected; retrying with fresh token (attempt %d/%d)",
                        attempt + 1,
                        max_attempts,
                    )
                    continue
                raise

    raise AuthError("Failed to provision device due to repeated credential collisions.")

def revoke_device(conn, device_id: UUID, user_id: Optional[UUID] = None, household_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    """
    Atomically revokes a device by ID. If user_id/household_id is provided, enforces that the device belongs to that scope.
    """
    query = """
        UPDATE devices
        SET status = 'revoked', revoked_at = now()
        WHERE id = %s
    """
    params: List[Any] = [device_id]
    if user_id is not None:
        query += " AND user_id = %s"
        params.append(user_id)
    if household_id is not None:
        query += " AND household_id = %s"
        params.append(household_id)

    query += " RETURNING id, household_id, user_id, name, platform, status, client_version, created_at, last_seen_at, revoked_at;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "device_id": row[0],
            "id": row[0],
            "household_id": row[1],
            "user_id": row[2],
            "device_name": row[3],
            "name": row[3],
            "platform": row[4],
            "status": row[5],
            "client_version": row[6],
            "created_at": row[7],
            "last_seen_at": row[8],
            "revoked_at": row[9],
        }

def update_device_last_seen(conn, device_id: UUID) -> None:
    """
    Updates the device's last_seen_at timestamp within a supplied connection.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devices
            SET last_seen_at = now()
            WHERE id = %s;
            """,
            (device_id,)
        )

def update_device_last_seen_isolated(device_id: UUID, schema: Optional[str] = None) -> None:
    """
    Autonomous telemetry updater: executes in an isolated DB connection from the connection pool.
    Commits immediately and swallows errors so endpoint business logic is never rolled back or committed prematurely.
    """
    try:
        from app.db import get_db_connection
        with get_db_connection(schema) as conn:
            update_device_last_seen(conn, device_id)
            conn.commit()
    except Exception as e:
        logger.warning("Failed to update device last_seen_at telemetry for %s: %s", device_id, e)

def create_device(
    conn,
    device_id: UUID,
    user_id: UUID,
    device_name: str,
    token_hash: bytes,
    platform: str = "ios_shortcuts",
    status: str = "active",
    client_version: Optional[str] = None,
    household_id: Optional[UUID] = None,
) -> None:
    """
    Inserts a device record (used during test provisioning).
    """
    if household_id is None:
        from app.repositories import household_members as repo_members
        memberships = repo_members.list_active_household_memberships_for_user(conn, user_id)
        if memberships:
            household_id = memberships[0]["household_id"]
        else:
            raise AuthError("User has no active household membership")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devices (
                id, household_id, user_id, name, platform, token_hash, status, client_version, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now());
            """,
            (device_id, household_id, user_id, device_name, platform, psycopg2.Binary(token_hash), status, client_version)
        )
