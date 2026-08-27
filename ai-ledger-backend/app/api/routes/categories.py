from typing import Optional, Dict, Any
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_actor
from app.db import transaction
from app.domain.transactions import (
    CategoryResourceNotFoundError,
    CategoryNameConflictError
)
import app.repositories.categories as categories_repo
import app.repositories.audit as audit_repo

router = APIRouter(prefix="/api/v1/categories", tags=["Categories"])

class CreateCategoryRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Category name")
    type: str = Field(..., pattern="^(expense|income)$", description="Category type (expense or income)")

class PatchCategoryRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="New category name")

def _get_audit_actor_info(actor: Dict[str, Any]) -> tuple[str, Optional[UUID], Optional[UUID]]:
    auth_mode = actor.get("auth_mode")
    user_id = actor.get("user_id")
    device_id = actor.get("device_id")
    if auth_mode == "browser":
        return "user", user_id, None
    elif auth_mode == "device":
        return "device", user_id, device_id
    else:
        if device_id is not None:
            return "device", user_id, device_id
        return "user", user_id, None

def _format_category(cat: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(cat["id"]),
        "name": cat["name"],
        "type": cat["category_type"],
        "status": cat["status"]
    }

@router.get("", summary="List Categories")
def list_categories(
    type: Optional[str] = Query(None, pattern="^(expense|income)$", description="Filter by category type"),
    status: Optional[str] = Query(None, pattern="^(active|inactive)$", description="Filter by status"),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Lists active and/or inactive categories belonging to the authenticated household.
    """
    categories = categories_repo.list_categories(
        conn=conn,
        household_id=device["household_id"],
        category_type=type,
        status=status
    )
    return {"items": [_format_category(c) for c in categories]}

@router.post("", status_code=status.HTTP_201_CREATED, summary="Create Category")
def create_category(
    payload: CreateCategoryRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Creates a new category under the authenticated household.
    """
    household_id = device["household_id"]
    clean_name = payload.name.strip()

    if categories_repo.check_category_name_exists(conn, household_id, payload.type, clean_name):
        raise CategoryNameConflictError(clean_name, payload.type)

    category_id = uuid4()
    with transaction(conn):
        created = categories_repo.create_category(
            conn=conn,
            category_id=category_id,
            household_id=household_id,
            name=clean_name,
            category_type=payload.type,
            status='active'
        )
        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="category",
            entity_id=category_id,
            action="create",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            after_data={"name": clean_name, "category_type": payload.type}
        )

    return _format_category(created)

@router.patch("/{category_id}", summary="Rename Category")
def patch_category(
    category_id: UUID,
    payload: PatchCategoryRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Renames an existing category within the authenticated household.
    """
    household_id = device["household_id"]
    existing = categories_repo.get_category(conn, category_id, household_id)
    if not existing:
        raise CategoryResourceNotFoundError(category_id)

    clean_name = payload.name.strip()
    if clean_name.lower() != existing["name"].lower():
        if categories_repo.check_category_name_exists(
            conn, household_id, existing["category_type"], clean_name, exclude_category_id=category_id
        ):
            raise CategoryNameConflictError(clean_name, existing["category_type"])

    with transaction(conn):
        updated = categories_repo.update_category(conn, category_id, clean_name)
        if not updated:
            raise CategoryResourceNotFoundError(category_id)

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="category",
            entity_id=category_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            before_data={"name": existing["name"]},
            after_data={"name": clean_name}
        )

    return _format_category(updated)

@router.post("/{category_id}/deactivate", summary="Deactivate Category")
def deactivate_category(
    category_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Soft-deactivates a category. Inactive categories cannot be selected for new financial transactions.
    """
    household_id = device["household_id"]
    existing = categories_repo.get_category(conn, category_id, household_id)
    if not existing:
        raise CategoryResourceNotFoundError(category_id)

    with transaction(conn):
        deactivated = categories_repo.deactivate_category(conn, category_id)
        if not deactivated:
            raise CategoryResourceNotFoundError(category_id)

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="category",
            entity_id=category_id,
            action="soft_delete",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            before_data={"status": existing["status"]},
            after_data={"status": "inactive"}
        )

    return _format_category(deactivated)


