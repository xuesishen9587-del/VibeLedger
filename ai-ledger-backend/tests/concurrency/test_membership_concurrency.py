"""
Real PostgreSQL Concurrency Tests for One-Active-Household Invariant.

Proves:
1. Full bootstrap transaction concurrency:
   Two independent PostgreSQL connections/transactions run bootstrap_simplified_environment
   for two different active households targeting the same pre-existing user.
   Deterministic row locking (SELECT id FROM users WHERE id = %s FOR UPDATE) guarantees:
   - Exactly one bootstrap succeeds and commits.
   - Exactly one bootstrap fails with BootstrapDriftError and rolls back.
   - Final database state contains exactly 1 active household membership for the user.
   - The losing bootstrap leaves ZERO partially committed state (0 households, 0 categories, 0 accounts).
2. Owning-layer direct concurrency:
   Two concurrent transactions calling simplified_schema.add_household_member for different households
   on the same user: exactly one commits, the other raises ActiveHouseholdMembershipConflictError,
   and final DB state has at most 1 active membership.
"""

import os
import unittest
import uuid
import threading
from psycopg2 import sql

from app import config
from app.config import is_safe_for_testing, validate_test_schema
from app.db import get_connection, transaction
from migrations import runner
from migrations.runner import LINEAGE_SIMPLIFIED
from app.repositories import simplified_schema as repo
from scripts.bootstrap_simplified import (
    BootstrapDriftError,
    bootstrap_simplified_environment,
)

os.environ["ENVIRONMENT"] = "test"
if "DB_SCHEMA" not in os.environ:
    os.environ["DB_SCHEMA"] = "vibeledger_test_runner"


def _check_local_pg_available() -> bool:
    if not is_safe_for_testing():
        return False
    try:
        settings = config.get_settings()
        conn = get_connection(settings.DB_SCHEMA)
        conn.close()
        return True
    except Exception:
        return False


