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

import threading

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
    BootstrapDriftError,
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

            # 6. Ingestion request: NULL request_hash on expense fails chk_ingestion_requests_hash
            with self.assertRaises(errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ingestion_requests (
                            id, household_id, user_id, actor_scope, idempotency_key,
                            request_kind, operation, request_hash, status
                        ) VALUES (
                            gen_random_uuid(), %s, %s, 'user:test', 'idemp-key-null-hash',
                            'expense', 'POST /expenses', NULL, 'processing'
                        );
                        """,
                        (str(hh_id), str(user_id)),
                    )
            conn.rollback()

            # 7. Ingestion request: NULL request_hash on command cancel succeeds
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingestion_requests (
                        id, household_id, user_id, actor_scope, idempotency_key,
                        request_kind, operation, request_hash, status
                    ) VALUES (
                        gen_random_uuid(), %s, %s, 'user:test', 'idemp-key-cancel-cmd',
                        'command', 'cancel', NULL, 'processing'
                    );
                    """,
                    (str(hh_id), str(user_id)),
                )
            conn.commit()

            # 8. Ingestion request: valid 64-char SHA256 hex hash on expense succeeds
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingestion_requests (
                        id, household_id, user_id, actor_scope, idempotency_key,
                        request_kind, operation, request_hash, status
                    ) VALUES (
                        gen_random_uuid(), %s, %s, 'user:test', 'idemp-key-sha-hash',
                        'expense', 'POST /expenses', %s, 'processing'
                    );
                    """,
                    (str(hh_id), str(user_id), 'e' * 64),
                )
            conn.commit()

        finally:
            conn.close()

    def test_composite_foreign_keys_prevent_cross_household_references(self):
        """Validates that composite foreign keys enforce strict household isolation across all entities in PostgreSQL."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            # Setup Household A
            hh_a = repo.create_household(conn, name="Household A")
            hh_a_id = uuid.UUID(str(hh_a["id"]))
            user_a = repo.create_user(conn, auth_subject="sub_a", email="a@test.local", display_name="User A")
            user_a_id = uuid.UUID(str(user_a["id"]))
            repo.add_household_member(conn, hh_a_id, user_a_id, role="owner")
            dev_a = repo.create_device(
                conn,
                household_id=hh_a_id,
                user_id=user_a_id,
                name="Device A",
                platform="ios",
                token_hash=b"a" * 32,
            )
            dev_a_id = uuid.UUID(str(dev_a["id"]))
            cat_a = repo.create_category(conn, hh_a_id, "Grocery A", "expense")
            cat_a_id = uuid.UUID(str(cat_a["id"]))
            acc_a = repo.create_account(
                conn,
                household_id=hh_a_id,
                name="Account A",
                balance_scope="main cash",
                account_type="cash",
                currency="CNY",
                owner_user_id=user_a_id,
            )
            acc_a_id = uuid.UUID(str(acc_a["id"]))
            req_a = repo.create_ingestion_request(
                conn,
                household_id=hh_a_id,
                user_id=user_a_id,
                actor_scope=f"user:{user_a_id}",
                idempotency_key="idemp-key-valid-a",
                request_kind="expense",
                operation="POST /api/v1/expenses",
            )
            req_a_id = uuid.UUID(str(req_a["id"]))

            # Setup Household B
            hh_b = repo.create_household(conn, name="Household B")
            hh_b_id = uuid.UUID(str(hh_b["id"]))
            user_b = repo.create_user(conn, auth_subject="sub_b", email="b@test.local", display_name="User B")
            user_b_id = uuid.UUID(str(user_b["id"]))
            repo.add_household_member(conn, hh_b_id, user_b_id, role="owner")
            dev_b = repo.create_device(
                conn,
                household_id=hh_b_id,
                user_id=user_b_id,
                name="Device B",
                platform="ios",
                token_hash=b"b" * 32,
            )
            dev_b_id = uuid.UUID(str(dev_b["id"]))
            conn.commit()

            # 1. Cross-household: device in Household B referencing user in Household A fails FK
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

            # 2. Cross-household: account in Household B referencing user in Household A fails FK
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

            # 3. Cross-household: ingestion_request in HH A referencing device in HH B fails fk_ingestion_requests_device
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ingestion_requests (
                            id, household_id, user_id, device_id, actor_scope,
                            idempotency_key, request_kind, operation, request_hash, status
                        ) VALUES (
                            gen_random_uuid(), %s, %s, %s, 'device:test',
                            'idemp-key-leak-device', 'expense', 'POST /expenses', 'a' * 64, 'processing'
                        );
                        """,
                        (str(hh_a_id), str(user_a_id), str(dev_b_id)),
                    )
            conn.rollback()

            # 4. Cross-household: spending_schedules in HH A referencing device in HH B fails fk_spending_schedules_device
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO spending_schedules (
                            id, household_id, created_by_user_id, created_by_device_id,
                            name, kind, amount_per_period, currency, start_month,
                            day_of_month, merchant, category_id
                        ) VALUES (
                            gen_random_uuid(), %s, %s, %s,
                            'Schedule A', 'recurring', 100, 'CNY', '2026-01-01',
                            1, 'Merchant', %s
                        );
                        """,
                        (str(hh_a_id), str(user_a_id), str(dev_b_id), str(cat_a_id)),
                    )
            conn.rollback()

            # 5. Cross-household: transactions in HH A referencing device in HH B fails fk_transactions_device
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO transactions (
                            id, household_id, transaction_type, occurred_on, date_source,
                            original_amount, original_currency, category_id, source,
                            created_by_user_id, created_by_device_id, source_request_id
                        ) VALUES (
                            gen_random_uuid(), %s, 'expense', '2026-01-01', 'manual',
                            10, 'CNY', %s, 'shortcut',
                            %s, %s, %s
                        );
                        """,
                        (str(hh_a_id), str(cat_a_id), str(user_a_id), str(dev_b_id), str(req_a_id)),
                    )
            conn.rollback()

            # 6. Cross-household: transactions in HH A referencing user_b as deleter fails fk_transactions_deleter
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO transactions (
                            id, household_id, transaction_type, occurred_on, date_source,
                            original_amount, original_currency, category_id, source,
                            created_by_user_id, source_request_id, status,
                            deleted_at, deleted_by_user_id, delete_reason
                        ) VALUES (
                            gen_random_uuid(), %s, 'expense', '2026-01-01', 'manual',
                            10, 'CNY', %s, 'dashboard_manual',
                            %s, %s, 'voided',
                            now(), %s, 'Deleted'
                        );
                        """,
                        (str(hh_a_id), str(cat_a_id), str(user_a_id), str(req_a_id), str(user_b_id)),
                    )
            conn.rollback()

            # 7. Cross-household: account_snapshots in HH A referencing device in HH B fails fk_snapshots_device
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO account_snapshots (
                            id, household_id, account_id, as_of, time_basis, balance,
                            currency, source, created_by_user_id, created_by_device_id, source_request_id
                        ) VALUES (
                            gen_random_uuid(), %s, %s, now(), 'explicit', 100,
                            'CNY', 'manual', %s, %s, %s
                        );
                        """,
                        (str(hh_a_id), str(acc_a_id), str(user_a_id), str(dev_b_id), str(req_a_id)),
                    )
            conn.rollback()

            # 8. Cross-household: account_snapshots in HH A referencing user_b as voider fails fk_snapshots_voider
            with self.assertRaises(errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO account_snapshots (
                            id, household_id, account_id, as_of, time_basis, balance,
                            currency, source, created_by_user_id, source_request_id, status,
                            voided_at, voided_by_user_id, void_reason
                        ) VALUES (
                            gen_random_uuid(), %s, %s, now(), 'explicit', 100,
                            'CNY', 'manual', %s, %s, 'voided',
                            now(), %s, 'Voided'
                        );
                        """,
                        (str(hh_a_id), str(acc_a_id), str(user_a_id), str(req_a_id), str(user_b_id)),
                    )
            conn.rollback()

            # 9. Cross-household: audit_events in HH A referencing user_b as actor fails fk_audit_events_user
            with self.assertRaises(errors.ForeignKeyViolation):
                repo.insert_audit_event(
                    conn,
                    household_id=hh_a_id,
                    actor_type="user",
                    actor_user_id=user_b_id,
                    entity_type="household",
                    entity_id=hh_a_id,
                    action="create",
                )
            conn.rollback()

            # 10. Cross-household: audit_events in HH A referencing dev_b as actor fails fk_audit_events_device
            with self.assertRaises(errors.ForeignKeyViolation):
                repo.insert_audit_event(
                    conn,
                    household_id=hh_a_id,
                    actor_type="device",
                    actor_device_id=dev_b_id,
                    entity_type="household",
                    entity_id=hh_a_id,
                    action="create",
                )
            conn.rollback()

            # Positive validation: inserting valid records in HH A using HH A's own member and device succeeds
            valid_evt = repo.insert_audit_event(
                conn,
                household_id=hh_a_id,
                actor_type="user",
                actor_user_id=user_a_id,
                actor_device_id=dev_a_id,
                entity_type="household",
                entity_id=hh_a_id,
                action="create",
                reason="Valid audit in HH A",
            )
            self.assertIsNotNone(valid_evt["id"])
            conn.commit()

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

    def test_household_finance_write_lock_two_connection_concurrency(self):
        """Validates acquire_household_finance_lock blocks concurrent connection on same household but not different household."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn_a = get_connection(self.test_schema)
        conn_b = get_connection(self.test_schema)
        try:
            hh_a = repo.create_household(conn_a, name="Lock HH A")
            hh_a_id = uuid.UUID(str(hh_a["id"]))
            hh_b = repo.create_household(conn_a, name="Lock HH B")
            hh_b_id = uuid.UUID(str(hh_b["id"]))
            conn_a.commit()

            # Step 1: Different household lock on conn_b succeeds immediately while conn_a holds hh_a
            conn_a.autocommit = False
            locked_a = repo.acquire_household_finance_lock(conn_a, hh_a_id)
            self.assertEqual(str(locked_a["id"]), str(hh_a_id))

            conn_b.autocommit = False
            locked_b = repo.acquire_household_finance_lock(conn_b, hh_b_id)
            self.assertEqual(str(locked_b["id"]), str(hh_b_id))
            conn_b.commit()

            # Step 2: Connection B attempts to acquire lock on HH A in a thread
            b_attempt_started = threading.Event()
            b_lock_acquired = threading.Event()
            b_error = []

            def worker_b():
                try:
                    b_attempt_started.set()
                    repo.acquire_household_finance_lock(conn_b, hh_a_id)
                    b_lock_acquired.set()
                except Exception as e:
                    b_error.append(e)

            t = threading.Thread(target=worker_b)
            t.start()

            b_attempt_started.wait(timeout=3.0)
            # Verify B has NOT acquired lock because A is holding it
            self.assertFalse(b_lock_acquired.wait(timeout=0.3), "Connection B must be blocked waiting for lock on HH A")

            # Connection A commits, releasing the lock on HH A
            conn_a.commit()

            # Connection B now unblocks and acquires lock
            self.assertTrue(b_lock_acquired.wait(timeout=3.0), "Connection B must acquire lock on HH A after Connection A commits")
            conn_b.commit()
            t.join(timeout=2.0)
            self.assertEqual(len(b_error), 0, f"Worker B encountered error: {b_error}")
        finally:
            conn_a.close()
            conn_b.close()

    def test_least_privilege_database_roles(self):
        """Validates least-privilege separation: vibeledger_runtime has DML, no DDL, immutable audit, and no migrations write."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        # Execute setup_roles_simplified.sql with test schema
        roles_sql_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "scripts",
            "setup_roles_simplified.sql",
        )
        with open(roles_sql_path, "r", encoding="utf-8") as f:
            roles_sql = f.read().replace("__DB_SCHEMA__", self.test_schema)

        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(roles_sql)
            conn.commit()

            # Test runtime permissions using SET ROLE vibeledger_runtime
            with conn.cursor() as cur:
                cur.execute("SET ROLE vibeledger_runtime;")

                # 1. DML: SELECT and INSERT on households succeed
                cur.execute("SELECT COUNT(*) FROM households;")
                hh_id = uuid.uuid4()
                cur.execute(
                    """
                    INSERT INTO households (id, name, started_on)
                    VALUES (%s, 'Runtime HH', '2026-01-01');
                    """,
                    (str(hh_id),),
                )
            # Commit fixture before entering expected-failure transactions
            conn.commit()

            with conn.cursor() as cur:
                # 2. Schema DDL: CREATE TABLE fails with InsufficientPrivilege
                cur.execute("SET ROLE vibeledger_runtime;")
                with self.assertRaises(errors.InsufficientPrivilege):
                    cur.execute("CREATE TABLE evil_table (id INT);")
            conn.rollback()

            with conn.cursor() as cur:
                # 3. audit_events: INSERT succeeds
                cur.execute("SET ROLE vibeledger_runtime;")
                cur.execute(
                    """
                    INSERT INTO audit_events (household_id, actor_type, entity_type, entity_id, action, reason)
                    VALUES (%s, 'system', 'household', %s, 'create', 'test audit');
                    """,
                    (str(hh_id), str(hh_id)),
                )
            conn.commit()

            with conn.cursor() as cur:
                # audit_events: UPDATE fails with InsufficientPrivilege / RaiseException
                cur.execute("SET ROLE vibeledger_runtime;")
                with self.assertRaises((errors.InsufficientPrivilege, errors.RaiseException)):
                    cur.execute("UPDATE audit_events SET reason = 'tampered' WHERE household_id = %s;", (str(hh_id),))
            conn.rollback()

            with conn.cursor() as cur:
                # audit_events: DELETE fails with InsufficientPrivilege / RaiseException
                cur.execute("SET ROLE vibeledger_runtime;")
                with self.assertRaises((errors.InsufficientPrivilege, errors.RaiseException)):
                    cur.execute("DELETE FROM audit_events WHERE household_id = %s;", (str(hh_id),))
            conn.rollback()

            with conn.cursor() as cur:
                # 4. schema_migrations: SELECT succeeds
                cur.execute("SET ROLE vibeledger_runtime;")
                cur.execute("SELECT COUNT(*) FROM schema_migrations;")

                # schema_migrations: INSERT fails with InsufficientPrivilege
                with self.assertRaises(errors.InsufficientPrivilege):
                    cur.execute("INSERT INTO schema_migrations VALUES ('bad.sql', 'hash', now());")
            conn.rollback()

            with conn.cursor() as cur:
                cur.execute("RESET ROLE;")
            conn.commit()
        finally:
            conn.close()

    def test_bootstrap_drift_detection_in_real_postgres(self):
        """Validates that configuration drift triggers BootstrapDriftError in real PostgreSQL."""
        runner.run_migrations(schema=self.test_schema, lineage=LINEAGE_SIMPLIFIED)

        conn = get_connection(self.test_schema)
        try:
            with transaction(conn):
                bootstrap_simplified_environment(conn, household_name="Drift HH")

            # Case 1: Household currency drift
            with conn.cursor() as cur:
                cur.execute("UPDATE households SET reporting_currency = 'USD' WHERE name = 'Drift HH';")
            conn.commit()

            with self.assertRaises(BootstrapDriftError):
                with transaction(conn):
                    bootstrap_simplified_environment(conn, household_name="Drift HH", reporting_currency="CNY")
            conn.rollback()

            # Reset currency
            with conn.cursor() as cur:
                cur.execute("UPDATE households SET reporting_currency = 'CNY' WHERE name = 'Drift HH';")
            conn.commit()

            # Case 2: Category status drift
            with conn.cursor() as cur:
                cur.execute("UPDATE categories SET status = 'inactive' WHERE name = 'Grocery' AND category_type = 'expense';")
            conn.commit()

            with self.assertRaises(BootstrapDriftError):
                with transaction(conn):
                    bootstrap_simplified_environment(conn, household_name="Drift HH")
            conn.rollback()

            # Reset category
            with conn.cursor() as cur:
                cur.execute("UPDATE categories SET status = 'active' WHERE name = 'Grocery' AND category_type = 'expense';")
            conn.commit()

            # Case 3: Account balance_scope drift
            with conn.cursor() as cur:
                cur.execute("UPDATE accounts SET balance_scope = 'tampered' WHERE name = 'Cash Wallet';")
            conn.commit()

            with self.assertRaises(BootstrapDriftError):
                with transaction(conn):
                    bootstrap_simplified_environment(conn, household_name="Drift HH")
            conn.rollback()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
