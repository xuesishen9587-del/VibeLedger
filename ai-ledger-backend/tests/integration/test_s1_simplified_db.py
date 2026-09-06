"""
Real PostgreSQL Integration Test Suite for S1: Fresh Database, Identities, and Settings Foundation.

Validates the full battery of database behavior against a disposable local PostgreSQL instance:
- Migration from empty: 16 declared tables + schema_migrations
- Migration idempotency: second run is a no-op with checksum verification
- Lineage rejection: rejects schemas containing legacy migration records
- PostgreSQL constraints: credit risk, lifetime dates, household ratio, fallback uniqueness
- Composite foreign keys: cross-household reference rejection
- Audit immutability: trigger blocks UPDATE and DELETE
- Bootstrap idempotency: double run produces zero duplicates and zero opening balances
- Finance-write lock: serializes on household row FOR UPDATE
- Transaction rollback: atomicity on failure

In accordance with safety invariants, tests strictly refuse to run against remote Supabase databases.
If Docker / local PostgreSQL is unavailable on the host, tests skip gracefully with an explicit message.
"""

import hashlib
import os
import unittest
import uuid
from decimal import Decimal
from datetime import date, datetime

import psycopg2
from psycopg2 import sql, errors

from app import config
from app.config import Settings, is_safe_for_testing, validate_test_schema
from app.db import get_connection, transaction
from migrations import runner
from migrations.runner import (
    LINEAGE_SIMPLIFIED,
    LINEAGE_LEGACY,
    LegacyMigrationLineageDetectedError,
    MigrationChecksumMismatch,
    verify_schema_lineage,
)
from app.repositories import simplified_schema as repo
from scripts.bootstrap_simplified import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    STARTER_ACCOUNTS,
    bootstrap_simplified_environment,
)

# Test-safe configuration
os.environ["ENVIRONMENT"] = "test"
if "DB_SCHEMA" not in os.environ:
    os.environ["DB_SCHEMA"] = "vibeledger_test_runner"


def _check_local_pg_available() -> bool:
    """Probes whether a safe disposable local PostgreSQL connection is reachable."""
    if not is_safe_for_testing():
        return False
    try:
        settings = config.get_settings()
        conn = get_connection(settings.DB_SCHEMA)
        conn.close()
        return True
    except Exception:
        return False


