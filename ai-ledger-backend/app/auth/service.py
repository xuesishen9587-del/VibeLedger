import hashlib
from typing import Optional, Dict, Any, List, Tuple
from uuid import UUID

from app.auth.context import AuthContext
from app.auth.browser_verifier import BrowserAuthVerifier, get_browser_verifier
from app.domain.auth import (
    AuthRequiredError,
    InvalidCredentialsError,
    DeviceRevokedError,
    UserDisabledError,
    UserNotInHouseholdError,
    AmbiguousHouseholdMembershipError,
    HouseholdPermissionDeniedError,
    DeviceNotFoundError,
)
from app.repositories import devices as repo_devices
from app.repositories import users as repo_users
from app.repositories import household_members as repo_members
from app.repositories import audit as repo_audit
from app.services.durable_commands import execute_durable_command, get_audit_actor_info


class AuthService:
    """Core domain authentication and authorization service."""

    @staticmethod
    def authenticate_device(conn, token: str) -> AuthContext:
        """
        Authenticates a device bearer token via SHA-256 hash lookup.
        Deterministic single active household membership is strictly enforced.
        """
        if not token or not token.strip():
            raise AuthRequiredError("Device token is required.")

        token_hash = hashlib.sha256(token.encode("utf-8")).digest()
        device = repo_devices.get_device_by_token_hash(conn, token_hash)
        if not device:
            raise InvalidCredentialsError("Invalid device credentials.")

        if device.get("status") == "revoked" or device.get("revoked_at") is not None:
            raise DeviceRevokedError("Device token has been revoked.")

        if device.get("status") != "active":
            raise InvalidCredentialsError("Device is not active.")

        user_id = device["user_id"]
        user = repo_users.get_user_by_id(conn, user_id)
        if not user:
            raise InvalidCredentialsError("Owning user not found.")

        if user.get("status") != "active":
            raise UserDisabledError("User account is disabled.")

        memberships = repo_members.list_active_household_memberships_for_user(conn, user_id)
        if len(memberships) == 0:
            raise UserNotInHouseholdError("User does not belong to any active household.")
        if len(memberships) > 1:
            raise AmbiguousHouseholdMembershipError(
                "User belongs to multiple households; single active household membership required."
            )

        membership = memberships[0]
        household_id = membership["household_id"]
        household_role = membership["role"]

        if device.get("household_id") and str(device["household_id"]) != str(household_id):
            raise HouseholdPermissionDeniedError("Device does not belong to user's active household.")

        # Autonomous telemetry update for last_seen_at
        schema = None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_schema();")
                row = cur.fetchone()
                if row and row[0]:
                    schema = row[0]
        except Exception:
            pass
        repo_devices.update_device_last_seen_isolated(device["device_id"], schema=schema)

        return AuthContext(
            auth_mode="device",
            user_id=user["id"],
            household_id=household_id,
            household_role=household_role,
            device_id=device["device_id"],
            auth_subject=user.get("auth_subject"),
        )

    @staticmethod
    def authenticate_browser(conn, token: str, verifier: Optional[BrowserAuthVerifier] = None) -> AuthContext:
        """
        Authenticates a browser JWT token via the configured BrowserAuthVerifier.
        Resolves auth_subject -> User -> exactly 1 active Household membership.
        """
        if not token or not token.strip():
            raise AuthRequiredError("Browser token is required.")

        active_verifier = verifier or get_browser_verifier()
        claims = active_verifier.verify(token)

        sub = claims.get("sub")
        if not sub or not isinstance(sub, str) or not sub.strip():
            raise InvalidCredentialsError("Invalid browser credentials.")

        user = repo_users.get_user_by_auth_subject(conn, sub)
        if not user:
            raise InvalidCredentialsError("Invalid browser credentials.")

        if user.get("status") != "active":
            raise UserDisabledError("User account is disabled.")

        memberships = repo_members.list_active_household_memberships_for_user(conn, user["id"])
        if len(memberships) == 0:
            raise UserNotInHouseholdError("User does not belong to any active household.")
        if len(memberships) > 1:
            raise AmbiguousHouseholdMembershipError(
                "User belongs to multiple households; single active household membership required."
            )

        membership = memberships[0]
        household_id = membership["household_id"]
        household_role = membership["role"]

        return AuthContext(
            auth_mode="browser",
            user_id=user["id"],
            household_id=household_id,
            household_role=household_role,
            device_id=None,
            auth_subject=sub,
        )

    @staticmethod
    def authenticate(conn, token: str, verifier: Optional[BrowserAuthVerifier] = None) -> AuthContext:
        """
        Dual-mode authentication: inspects token format to route to either browser JWT or device bearer auth.
        Deterministic classification:
        - 3-segment token (2 dots): browser JWT ONLY (no device fallback).
        - opaque token (not 2 dots): device token ONLY.
        """
        if not token or not token.strip():
            raise AuthRequiredError("Authorization bearer token is missing.")

        clean_token = token.strip()
        # JWTs consist of 3 base64url segments separated by dots
        if clean_token.count(".") == 2:
            return AuthService.authenticate_browser(conn, clean_token, verifier)
        else:
            return AuthService.authenticate_device(conn, clean_token)

    @staticmethod
    def list_devices(conn, auth_context: AuthContext) -> List[Dict[str, Any]]:
        """Lists registered devices for the current user (redacted, no token hash)."""
        return repo_devices.list_devices_for_user(conn, auth_context.user_id)

    @staticmethod
    def provision_device(
        conn,
        auth_context: AuthContext,
        device_name: str,
        platform: str,
        client_version: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """
        Provisions a new device for the caller's user and returns the device metadata and secret token.
        Requires browser authentication (device credentials cannot mint new device credentials).
        """
        if not auth_context.is_browser:
            raise HouseholdPermissionDeniedError("Browser authentication required to provision devices.")
        device_dict, raw_token = repo_devices.create_device_with_token(
            conn,
            user_id=auth_context.user_id,
            device_name=device_name,
            platform=platform,
            client_version=client_version,
            household_id=auth_context.household_id,
        )

        actor_type = "user" if auth_context.is_browser else "device"
        repo_audit.insert_audit_event(
            conn,
            household_id=auth_context.household_id,
            actor_type=actor_type,
            actor_user_id=auth_context.user_id,
            actor_device_id=auth_context.device_id,
            entity_type="device",
            entity_id=device_dict["device_id"],
            action="create",
            after_data={
                "device_name": device_name,
                "platform": platform,
                "client_version": client_version,
                "status": "active",
            },
        )

        return device_dict, raw_token

    @staticmethod
    def revoke_device(
        conn,
        auth_context: AuthContext,
        device_id: UUID,
        source_request_id: UUID,
    ) -> Dict[str, Any]:
        """
        Revokes a device belonging to the caller's user.
        Raises DeviceNotFoundError if the device does not exist or belongs to another user.
        Requires mandatory source_request_id linking the mutation and audit event to a command receipt.
        """
        if not source_request_id:
            raise ValueError("source_request_id is required for device revocation.")

        device_dict = repo_devices.revoke_device(
            conn,
            device_id=device_id,
            user_id=auth_context.user_id,
            household_id=auth_context.household_id,
        )
        if not device_dict:
            raise DeviceNotFoundError(f"Device {device_id} not found.")

        actor_type, actor_user_id, actor_device_id = get_audit_actor_info(auth_context)
        repo_audit.insert_audit_event(
            conn,
            household_id=auth_context.household_id,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=source_request_id,
            entity_type="device",
            entity_id=device_id,
            action="update",
            after_data={"status": "revoked"},
        )

        return device_dict

    @staticmethod
    def revoke_device_command(
        conn,
        auth_context: AuthContext,
        device_id: UUID,
        idempotency_key: str,
    ) -> Tuple[Dict[str, Any], int]:
        operation = f"POST /api/v1/devices/{device_id}/revoke"
        body: Dict[str, Any] = {}

        def _mutate(c: Any, receipt_id: UUID) -> Tuple[Dict[str, Any], int]:
            revoked_dev = AuthService.revoke_device(
                conn=c,
                auth_context=auth_context,
                device_id=device_id,
                source_request_id=receipt_id,
            )
            result = {
                "device": {
                    "device_id": str(revoked_dev["device_id"]),
                    "user_id": str(revoked_dev["user_id"]),
                    "device_name": revoked_dev["device_name"],
                    "platform": revoked_dev["platform"],
                    "status": revoked_dev["status"],
                    "client_version": revoked_dev.get("client_version"),
                    "created_at": revoked_dev["created_at"].isoformat() if revoked_dev.get("created_at") else None,
                    "last_seen_at": revoked_dev["last_seen_at"].isoformat() if revoked_dev.get("last_seen_at") else None,
                    "revoked_at": revoked_dev["revoked_at"].isoformat() if revoked_dev.get("revoked_at") else None,
                }
            }
            return result, 200

        return execute_durable_command(
            conn=conn,
            auth_context=auth_context,
            idempotency_key=idempotency_key,
            operation=operation,
            body=body,
            mutation_fn=_mutate,
        )
