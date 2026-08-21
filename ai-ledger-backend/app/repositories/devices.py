from typing import Optional, Dict, Any
from uuid import UUID

def get_active_device_by_token_hash(conn, token_hash: bytes) -> Optional[Dict[str, Any]]:
    """
    Looks up an active device by its SHA-256 token hash and resolves owning user and household context.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id AS device_id, d.user_id, d.device_name, d.platform, d.status, d.client_version,
                   u.display_name AS user_name, u.default_currency,
                   hm.household_id, hm.role AS household_role
            FROM devices d
            JOIN users u ON u.id = d.user_id
            JOIN household_members hm ON hm.user_id = u.id
            JOIN households h ON h.id = hm.household_id
            WHERE d.token_hash = %s
              AND d.status = 'active'
              AND d.revoked_at IS NULL
              AND u.status = 'active'
              AND h.status = 'active'
            LIMIT 1;
            """,
            (token_hash,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "device_id": row[0],
            "user_id": row[1],
            "device_name": row[2],
            "platform": row[3],
            "status": row[4],
            "client_version": row[5],
            "user_name": row[6],
            "default_currency": row[7],
            "household_id": row[8],
            "household_role": row[9]
        }

def update_device_last_seen(conn, device_id: UUID) -> None:
    """
    Updates the device's last_seen_at timestamp.
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

def create_device(
    conn,
    device_id: UUID,
    user_id: UUID,
    device_name: str,
    token_hash: bytes,
    platform: str = "ios_shortcuts",
    status: str = "active",
    client_version: Optional[str] = None
) -> None:
    """
    Inserts a device record (used during test provisioning).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devices (
                id, user_id, device_name, platform, token_hash, status, client_version, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, now());
            """,
            (device_id, user_id, device_name, platform, token_hash, status, client_version)
        )
