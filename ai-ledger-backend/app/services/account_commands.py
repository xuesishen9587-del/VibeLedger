from datetime import date, datetime
from typing import Any, Callable, Dict, Optional, Tuple
from uuid import UUID, uuid4
import psycopg2
from fastapi import HTTPException

from app.auth.context import AuthContext
from app.domain.money import validate_currency_code
from app.domain.transactions import (
    LedgerDomainError,
    AccountResourceNotFoundError,
    RowVersionConflictError,
    AccountNameConflictError,
    CurrencyImmutableError,
    AccountTypeImmutableError,
    UserNotInHouseholdError,
    LinkedAccountInvalidError,
    IdempotencyKeyReuseError,
)
import app.repositories.accounts as accounts_repo
import app.repositories.audit as audit_repo
from app.services.durable_commands import (
    to_json,
    compute_command_hash,
    get_audit_actor_info,
    execute_durable_command,
)

# Backward-compatible aliases
_to_json = to_json
_get_audit_actor_info = get_audit_actor_info
execute_account_command = execute_durable_command


def format_account(acc: Dict[str, Any]) -> Dict[str, Any]:
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



def create_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = "POST /api/v1/accounts"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        currency = validate_currency_code(payload.currency)
        household_id = auth_context.household_id

        if payload.account_type == "credit" and payload.risk_level is not None:
            raise LinkedAccountInvalidError("Credit accounts cannot have a risk level.")

        balance_scope = payload.balance_scope.strip()
        opened_on = payload.opened_on or date.today()

        if payload.owner_user_id is not None:
            if not accounts_repo.check_user_in_household(c, payload.owner_user_id, household_id):
                raise UserNotInHouseholdError(payload.owner_user_id)

        if accounts_repo.check_account_name_exists(c, household_id, payload.name):
            raise AccountNameConflictError(payload.name)

        account_id = uuid4()
        try:
            created = accounts_repo.create_account(
                conn=c,
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
        except psycopg2.IntegrityError as e:
            if "uq_accounts_active_name" in str(e) or "accounts" in str(e):
                raise AccountNameConflictError(payload.name)
            raise

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="create",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
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
        return format_account(created), 201

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def patch_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"PATCH /api/v1/accounts/{account_id}"
    body = payload.model_dump(mode="json", exclude_unset=True)
    fields_set = payload.model_fields_set

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        expected_ver = payload.expected_version

        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        if "name" in fields_set:
            if payload.name is None or not payload.name.strip():
                raise LinkedAccountInvalidError("Account name cannot be empty.")
            new_name = payload.name.strip()
        else:
            new_name = existing["name"]

        if "balance_scope" in fields_set:
            if payload.balance_scope is None or not payload.balance_scope.strip():
                raise LinkedAccountInvalidError("balance_scope cannot be empty.")
            new_scope = payload.balance_scope.strip()
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

        if new_type == "credit" and new_risk is not None:
            raise LinkedAccountInvalidError("Credit accounts cannot have a risk level.")

        if new_opened_on != existing["opened_on"]:
            if existing.get("closed_on") and new_opened_on > existing["closed_on"]:
                raise HTTPException(
                    status_code=400,
                    detail="Account opened_on cannot be after closed_on."
                )
            if not accounts_repo.check_account_observations_within_lifetime(
                c, household_id, account_id, new_opened_on
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Account has financial observations before new opened_on date."
                )

        if new_currency != existing["currency"]:
            if accounts_repo.has_financial_history(c, household_id, account_id):
                raise CurrencyImmutableError()

        if new_type != existing["account_type"]:
            if accounts_repo.has_financial_history(c, household_id, account_id):
                raise AccountTypeImmutableError()

        if new_name.lower() != existing["name"].lower():
            if accounts_repo.check_account_name_exists(c, household_id, new_name, exclude_account_id=account_id):
                raise AccountNameConflictError(new_name)

        if new_owner is not None and new_owner != existing.get("owner_user_id"):
            if not accounts_repo.check_user_in_household(c, new_owner, household_id):
                raise UserNotInHouseholdError(new_owner)

        updated = accounts_repo.update_account(
            conn=c,
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
            expected_row_version=expected_ver,
            fields_set=fields_set
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

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data=before_data,
            after_data=after_data,
            reason=payload.reason
        )
        return format_account(updated), 200

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def close_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"POST /api/v1/accounts/{account_id}/close"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        expected_ver = payload.expected_version

        if not payload.closing_snapshot_id:
            raise HTTPException(status_code=400, detail="closing_snapshot_id is required to close an account.")

        closed_on = payload.closed_on or date.today()

        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        if existing["status"] != "active":
            raise HTTPException(status_code=400, detail="Only active accounts can be closed.")

        if closed_on < existing["opened_on"]:
            raise HTTPException(status_code=400, detail="closed_on cannot be earlier than opened_on.")

        try:
            accounts_repo.validate_closing_snapshot_for_close(
                conn=c,
                household_id=household_id,
                account_id=account_id,
                closing_snapshot_id=payload.closing_snapshot_id,
                account=existing,
                closed_on=closed_on
            )
        except (ValueError, AccountResourceNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))

        updated = accounts_repo.close_account(c, household_id, account_id, expected_ver, closed_on)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="close",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data={"status": "active", "closed_on": None},
            after_data={
                "status": "closed",
                "closed_on": closed_on.isoformat(),
                "closing_snapshot_id": str(payload.closing_snapshot_id)
            },
            reason=payload.reason
        )
        return format_account(updated), 200

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def reopen_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"POST /api/v1/accounts/{account_id}/reopen"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        expected_ver = payload.expected_version

        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        if existing["status"] != "closed":
            raise HTTPException(status_code=400, detail="Only closed accounts can be reopened.")

        updated = accounts_repo.reopen_account(c, household_id, account_id, expected_ver)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="reopen",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data={"status": "closed", "closed_on": existing["closed_on"].isoformat() if existing.get("closed_on") else None},
            after_data={"status": "active", "closed_on": None},
            reason=payload.reason
        )
        return format_account(updated), 200

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)


