from datetime import date
from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, Query, status, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_actor
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

router = APIRouter(prefix="/api/v1/accounts", tags=["Accounts"])

class CreateAccountRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Account name")
    account_type: str = Field(..., pattern="^(cash|savings|credit|investment)$", description="Account type")
    balance_scope: Optional[str] = Field(None, pattern="^(asset|liability)$", description="Balance scope (asset or liability)")
    currency: str = Field(..., min_length=3, max_length=3, description="3-letter uppercase currency code")
    owner_user_id: Optional[UUID] = Field(None, description="Owning user ID (must belong to household)")
    risk_level: Optional[str] = Field(None, pattern="^(very_low|low|medium|high)$", description="Risk level (not allowed on credit)")
    opened_on: Optional[date] = Field(None, description="Opening business date")
    statement_import_enabled: bool = Field(False, description="Whether statement import is enabled")
    # Legacy fields ignored gracefully
    institution: Optional[str] = Field(None, max_length=100)
    linked_cash_account_id: Optional[UUID] = None
    billing_day: Optional[int] = None
    due_day: Optional[int] = None

class PatchAccountRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    balance_scope: Optional[str] = Field(None, pattern="^(asset|liability)$")
    risk_level: Optional[str] = Field(None, pattern="^(very_low|low|medium|high)$")
    owner_user_id: Optional[UUID] = None
    opened_on: Optional[date] = None
    statement_import_enabled: Optional[bool] = None
    account_type: Optional[str] = Field(None, pattern="^(cash|savings|credit|investment)$")
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    row_version: Optional[int] = Field(None, ge=0, description="Optimistic concurrency control version")
    expected_version: Optional[int] = Field(None, ge=0, description="Optimistic concurrency control version")
    reason: Optional[str] = None
    # Legacy fields ignored gracefully
    institution: Optional[str] = None
    linked_cash_account_id: Optional[UUID] = None
    billing_day: Optional[int] = None
    due_day: Optional[int] = None

class CloseAccountRequest(BaseModel):
    expected_version: Optional[int] = Field(None, ge=0)
    row_version: Optional[int] = Field(None, ge=0)
    closed_on: Optional[date] = None
    closing_snapshot_id: Optional[UUID] = None

class ReopenAccountRequest(BaseModel):
    expected_version: Optional[int] = Field(None, ge=0)
    row_version: Optional[int] = Field(None, ge=0)
    reason: Optional[str] = None

class CancelAccountRequest(BaseModel):
    expected_version: Optional[int] = Field(None, ge=0)
    row_version: Optional[int] = Field(None, ge=0)
    reason: Optional[str] = None

class CreateAliasRequest(BaseModel):
    alias: str = Field(..., min_length=1, max_length=120, description="Alias text")

class PatchAliasRequest(BaseModel):
    expected_version: Optional[int] = Field(None, ge=0)
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
    status: Optional[str] = Query(None, pattern="^(active|closed|cancelled|inactive)$"),
    account_type: Optional[str] = Query(None, pattern="^(cash|savings|credit|investment)$"),
    owner_user_id: Optional[UUID] = Query(None),
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Lists accounts belonging to the authenticated household.
    """
    # Map legacy 'inactive' status query to 'closed'
    query_status = "closed" if status == "inactive" else status

    accounts = accounts_repo.list_accounts(
        conn=conn,
        household_id=device["household_id"],
        status=query_status,
        account_type=account_type,
        owner_user_id=owner_user_id
    )
    return {"items": [_format_account(a) for a in accounts]}

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
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Creates a new account in the authenticated household.
    Spending and balance observations are independent; does not create transactions or snapshots.
    """
    currency = validate_currency_code(payload.currency)
    household_id = device["household_id"]

    if payload.account_type == "credit" and payload.risk_level is not None:
        raise LinkedAccountInvalidError("Credit accounts cannot have a risk level.")

    balance_scope = payload.balance_scope
    if not balance_scope:
        balance_scope = "liability" if payload.account_type == "credit" else "asset"

    opened_on = payload.opened_on or date.today()

    if payload.owner_user_id is not None:
        if not accounts_repo.check_user_in_household(conn, payload.owner_user_id, household_id):
            raise UserNotInHouseholdError(payload.owner_user_id)

    if accounts_repo.check_account_name_exists(conn, household_id, payload.name):
        raise AccountNameConflictError(payload.name)

    account_id = uuid4()

    with transaction(conn):
        created = accounts_repo.create_account(
            conn=conn,
            account_id=account_id,
            household_id=household_id,
            name=payload.name.strip(),
            balance_scope=balance_scope,
            account_type=payload.account_type,
            currency=currency,
            owner_user_id=payload.owner_user_id,
            risk_level=payload.risk_level,
            opened_on=opened_on,
            closed_on=None,
            status='active',
            statement_import_enabled=payload.statement_import_enabled
        )
        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="create",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            after_data={
                "name": payload.name.strip(),
                "balance_scope": balance_scope,
                "account_type": payload.account_type,
                "currency": currency,
                "owner_user_id": str(payload.owner_user_id) if payload.owner_user_id else None,
                "risk_level": payload.risk_level,
                "opened_on": opened_on.isoformat(),
                "closed_on": None,
                "status": "active",
                "statement_import_enabled": payload.statement_import_enabled,
            }
        )

    return _format_account(created)

