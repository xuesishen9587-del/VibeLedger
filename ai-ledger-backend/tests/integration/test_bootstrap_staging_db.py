import unittest
from datetime import date
from decimal import Decimal
from uuid import UUID

from tests.support.db_helper import BaseDbTestCase
from app.db import get_connection, transaction
from app.repositories import accounts as accounts_repo
from app.repositories import categories as categories_repo
from scripts.bootstrap_staging import bootstrap_staging_environment, BootstrapConsistencyError


class TestBootstrapStagingDb(BaseDbTestCase):
    """
    Integration tests proving:
    1. Bootstrap idempotency and zero opening balance/transaction creation.
    2. Consistency verification on existing entities (fail loudly on attribute mismatch).
    3. Failure on unresolved linked cash account or unknown alias account.
    4. account_state projection verification (initialized_at must be NULL).
    """

    def setUp(self):
        super().setUp()
        self.sample_seed = {
            "household": {
                "name": "Integration Staging Family",
                "reporting_currency": "CNY"
            },
            "owner": {
                "display_name": "Integration Owner",
                "email": "owner@staging.test.com",
                "default_currency": "CNY"
            },
            "accounts": [
                {
                    "name": "招商银行储蓄卡",
                    "institution": "招商银行",
                    "account_type": "savings",
                    "currency": "CNY"
                },
                {
                    "name": "招商银行信用卡",
                    "institution": "招商银行",
                    "account_type": "credit",
                    "currency": "CNY",
                    "billing_day": 5,
                    "due_day": 25,
                    "linked_cash_account_name": "招商银行储蓄卡"
                },
                {
                    "name": "Chase Sapphire Card",
                    "institution": "Chase",
                    "account_type": "credit",
                    "currency": "USD",
                    "billing_day": 10,
                    "due_day": 30
                },
                {
                    "name": "美股投资账户",
                    "institution": "富途证券",
                    "account_type": "investment",
                    "currency": "USD"
                },
                {
                    "name": "现金钱包",
                    "institution": "现金",
                    "account_type": "cash",
                    "currency": "CNY"
                }
            ],
            "aliases": {
                "招商银行储蓄卡": ["招行储蓄", "CMB Debit"],
                "招商银行信用卡": ["招行信用卡", "CMB Credit"],
                "Chase Sapphire Card": ["Chase CC", "CSP"],
                "现金钱包": ["现金", "Cash"]
            },
            "categories": [
                {"name": "餐饮美食", "category_type": "expense"},
                {"name": "交通出行", "category_type": "expense"},
                {"name": "工资收入", "category_type": "income"}
            ]
        }
        self.ledger_start_date = date(2026, 8, 1)
        self.owner_sub = "auth0|staging_owner_integration_test"

    def test_bootstrap_initial_and_idempotent_rerun(self):
        with get_connection(self.test_schema) as conn:
            # 1. Initial bootstrap
            with transaction(conn):
                res1 = bootstrap_staging_environment(
                    conn=conn,
                    seed_data=self.sample_seed,
                    ledger_start_date=self.ledger_start_date,
                    owner_auth_subject=self.owner_sub
                )

            self.assertIsNotNone(res1["household_id"])
            self.assertIsNotNone(res1["owner_user_id"])
            self.assertEqual(res1["accounts_created"], 5)
            self.assertEqual(res1["accounts_verified"], 0)
            self.assertEqual(res1["aliases_created"], 8)
            self.assertEqual(res1["categories_created"], 3)

            hh_id = UUID(res1["household_id"])
            owner_id = UUID(res1["owner_user_id"])

            # Verify household attributes
            hh = accounts_repo.get_household(conn, hh_id)
            self.assertEqual(hh["name"], "Integration Staging Family")
            self.assertEqual(hh["reporting_currency"], "CNY")
            self.assertEqual(hh["ledger_start_date"], self.ledger_start_date)

            # Verify owner user attributes
            owner = accounts_repo.get_user(conn, owner_id)
            self.assertEqual(owner["auth_subject"], self.owner_sub)
            self.assertEqual(owner["default_currency"], "CNY")

            # Verify household membership
            members = accounts_repo.get_household_members(conn, hh_id)
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0]["user_id"], owner_id)
            self.assertEqual(members[0]["role"], "owner")

            # Verify account_state initialization (initialized_at must be NULL, ledger_balance 0)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id, ledger_balance, initialized_at FROM account_state WHERE account_id IN (SELECT id FROM accounts WHERE household_id = %s);",
                    (hh_id,)
                )
                states = cur.fetchall()
                self.assertEqual(len(states), 5)
                for st in states:
                    self.assertEqual(st[1], Decimal("0.000000"))
                    self.assertIsNone(st[2], "account_state.initialized_at must be NULL during staging bootstrap")

                # Verify ZERO transactions exist
                cur.execute("SELECT count(*) FROM transactions WHERE household_id = %s;", (hh_id,))
                tx_count = cur.fetchone()[0]
                self.assertEqual(tx_count, 0, "Bootstrap must create zero transactions")

            # 2. Idempotent second bootstrap run with identical data
            with transaction(conn):
                res2 = bootstrap_staging_environment(
                    conn=conn,
                    seed_data=self.sample_seed,
                    ledger_start_date=self.ledger_start_date,
                    owner_auth_subject=self.owner_sub
                )

            self.assertEqual(res2["household_id"], str(hh_id))
            self.assertEqual(res2["owner_user_id"], str(owner_id))
            self.assertEqual(res2["accounts_created"], 0)
            self.assertEqual(res2["accounts_verified"], 5)
            self.assertEqual(res2["aliases_created"], 0)
            self.assertEqual(res2["aliases_verified"], 8)
            self.assertEqual(res2["categories_created"], 0)
            self.assertEqual(res2["categories_verified"], 3)

    def test_bootstrap_consistency_error_on_mismatched_attributes(self):
        with get_connection(self.test_schema) as conn:
            # Initial setup
            with transaction(conn):
                bootstrap_staging_environment(
                    conn=conn,
                    seed_data=self.sample_seed,
                    ledger_start_date=self.ledger_start_date,
                    owner_auth_subject=self.owner_sub
                )

            # Test A: Conflicting ledger_start_date for existing household
            with self.assertRaises(BootstrapConsistencyError) as ctx_hh:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=self.sample_seed,
                        ledger_start_date=date(2025, 1, 1),
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("ledger_start_date mismatch", str(ctx_hh.exception))

            # Test B: Conflicting account currency for existing account
            bad_acc_seed = dict(self.sample_seed)
            bad_acc_seed["accounts"] = [
                {
                    "name": "招商银行储蓄卡",
                    "institution": "招商银行",
                    "account_type": "savings",
                    "currency": "USD"  # Changed from CNY
                }
            ]
            with self.assertRaises(BootstrapConsistencyError) as ctx_acc:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=bad_acc_seed,
                        ledger_start_date=self.ledger_start_date,
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("currency mismatch", str(ctx_acc.exception))

            # Test C: Conflicting linked_cash_account_id (refuses silent update)
            bad_link_seed = dict(self.sample_seed)
            bad_link_seed["accounts"] = [
                {
                    "name": "招商银行储蓄卡",
                    "institution": "招商银行",
                    "account_type": "savings",
                    "currency": "CNY"
                },
                {
                    "name": "招商银行信用卡",
                    "institution": "招商银行",
                    "account_type": "credit",
                    "currency": "CNY",
                    "billing_day": 5,
                    "due_day": 25,
                    "linked_cash_account_name": None  # Changed from linked to unlinked
                }
            ]
            with self.assertRaises(BootstrapConsistencyError) as ctx_link:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=bad_link_seed,
                        ledger_start_date=self.ledger_start_date,
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("linked_cash_account_id mismatch", str(ctx_link.exception))

    def test_bootstrap_fails_on_unresolved_linked_cash_account(self):
        bad_seed = dict(self.sample_seed)
        bad_seed["accounts"] = [
            {
                "name": "信用卡",
                "institution": "Bank",
                "account_type": "credit",
                "currency": "CNY",
                "linked_cash_account_name": "不存在的储蓄卡"
            }
        ]
        with get_connection(self.test_schema) as conn:
            with self.assertRaises(BootstrapConsistencyError) as ctx:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=bad_seed,
                        ledger_start_date=self.ledger_start_date,
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("not in seed accounts", str(ctx.exception))

    def test_bootstrap_fails_on_unknown_account_in_aliases(self):
        bad_seed = dict(self.sample_seed)
        bad_seed["aliases"] = {
            "未定义账户名": ["别名1", "别名2"]
        }
        with get_connection(self.test_schema) as conn:
            with self.assertRaises(BootstrapConsistencyError) as ctx:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=bad_seed,
                        ledger_start_date=self.ledger_start_date,
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("unknown account '未定义账户名'", str(ctx.exception))

    def test_bootstrap_fails_if_existing_account_state_has_initialized_at(self):
        with get_connection(self.test_schema) as conn:
            with transaction(conn):
                res = bootstrap_staging_environment(
                    conn=conn,
                    seed_data=self.sample_seed,
                    ledger_start_date=self.ledger_start_date,
                    owner_auth_subject=self.owner_sub
                )
            hh_id = UUID(res["household_id"])

            # Manually simulate established opening balance / baseline
            with transaction(conn):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE account_state SET initialized_at = now() WHERE account_id IN (SELECT id FROM accounts WHERE household_id = %s LIMIT 1);",
                        (hh_id,)
                    )

            # Rerun bootstrap must fail loudly and refuse to overwrite authoritative baseline
            with self.assertRaises(BootstrapConsistencyError) as ctx:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=self.sample_seed,
                        ledger_start_date=self.ledger_start_date,
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("already has initialized_at", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
