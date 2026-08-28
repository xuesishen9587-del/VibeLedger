import os
import sys
import json
import argparse
from typing import Dict, Any, Optional, List
from uuid import UUID, uuid4
from datetime import date, datetime

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
    ledger_start_date: date,
    owner_auth_subject: str,
) -> Dict[str, Any]:
    """
    Idempotently sets up initial staging configuration using household-scoped natural keys.
    Validates consistency if an entity already exists and raises BootstrapConsistencyError on conflict.
    Initializes account_state with initialized_at=NULL (does not establish opening balances).
    """
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
    # 1. Household
    # -------------------------------------------------------------
    hh_config = seed_data.get("household", {})
    hh_name = hh_config.get("name", "Staging Household").strip()
    reporting_currency = hh_config.get("reporting_currency", "CNY").strip().upper()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, reporting_currency, ledger_start_date, status
            FROM households
            WHERE lower(name) = lower(%s);
            """,
            (hh_name,)
        )
        existing_hh = cur.fetchone()

    if existing_hh:
        hh_id, db_name, db_currency, db_start_date, db_status = existing_hh
        if db_start_date != ledger_start_date:
            raise BootstrapConsistencyError(
                f"Household '{hh_name}' exists but ledger_start_date mismatch: "
                f"existing '{db_start_date}' != expected '{ledger_start_date}'"
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
            ledger_start_date=ledger_start_date,
            reporting_currency=reporting_currency,
            status="active"
        )

    summary["household_id"] = str(household_id)

    # -------------------------------------------------------------
    # 2. Owner User (Natural Key: auth_subject)
    # -------------------------------------------------------------
    owner_config = seed_data.get("owner", {})
    display_name = owner_config.get("display_name", "Staging Owner").strip()
    email = normalize_str(owner_config.get("email"))
    default_currency = owner_config.get("default_currency", "CNY").strip().upper()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, auth_subject, email, display_name, default_currency, status
            FROM users
            WHERE auth_subject = %s;
            """,
            (owner_auth_subject,)
        )
        existing_user = cur.fetchone()

    if existing_user:
        u_id, u_sub, u_email, u_dname, u_curr, u_status = existing_user
        if u_curr != default_currency:
            raise BootstrapConsistencyError(
                f"User with auth_subject '{owner_auth_subject}' exists but default_currency mismatch: "
                f"existing '{u_curr}' != expected '{default_currency}'"
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
            default_currency=default_currency,
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
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, institution, account_type, currency, billing_day, due_day, linked_cash_account_id, status
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
            "institution": r[2],
            "account_type": r[3],
            "currency": r[4],
            "billing_day": r[5],
            "due_day": r[6],
            "linked_cash_account_id": r[7],
            "status": r[8],
        }

    raw_accounts = seed_data.get("accounts", [])
    account_id_map: Dict[str, UUID] = {}

    # Pass 1: Create or verify accounts
    for acc in raw_accounts:
        a_name = acc["name"].strip()
        a_key = a_name.lower()
        a_type = acc["account_type"].strip()
        a_curr = acc["currency"].strip().upper()
        a_inst = normalize_str(acc.get("institution"))
        a_bday = acc.get("billing_day")
        a_dday = acc.get("due_day")

        if a_key in acc_by_name:
            curr_acc = acc_by_name[a_key]
            # Consistency checks
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
            if (curr_acc["billing_day"] or None) != (a_bday or None):
                raise BootstrapConsistencyError(
                    f"Account '{a_name}' exists but billing_day mismatch: "
                    f"existing '{curr_acc['billing_day']}' != expected '{a_bday}'"
                )
            if (curr_acc["due_day"] or None) != (a_dday or None):
                raise BootstrapConsistencyError(
                    f"Account '{a_name}' exists but due_day mismatch: "
                    f"existing '{curr_acc['due_day']}' != expected '{a_dday}'"
                )
            if (curr_acc["institution"] or None) != (a_inst or None):
                raise BootstrapConsistencyError(
                    f"Account '{a_name}' exists but institution mismatch: "
                    f"existing '{curr_acc['institution']}' != expected '{a_inst}'"
                )

            # Ensure account_state row exists with initialized_at=NULL
            state = accounts_repo.get_account_state(conn, curr_acc["id"])
            if not state:
                raise BootstrapConsistencyError(f"Account '{a_name}' exists but missing account_state projection row.")

            account_id_map[a_name] = curr_acc["id"]
            summary["accounts_verified"] += 1
        else:
            new_id = uuid4()
            accounts_repo.create_account(
                conn=conn,
                account_id=new_id,
                household_id=household_id,
                name=a_name,
                account_type=a_type,
                currency=a_curr,
                institution=a_inst,
                owner_user_id=owner_user_id,
                linked_cash_account_id=None,
                billing_day=a_bday,
                due_day=a_dday,
                status="active"
            )
            account_id_map[a_name] = new_id
            summary["accounts_created"] += 1

    # Pass 2: Link cash account for credit cards if specified
    for acc in raw_accounts:
        linked_cash_name = acc.get("linked_cash_account_name")
        if linked_cash_name and acc["name"] in account_id_map and linked_cash_name in account_id_map:
            card_id = account_id_map[acc["name"]]
            cash_id = account_id_map[linked_cash_name]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE accounts
                    SET linked_cash_account_id = %s, updated_at = now()
                    WHERE id = %s AND (linked_cash_account_id IS NULL OR linked_cash_account_id <> %s);
                    """,
                    (cash_id, card_id, cash_id)
                )

    # -------------------------------------------------------------
    # 5. Account Aliases (Natural Key: (account_id, normalized_alias))
    # -------------------------------------------------------------
    aliases_dict = seed_data.get("aliases", {})
    for acc_name, alias_list in aliases_dict.items():
        if acc_name not in account_id_map:
            continue
        target_acc_id = account_id_map[acc_name]

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT normalized_alias FROM account_aliases
                WHERE account_id = %s AND deleted_at IS NULL;
                """,
                (target_acc_id,)
            )
            existing_aliases = {row[0] for row in cur.fetchall()}

        for alias_text in alias_list:
            raw_alias = alias_text.strip()
            norm_alias = raw_alias.lower()
            if norm_alias in existing_aliases:
                summary["aliases_verified"] += 1
            else:
                accounts_repo.create_account_alias(
                    conn=conn,
                    alias_id=uuid4(),
                    account_id=target_acc_id,
                    alias_text=raw_alias,
                    normalized_alias=norm_alias,
                    status="active"
                )
                existing_aliases.add(norm_alias)
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
                category_id=uuid4(),
                household_id=household_id,
                name=c_name,
                category_type=c_type,
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
        help="Path to staging seed JSON configuration file."
    )
    parser.add_argument(
        "--owner-auth-subject",
        type=str,
        default=None,
        help="Explicit auth_subject for the staging owner (overrides STAGING_OWNER_AUTH_SUBJECT env)."
    )
    parser.add_argument(
        "--ledger-start-date",
        type=str,
        default=None,
        help="Explicit ledger start date YYYY-MM-DD (overrides STAGING_LEDGER_START_DATE env)."
    )

    args = parser.parse_args()

    # Strict CLI safety check
    settings = get_settings()
    if settings.ENVIRONMENT != "staging":
        print(f"ERROR: Operator CLI execution is strictly restricted to ENVIRONMENT='staging'. Current: '{settings.ENVIRONMENT}'.")
        sys.exit(1)

    owner_sub = args.owner_auth_subject or os.environ.get("STAGING_OWNER_AUTH_SUBJECT")
    if not owner_sub or not owner_sub.strip():
        print("ERROR: STAGING_OWNER_AUTH_SUBJECT must be set in environment or passed via --owner-auth-subject.")
        sys.exit(1)

    start_date_str = args.ledger_start_date or os.environ.get("STAGING_LEDGER_START_DATE")
    if not start_date_str or not start_date_str.strip():
        print("ERROR: STAGING_LEDGER_START_DATE must be set in environment or passed via --ledger-start-date.")
        sys.exit(1)

    try:
        ledger_start_date = datetime.strptime(start_date_str.strip(), "%Y-%m-%d").date()
    except ValueError as e:
        print(f"ERROR: Invalid STAGING_LEDGER_START_DATE format '{start_date_str}': expected YYYY-MM-DD.")
        sys.exit(1)

    if not os.path.exists(args.config):
        print(f"ERROR: Configuration file not found at '{args.config}'.")
        sys.exit(1)

    with open(args.config, "r", encoding="utf-8") as f:
        seed_data = json.load(f)

    print(f"LOG: Starting staging bootstrap against schema '{settings.DB_SCHEMA}'...")
    print(f"LOG: Owner auth_subject: '{owner_sub}'")
    print(f"LOG: Ledger start date: '{ledger_start_date}'")

    conn = get_connection(schema=settings.DB_SCHEMA)
    try:
        with transaction(conn):
            res = bootstrap_staging_environment(
                conn=conn,
                seed_data=seed_data,
                ledger_start_date=ledger_start_date,
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
