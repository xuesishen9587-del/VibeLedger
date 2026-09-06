"""
VibeLedger Astra-Simplified Environment Bootstrap Script
Idempotently provisions a fresh Astra-simplified database schema with:
- Household settings (started_on, timezone, investment_review_change_ratio)
- Owner user and household membership
- Canonical 15 expense categories + 3 income categories
- Starter accounts with explicit balance scopes
Invariants:
- Zero opening-balance transactions
- Zero balances defaulted to zero
- Zero account_state rows
"""

import os
import sys
import argparse
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid

# Ensure project root is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.config import get_settings, validate_safety
from app.db import get_connection, transaction
from app.repositories import simplified_schema as repo

# ----------------------------------------------------------------------
# Seed Definitions from TARGET_DOMAIN_MODEL.md
# ----------------------------------------------------------------------

EXPENSE_CATEGORIES = [
    ("Grocery", "Groceries, household consumables, ingredients", False),
    ("Dine", "Restaurants, takeaway, coffee, drinks and ready-to-eat snacks", False),
    ("Child", "All explicitly child-related purchases, including health, clothing and education; precedes those adult categories", False),
    ("Home & Utilities", "Rent, utilities, maintenance, appliances; excludes loan principal, includes identifiable mortgage interest", False),
    ("Digital & Gadgets", "Phones, computers, accessories and electronics", False),
    ("Clothing", "Adult clothing, shoes and accessories", False),
    ("Beauty", "Adult skincare, cosmetics, haircuts and personal care", False),
    ("Transportation", "Transit, taxi, fuel, parking, maintenance and tolls", False),
    ("Health", "Adult medicine, care, checkups and medical insurance", False),
    ("Education", "Adult books, training, software and AI subscriptions", False),
    ("Gift & Socials", "Gifts, social occasions and cash gifts to parents", False),
    ("Parents", "Specific goods/services for parents, excluding cash gifts", False),
    ("Fun & Games", "Routine entertainment, games, cinema and recreation", False),
    ("Trips & Occasions", "Holidays, anniversaries and distinct major occasions", False),
    ("Other", "Clear expenses without a sufficiently reliable category", True),
]

INCOME_CATEGORIES = [
    ("Salary", "Regular wages, salary, and bonuses", False),
    ("Interest", "Interest, dividends, and yields", False),
    ("Other income", "Other miscellaneous household income", True),
]

STARTER_ACCOUNTS = [
    ("Cash Wallet", "Wallet cash on hand", "cash", "CNY", None),
    ("Checking Account", "Main everyday bank checking account", "savings", "CNY", "low"),
    ("Credit Card", "Everyday credit card total balance owed", "credit", "CNY", None),
]


