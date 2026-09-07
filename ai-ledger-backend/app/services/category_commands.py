from typing import Any, Dict, Tuple
from uuid import UUID, uuid4
import psycopg2
from fastapi import HTTPException

from app.auth.context import AuthContext
from app.domain.transactions import (
    CategoryResourceNotFoundError,
    CategoryNameConflictError,
    RowVersionConflictError,
)
import app.repositories.categories as categories_repo
import app.repositories.audit as audit_repo
from app.services.durable_commands import (
    get_audit_actor_info,
    execute_durable_command,
)


def format_category(cat: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(cat["id"]),
        "household_id": str(cat["household_id"]),
        "name": cat["name"],
        "type": cat["category_type"],
        "category_type": cat["category_type"],
        "description": cat.get("description"),
        "is_fallback": cat.get("is_fallback", False),
        "status": cat["status"],
        "row_version": cat.get("row_version", 0),
    }


def _is_category_conflict(e: psycopg2.IntegrityError) -> bool:
    diag = getattr(e, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name == "uq_categories_active_name"
    return '"uq_categories_active_name"' in str(e) or "'uq_categories_active_name'" in str(e)


def create_category_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = "POST /api/v1/categories"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        clean_name = payload.name.strip()
        clean_desc = payload.description.strip() if payload.description else None

        if categories_repo.check_category_name_exists(c, household_id, payload.type, clean_name):
            raise CategoryNameConflictError(clean_name, payload.type)

        category_id = uuid4()
        try:
            created = categories_repo.create_category(
                conn=c,
                category_id=category_id,
                household_id=household_id,
                name=clean_name,
                category_type=payload.type,
                description=clean_desc,
                is_fallback=False,
                status="active",
            )
        except psycopg2.IntegrityError as e:
            if _is_category_conflict(e):
                raise CategoryNameConflictError(clean_name, payload.type)
            raise

        actor_type, actor_user_id, actor_device_id = get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="category",
            entity_id=category_id,
            action="create",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            after_data={
                "name": clean_name,
                "category_type": payload.type,
                "description": clean_desc,
                "is_fallback": False,
                "status": "active",
            },
        )
        return format_category(created), 201

    return execute_durable_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def patch_category_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    category_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"PATCH /api/v1/categories/{category_id}"
    body = payload.model_dump(mode="json", exclude_unset=True)

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        existing = categories_repo.get_category(c, category_id, household_id)
        if not existing:
            raise CategoryResourceNotFoundError(category_id)

        expected_ver = payload.expected_version
        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        clean_name = payload.name.strip() if payload.name is not None else None
        if clean_name and clean_name.lower() != existing["name"].lower():
            if categories_repo.check_category_name_exists(
                c, household_id, existing["category_type"], clean_name, exclude_category_id=category_id
            ):
                raise CategoryNameConflictError(clean_name, existing["category_type"])

        clean_desc = payload.description.strip() if payload.description is not None else None

        if payload.status == "inactive" and existing.get("is_fallback"):
            raise HTTPException(
                status_code=400,
                detail="Fallback category cannot be archived.",
            )

        try:
            updated = categories_repo.update_category(
                conn=c,
                household_id=household_id,
                category_id=category_id,
                name=clean_name,
                description=clean_desc,
                status=payload.status,
                expected_version=expected_ver,
                fields_set=payload.model_fields_set,
            )
            if not updated:
                raise RowVersionConflictError()
        except psycopg2.IntegrityError as e:
            if _is_category_conflict(e):
                raise CategoryNameConflictError(clean_name or existing["name"], existing["category_type"])
            raise

        actor_type, actor_user_id, actor_device_id = get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="category",
            entity_id=category_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data={
                "name": existing["name"],
                "description": existing.get("description"),
                "status": existing["status"],
            },
            after_data={
                "name": updated["name"],
                "description": updated.get("description"),
                "status": updated["status"],
            },
        )
        return format_category(updated), 200

    return execute_durable_command(conn, auth_context, idempotency_key, operation, body, _mutate)
