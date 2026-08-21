import hashlib
from typing import Optional, Dict, Any, Generator
from fastapi import Header, HTTPException, Depends
from app.db import get_connection
import app.repositories.devices as devices_repo

def get_db_connection() -> Generator[Any, None, None]:
    """
    FastAPI dependency that yields an active database connection.
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        if conn and not conn.closed:
            conn.close()

def get_authenticated_device(
    authorization: Optional[str] = Header(None),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    FastAPI dependency that authenticates incoming iOS Shortcut / Device requests via Bearer token.
    Hashes the raw token with SHA-256 and validates active status.
    Raw tokens are never logged or stored.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Missing or invalid Authorization Bearer header.",
                    "retryable": false if False else False,
                    "details": {}
                }
            }
        )

    raw_token = authorization[7:].strip()
    if not raw_token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Empty device bearer token.",
                    "retryable": False,
                    "details": {}
                }
            }
        )

    token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
    device = devices_repo.get_active_device_by_token_hash(conn, token_hash)
    if not device:
        revoked_dev = devices_repo.get_device_by_token_hash(conn, token_hash)
        if revoked_dev and (revoked_dev["status"] != "active" or revoked_dev["revoked_at"] is not None):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "code": "DEVICE_REVOKED",
                        "message": "Device token is revoked or inactive.",
                        "retryable": False,
                        "details": {}
                    }
                }
            )

        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Invalid or unknown device token.",
                    "retryable": False,
                    "details": {}
                }
            }
        )

    # Update last_seen_at
    try:
        devices_repo.update_device_last_seen(conn, device["device_id"])
        conn.commit()
    except Exception:
        conn.rollback()

    return device
