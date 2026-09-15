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
    1. Bootstrap idempotency and zero transaction creation.
    2. Consistency verification on existing entities (fail loudly on attribute mismatch).
    3. Failure on unknown alias account.
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
                "email": "owner@staging.test.com"
            },
            "accounts": [
                {
                    "name": "招商银行储蓄卡",
                    "account_type": "savings",
                    "balance_scope": "招商银行储蓄卡 balance",
                    "currency": "CNY"
                },
                {
                    "name": "招商银行信用卡",
                    "account_type": "credit",
                    "balance_scope": "招商银行信用卡 balance",
                    "currency": "CNY"
                },
                {
                    "name": "Chase Sapphire Card",
                    "account_type": "credit",
                    "balance_scope": "Chase Sapphire Card balance",
                    "currency": "USD"
                },
                {
                    "name": "美股投资账户",
                    "account_type": "investment",
                    "balance_scope": "美股投资账户 balance",
                    "currency": "USD",
                    "risk_level": "medium"
                },
                {
                    "name": "现金钱包",
                    "account_type": "cash",
                    "balance_scope": "现金钱包 balance",
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
        self.started_on = date(2026, 8, 1)
        self.ledger_start_date = self.started_on
        self.owner_sub = "auth0|staging_owner_integration_test"

    def test_bootstrap_initial_and_idempotent_rerun(self):
        with get_connection(self.test_schema) as conn:
            # 1. Initial bootstrap
            with transaction(conn):
                res1 = bootstrap_staging_environment(
                    conn=conn,
                    seed_data=self.sample_seed,
                    started_on=self.started_on,
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
            self.assertEqual(hh["started_on"], self.started_on)

            # Verify owner user attributes
            owner = accounts_repo.get_user(conn, owner_id)
            self.assertEqual(owner["auth_subject"], self.owner_sub)

            # Verify household membership
            members = accounts_repo.get_household_members(conn, hh_id)
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0]["user_id"], owner_id)
            self.assertEqual(members[0]["role"], "owner")

            # Verify ZERO transactions exist
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM transactions WHERE household_id = %s;", (hh_id,))
                tx_count = cur.fetchone()[0]
                self.assertEqual(tx_count, 0, "Bootstrap must create zero transactions")

            # 2. Idempotent second bootstrap run with identical data
            with transaction(conn):
                res2 = bootstrap_staging_environment(
                    conn=conn,
                    seed_data=self.sample_seed,
                    started_on=self.started_on,
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
                    started_on=self.started_on,
                    owner_auth_subject=self.owner_sub
                )

            # Test A: Conflicting started_on / ledger_start_date for existing household
            with self.assertRaises(BootstrapConsistencyError) as ctx_hh:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=self.sample_seed,
                        started_on=date(2025, 1, 1),
                        owner_auth_subject=self.owner_sub
                    )
            self.assertTrue(
                "started_on" in str(ctx_hh.exception) or "ledger_start_date" in str(ctx_hh.exception)
            )

            # Test B: Conflicting account currency for existing account
            bad_acc_seed = dict(self.sample_seed)
            bad_acc_seed["accounts"] = [
                {
                    "name": "招商银行储蓄卡",
                    "account_type": "savings",
                    "balance_scope": "招商银行储蓄卡 balance",
                    "currency": "USD"  # Changed from CNY
                }
            ]
            with self.assertRaises(BootstrapConsistencyError) as ctx_acc:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=bad_acc_seed,
                        started_on=self.started_on,
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("currency mismatch", str(ctx_acc.exception))

            # Test C: Conflicting account_type for existing account
            bad_type_seed = dict(self.sample_seed)
            bad_type_seed["accounts"] = [
                {
                    "name": "招商银行储蓄卡",
                    "account_type": "credit",  # Changed from savings
                    "balance_scope": "招商银行储蓄卡 balance",
                    "currency": "CNY"
                }
            ]
            with self.assertRaises(BootstrapConsistencyError) as ctx_type:
                with transaction(conn):
                    bootstrap_staging_environment(
                        conn=conn,
                        seed_data=bad_type_seed,
                        started_on=self.started_on,
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("account_type mismatch", str(ctx_type.exception))

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
                        started_on=self.started_on,
                        owner_auth_subject=self.owner_sub
                    )
            self.assertIn("unknown account '未定义账户名'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
