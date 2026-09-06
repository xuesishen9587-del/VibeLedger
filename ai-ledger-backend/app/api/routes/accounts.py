from datetime import date
from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
import psycopg2
from fastapi import APIRouter, Depends, Query, status, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict

from app.api.deps import get_db_connection, get_authenticated_actor, require_idempotency_key, get_auth_context
from app.auth.context import AuthContext
import app.services.account_commands as account_commands
from app.db import transaction
from app.domain.money import validate_currency_code
from app.domain.transactions import (
    AccountResourceNotFoundError,
    AliasResourceNotFoundError,
    RowVersionConflictError,
    AccountNameConflictError,
    AccountAliasConflictError,
    CurrencyImmutableError,
    AccountTypeImmutableError,
    UserNotInHouseholdError,
    LinkedAccountInvalidError
)
import app.repositories.accounts as accounts_repo
import app.repositories.audit as audit_repo
from app.repositories.simplified_schema import acquire_household_finance_lock

router = APIRouter(prefix="/api/v1/accounts", tags=["Accounts"])

class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120, description="Account name")
    balance_scope: str = Field(..., min_length=1, max_length=120, description="Balance scope (descriptive free text)")
    account_type: str = Field(..., pattern="^(cash|savings|credit|investment)$", description="Account type")
    currency: str = Field(..., min_length=3, max_length=3, description="3-letter uppercase currency code")
    owner_user_id: Optional[UUID] = Field(None, description="Owning user ID (must belong to household)")
    risk_level: Optional[str] = Field(None, pattern="^(very_low|low|medium|high)$", description="Risk level (not allowed on credit)")
    opened_on: Optional[date] = Field(None, description="Opening business date")
    statement_import_enabled: bool = Field(False, description="Whether statement import is enabled")

class PatchAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=120)
    balance_scope: Optional[str] = Field(None, min_length=1, max_length=120)
    risk_level: Optional[str] = Field(None, pattern="^(very_low|low|medium|high)$")
    owner_user_id: Optional[UUID] = None
    opened_on: Optional[date] = None
    statement_import_enabled: Optional[bool] = None
    account_type: Optional[str] = Field(None, pattern="^(cash|savings|credit|investment)$")
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    expected_version: int = Field(..., ge=0, description="Optimistic concurrency control version")
    reason: Optional[str] = None

class CloseAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=0)
    closed_on: date = Field(..., description="Effective closing date")
    closing_snapshot_id: UUID = Field(..., description="Snapshot proving zero balance")
    reason: Optional[str] = None

class ReopenAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=0)
    reason: Optional[str] = None

class CancelAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=0)
    reason: Optional[str] = None

class CreateAliasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., min_length=1, max_length=120, description="Alias text")

class PatchAliasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(..., ge=0)
    alias: Optional[str] = Field(None, min_length=1, max_length=120)
    status: Optional[str] = Field(None, pattern="^(active|inactive)$")

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

def _format_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    curr = acc["currency"]
    return {
        "id": str(acc["id"]),
        "household_id": str(acc["household_id"]) if acc.get("household_id") else None,
        "name": acc["name"],
        "balance_scope": acc.get("balance_scope", "liability" if acc["account_type"] == "credit" else "asset"),
        "account_type": acc["account_type"],
        "currency": curr,
        "owner_user_id": str(acc["owner_user_id"]) if acc.get("owner_user_id") else None,
        "risk_level": acc.get("risk_level"),
        "opened_on": acc["opened_on"].isoformat() if hasattr(acc.get("opened_on"), "isoformat") else (str(acc["opened_on"]) if acc.get("opened_on") else None),
        "closed_on": acc["closed_on"].isoformat() if hasattr(acc.get("closed_on"), "isoformat") else (str(acc["closed_on"]) if acc.get("closed_on") else None),
        "status": acc["status"],
        "statement_import_enabled": acc.get("statement_import_enabled", False),
        "row_version": acc.get("row_version", 0),
        "latest_snapshot": acc.get("latest_snapshot"),
    }

