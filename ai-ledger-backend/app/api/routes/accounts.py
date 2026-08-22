from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import get_db_connection, get_authenticated_device
from app.db import transaction
from app.domain.money import validate_currency_code, quantize_money
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
    name: str = Field(..., min_length=1, max_length=100, description="Account name")
    institution: Optional[str] = Field(None, max_length=100, description="Financial institution")
    account_type: str = Field(..., pattern="^(cash|savings|credit|investment)$", description="Account type")
    currency: str = Field(..., min_length=3, max_length=3, description="3-letter uppercase currency code")
    owner_user_id: Optional[UUID] = Field(None, description="Owning user ID (must belong to household)")
    linked_cash_account_id: Optional[UUID] = Field(None, description="Linked cash account ID")
    billing_day: Optional[int] = Field(None, ge=1, le=31, description="Billing day (credit accounts only)")
    due_day: Optional[int] = Field(None, ge=1, le=31, description="Due day (credit accounts only)")

class PatchAccountRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    institution: Optional[str] = Field(None, max_length=100)
    owner_user_id: Optional[UUID] = None
    linked_cash_account_id: Optional[UUID] = None
    billing_day: Optional[int] = Field(None, ge=1, le=31)
    due_day: Optional[int] = Field(None, ge=1, le=31)
    account_type: Optional[str] = Field(None, pattern="^(cash|savings|credit|investment)$")
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    row_version: int = Field(..., ge=0, description="Optimistic concurrency control version")

class CreateAliasRequest(BaseModel):
    alias: str = Field(..., min_length=1, max_length=100, description="Alias text")

def _format_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    curr = acc["currency"]
    bal = acc.get("ledger_balance", 0)
    last_snap = acc.get("last_authoritative_snapshot_at")
    
    return {
        "id": str(acc["id"]),
        "name": acc["name"],
        "institution": acc.get("institution"),
        "account_type": acc["account_type"],
        "currency": curr,
        "owner_user_id": str(acc["owner_user_id"]) if acc.get("owner_user_id") else None,
        "linked_cash_account_id": str(acc["linked_cash_account_id"]) if acc.get("linked_cash_account_id") else None,
        "billing_day": acc.get("billing_day"),
        "due_day": acc.get("due_day"),
        "status": acc["status"],
        "row_version": acc.get("row_version", 0),
        "state": {
            "ledger_balance": f"{quantize_money(bal, curr):.2f}",
            "last_authoritative_snapshot_at": last_snap.isoformat() if last_snap else None
        }
    }

@router.get("", summary="List Household Accounts")
def list_accounts(
    status: Optional[str] = Query(None, pattern="^(active|inactive)$"),
    account_type: Optional[str] = Query(None, pattern="^(cash|savings|credit|investment)$"),
    owner_user_id: Optional[UUID] = Query(None),
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Lists accounts belonging to the authenticated household with current state projections.
    """
    accounts = accounts_repo.list_accounts(
        conn=conn,
        household_id=device["household_id"],
        status=status,
        account_type=account_type,
        owner_user_id=owner_user_id
    )
    return {"items": [_format_account(a) for a in accounts]}

@router.post("", status_code=status.HTTP_201_CREATED, summary="Create Account")
def create_account(
    payload: CreateAccountRequest,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Creates a new account in the authenticated household with an initial account_state row.
    """
    currency = validate_currency_code(payload.currency)
    household_id = device["household_id"]

    if payload.account_type != "credit":
        if payload.billing_day is not None or payload.due_day is not None:
            raise LinkedAccountInvalidError("Billing day and due day are only allowed on credit accounts.")

    if payload.owner_user_id is not None:
        if not accounts_repo.check_user_in_household(conn, payload.owner_user_id, household_id):
            raise UserNotInHouseholdError(payload.owner_user_id)

    if payload.linked_cash_account_id is not None:
        linked_acc = accounts_repo.get_account(conn, payload.linked_cash_account_id)
        if not linked_acc or linked_acc["household_id"] != household_id or linked_acc["account_type"] != "cash":
            raise LinkedAccountInvalidError("Linked cash account must be an active cash account in the same household.")

    if accounts_repo.check_account_name_exists(conn, household_id, payload.name):
        raise AccountNameConflictError(payload.name)

    account_id = uuid4()

    with transaction(conn):
        accounts_repo.create_account(
            conn=conn,
            account_id=account_id,
            household_id=household_id,
            name=payload.name.strip(),
            account_type=payload.account_type,
            currency=currency,
            institution=payload.institution.strip() if payload.institution else None,
            owner_user_id=payload.owner_user_id,
            linked_cash_account_id=payload.linked_cash_account_id,
            billing_day=payload.billing_day,
            due_day=payload.due_day,
            status='active'
        )
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device",
            entity_type="account",
            entity_id=account_id,
            action="create",
            actor_user_id=device.get("user_id"),
            actor_device_id=device.get("device_id"),
            after_data={
                "name": payload.name.strip(),
                "account_type": payload.account_type,
                "currency": currency,
                "institution": payload.institution
            }
        )

    acc = accounts_repo.get_account_with_state(conn, account_id, household_id)
    return _format_account(acc)

