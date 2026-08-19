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
            settings = config.get_settings()
            # Connect using the main target schema which is allowed
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
        
        # Verify schema table creation
        conn = get_connection(self.test_schema)
        try:
            with conn.cursor() as cur:
                # Check if schema_migrations exists and has 9 entries
                cur.execute("SELECT migration_name FROM schema_migrations ORDER BY migration_name;")
                rows = cur.fetchall()
                migration_names = [row[0] for row in rows]
                self.assertEqual(len(migration_names), 9)
                self.assertTrue(migration_names[0].startswith("0001_"))
                self.assertTrue(migration_names[8].startswith("0009_"))
                
                # Check that a few main tables are present
                cur.execute("""
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = %s;
                """, (self.test_schema,))
                tables = {row[0] for row in cur.fetchall()}
                self.assertIn("households", tables)
                self.assertIn("users", tables)
                self.assertIn("accounts", tables)
                self.assertIn("transactions", tables)
                self.assertIn("audit_events", tables)
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