@router.get("", summary="List Household Accounts")
def list_accounts(
    status: Optional[str] = Query(None, pattern="^(active|closed|cancelled)$"),
    account_type: Optional[str] = Query(None, pattern="^(cash|savings|credit|investment)$"),
    owner_user_id: Optional[UUID] = Query(None),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Lists accounts belonging to the authenticated household.
    """
    accounts = accounts_repo.list_accounts(
        conn=conn,
        household_id=device["household_id"],
        status=status,
        account_type=account_type,
        owner_user_id=owner_user_id
    )
    return {"items": [_format_account(a) for a in accounts], "next_cursor": None}

@router.get("/{account_id}", summary="Get Account Details")
def get_account(
    account_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Retrieves detail of an account in the authenticated household.
    """
    household_id = device["household_id"]
    acc = accounts_repo.get_account(conn, account_id, household_id)
    if not acc:
        raise AccountResourceNotFoundError(account_id)
    return _format_account(acc)

@router.post("", status_code=status.HTTP_201_CREATED, summary="Create Account")
def create_account(
    payload: CreateAccountRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection)
) -> JSONResponse:
    """
    Creates a new account in the authenticated household as a short durable command.
    Spending and balance observations are independent; does not create transactions or snapshots.
    """
    res, status_code = account_commands.create_account_command(
        conn=conn,
        auth_context=auth_context,
        idempotency_key=idempotency_key,
        payload=payload
    )
    return JSONResponse(status_code=status_code, content=res)

@router.patch("/{account_id}", summary="Update Account Metadata")
def patch_account(
    account_id: UUID,
    payload: PatchAccountRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection)
) -> JSONResponse:
    """
    Updates mutable metadata on an account using row_version optimistic concurrency control as a short durable command.
    """
    res, status_code = account_commands.patch_account_command(
        conn=conn,
        auth_context=auth_context,
        idempotency_key=idempotency_key,
        account_id=account_id,
        payload=payload
    )
    return JSONResponse(status_code=status_code, content=res)

@router.post("/{account_id}/close", summary="Close Account")
def close_account(
    account_id: UUID,
    payload: CloseAccountRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection)
) -> JSONResponse:
    """
    Closes an active account as a short durable command. Requires expected_version, closing_snapshot_id (explicit zero balance), and closed_on.
    """
    res, status_code = account_commands.close_account_command(
        conn=conn,
        auth_context=auth_context,
        idempotency_key=idempotency_key,
        account_id=account_id,
        payload=payload
    )
    return JSONResponse(status_code=status_code, content=res)

@router.post("/{account_id}/reopen", summary="Reopen Account")
def reopen_account(
    account_id: UUID,
    payload: ReopenAccountRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection)
) -> JSONResponse:
    """
    Reopens a closed account as a short durable command, clearing closed_on.
    """
    res, status_code = account_commands.reopen_account_command(
        conn=conn,
        auth_context=auth_context,
        idempotency_key=idempotency_key,
        account_id=account_id,
        payload=payload
    )
    return JSONResponse(status_code=status_code, content=res)

@router.post("/{account_id}/cancel", summary="Cancel Account")
def cancel_account(
    account_id: UUID,
    payload: CancelAccountRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    auth_context: AuthContext = Depends(get_auth_context),
    conn: Any = Depends(get_db_connection)
) -> JSONResponse:
    """
    Cancels an unused account with no financial references as a short durable command.
    """
    res, status_code = account_commands.cancel_account_command(
        conn=conn,
        auth_context=auth_context,
        idempotency_key=idempotency_key,
        account_id=account_id,
        payload=payload
    )
    return JSONResponse(status_code=status_code, content=res)


# --- Aliases ---