@router.patch("/{account_id}", summary="Update Account Metadata")
def patch_account(
    account_id: UUID,
    payload: PatchAccountRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Updates mutable metadata on an account using row_version optimistic concurrency control.
    """
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    expected_ver = payload.expected_version if payload.expected_version is not None else payload.row_version
    if expected_ver is None:
        raise HTTPException(status_code=400, detail="row_version or expected_version is required.")
    if existing["row_version"] != expected_ver:
        raise RowVersionConflictError()

    fields_set = payload.model_fields_set

    # 1. Determine complete resulting state
    if "name" in fields_set:
        if payload.name is None or not payload.name.strip():
            raise LinkedAccountInvalidError("Account name cannot be empty.")
        new_name = payload.name.strip()
    else:
        new_name = existing["name"]

    if "balance_scope" in fields_set and payload.balance_scope is not None:
        new_scope = payload.balance_scope
    else:
        new_scope = existing.get("balance_scope", "asset")

    if "risk_level" in fields_set:
        new_risk = payload.risk_level
    else:
        new_risk = existing.get("risk_level")

    if "owner_user_id" in fields_set:
        new_owner = payload.owner_user_id
    else:
        new_owner = existing.get("owner_user_id")

    if "opened_on" in fields_set and payload.opened_on is not None:
        new_opened_on = payload.opened_on
    else:
        new_opened_on = existing["opened_on"]

    if "statement_import_enabled" in fields_set and payload.statement_import_enabled is not None:
        new_stmt_enabled = payload.statement_import_enabled
    else:
        new_stmt_enabled = existing.get("statement_import_enabled", False)

    if "account_type" in fields_set and payload.account_type is not None:
        new_type = payload.account_type
    else:
        new_type = existing["account_type"]

    if "currency" in fields_set and payload.currency is not None:
        new_currency = validate_currency_code(payload.currency)
    else:
        new_currency = existing["currency"]

    # 2. Validate complete resulting state
    if new_type == "credit" and new_risk is not None:
        raise LinkedAccountInvalidError("Credit accounts cannot have a risk level.")

    if new_currency != existing["currency"]:
        if accounts_repo.has_financial_history(conn, account_id):
            raise CurrencyImmutableError()

    if new_type != existing["account_type"]:
        if accounts_repo.has_financial_history(conn, account_id):
            raise AccountTypeImmutableError()

    if new_name.lower() != existing["name"].lower():
        if accounts_repo.check_account_name_exists(conn, household_id, new_name, exclude_account_id=account_id):
            raise AccountNameConflictError(new_name)

    if new_owner is not None and new_owner != existing.get("owner_user_id"):
        if not accounts_repo.check_user_in_household(conn, new_owner, household_id):
            raise UserNotInHouseholdError(new_owner)

    with transaction(conn):
        updated = accounts_repo.update_account(
            conn=conn,
            household_id=household_id,
            account_id=account_id,
            name=new_name,
            balance_scope=new_scope,
            account_type=new_type,
            currency=new_currency,
            owner_user_id=new_owner,
            risk_level=new_risk,
            opened_on=new_opened_on,
            statement_import_enabled=new_stmt_enabled,
            expected_row_version=expected_ver
        )
        if not updated:
            raise RowVersionConflictError()

        before_data = {
            "name": existing["name"],
            "balance_scope": existing.get("balance_scope"),
            "account_type": existing["account_type"],
            "currency": existing["currency"],
            "owner_user_id": str(existing["owner_user_id"]) if existing.get("owner_user_id") else None,
            "risk_level": existing.get("risk_level"),
            "opened_on": existing["opened_on"].isoformat() if hasattr(existing.get("opened_on"), "isoformat") else str(existing.get("opened_on")),
            "status": existing["status"],
            "statement_import_enabled": existing.get("statement_import_enabled", False),
        }
        after_data = {
            "name": updated["name"],
            "balance_scope": updated.get("balance_scope"),
            "account_type": updated["account_type"],
            "currency": updated["currency"],
            "owner_user_id": str(updated["owner_user_id"]) if updated.get("owner_user_id") else None,
            "risk_level": updated.get("risk_level"),
            "opened_on": updated["opened_on"].isoformat() if hasattr(updated.get("opened_on"), "isoformat") else str(updated.get("opened_on")),
            "status": updated["status"],
            "statement_import_enabled": updated.get("statement_import_enabled", False),
        }

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            before_data=before_data,
            after_data=after_data
        )

    return _format_account(updated)

@router.post("/{account_id}/close", summary="Close Account")
def close_account(
    account_id: UUID,
    payload: CloseAccountRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Closes an active account. Requires expected_version and valid closed_on date.
    """
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    expected_ver = payload.expected_version if payload.expected_version is not None else payload.row_version
    if expected_ver is None:
        raise HTTPException(status_code=400, detail="expected_version is required.")
    if existing["row_version"] != expected_ver:
        raise RowVersionConflictError()

    if existing["status"] != "active":
        raise HTTPException(status_code=400, detail="Only active accounts can be closed.")

    closed_on = payload.closed_on or date.today()
    if closed_on < existing["opened_on"]:
        raise HTTPException(status_code=400, detail="closed_on cannot be earlier than opened_on.")

    with transaction(conn):
        updated = accounts_repo.close_account(conn, household_id, account_id, expected_ver, closed_on)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="close",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            before_data={"status": "active", "closed_on": None},
            after_data={"status": "closed", "closed_on": closed_on.isoformat()}
        )

    return _format_account(updated)