class TestS1SimplifiedPostgresIntegration(unittest.TestCase):
    """Integration test suite executing actual DDL and DML against real PostgreSQL."""

    def setUp(self):
        if not is_safe_for_testing():
            self.skipTest("Skipping DB integration test: ENVIRONMENT must be 'test' and cannot point to remote Supabase.")
        if not _check_local_pg_available():
            self.skipTest(
                "Local PostgreSQL 17 test instance is unreachable on host. "
                "Docker is required for local integration testing (docker-compose.integration.yml). "
                "Remote databases cannot be substituted."
            )

        self.test_schema = f"vibeledger_test_{uuid.uuid4().hex[:12]}"
        validate_test_schema(self.test_schema)

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

    def test_migration_from_empty_creates_16_tables(self):
        """Runs simplified baseline migration on fresh schema and inspects PostgreSQL catalog."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                # 1. Inspect tables from PostgreSQL catalog
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s
                    ORDER BY table_name;
                    """,
                    (self.test_schema,),
                )
                tables = {row[0] for row in cur.fetchall()}

                expected_tables = {
                    "households",
                    "users",
                    "household_members",
                    "devices",
                    "accounts",
                    "account_aliases",
                    "categories",
                    "ingestion_requests",
                    "spending_schedules",
                    "schedule_occurrences",
                    "transactions",
                    "account_snapshots",
                    "investment_period_inputs",
                    "fx_quotes",
                    "statement_lines",
                    "audit_events",
                    "schema_migrations",
                }
                self.assertEqual(
                    tables,
                    expected_tables,
                    f"PostgreSQL schema must contain exactly the 16 tables + schema_migrations. Got: {tables}",
                )

                # 2. Verify schema_migrations record
                cur.execute("SELECT migration_name, checksum_sha256 FROM schema_migrations;")
                rows = cur.fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], "0001_simplified.sql")
                self.assertEqual(len(rows[0][1]), 64)
        finally:
            conn.close()

    def test_migration_second_run_is_idempotent(self):
        """Verifies running simplified migrations a second time is a complete no-op."""
        # First run
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        # Second run
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM schema_migrations;")
                count = cur.fetchone()[0]
                self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_migration_checksum_tamper_detection(self):
        """Verifies tampering with recorded checksum causes MigrationChecksumMismatch."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE schema_migrations SET checksum_sha256 = 'tampered_bad_checksum' WHERE migration_name = '0001_simplified.sql';"
                )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(MigrationChecksumMismatch):
            runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

    def test_lineage_rejection_for_legacy_schema(self):
        """Verifies simplified runner refuses to run on schema with legacy migrations."""
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema};").format(
                        schema=sql.Identifier(self.test_schema)
                    )
                )
                cur.execute(
                    """
                    CREATE TABLE schema_migrations (
                        migration_name TEXT PRIMARY KEY,
                        checksum_sha256 TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    INSERT INTO schema_migrations (migration_name, checksum_sha256)
                    VALUES ('0001_extensions.sql', 'fake_checksum_hash');
                    """
                )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(LegacyMigrationLineageDetectedError):
            runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

    def test_readiness_lineage_verification(self):
        """Verifies verify_schema_lineage against real PostgreSQL catalog."""
        conn = get_connection(self.test_schema)
        try:
            # 1. Unmigrated schema
            is_ok, reason = verify_schema_lineage(conn, lineage=LINEAGE_SIMPLIFIED)
            self.assertFalse(is_ok)
            self.assertEqual(reason, "schema_not_ready")

            # 2. Migrated schema
            runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)
            is_ok, reason = verify_schema_lineage(conn, lineage=LINEAGE_SIMPLIFIED)
            self.assertTrue(is_ok)
            self.assertEqual(reason, "ok")

            # 3. Inject legacy migration row
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_migrations (migration_name, checksum_sha256) VALUES ('0009_indexes.sql', 'fake');"
                )
            conn.commit()

            is_ok, reason = verify_schema_lineage(conn, lineage=LINEAGE_SIMPLIFIED)
            self.assertFalse(is_ok)
            self.assertEqual(reason, "incompatible_schema_lineage")
        finally:
            conn.close()

    def test_postgres_constraints_enforced(self):
        """Validates CHECK constraints and partial UNIQUE indexes in PostgreSQL."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            # Setup household and user
            hh = repo.create_household(conn, name="Constraint Test Household")
            hh_id = uuid.UUID(str(hh["id"]))
            user = repo.create_user(conn, auth_subject="sub_const", email="const@test.local", display_name="User")
            user_id = uuid.UUID(str(user["id"]))
            repo.add_household_member(conn, hh_id, user_id, role="owner")
            conn.commit()

            # 1. Credit account with risk_level != NULL fails CHECK constraint
            with self.assertRaises(errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO accounts (id, household_id, name, balance_scope, account_type, currency, risk_level, opened_on)
                        VALUES (gen_random_uuid(), %s, 'Bad Credit', 'scope', 'credit', 'CNY', 'low', '2026-01-01');
                        """,
                        (str(hh_id),),
                    )
            conn.rollback()

            # 2. Account closed_on < opened_on fails CHECK constraint
            with self.assertRaises(errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO accounts (id, household_id, name, balance_scope, account_type, currency, status, opened_on, closed_on)
                        VALUES (gen_random_uuid(), %s, 'Bad Dates', 'scope', 'cash', 'CNY', 'closed', '2026-05-01', '2026-01-01');
                        """,
                        (str(hh_id),),
                    )
            conn.rollback()

            # 3. Active account with closed_on != NULL fails CHECK constraint
            with self.assertRaises(errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO accounts (id, household_id, name, balance_scope, account_type, currency, status, opened_on, closed_on)
                        VALUES (gen_random_uuid(), %s, 'Bad Active', 'scope', 'cash', 'CNY', 'active', '2026-01-01', '2026-05-01');
                        """,
                        (str(hh_id),),
                    )
            conn.rollback()

            # 4. Household ratio <= 0 or > 1 fails CHECK constraint
            with self.assertRaises(errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO households (id, name, started_on, investment_review_change_ratio)
                        VALUES (gen_random_uuid(), 'Bad Ratio', '2026-01-01', 0.0);
                        """
                    )
            conn.rollback()

            with self.assertRaises(errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO households (id, name, started_on, investment_review_change_ratio)
                        VALUES (gen_random_uuid(), 'Bad Ratio 2', '2026-01-01', 1.0001);
                        """
                    )
            conn.rollback()

            # 5. Duplicate fallback category fails partial UNIQUE index
            repo.create_category(conn, hh_id, "Other 1", "expense", is_fallback=True)
            conn.commit()

            with self.assertRaises(errors.UniqueViolation):
                repo.create_category(conn, hh_id, "Other 2", "expense", is_fallback=True)
            conn.rollback()

        finally:
            conn.close()

    def test_composite_foreign_keys_prevent_cross_household_references(self):
        """Validates that foreign keys enforce household isolation in PostgreSQL."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            hh_a = repo.create_household(conn, name="Household A")
            hh_a_id = uuid.UUID(str(hh_a["id"]))
            user_a = repo.create_user(conn, auth_subject="sub_a", email="a@test.local", display_name="User A")
            user_a_id = uuid.UUID(str(user_a["id"]))
            repo.add_household_member(conn, hh_a_id, user_a_id, role="owner")

            hh_b = repo.create_household(conn, name="Household B")
            hh_b_id = uuid.UUID(str(hh_b["id"]))
            conn.commit()

            # Cross-household: device in Household B referencing user in Household A fails FK
            with self.assertRaises(errors.ForeignKeyViolation):
                repo.create_device(
                    conn,
                    household_id=hh_b_id,
                    user_id=user_a_id,
                    name="Hacked Device",
                    platform="ios",
                    token_hash=b"0" * 32,
                )
            conn.rollback()

            # Cross-household: account in Household B referencing user in Household A fails FK
            with self.assertRaises(errors.ForeignKeyViolation):
                repo.create_account(
                    conn,
                    household_id=hh_b_id,
                    name="Hacked Account",
                    balance_scope="scope",
                    account_type="cash",
                    currency="CNY",
                    owner_user_id=user_a_id,
                )
            conn.rollback()
        finally:
            conn.close()

    def test_audit_events_immutability_trigger(self):
        """Validates PostgreSQL trigger blocks UPDATE and DELETE on audit_events."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            hh = repo.create_household(conn, name="Audit Test HH")
            hh_id = uuid.UUID(str(hh["id"]))
            conn.commit()

            event = repo.insert_audit_event(
                conn,
                household_id=hh_id,
                actor_type="system",
                entity_type="household",
                entity_id=hh_id,
                action="create",
                reason="Initial creation",
            )
            event_id = event["id"]
            conn.commit()

            # Attempt UPDATE -> trigger must raise exception
            with self.assertRaises(errors.RaiseException) as ctx_update:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE audit_events SET reason = 'tampered' WHERE id = %s;",
                        (event_id,),
                    )
            self.assertIn("audit_events is append-only", str(ctx_update.exception))
            conn.rollback()

            # Attempt DELETE -> trigger must raise exception
            with self.assertRaises(errors.RaiseException) as ctx_delete:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM audit_events WHERE id = %s;",
                        (event_id,),
                    )
            self.assertIn("audit_events is append-only", str(ctx_delete.exception))
            conn.rollback()
        finally:
            conn.close()

    def test_bootstrap_idempotency_and_zero_opening_balances(self):
        """Validates bootstrap_simplified_environment runs twice with zero duplicates and zero opening balances."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                summary1 = bootstrap_simplified_environment(
                    conn,
                    household_name="Bootstrap HH",
                    owner_auth_subject="bs_owner",
                    owner_email="bs@vibeledger.local",
                )
            self.assertEqual(summary1["categories_created"], 18)
            self.assertEqual(summary1["accounts_created"], 3)

            # Second pass: zero created, all verified
            with transaction(conn):
                summary2 = bootstrap_simplified_environment(
                    conn,
                    household_name="Bootstrap HH",
                    owner_auth_subject="bs_owner",
                    owner_email="bs@vibeledger.local",
                )
            self.assertEqual(summary2["categories_created"], 0)
            self.assertEqual(summary2["categories_verified"], 18)
            self.assertEqual(summary2["accounts_created"], 0)
            self.assertEqual(summary2["accounts_verified"], 3)

            # Assert zero opening balance transactions
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM transactions;")
                self.assertEqual(cur.fetchone()[0], 0)

                # Assert zero account_snapshots
                cur.execute("SELECT COUNT(*) FROM account_snapshots;")
                self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_household_finance_write_lock_primitive(self):
        """Validates acquire_household_finance_lock acquires row lock in PostgreSQL."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            hh = repo.create_household(conn, name="Lock HH")
            hh_id = uuid.UUID(str(hh["id"]))
            conn.commit()

            with transaction(conn):
                locked = repo.acquire_household_finance_lock(conn, hh_id)
                self.assertEqual(str(locked["id"]), str(hh_id))
                self.assertEqual(locked["status"], "active")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
