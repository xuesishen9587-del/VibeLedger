from typing import Optional, Dict, Any, List
from uuid import UUID
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_auth_context
from app.auth.context import AuthContext
from app.auth.service import AuthService
from app.db import transaction

router = APIRouter(prefix="/api/v1/devices", tags=["Devices"])


class CreateDeviceRequest(BaseModel):
    device_name: str = Field(..., min_length=1, max_length=100, description="Device name")
    platform: str = Field(..., min_length=1, max_length=50, description="Platform identifier, e.g. 'ios_shortcuts'")
    client_version: Optional[str] = Field(None, max_length=50, description="Client app or shortcut version")


def _format_device(dev: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "device_id": str(dev["device_id"]),
        "user_id": str(dev["user_id"]),
        "device_name": dev["device_name"],
        "platform": dev["platform"],
        "status": dev["status"],
        "client_version": dev.get("client_version"),
        "created_at": dev["created_at"].isoformat() if dev.get("created_at") else None,
        "last_seen_at": dev["last_seen_at"].isoformat() if dev.get("last_seen_at") else None,
        "revoked_at": dev["revoked_at"].isoformat() if dev.get("revoked_at") else None,
    }


@router.get("", summary="List User Devices")
def list_devices(
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection),
) -> Dict[str, Any]:
    """
    Lists registered devices for the authenticated caller.
    Credentials (token hashes) are strictly redacted.
    """
    devices = AuthService.list_devices(conn, auth_context)
    return {"items": [_format_device(d) for d in devices]}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Provision Device")
def create_device(
    payload: CreateDeviceRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection),
) -> Dict[str, Any]:
    """
    Provisions a new device and returns the high-entropy raw Bearer token ONCE.
    """
    with transaction(conn):
        device_dict, raw_token = AuthService.provision_device(
            conn=conn,
            auth_context=auth_context,
            device_name=payload.device_name,
            platform=payload.platform,
            client_version=payload.client_version,
        )

    return {
        "device": _format_device(device_dict),
        "token": raw_token,
    }


@router.post("/{device_id}/revoke", summary="Revoke Device")
def revoke_device(
    device_id: UUID,
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection),
) -> Dict[str, Any]:
    """
    Atomically revokes an active device. The device token will immediately fail subsequent authentication.
    """
    with transaction(conn):
        revoked_dev = AuthService.revoke_device(
            conn=conn,
            auth_context=auth_context,
            device_id=device_id,
        )

    return {
        "device": _format_device(revoked_dev),
    }