def bootstrap_simplified_environment(
    conn,
    household_name: str = "Household",
    reporting_currency: str = "CNY",
    started_on: Optional[date] = None,
    tz_name: str = "Asia/Singapore",
    investment_review_change_ratio: Decimal = Decimal("0.2000"),
    owner_auth_subject: str = "default_owner",
    owner_email: str = "owner@vibeledger.local",
    owner_display_name: str = "Household Owner",
) -> Dict[str, Any]:
    """
    Idempotently bootstraps a fresh Astra-simplified household, owner, categories, and starter accounts.
    Returns a summary dictionary of created and verified items.
    """
    if started_on is None:
        started_on = date(2026, 1, 1)

    summary = {
        "household_id": None,
        "owner_user_id": None,
        "categories_created": 0,
        "categories_verified": 0,
        "accounts_created": 0,
        "accounts_verified": 0,
    }

    with conn.cursor() as cur:
        # 1. Household
        cur.execute("SELECT id FROM households WHERE lower(name) = lower(%s);", (household_name.strip(),))
        row = cur.fetchone()
        if row:
            hh_id = uuid.UUID(str(row[0]))
        else:
            new_hh = repo.create_household(
                conn,
                name=household_name.strip(),
                reporting_currency=reporting_currency,
                started_on=started_on,
                tz_name=tz_name,
                investment_review_change_ratio=investment_review_change_ratio,
            )
            hh_id = uuid.UUID(str(new_hh["id"]))
        summary["household_id"] = str(hh_id)

        # 2. Owner User
        cur.execute("SELECT id FROM users WHERE auth_subject = %s OR email = %s;", (owner_auth_subject, owner_email))
        row = cur.fetchone()
        if row:
            user_id = uuid.UUID(str(row[0]))
        else:
            new_user = repo.create_user(
                conn,
                auth_subject=owner_auth_subject,
                email=owner_email,
                display_name=owner_display_name,
            )
            user_id = uuid.UUID(str(new_user["id"]))
        summary["owner_user_id"] = str(user_id)

        # 3. Household Membership
        cur.execute(
            "SELECT role FROM household_members WHERE household_id = %s AND user_id = %s;",
            (str(hh_id), str(user_id)),
        )
        if not cur.fetchone():
            repo.add_household_member(conn, hh_id, user_id, role="owner")

        # 4. Categories (15 Expense + 3 Income)
        for cat_name, cat_desc, is_fallback in EXPENSE_CATEGORIES:
            cur.execute(
                """
                SELECT id FROM categories
                WHERE household_id = %s AND category_type = 'expense' AND lower(name) = lower(%s);
                """,
                (str(hh_id), cat_name),
            )
            if cur.fetchone():
                summary["categories_verified"] += 1
            else:
                repo.create_category(
                    conn,
                    household_id=hh_id,
                    name=cat_name,
                    category_type="expense",
                    description=cat_desc,
                    is_fallback=is_fallback,
                )
                summary["categories_created"] += 1

        for cat_name, cat_desc, is_fallback in INCOME_CATEGORIES:
            cur.execute(
                """
                SELECT id FROM categories
                WHERE household_id = %s AND category_type = 'income' AND lower(name) = lower(%s);
                """,
                (str(hh_id), cat_name),
            )
            if cur.fetchone():
                summary["categories_verified"] += 1
            else:
                repo.create_category(
                    conn,
                    household_id=hh_id,
                    name=cat_name,
                    category_type="income",
                    description=cat_desc,
                    is_fallback=is_fallback,
                )
                summary["categories_created"] += 1

        # 5. Starter Accounts (zero opening balance, zero account_state)
        for acc_name, acc_scope, acc_type, acc_currency, risk_lvl in STARTER_ACCOUNTS:
            cur.execute(
                """
                SELECT id FROM accounts
                WHERE household_id = %s AND lower(name) = lower(%s);
                """,
                (str(hh_id), acc_name),
            )
            if cur.fetchone():
                summary["accounts_verified"] += 1
            else:
                repo.create_account(
                    conn,
                    household_id=hh_id,
                    name=acc_name,
                    balance_scope=acc_scope,
                    account_type=acc_type,
                    currency=acc_currency,
                    owner_user_id=user_id,
                    risk_level=risk_lvl,
                    opened_on=started_on,
                    statement_import_enabled=False,
                )
                summary["accounts_created"] += 1

    return summary


def main():
    parser = argparse.ArgumentParser(description="Bootstrap Astra-simplified database environment")
    parser.add_argument("--household-name", default="Household")
    parser.add_argument("--owner-email", default=os.environ.get("OWNER_EMAIL", "owner@vibeledger.local"))
    parser.add_argument("--owner-auth-subject", default=os.environ.get("OWNER_AUTH_SUBJECT", "default_owner"))
    parser.add_argument("--owner-display-name", default="Household Owner")
    parser.add_argument("--reporting-currency", default="CNY")
    parser.add_argument("--timezone", default="Asia/Singapore")
    parser.add_argument("--schema", default=None)
    args = parser.parse_args()

    validate_safety()
    settings = get_settings()
    schema = args.schema or settings.DB_SCHEMA

    print(f"LOG: Bootstrapping simplified environment in schema: {schema}")
    conn = get_connection(schema)
    try:
        with transaction(conn):
            result = bootstrap_simplified_environment(
                conn,
                household_name=args.household_name,
                reporting_currency=args.reporting_currency,
                tz_name=args.timezone,
                owner_auth_subject=args.owner_auth_subject,
                owner_email=args.owner_email,
                owner_display_name=args.owner_display_name,
            )
        print("SUCCESS: Bootstrap completed successfully.")
        print(f"Summary: {result}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