@router.post("/{account_id}/reopen", summary="Reopen Account")
def reopen_account(
    account_id: UUID,
    payload: ReopenAccountRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Reopens a closed account, clearing closed_on.
    """
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    expected_ver = payload.expected_version if payload.expected_version is not None else payload.row_version
    if expected_ver is None:
        raise HTTPException(status_code=400, detail="expected_version is required.")
    if existing["row_version"] != expected_ver:
        raise RowVersionConflictError()

    if existing["status"] != "closed":
        raise HTTPException(status_code=400, detail="Only closed accounts can be reopened.")

    with transaction(conn):
        updated = accounts_repo.reopen_account(conn, household_id, account_id, expected_ver)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="reopen",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            before_data={"status": "closed", "closed_on": existing["closed_on"].isoformat() if existing.get("closed_on") else None},
            after_data={"status": "active", "closed_on": None}
        )

    return _format_account(updated)

@router.post("/{account_id}/cancel", summary="Cancel Account")
def cancel_account(
    account_id: UUID,
    payload: CancelAccountRequest,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Cancels an unused account with no financial references.
    """
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    expected_ver = payload.expected_version if payload.expected_version is not None else payload.row_version
    if expected_ver is None:
        raise HTTPException(status_code=400, detail="expected_version is required.")
    if existing["row_version"] != expected_ver:
        raise RowVersionConflictError()

    if existing["status"] != "active":
        raise HTTPException(status_code=400, detail="Only active accounts can be cancelled.")

    if accounts_repo.has_financial_history(conn, account_id):
        raise HTTPException(status_code=400, detail="Cannot cancel account with existing financial history.")

    with transaction(conn):
        updated = accounts_repo.cancel_account(conn, household_id, account_id, expected_ver)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(device)
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            before_data={"status": "active"},
            after_data={"status": "cancelled"}
        )

    return _format_account(updated)

@router.post("/{account_id}/deactivate", summary="Deactivate Account (Legacy mapping to close)")
def deactivate_account(
    account_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Legacy deactivate endpoint mapped to close_account for backwards compatibility.
    """
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    return close_account(
        account_id=account_id,
        payload=CloseAccountRequest(expected_version=existing["row_version"], closed_on=date.today()),
        device=device,
        conn=conn
    )

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

    aliases = accounts_repo.list_account_aliases(conn, account_id)
    return {
        "items": [
            {
                "id": str(a["id"]),
                "account_id": str(a["account_id"]),
                "alias": a["alias_text"],
                "status": a.get("status", "active"),
                "created_at": a["created_at"].isoformat() if hasattr(a.get("created_at"), "isoformat") else str(a.get("created_at"))
            }
            for a in aliases
        ]
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

    if accounts_repo.check_account_alias_exists(conn, account_id, normalized):
        raise AccountAliasConflictError(raw_alias)

    alias_id = uuid4()
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

    alias_obj = accounts_repo.get_account_alias(conn, alias_id, account_id)
    return {
        "id": str(alias_obj["id"]),
        "account_id": str(alias_obj["account_id"]),
        "alias": alias_obj["alias_text"],
        "created_at": alias_obj["created_at"].isoformat() if hasattr(alias_obj.get("created_at"), "isoformat") else str(alias_obj.get("created_at"))
    }

@router.delete("/{account_id}/aliases/{alias_id}", summary="Deactivate Account Alias")
def delete_account_alias(
    account_id: UUID,
    alias_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_actor),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    alias = accounts_repo.get_account_alias(conn, alias_id, account_id)
    if not alias or alias.get("status") != "active":
        raise AliasResourceNotFoundError(alias_id)

    with transaction(conn):
        deactivated = accounts_repo.deactivate_account_alias(conn, alias_id, account_id)
        if not deactivated:
            raise AliasResourceNotFoundError(alias_id)

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
            before_data={"status": "active"},
            after_data={"status": "inactive"}
        )

    return {
        "status": "deactivated",
        "id": str(alias_id),
        "account_id": str(account_id)
    }
