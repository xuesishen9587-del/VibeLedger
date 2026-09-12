from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict

from app.api.deps import get_db_connection, get_authenticated_actor, require_idempotency_key, get_auth_context
from app.auth.context import AuthContext
from app.domain.transactions import CategoryResourceNotFoundError
import app.repositories.categories as categories_repo
import app.services.category_commands as category_commands

router = APIRouter(prefix="/api/v1/categories", tags=["Categories"])

class CreateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100, description="Category name")
    type: str = Field(..., pattern="^(expense|income)$", description="Category type (expense or income)")
    description: Optional[str] = Field(None, max_length=500, description="Category description")

class PatchCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=100, description="New category name")
    description: Optional[str] = Field(None, max_length=500, description="Category description")
    status: Optional[str] = Field(None, pattern="^(active|inactive)$", description="Category status")
    expected_version: int = Field(..., ge=0, description="Optimistic concurrency control version")

def _format_category(cat: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(cat["id"]),
        "household_id": str(cat["household_id"]),
        "name": cat["name"],
        "type": cat["category_type"],
        "category_type": cat["category_type"],
        "description": cat.get("description"),
        "is_fallback": cat.get("is_fallback", False),
        "status": cat["status"],
        "row_version": cat.get("row_version", 0)
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
    return {"items": [_format_category(c) for c in categories], "next_cursor": None}

@router.get("/{category_id}", summary="Get Category")
def get_category(
    category_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    household_id = device["household_id"]
    existing = categories_repo.get_category(conn, category_id, household_id)
    if not existing:
        raise CategoryResourceNotFoundError(category_id)
    return _format_category(existing)

@router.post("", status_code=status.HTTP_201_CREATED, summary="Create Category")
def create_category(
    payload: CreateCategoryRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection)
) -> JSONResponse:
    """
    Creates a new category under the authenticated household as a short durable command.
    """
    res, status_code = category_commands.create_category_command(
        conn=conn,
        auth_context=auth_context,
        idempotency_key=idempotency_key,
        payload=payload
    )
    return JSONResponse(status_code=status_code, content=res)

@router.patch("/{category_id}", summary="Update Category")
def patch_category(
    category_id: UUID,
    payload: PatchCategoryRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection)
) -> JSONResponse:
    """
    Updates an existing category within the authenticated household as a short durable command.
    """
    res, status_code = category_commands.patch_category_command(
        conn=conn,
        auth_context=auth_context,
        idempotency_key=idempotency_key,
        category_id=category_id,
        payload=payload
    )
    return JSONResponse(status_code=status_code, content=res)