@router.patch("/{account_id}", summary="Update Account Metadata")
def patch_account(
    account_id: UUID,
    payload: PatchAccountRequest,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Updates mutable metadata on an account using row_version optimistic concurrency control.
    """
    household_id = device["household_id"]
    existing = accounts_repo.get_account_with_state(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    if existing["row_version"] != payload.row_version:
        raise RowVersionConflictError()

    if payload.currency is not None and payload.currency.strip().upper() != existing["currency"]:
        if accounts_repo.has_financial_history(conn, account_id):
            raise CurrencyImmutableError()

    if payload.account_type is not None and payload.account_type != existing["account_type"]:
        if accounts_repo.has_financial_history(conn, account_id):
            raise AccountTypeImmutableError()

    target_type = payload.account_type or existing["account_type"]
    if target_type != "credit":
        if payload.billing_day is not None or payload.due_day is not None:
            raise LinkedAccountInvalidError("Billing day and due day are only allowed on credit accounts.")

    if payload.name is not None and payload.name.strip().lower() != existing["name"].lower():
        if accounts_repo.check_account_name_exists(conn, household_id, payload.name, exclude_account_id=account_id):
            raise AccountNameConflictError(payload.name)

    if payload.owner_user_id is not None and payload.owner_user_id != existing["owner_user_id"]:
        if not accounts_repo.check_user_in_household(conn, payload.owner_user_id, household_id):
            raise UserNotInHouseholdError(payload.owner_user_id)

    if payload.linked_cash_account_id is not None:
        if payload.linked_cash_account_id == account_id:
            raise LinkedAccountInvalidError("An account cannot link to itself.")
        linked_acc = accounts_repo.get_account(conn, payload.linked_cash_account_id)
        if not linked_acc or linked_acc["household_id"] != household_id or linked_acc["account_type"] != "cash":
            raise LinkedAccountInvalidError("Linked cash account must be an active cash account in the same household.")

    with transaction(conn):
        new_name = payload.name.strip() if payload.name is not None else existing["name"]
        new_inst = payload.institution.strip() if payload.institution is not None else existing["institution"]
        new_owner = payload.owner_user_id if payload.owner_user_id is not None else existing["owner_user_id"]
        new_linked = payload.linked_cash_account_id if "linked_cash_account_id" in payload.model_fields_set else existing["linked_cash_account_id"]
        new_billing = payload.billing_day if "billing_day" in payload.model_fields_set else existing["billing_day"]
        new_due = payload.due_day if "due_day" in payload.model_fields_set else existing["due_day"]
        new_type = payload.account_type if payload.account_type is not None else existing["account_type"]

        updated = accounts_repo.update_account(
            conn=conn,
            account_id=account_id,
            name=new_name,
            institution=new_inst,
            owner_user_id=new_owner,
            linked_cash_account_id=new_linked,
            billing_day=new_billing,
            due_day=new_due,
            account_type=new_type,
            expected_row_version=payload.row_version
        )
        if not updated:
            raise RowVersionConflictError()

        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device",
            entity_type="account",
            entity_id=account_id,
            action="update",
            actor_user_id=device.get("user_id"),
            actor_device_id=device.get("device_id"),
            before_data={"name": existing["name"], "institution": existing["institution"], "status": existing["status"]},
            after_data={"name": updated["name"], "institution": updated["institution"], "status": updated["status"]}
        )

    acc = accounts_repo.get_account_with_state(conn, account_id, household_id)
    return _format_account(acc)

@router.post("/{account_id}/deactivate", summary="Deactivate Account")
def deactivate_account(
    account_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    """
    Soft-deactivates an account. Historical transactions and snapshots remain completely preserved.
    """
    household_id = device["household_id"]
    existing = accounts_repo.get_account_with_state(conn, account_id, household_id)
    if not existing:
        raise AccountResourceNotFoundError(account_id)

    with transaction(conn):
        deactivated = accounts_repo.deactivate_account(conn, account_id)
        if not deactivated:
            raise AccountResourceNotFoundError(account_id)

        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device",
            entity_type="account",
            entity_id=account_id,
            action="soft_delete",
            actor_user_id=device.get("user_id"),
            actor_device_id=device.get("device_id"),
            before_data={"status": existing["status"]},
            after_data={"status": "inactive"}
        )

    acc = accounts_repo.get_account_with_state(conn, account_id, household_id)
    return _format_account(acc)

# --- Aliases ---

@router.get("/{account_id}/aliases", summary="List Account Aliases")
def list_account_aliases(
    account_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id)
    if not existing or existing["household_id"] != household_id:
        raise AccountResourceNotFoundError(account_id)

    aliases = accounts_repo.list_account_aliases(conn, account_id)
    return {

        "items": [
            {
                "id": str(a["id"]),
                "account_id": str(a["account_id"]),
                "alias": a["alias_text"],
                "created_at": a["created_at"].isoformat()
            }
            for a in aliases
        ]
    }

@router.post("/{account_id}/aliases", status_code=status.HTTP_201_CREATED, summary="Create Account Alias")
def create_account_alias(
    account_id: UUID,
    payload: CreateAliasRequest,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id)
    if not existing or existing["household_id"] != household_id:
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
            status='active'
        )
        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device",
            entity_type="account_alias",
            entity_id=alias_id,
            action="create",
            actor_user_id=device.get("user_id"),
            actor_device_id=device.get("device_id"),
            after_data={"account_id": str(account_id), "alias": raw_alias}
        )

    alias_obj = accounts_repo.get_account_alias(conn, alias_id, account_id)
    return {
        "id": str(alias_obj["id"]),
        "account_id": str(alias_obj["account_id"]),
        "alias": alias_obj["alias_text"],
        "created_at": alias_obj["created_at"].isoformat()
    }

@router.delete("/{account_id}/aliases/{alias_id}", summary="Soft-Delete Account Alias")
def delete_account_alias(
    account_id: UUID,
    alias_id: UUID,
    device: Dict[str, Any] = Depends(get_authenticated_device),
    conn: Any = Depends(get_db_connection)
) -> Dict[str, Any]:
    household_id = device["household_id"]
    existing = accounts_repo.get_account(conn, account_id)
    if not existing or existing["household_id"] != household_id:
        raise AccountResourceNotFoundError(account_id)

    alias = accounts_repo.get_account_alias(conn, alias_id, account_id)
    if not alias or alias["status"] != "active" or alias["deleted_at"] is not None:
        raise AliasResourceNotFoundError(alias_id)

    with transaction(conn):
        deactivated = accounts_repo.deactivate_account_alias(conn, alias_id, account_id)
        if not deactivated:

            raise AliasResourceNotFoundError(alias_id)

        audit_repo.insert_audit_event(
            conn=conn,
            household_id=household_id,
            actor_type="device",
            entity_type="account_alias",
            entity_id=alias_id,
            action="soft_delete",
            actor_user_id=device.get("user_id"),
            actor_device_id=device.get("device_id"),
            before_data={"status": "active"},
            after_data={"status": "inactive"}
        )


    return {
        "status": "deactivated",
        "id": str(alias_id),
        "account_id": str(account_id)
    }