@router.get("/{account_id}/aliases", summary="List Account Aliases")
def list_account_aliases(
    account_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    aliases = accounts_repo.list_account_aliases(conn, account_id, household_id=household_id)
    return {
        "items": [
            {
                "id": str(a["id"]),
                "household_id": str(a["household_id"]),
                "account_id": str(a["account_id"]),
                "alias": a["alias_text"],
                "status": a.get("status", "active"),
                "row_version": a.get("row_version", 0),
                "created_at": a["created_at"].isoformat() if hasattr(a.get("created_at"), "isoformat") else str(a.get("created_at")),
                "updated_at": a["updated_at"].isoformat() if hasattr(a.get("updated_at"), "isoformat") else str(a.get("updated_at"))
            }
            for a in aliases
        ],
        "next_cursor": None
    }

@router.post("/{account_id}/aliases", status_code=status.HTTP_201_CREATED, summary="Create Account Alias")
def create_account_alias(
    account_id: UUID,
    payload: CreateAliasRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    raw_alias = payload.alias.strip()
    normalized = raw_alias.lower()

    if accounts_repo.check_account_alias_exists(conn, account_id, normalized, household_id=household_id):
        raise AccountAliasConflictError(raw_alias)

    alias_id = uuid4()
    try:
        with transaction(conn):
            accounts_repo.create_account_alias(
                conn=conn,
                alias_id=alias_id,
                account_id=account_id,
                alias_text=raw_alias,
                normalized_alias=normalized,
                status='active',
                household_id=household_id
            )
            actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type=actor_type,
                entity_type="account_alias",
                entity_id=alias_id,
                action="create",
                actor_user_id=actor_user_id,
                actor_device_id=actor_device_id,
                after_data={"account_id": str(account_id), "alias": raw_alias}
            )
    except psycopg2.IntegrityError as e:
        if "uq_account_aliases_active" in str(e) or "account_aliases" in str(e):
            raise AccountAliasConflictError(raw_alias)
        raise

    alias_obj = accounts_repo.get_account_alias(conn, alias_id=alias_id, household_id=household_id, account_id=account_id)
    return {
        "id": str(alias_obj["id"]),
        "household_id": str(alias_obj["household_id"]),
        "account_id": str(alias_obj["account_id"]),
        "alias": alias_obj["alias_text"],
        "status": alias_obj["status"],
        "row_version": alias_obj.get("row_version", 0),
        "created_at": alias_obj["created_at"].isoformat() if hasattr(alias_obj.get("created_at"), "isoformat") else str(alias_obj.get("created_at")),
        "updated_at": alias_obj["updated_at"].isoformat() if hasattr(alias_obj.get("updated_at"), "isoformat") else str(alias_obj.get("updated_at"))
    }

@router.patch("/{account_id}/aliases/{alias_id}", summary="Update Account Alias")
def patch_account_alias(
    account_id: UUID,
    alias_id: UUID,
    payload: PatchAliasRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    alias_obj = accounts_repo.get_account_alias(conn, alias_id=alias_id, household_id=household_id, account_id=account_id)
    if not alias_obj:
        raise AliasResourceNotFoundError(alias_id)

    expected_ver = payload.expected_version
    if alias_obj.get("row_version") != expected_ver:
        raise RowVersionConflictError()

    clean_alias = payload.alias.strip() if payload.alias is not None else None
    if clean_alias is not None:
        if accounts_repo.check_account_alias_exists(conn, account_id, clean_alias.lower(), exclude_alias_id=alias_id, household_id=household_id):
            raise AccountAliasConflictError(clean_alias)

    try:
        with transaction(conn):
            updated = accounts_repo.update_account_alias(
                conn=conn,
                household_id=household_id,
                account_id=account_id,
                alias_id=alias_id,
                expected_version=expected_ver,
                alias_text=clean_alias,
                status=payload.status
            )
            if not updated:
                raise RowVersionConflictError()

            actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
            audit_repo.insert_audit_event(
                conn=conn,
                household_id=household_id,
                actor_type=actor_type,
                entity_type="account_alias",
                entity_id=alias_id,
                action="update",
                actor_user_id=actor_user_id,
                actor_device_id=actor_device_id,
                before_data={"alias": alias_obj["alias_text"], "status": alias_obj["status"]},
                after_data={"alias": updated["alias_text"], "status": updated["status"]}
            )
    except psycopg2.IntegrityError as e:
        if "uq_account_aliases_active" in str(e) or "account_aliases" in str(e):
            raise AccountAliasConflictError(clean_alias or "")
        raise

    return {
        "id": str(updated["id"]),
        "household_id": str(updated["household_id"]),
        "account_id": str(updated["account_id"]),
        "alias": updated["alias_text"],
        "status": updated["status"],
        "row_version": updated["row_version"],
        "created_at": updated["created_at"].isoformat() if hasattr(updated["created_at"], "isoformat") else str(updated["created_at"]),
        "updated_at": updated["updated_at"].isoformat() if hasattr(updated["updated_at"], "updated_at") else str(updated["updated_at"])
    }
