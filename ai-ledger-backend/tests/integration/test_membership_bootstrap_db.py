"""
Real PostgreSQL Integration Tests for One-Active-Household Membership Invariant.

Proves:
A. user has no active household + target household is active -> membership may be created
B. user already belongs to target active household -> repeated bootstrap remains idempotent -> no duplicate
C. user belongs to another active household + target membership does not exist -> reject before creating a second active membership
D. user belongs to another active household + target membership already exists -> reject the ambiguous state
E. user belongs only to an archived/inactive household -> creation of a new active-household membership is allowed
- Role drift rejection: existing target membership with role='member' is NOT silently upserted to 'owner', raises BootstrapDriftError
- Owning layer enforcement: repo.add_household_member directly enforces invariant and raises ActiveHouseholdMembershipConflictError
"""

import os
import unittest
import uuid
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


class TestMembershipBootstrapPostgresIntegration(unittest.TestCase):
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

    def _get_active_membership_count(self, conn, user_id: uuid.UUID) -> int:
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
            row = cur.fetchone()
            return row[0] if row else 0

    def test_case_a_user_no_active_household_creates_membership(self):
        """Case A: user has no active household + target household is active -> membership created."""
        auth_sub = f"auth0|user_a_{uuid.uuid4().hex[:6]}"
        email = f"user_a_{uuid.uuid4().hex[:6]}@example.com"

        with transaction(schema=self.test_schema) as conn:
            summary = bootstrap_simplified_environment(
                conn,
                household_name="Household A",
                owner_auth_subject=auth_sub,
                owner_email=email,
                owner_display_name="User A",
            )

        user_id = uuid.UUID(summary["owner_user_id"])
        hh_id = uuid.UUID(summary["household_id"])
        conn = get_connection(self.test_schema)
        try:
            count = self._get_active_membership_count(conn, user_id)
            self.assertEqual(count, 1)

            mem = repo.get_household_member(conn, hh_id, user_id)
            self.assertIsNotNone(mem)
            self.assertEqual(mem["role"], "owner")
        finally:
            conn.close()

    def test_case_b_repeated_bootstrap_is_idempotent_no_duplicate(self):
        """Case B: user already belongs to target active household -> repeated bootstrap idempotent, no duplicate."""
        auth_sub = f"auth0|user_b_{uuid.uuid4().hex[:6]}"
        email = f"user_b_{uuid.uuid4().hex[:6]}@example.com"

        with transaction(schema=self.test_schema) as conn:
            summary1 = bootstrap_simplified_environment(
                conn,
                household_name="Household B",
                owner_auth_subject=auth_sub,
                owner_email=email,
                owner_display_name="User B",
            )
        user_id = uuid.UUID(summary1["owner_user_id"])

        with transaction(schema=self.test_schema) as conn:
            summary2 = bootstrap_simplified_environment(
                conn,
                household_name="Household B",
                owner_auth_subject=auth_sub,
                owner_email=email,
                owner_display_name="User B",
            )
        self.assertEqual(summary2["owner_user_id"], str(user_id))

        conn = get_connection(self.test_schema)
        try:
            count = self._get_active_membership_count(conn, user_id)
            self.assertEqual(count, 1)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM household_members WHERE user_id = %s;",
                    (str(user_id),),
                )
                total_mems = cur.fetchone()[0]
                self.assertEqual(total_mems, 1)
        finally:
            conn.close()

    def test_case_c_user_in_active_household_rejects_second_household_bootstrap(self):
        """Case C: user in active Household A + target Household B -> reject before creating second active membership."""
        auth_sub = f"auth0|shared_{uuid.uuid4().hex[:6]}"
        email = f"shared_{uuid.uuid4().hex[:6]}@example.com"

        # Bootstrap Household A
        with transaction(schema=self.test_schema) as conn:
            summary_a = bootstrap_simplified_environment(
                conn,
                household_name="Household A",
                owner_auth_subject=auth_sub,
                owner_email=email,
                owner_display_name="Shared User",
            )
        user_id = uuid.UUID(summary_a["owner_user_id"])
        hh_a_id = uuid.UUID(summary_a["household_id"])

        # Attempt to bootstrap Household B for same user
        with self.assertRaises(BootstrapDriftError) as ctx:
            with transaction(schema=self.test_schema) as conn:
                bootstrap_simplified_environment(
                    conn,
                    household_name="Household B",
                    owner_auth_subject=auth_sub,
                    owner_email=email,
                    owner_display_name="Shared User",
                )
        self.assertIn("already has an active membership in another household", str(ctx.exception))

        conn = get_connection(self.test_schema)
        try:
            # Final database state verification: user belongs to exactly one active household
            count = self._get_active_membership_count(conn, user_id)
            self.assertEqual(count, 1)

            # No partially committed Household B state (transaction rolled back)
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM households WHERE lower(name) = 'household b';")
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_case_d_user_in_another_active_household_and_target_exists_rejects(self):
        """Case D: user in another active household + target membership already exists -> reject ambiguous state."""
        auth_sub = f"auth0|ambig_{uuid.uuid4().hex[:6]}"
        email = f"ambig_{uuid.uuid4().hex[:6]}@example.com"

        # Create Household A and User via bootstrap
        with transaction(schema=self.test_schema) as conn:
            summary_a = bootstrap_simplified_environment(
                conn,
                household_name="Household A",
                owner_auth_subject=auth_sub,
                owner_email=email,
                owner_display_name="Ambig User",
            )
        user_id = uuid.UUID(summary_a["owner_user_id"])

        # Directly insert a household B and membership for user (simulating pre-existing corrupted/ambiguous state)
        from datetime import date
        hh_b_id = uuid.uuid4()
        conn = get_connection(self.test_schema)
        try:
            repo.create_household(conn, household_id=hh_b_id, name="Household B", reporting_currency="CNY", started_on=date(2026, 1, 1))
            with conn.cursor() as cur:
                # Raw insert bypassing active check to simulate pre-existing anomalous state
                cur.execute(
                    "INSERT INTO household_members (household_id, user_id, role, joined_at) VALUES (%s, %s, 'owner', now());",
                    (str(hh_b_id), str(user_id)),
                )
            conn.commit()
        finally:
            conn.close()

        # Bootstrap Household B must detect ambiguous state and reject
        with self.assertRaises(BootstrapDriftError) as ctx:
            with transaction(schema=self.test_schema) as conn:
                bootstrap_simplified_environment(
                    conn,
                    household_name="Household B",
                    owner_auth_subject=auth_sub,
                    owner_email=email,
                    owner_display_name="Ambig User",
                )
        self.assertIn("already has an active membership in another household", str(ctx.exception))

    def test_case_e_inactive_household_membership_allows_new_active_household(self):
        """Case E: user belongs only to an inactive household -> creation of new active household succeeds."""
        hh_old_id = uuid.uuid4()
        auth_sub = f"auth0|inactive_user_{uuid.uuid4().hex[:6]}"
        email = f"inactive_user_{uuid.uuid4().hex[:6]}@example.com"

        conn = get_connection(self.test_schema)
        try:
            # Create user
            user = repo.create_user(conn, auth_subject=auth_sub, email=email, display_name="Archived User")
            user_id = uuid.UUID(str(user["id"]))

            # Create an inactive household and add user to it
            repo.create_household(conn, household_id=hh_old_id, name="Old Household", reporting_currency="CNY")
            with conn.cursor() as cur:
                cur.execute("UPDATE households SET status = 'archived' WHERE id = %s;", (str(hh_old_id),))
            repo.add_household_member(conn, hh_old_id, user_id, role="member")
            conn.commit()

            # Verify active count is 0
            self.assertEqual(self._get_active_membership_count(conn, user_id), 0)
        finally:
            conn.close()

        # Bootstrap new active household for this user must succeed
        with transaction(schema=self.test_schema) as conn:
            summary = bootstrap_simplified_environment(
                conn,
                household_name="New Active Household",
                owner_auth_subject=auth_sub,
                owner_email=email,
                owner_display_name="Archived User",
            )
        self.assertEqual(summary["owner_user_id"], str(user_id))

        conn = get_connection(self.test_schema)
        try:
            count = self._get_active_membership_count(conn, user_id)
            self.assertEqual(count, 1)

            hh_new_id = uuid.UUID(summary["household_id"])
            mem_new = repo.get_household_member(conn, hh_new_id, user_id)
            self.assertIsNotNone(mem_new)
            self.assertEqual(mem_new["role"], "owner")
        finally:
            conn.close()

    def test_no_silent_role_upsert_on_role_drift(self):
        """If user belongs to target household as 'member', bootstrap does NOT silently change to 'owner', fails on role drift."""
        hh_id = uuid.uuid4()
        auth_sub = f"auth0|member_user_{uuid.uuid4().hex[:6]}"
        email = f"member_user_{uuid.uuid4().hex[:6]}@example.com"

        conn = get_connection(self.test_schema)
        try:
            from datetime import date
            user = repo.create_user(conn, auth_subject=auth_sub, email=email, display_name="Member User")
            user_id = uuid.UUID(str(user["id"]))
            repo.create_household(conn, household_id=hh_id, name="Household Target", reporting_currency="CNY", started_on=date(2026, 1, 1))
            # User is added with role 'member'
            repo.add_household_member(conn, hh_id, user_id, role="member")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(BootstrapDriftError) as ctx:
            with transaction(schema=self.test_schema) as conn:
                bootstrap_simplified_environment(
                    conn,
                    household_name="Household Target",
                    owner_auth_subject=auth_sub,
                    owner_email=email,
                    owner_display_name="Member User",
                )
        self.assertIn("has role 'member', expected 'owner'", str(ctx.exception))

        conn = get_connection(self.test_schema)
        try:
            # Confirm role is still 'member', not silently changed
            mem = repo.get_household_member(conn, hh_id, user_id)
            self.assertEqual(mem["role"], "member")
        finally:
            conn.close()

    def test_owning_layer_add_household_member_direct_enforcement(self):
        """Proves repo.add_household_member directly enforces invariant and raises ActiveHouseholdMembershipConflictError."""
        conn = get_connection(self.test_schema)
        try:
            user = repo.create_user(conn, auth_subject=f"sub_{uuid.uuid4().hex[:6]}", email=f"u_{uuid.uuid4().hex[:6]}@ex.com", display_name="Direct User")
            user_id = uuid.UUID(str(user["id"]))

            hh1_id = uuid.uuid4()
            hh2_id = uuid.uuid4()
            repo.create_household(conn, household_id=hh1_id, name="HH 1")
            repo.create_household(conn, household_id=hh2_id, name="HH 2")

            # First active membership succeeds
            m1 = repo.add_household_member(conn, hh1_id, user_id, role="owner")
            self.assertEqual(m1["role"], "owner")
            conn.commit()

            # Second active membership on different household raises ActiveHouseholdMembershipConflictError
            with self.assertRaises(repo.ActiveHouseholdMembershipConflictError):
                repo.add_household_member(conn, hh2_id, user_id, role="owner")
            conn.rollback()

            # Repeated call on same household returns existing unchanged
            m1_again = repo.add_household_member(conn, hh1_id, user_id, role="owner")
            self.assertEqual(m1_again["household_id"], m1["household_id"])
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
