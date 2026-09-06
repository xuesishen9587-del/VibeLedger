import os
import sys
import json
import argparse
from typing import Dict, Any, Optional, List
from uuid import UUID, uuid4
from datetime import date, datetime
from decimal import Decimal

# Ensure project root is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.config import get_settings
from app.db import get_connection, transaction
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo


class BootstrapConsistencyError(Exception):
    """Raised when an existing database entity matches a natural key but material attributes differ."""
    pass


def normalize_str(val: Optional[str]) -> Optional[str]:
    return val.strip() if val else None


def bootstrap_staging_environment(
    conn,
    seed_data: Dict[str, Any],
    started_on: Optional[date] = None,
    owner_auth_subject: str = "",
    ledger_start_date: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Idempotently sets up initial staging configuration using household-scoped natural keys.
    Conforms to the Astra-simplified 16-table architecture (no legacy account_state table).
    Validates strict consistency if an entity already exists and raises BootstrapConsistencyError on conflict.
    """
    effective_started_on = started_on or ledger_start_date or date(2026, 1, 1)

    summary: Dict[str, Any] = {
        "household_id": None,
        "owner_user_id": None,
        "accounts_created": 0,
        "accounts_verified": 0,
        "aliases_created": 0,
        "aliases_verified": 0,
        "categories_created": 0,
        "categories_verified": 0,
    }

    # -------------------------------------------------------------
    # 1. Household (Natural Key: lower(name))
    # -------------------------------------------------------------
    hh_config = seed_data.get("household", {})
    hh_name = hh_config.get("name", "Staging Household").strip()
    reporting_currency = hh_config.get("reporting_currency", "CNY").strip().upper()
    timezone = hh_config.get("timezone", "Asia/Singapore").strip()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, reporting_currency, started_on, status
            FROM households
            WHERE lower(name) = lower(%s);
            """,
            (hh_name,)
        )
        existing_hh = cur.fetchone()

    if existing_hh:
        hh_id, db_name, db_currency, db_started_on, db_status = existing_hh
        if db_status != "active":
            raise BootstrapConsistencyError(
                f"Household '{hh_name}' exists but status is '{db_status}' (expected 'active')"
            )
        if db_started_on != effective_started_on:
            raise BootstrapConsistencyError(
                f"Household '{hh_name}' exists but started_on/ledger_start_date mismatch: "
                f"existing '{db_started_on}' != expected '{effective_started_on}'"
            )
        if db_currency != reporting_currency:
            raise BootstrapConsistencyError(
                f"Household '{hh_name}' exists but reporting_currency mismatch: "
                f"existing '{db_currency}' != expected '{reporting_currency}'"
            )
        household_id = hh_id
    else:
        household_id = uuid4()
        accounts_repo.create_household(
            conn=conn,
            household_id=household_id,
            name=hh_name,
            started_on=effective_started_on,
            reporting_currency=reporting_currency,
            tz_name=timezone,
            status="active"
        )

    summary["household_id"] = str(household_id)

    # -------------------------------------------------------------
    # 2. Owner User (Natural Key: auth_subject)
    # -------------------------------------------------------------
    owner_config = seed_data.get("owner", {})
    display_name = owner_config.get("display_name", "Staging Owner").strip()
    email = normalize_str(owner_config.get("email"))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, auth_subject, email, display_name, status
            FROM users
            WHERE auth_subject = %s;
            """,
            (owner_auth_subject,)
        )
        existing_user = cur.fetchone()

    if existing_user:
        u_id, u_sub, u_email, u_dname, u_status = existing_user
        if u_status != "active":
            raise BootstrapConsistencyError(
                f"User with auth_subject '{owner_auth_subject}' exists but status is '{u_status}' (expected 'active')"
            )
        if u_dname != display_name:
            raise BootstrapConsistencyError(
                f"User with auth_subject '{owner_auth_subject}' exists but display_name mismatch: "
                f"existing '{u_dname}' != expected '{display_name}'"
            )
        if normalize_str(u_email) != normalize_str(email):
            raise BootstrapConsistencyError(
                f"User with auth_subject '{owner_auth_subject}' exists but email mismatch: "
                f"existing '{u_email}' != expected '{email}'"
            )
        owner_user_id = u_id
    else:
        owner_user_id = uuid4()
        accounts_repo.create_user(
            conn=conn,
            user_id=owner_user_id,
            auth_subject=owner_auth_subject,
            display_name=display_name,
            email=email,
            status="active"
        )

    summary["owner_user_id"] = str(owner_user_id)

    # -------------------------------------------------------------
    # 3. Household Membership (Natural Key: (household_id, user_id))
    # -------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT role FROM household_members
            WHERE household_id = %s AND user_id = %s;
            """,
            (household_id, owner_user_id)
        )
        existing_member = cur.fetchone()

    if existing_member:
        if existing_member[0] != "owner":
            raise BootstrapConsistencyError(
                f"Membership for user '{owner_auth_subject}' in household '{hh_name}' exists with non-owner role '{existing_member[0]}'"
            )
    else:
        accounts_repo.add_household_member(
            conn=conn,
            household_id=household_id,
            user_id=owner_user_id,
            role="owner"
        )

    # -------------------------------------------------------------
    # 4. Accounts (Natural Key: (household_id, lower(name)))
    # -------------------------------------------------------------
    raw_accounts = seed_data.get("accounts", [])

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, account_type, balance_scope, currency, risk_level, statement_import_enabled, owner_user_id, opened_on, status
            FROM accounts
            WHERE household_id = %s;
            """,
            (household_id,)
        )
        existing_acc_rows = cur.fetchall()

    acc_by_name: Dict[str, Dict[str, Any]] = {}
    for r in existing_acc_rows:
        acc_by_name[r[1].lower().strip()] = {
            "id": r[0],
            "name": r[1],
            "account_type": r[2],
            "balance_scope": r[3],
            "currency": r[4],
            "risk_level": r[5],
            "statement_import_enabled": r[6],
            "owner_user_id": r[7],
            "opened_on": r[8],
            "status": r[9],
        }

    account_id_map: Dict[str, UUID] = {}
    for acc in raw_accounts:
        a_name = acc["name"].strip()
        a_key = a_name.lower()
        if a_key in acc_by_name:
            account_id_map[a_name] = acc_by_name[a_key]["id"]
        else:
            account_id_map[a_name] = uuid4()

    for acc in raw_accounts:
        a_name = acc["name"].strip()
        a_key = a_name.lower()
        a_type = acc["account_type"].strip()
        a_scope = acc.get("balance_scope", f"{a_name} balance").strip()
        a_curr = acc["currency"].strip().upper()
        a_risk = acc.get("risk_level")
        a_stmt = bool(acc.get("statement_import_enabled", False))
        a_opened = acc.get("opened_on") or effective_started_on

        if a_key in acc_by_name:
            curr_acc = acc_by_name[a_key]
            # Strict consistency verification
            if curr_acc["status"] != "active":
                raise BootstrapConsistencyError(
                    f"Account '{a_name}' exists but status is '{curr_acc['status']}' (expected 'active')"
                )
            if curr_acc["owner_user_id"] != owner_user_id:
                raise BootstrapConsistencyError(
                    f"Account '{a_name}' exists but owner_user_id mismatch: "
                    f"existing '{curr_acc['owner_user_id']}' != expected '{owner_user_id}'"
                )
            if curr_acc["account_type"] != a_type:
                raise BootstrapConsistencyError(
                    f"Account '{a_name}' exists but account_type mismatch: "
                    f"existing '{curr_acc['account_type']}' != expected '{a_type}'"
                )
            if curr_acc["currency"] != a_curr:
                raise BootstrapConsistencyError(
                    f"Account '{a_name}' exists but currency mismatch: "
                    f"existing '{curr_acc['currency']}' != expected '{a_curr}'"
                )
            summary["accounts_verified"] += 1
        else:
            acc_id = account_id_map[a_name]
            accounts_repo.create_account(
                conn=conn,
                account_id=acc_id,
                household_id=household_id,
                name=a_name,
                balance_scope=a_scope,
                account_type=a_type,
                currency=a_curr,
                owner_user_id=owner_user_id,
                risk_level=a_risk,
                opened_on=a_opened,
                statement_import_enabled=a_stmt,
                status="active"
            )
            summary["accounts_created"] += 1

    # -------------------------------------------------------------
    # 5. Account Aliases (Natural Key: (account_id, normalized_alias))
    # -------------------------------------------------------------
    aliases_dict = seed_data.get("aliases", {})
    for acc_name, alias_list in aliases_dict.items():
        clean_acc_name = acc_name.strip()
        if clean_acc_name not in account_id_map:
            raise BootstrapConsistencyError(
                f"Alias configuration references unknown account '{clean_acc_name}'"
            )
        target_acc_id = account_id_map[clean_acc_name]

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, alias_text, normalized_alias, status
                FROM account_aliases
                WHERE household_id = %s AND account_id = %s;
                """,
                (household_id, target_acc_id)
            )
            existing_alias_rows = cur.fetchall()

        alias_by_norm: Dict[str, Dict[str, Any]] = {}
        for r in existing_alias_rows:
            alias_by_norm[r[2]] = {
                "id": r[0],
                "alias_text": r[1],
                "normalized_alias": r[2],
                "status": r[3],
            }

        for alias_text in alias_list:
            raw_alias = alias_text.strip()
            norm_alias = raw_alias.lower()
            if norm_alias in alias_by_norm:
                ex_a = alias_by_norm[norm_alias]
                if ex_a["status"] != "active":
                    raise BootstrapConsistencyError(
                        f"Alias '{raw_alias}' for account '{clean_acc_name}' exists in inactive state."
                    )
                summary["aliases_verified"] += 1
            else:
                accounts_repo.create_account_alias(
                    conn=conn,
                    alias_id=uuid4(),
                    account_id=target_acc_id,
                    alias_text=raw_alias,
                    normalized_alias=norm_alias,
                    status="active",
                    household_id=household_id,
                )
                alias_by_norm[norm_alias] = {
                    "id": target_acc_id,
                    "alias_text": raw_alias,
                    "normalized_alias": norm_alias,
                    "status": "active",
                }
                summary["aliases_created"] += 1

    # -------------------------------------------------------------
    # 6. Categories (Natural Key: (household_id, category_type, lower(name)))
    # -------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, category_type, status FROM categories
            WHERE household_id = %s;
            """,
            (household_id,)
        )
        existing_cats = cur.fetchall()

    cat_map: Dict[tuple, Dict[str, Any]] = {}
    for r in existing_cats:
        cat_map[(r[2].strip().lower(), r[1].strip().lower())] = {
            "id": r[0],
            "name": r[1],
            "category_type": r[2],
            "status": r[3],
        }

    raw_categories = seed_data.get("categories", [])
    for cat in raw_categories:
        c_name = cat["name"].strip()
        c_type = cat["category_type"].strip().lower()
        c_key = (c_type, c_name.lower())

        if c_key in cat_map:
            existing_c = cat_map[c_key]
            if existing_c["status"] != "active":
                raise BootstrapConsistencyError(
                    f"Category '{c_name}' ({c_type}) exists but is inactive"
                )
            summary["categories_verified"] += 1
        else:
            categories_repo.create_category(
                conn=conn,
                household_id=household_id,
                name=c_name,
                category_type=c_type,
                category_id=uuid4(),
                status="active"
            )
            summary["categories_created"] += 1

    return summary


def run_bootstrap_cli():
    parser = argparse.ArgumentParser(
        description="VibeLedger Staging Database Bootstrap Tool (Staging-only)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=os.path.join(SCRIPT_DIR, "staging_seed.example.json"),
        help="Path to staging seed JSON configuration file (e.g. scripts/staging_seed.local.json)."
    )

    args = parser.parse_args()

    # Strict CLI safety check
    settings = get_settings()
    if settings.ENVIRONMENT != "staging":
        print(f"ERROR: Operator CLI execution is strictly restricted to ENVIRONMENT='staging'. Current: '{settings.ENVIRONMENT}'.")
        sys.exit(1)

    # Single source of truth from environment variables ONLY
    owner_sub = os.environ.get("STAGING_OWNER_AUTH_SUBJECT")
    if not owner_sub or not owner_sub.strip():
        print("ERROR: STAGING_OWNER_AUTH_SUBJECT environment variable must be set.")
        sys.exit(1)

    start_date_str = os.environ.get("STAGING_STARTED_ON") or os.environ.get("STAGING_LEDGER_START_DATE")
    if not start_date_str or not start_date_str.strip():
        print("ERROR: STAGING_STARTED_ON (or STAGING_LEDGER_START_DATE) environment variable must be set (expected YYYY-MM-DD).")
        sys.exit(1)

    try:
        started_on = datetime.strptime(start_date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        print(f"ERROR: Invalid date format '{start_date_str}': expected YYYY-MM-DD.")
        sys.exit(1)

    if not os.path.exists(args.config):
        print(f"ERROR: Configuration file not found at '{args.config}'.")
        sys.exit(1)

    with open(args.config, "r", encoding="utf-8") as f:
        seed_data = json.load(f)

    print(f"LOG: Starting staging bootstrap against schema '{settings.DB_SCHEMA}'...")
    print(f"LOG: Owner auth_subject: '{owner_sub}'")
    print(f"LOG: Started on: '{started_on}'")

    conn = get_connection(schema=settings.DB_SCHEMA)
    try:
        with transaction(conn):
            res = bootstrap_staging_environment(
                conn=conn,
                seed_data=seed_data,
                started_on=started_on,
                owner_auth_subject=owner_sub.strip()
            )
        print("SUCCESS: Staging bootstrap completed successfully!")
        print(json.dumps(res, indent=2))
    except Exception as ex:
        print(f"ERROR: Staging bootstrap failed: {ex}")
        sys.exit(1)
    finally:
        if conn and not conn.closed:
            conn.close()


if __name__ == "__main__":
    run_bootstrap_cli()
