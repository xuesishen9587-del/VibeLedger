from typing import Optional, Dict, Any
from uuid import UUID

def get_user_by_auth_subject(conn, auth_subject: str) -> Optional[Dict[str, Any]]:
    """
    Looks up a user by their external authentication subject identifier.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, auth_subject, email, display_name, default_currency, status, created_at, updated_at
            FROM users
            WHERE auth_subject = %s;
            """,
            (auth_subject,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "auth_subject": row[1],
            "email": row[2],
            "display_name": row[3],
            "default_currency": row[4],
            "status": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }

def get_user_by_id(conn, user_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Looks up a user by their primary key UUID.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, auth_subject, email, display_name, default_currency, status, created_at, updated_at
            FROM users
            WHERE id = %s;
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "auth_subject": row[1],
            "email": row[2],
            "display_name": row[3],
            "default_currency": row[4],
            "status": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }

def create_user(
    conn,
    user_id: UUID,
    auth_subject: str,
    display_name: str,
    email: Optional[str] = None,
    default_currency: str = "CNY",
    status: str = "active",
) -> Dict[str, Any]:
    """
    Inserts a user record (primarily for provisioning and test fixtures).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (
                id, auth_subject, email, display_name, default_currency, status, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, now(), now())
            RETURNING id, auth_subject, email, display_name, default_currency, status, created_at, updated_at;
            """,
            (user_id, auth_subject, email, display_name, default_currency, status)
        )
        row = cur.fetchone()
        return {
            "id": row[0],
            "auth_subject": row[1],
            "email": row[2],
            "display_name": row[3],
            "default_currency": row[4],
            "status": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }
