from typing import Any, Dict, Tuple
from uuid import UUID, uuid4
import psycopg2

from app.auth.context import AuthContext
from app.domain.transactions import (
    AccountResourceNotFoundError,
    AliasResourceNotFoundError,
    AccountAliasConflictError,
    RowVersionConflictError,
)
import app.repositories.accounts as accounts_repo
import app.repositories.audit as audit_repo
from app.services.durable_commands import (
    get_audit_actor_info,
    execute_durable_command,
)


def format_alias(alias_obj: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(alias_obj["id"]),
        "household_id": str(alias_obj["household_id"]),
        "account_id": str(alias_obj["account_id"]),
        "alias": alias_obj["alias_text"],
        "status": alias_obj["status"],
        "row_version": alias_obj.get("row_version", 0),
        "created_at": alias_obj["created_at"].isoformat() if hasattr(alias_obj.get("created_at"), "isoformat") else str(alias_obj.get("created_at")),
        "updated_at": alias_obj["updated_at"].isoformat() if hasattr(alias_obj.get("updated_at"), "isoformat") else str(alias_obj.get("updated_at")),
    }


def create_alias_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"POST /api/v1/accounts/{account_id}/aliases"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        raw_alias = payload.alias.strip()
        normalized = raw_alias.lower()

        if accounts_repo.check_account_alias_exists(c, account_id, normalized, household_id=household_id):
            raise AccountAliasConflictError(raw_alias)

        alias_id = uuid4()
        try:
            accounts_repo.create_account_alias(
                conn=c,
                alias_id=alias_id,
                account_id=account_id,
                alias_text=raw_alias,
                normalized_alias=normalized,
                status="active",
                household_id=household_id,
            )
        except psycopg2.IntegrityError as e:
            if "uq_account_aliases_active" in str(e) or "account_aliases" in str(e):
                raise AccountAliasConflictError(raw_alias)
            raise

        actor_type, actor_user_id, actor_device_id = get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account_alias",
            entity_id=alias_id,
            action="create",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            after_data={"account_id": str(account_id), "alias": raw_alias},
        )

        alias_obj = accounts_repo.get_account_alias(
            c, alias_id=alias_id, household_id=household_id, account_id=account_id
        )
        return format_alias(alias_obj), 201

    return execute_durable_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def patch_alias_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    alias_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"PATCH /api/v1/accounts/{account_id}/aliases/{alias_id}"
    body = payload.model_dump(mode="json", exclude_unset=True)

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        alias_obj = accounts_repo.get_account_alias(
            c, alias_id=alias_id, household_id=household_id, account_id=account_id
        )
        if not alias_obj:
            raise AliasResourceNotFoundError(alias_id)

        expected_ver = payload.expected_version
        if alias_obj.get("row_version") != expected_ver:
            raise RowVersionConflictError()

        clean_alias = payload.alias.strip() if payload.alias is not None else None
        if clean_alias is not None:
            if accounts_repo.check_account_alias_exists(
                c, account_id, clean_alias.lower(), exclude_alias_id=alias_id, household_id=household_id
            ):
                raise AccountAliasConflictError(clean_alias)

        try:
            updated = accounts_repo.update_account_alias(
                conn=c,
                household_id=household_id,
                account_id=account_id,
                alias_id=alias_id,
                expected_version=expected_ver,
                alias_text=clean_alias,
                status=payload.status,
            )
            if not updated:
                raise RowVersionConflictError()
        except psycopg2.IntegrityError as e:
            if "uq_account_aliases_active" in str(e) or "account_aliases" in str(e):
                raise AccountAliasConflictError(clean_alias or "")
            raise

        actor_type, actor_user_id, actor_device_id = get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account_alias",
            entity_id=alias_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data={"alias": alias_obj["alias_text"], "status": alias_obj["status"]},
            after_data={"alias": updated["alias_text"], "status": updated["status"]},
        )

        return format_alias(updated), 200

    return execute_durable_command(conn, auth_context, idempotency_key, operation, body, _mutate)
