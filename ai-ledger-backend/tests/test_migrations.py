import os
os.environ["ENVIRONMENT"] = "test"
os.environ["DB_SCHEMA"] = "vibeledger_test_runner"

import unittest
import uuid
import psycopg2
from psycopg2 import sql
from app import config
from app.db import get_connection
from migrations import runner

class TestMigrations(unittest.TestCase):
    def setUp(self):
        if not config.is_safe_for_testing():
            self.skipTest("Skipping DB integration test. ENVIRONMENT must be set to 'test'.")
            
        # Generate a unique schema name for the test run
        self.test_schema = f"vibeledger_test_{uuid.uuid4().hex[:12]}"
        
    def tearDown(self):
        if config.is_safe_for_testing() and hasattr(self, "test_schema"):
            # Enforce strict validation before dropping test schema
            config.validate_test_schema(self.test_schema)
            settings = config.get_settings()
            conn = get_connection(settings.DB_SCHEMA)
            try:
                with conn.cursor() as cur:
                    quoted_schema = sql.Identifier(self.test_schema)
                    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE").format(schema=quoted_schema))
                conn.commit()
            except Exception as e:
                print(f"Warning: failed to drop test schema {self.test_schema}: {e}")
            finally:
                conn.close()

    def test_run_migrations_success(self):
        # 1. Run migrations first time
        runner.run_migrations(self.test_schema)
        
        # Verify schema table creation and checksum recording
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT migration_name, checksum_sha256 FROM schema_migrations ORDER BY migration_name;")
                rows = cur.fetchall()
                self.assertEqual(len(rows), 9)
                self.assertTrue(rows[0][0].startswith("0001_"))
                self.assertTrue(rows[8][0].startswith("0009_"))
                for filename, checksum in rows:
                    self.assertEqual(len(checksum), 64) # SHA256 hex string length
                
                # Check that main tables are present
                cur.execute("""
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = %s;
                """, (self.test_schema,))
                tables = {row[0] for row in cur.fetchall()}
                expected_tables = {
                    "households", "users", "household_members", "devices",
                    "accounts", "account_state", "account_aliases", "categories",
                    "ingestion_requests", "transactions", "transaction_links",
                    "account_snapshots", "credit_card_snapshots", "investment_pnl_periods",
                    "installment_plans", "installment_periods",
                    "reconciliation_batches", "statement_lines", "reconciliation_candidates",
                    "audit_events", "schema_migrations"
                }
                for t in expected_tables:
                    self.assertIn(t, tables, f"Expected table '{t}' missing from schema.")
        finally:
            conn.close()

        # 2. Re-running migrations should be idempotent and no-op
        runner.run_migrations(self.test_schema)
        
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM schema_migrations;")
                self.assertEqual(cur.fetchone()[0], 9)
        finally:
            conn.close()

    def test_migration_checksum_drift_protection(self):
        # 1. Apply all migrations
        runner.run_migrations(self.test_schema)
        
        # 2. Tamper with a recorded checksum in schema_migrations
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE schema_migrations 
                    SET checksum_sha256 = 'tampered_fake_checksum_000000000000000000000000000000000000000000' 
                    WHERE migration_name = '0002_identity_accounts.sql';
                    """
                )
            conn.commit()
        finally:
            conn.close()

        # 3. Attempting to rerun migrations must detect drift and fail loudly
        with self.assertRaises(runner.MigrationChecksumMismatch) as ctx:
            runner.run_migrations(self.test_schema)
        self.assertIn("Drift detected", str(ctx.exception))
        self.assertIn("0002_identity_accounts.sql", str(ctx.exception))

    def test_extension_discovery_and_non_destruction(self):
        # 1. Extensions discovery check
        conn = get_connection(self.test_schema)
        try:
            ext_map = runner.ensure_extensions(conn)
            self.assertIn("pgcrypto", ext_map)
            self.assertIn("pg_trgm", ext_map)
            self.assertIn("citext", ext_map)
            for ext, nsp in ext_map.items():
                self.assertTrue(bool(nsp), f"Extension {ext} has empty namespace")
                self.assertNotEqual(
                    nsp, self.test_schema,
                    f"Extension {ext} must NOT reside in disposable test schema {self.test_schema}"
                )
                self.assertNotEqual(
                    nsp, "vibeledger_target",
                    f"Extension {ext} must NOT reside in target DB_SCHEMA 'vibeledger_target'"
                )
        finally:
            conn.close()

        # 2. Verify dropping a test schema does NOT remove extensions
        runner.run_migrations(self.test_schema)
        
        # Drop test schema
        settings = config.get_settings()
        conn = get_connection(settings.DB_SCHEMA)
        try:
            with conn.cursor() as cur:
                quoted = sql.Identifier(self.test_schema)
                cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE;").format(schema=quoted))
            conn.commit()
            
            # Re-discover on connection after drop
            ext_map_after = runner.ensure_extensions(conn)
            self.assertIn("pgcrypto", ext_map_after)
            self.assertIn("pg_trgm", ext_map_after)
            self.assertIn("citext", ext_map_after)
            for ext, nsp in ext_map_after.items():
                self.assertNotEqual(
                    nsp, self.test_schema,
                    f"Extension {ext} unexpectedly mapped to dropped test schema {self.test_schema}"
                )
        finally:
            conn.close()