def cancel_account_command(
    conn: Any,
    auth_context: AuthContext,
    idempotency_key: str,
    account_id: UUID,
    payload: Any,
) -> Tuple[Dict[str, Any], int]:
    operation = f"POST /api/v1/accounts/{account_id}/cancel"
    body = payload.model_dump(mode="json")

    def _mutate(c: Any, rid: UUID) -> Tuple[Dict[str, Any], int]:
        household_id = auth_context.household_id
        expected_ver = payload.expected_version

        existing = accounts_repo.get_account(c, account_id, household_id)
        if not existing:
            raise AccountResourceNotFoundError(account_id)

        if existing["row_version"] != expected_ver:
            raise RowVersionConflictError()

        if existing["status"] != "active":
            raise HTTPException(status_code=400, detail="Only active accounts can be cancelled.")

        if accounts_repo.has_financial_history(c, household_id, account_id):
            raise HTTPException(status_code=400, detail="Cannot cancel account with existing financial history.")

        updated = accounts_repo.cancel_account(c, household_id, account_id, expected_ver)
        if not updated:
            raise RowVersionConflictError()

        actor_type, actor_user_id, actor_device_id = _get_audit_actor_info(auth_context)
        audit_repo.insert_audit_event(
            conn=c,
            household_id=household_id,
            actor_type=actor_type,
            entity_type="account",
            entity_id=account_id,
            action="update",
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            source_request_id=rid,
            before_data={"status": "active"},
            after_data={"status": "cancelled"},
            reason=payload.reason
        )
        return format_account(updated), 200

    return execute_account_command(conn, auth_context, idempotency_key, operation, body, _mutate)