class TestMembershipConcurrencyPostgres(unittest.TestCase):
    def setUp(self):
        if not is_safe_for_testing():
            self.skipTest("Skipping DB test: ENVIRONMENT must be 'test' and cannot point to remote Supabase.")
        if not _check_local_pg_available():
            self.skipTest("Local PostgreSQL instance is unreachable.")

        self.test_schema = f"vibeledger_test_{uuid.uuid4().hex[:12]}"
        validate_test_schema(self.test_schema)
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

    def tearDown(self):
        if hasattr(self, "test_schema") and is_safe_for_testing() and _check_local_pg_available():
            try:
                settings = config.get_settings()
                conn = get_connection(settings.DB_SCHEMA)
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE;").format(
                            schema=sql.Identifier(self.test_schema)
                        )
                    )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"Warning: Cleanup failed for schema {self.test_schema}: {e}")

    def test_concurrent_bootstrap_full_transaction(self):
        """
        Two concurrent threads run full bootstrap transactions for competing households
        targeting the same user.
        Proves: exactly one commits, losing transaction rolls back with zero partial state,
        and user has exactly one active membership in DB.
        """
        auth_sub = f"auth0|concurrent_owner_{uuid.uuid4().hex[:6]}"
        email = f"concurrent_owner_{uuid.uuid4().hex[:6]}@example.com"

        # Pre-create the user so both bootstrap paths resolve the same user identity
        conn_setup = get_connection(self.test_schema)
        try:
            user = repo.create_user(
                conn_setup,
                auth_subject=auth_sub,
                email=email,
                display_name="Concurrent Owner",
            )
            user_id = uuid.UUID(str(user["id"]))
            conn_setup.commit()
        finally:
            conn_setup.close()

        barrier = threading.Barrier(2)
        results = [None, None]
        errors = [None, None]

        def _worker(idx: int, hh_name: str):
            try:
                barrier.wait(timeout=10)
                with transaction(schema=self.test_schema) as conn:
                    summary = bootstrap_simplified_environment(
                        conn,
                        household_name=hh_name,
                        owner_auth_subject=auth_sub,
                        owner_email=email,
                        owner_display_name="Concurrent Owner",
                    )
                    results[idx] = summary
            except Exception as ex:
                errors[idx] = ex

        t1 = threading.Thread(target=_worker, args=(0, "Concurrent Household 1"))
        t2 = threading.Thread(target=_worker, args=(1, "Concurrent Household 2"))

        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        # Exactly one worker should succeed, and exactly one should fail with BootstrapDriftError
        success_count = sum(1 for r in results if r is not None)
        error_count = sum(1 for e in errors if e is not None)

        self.assertEqual(success_count, 1, f"Expected exactly 1 success, got {results}")
        self.assertEqual(error_count, 1, f"Expected exactly 1 error, got {errors}")

        failed_error = next(e for e in errors if e is not None)
        self.assertIsInstance(failed_error, BootstrapDriftError)
        self.assertIn("already has an active membership in another household", str(failed_error))

        # Identify winning and losing household
        winner_idx = 0 if results[0] is not None else 1
        loser_idx = 1 - winner_idx
        winning_summary = results[winner_idx]
        winning_hh_id = winning_summary["household_id"]
        losing_hh_name = "Concurrent Household 2" if winner_idx == 0 else "Concurrent Household 1"

        # Final database state verification
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                # 1. User belongs to EXACTLY ONE active household membership
                cur.execute(
                    """
                    SELECT hm.household_id, hm.role, h.name, h.status
                    FROM household_members hm
                    JOIN households h ON hm.household_id = h.id
                    WHERE hm.user_id = %s AND h.status = 'active';
                    """,
                    (str(user_id),),
                )
                mems = cur.fetchall()
                self.assertEqual(len(mems), 1)
                self.assertEqual(str(mems[0][0]), str(winning_hh_id))

                # 2. Total household memberships for user is 1 (zero membership in losing household)
                cur.execute(
                    "SELECT count(*) FROM household_members WHERE user_id = %s;",
                    (str(user_id),),
                )
                self.assertEqual(cur.fetchone()[0], 1)

                # 3. Losing household left ZERO partially committed state in DB
                cur.execute(
                    "SELECT count(*) FROM households WHERE lower(name) = lower(%s);",
                    (losing_hh_name,),
                )
                self.assertEqual(cur.fetchone()[0], 0, f"Losing household '{losing_hh_name}' was not rolled back")

                # 4. Categories created count matches winning household only (18 categories)
                cur.execute("SELECT count(*) FROM categories;")
                self.assertEqual(cur.fetchone()[0], 18)

                # 5. Accounts created count matches winning household only (3 starter accounts)
                cur.execute("SELECT count(*) FROM accounts;")
                self.assertEqual(cur.fetchone()[0], 3)
        finally:
            conn.close()

    def test_concurrent_add_household_member_owning_layer(self):
        """
        Two concurrent transactions directly call repo.add_household_member for different households.
        Proves: serialized by user row lock, exactly one commits, other raises
        ActiveHouseholdMembershipConflictError, final active membership count is 1.
        """
        conn_setup = get_connection(self.test_schema)
        try:
            user = repo.create_user(
                conn_setup,
                auth_subject=f"auth0|direct_conc_{uuid.uuid4().hex[:6]}",
                email=f"direct_conc_{uuid.uuid4().hex[:6]}@example.com",
                display_name="Direct Conc User",
            )
            user_id = uuid.UUID(str(user["id"]))
            hh_1 = repo.create_household(conn_setup, name="Direct HH 1")
            hh_2 = repo.create_household(conn_setup, name="Direct HH 2")
            hh_1_id = uuid.UUID(str(hh_1["id"]))
            hh_2_id = uuid.UUID(str(hh_2["id"]))
            conn_setup.commit()
        finally:
            conn_setup.close()

        barrier = threading.Barrier(2)
        results = [None, None]
        errors = [None, None]

        def _worker(idx: int, target_hh_id: uuid.UUID):
            try:
                barrier.wait(timeout=10)
                with transaction(schema=self.test_schema) as conn:
                    mem = repo.add_household_member(conn, target_hh_id, user_id, role="owner")
                    results[idx] = mem
            except Exception as ex:
                errors[idx] = ex

        t1 = threading.Thread(target=_worker, args=(0, hh_1_id))
        t2 = threading.Thread(target=_worker, args=(1, hh_2_id))

        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        success_count = sum(1 for r in results if r is not None)
        error_count = sum(1 for e in errors if e is not None)

        self.assertEqual(success_count, 1)
        self.assertEqual(error_count, 1)

        conflict_err = next(e for e in errors if e is not None)
        self.assertIsInstance(conflict_err, repo.ActiveHouseholdMembershipConflictError)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*)
                    FROM household_members hm
                    JOIN households h ON hm.household_id = h.id
                    WHERE hm.user_id = %s AND h.status = 'active';
                    """,
                    (str(user_id),),
                )
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
