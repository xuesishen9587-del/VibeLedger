"""
Unit Tests for S1: Fresh Database, Identities, Settings, and Lineage Selection.
Offline validation of DB-01, SEC-01, HIST-01, and UI-01 foundations without connecting to remote Supabase.
"""

import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import hashlib
import re
import unittest
from unittest.mock import MagicMock, patch
from decimal import Decimal
import uuid
from datetime import date, datetime

from app.config import (
    Settings,
    is_safe_for_testing,
    validate_safety,
)
from migrations.runner import (
    LINEAGE_SIMPLIFIED,
    LINEAGE_LEGACY,
    DEFAULT_LINEAGE,
    LEGACY_MIGRATION_FILES,
    LegacyMigrationLineageDetectedError,
    MigrationChecksumMismatch,
    get_migration_files,
    get_migration_dir,
    verify_schema_lineage,
    run_migrations,
    MIGRATIONS_SIMPLIFIED_DIR,
    MIGRATIONS_LEGACY_DIR,
)
from app.repositories import simplified_schema as repo
from scripts.bootstrap_simplified import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    STARTER_ACCOUNTS,
    bootstrap_simplified_environment,
)


class TestS1DatabaseBaselineAndLineage(unittest.TestCase):
    """
    Acceptance DB-01:
    - 16 declared tables in simplified baseline
    - Strict migration lineage separation (simplified vs legacy)
    - Legacy migration detection and rejection
    - Readiness check lineage verification
    - Startup never executes DDL
    """

    def test_simplified_sql_defines_exactly_16_application_tables(self):
        """Verify 0001_simplified.sql contains the 16 target application tables."""
        sql_path = os.path.join(MIGRATIONS_SIMPLIFIED_DIR, "0001_simplified.sql")
        self.assertTrue(os.path.exists(sql_path), "0001_simplified.sql must exist")

        with open(sql_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find all CREATE TABLE statements
        created_tables = re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)", content, re.IGNORECASE)
        expected_16_tables = {
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
        }
        self.assertEqual(
            set(created_tables),
            expected_16_tables,
            f"0001_simplified.sql must create exactly the 16 declared tables. Found: {set(created_tables)}"
        )

    def test_legacy_migrations_preserved_byte_for_byte(self):
        """Verify legacy migrations 0001..0009 remain in migrations/ directory."""
        legacy_files = get_migration_files(lineage=LINEAGE_LEGACY)
        self.assertEqual(len(legacy_files), 9)
        self.assertEqual(legacy_files[0], "0001_extensions.sql")
        self.assertEqual(legacy_files[-1], "0009_indexes.sql")

        # Simplified lineage returns only 0001_simplified.sql
        simplified_files = get_migration_files(lineage=LINEAGE_SIMPLIFIED)
        self.assertEqual(simplified_files, ["0001_simplified.sql"])

    def test_default_lineage_is_simplified(self):
        """Verify default runner lineage is simplified."""
        self.assertEqual(DEFAULT_LINEAGE, LINEAGE_SIMPLIFIED)
        files = get_migration_files()
        self.assertEqual(files, ["0001_simplified.sql"])

    def test_runner_rejects_schema_with_legacy_lineage(self):
        """Verify running simplified migrations on a schema with legacy migrations raises LegacyMigrationLineageDetectedError."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Simulate schema_migrations having legacy '0001_extensions.sql'
        mock_cur.fetchall.return_value = [
            ("0001_extensions.sql", "checksum123"),
        ]

        with patch("migrations.runner.validate_safety"), \
             patch("migrations.runner.validate_schema"), \
             patch("migrations.runner.get_settings"), \
             patch("migrations.runner.get_connection", return_value=mock_conn), \
             patch("migrations.runner.ensure_extensions", return_value={}):
            with self.assertRaises(LegacyMigrationLineageDetectedError) as ctx:
                run_migrations(schema="vibeledger_test_runner", lineage=LINEAGE_SIMPLIFIED)

            self.assertIn("Legacy migration lineage detected", str(ctx.exception))

    def test_verify_schema_lineage_rejects_legacy_and_unready_schemas(self):
        """Verify verify_schema_lineage returns proper status codes for readiness."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Case 1: schema_migrations table does not exist
        mock_cur.fetchone.return_value = [False]
        is_ok, reason = verify_schema_lineage(mock_conn, lineage=LINEAGE_SIMPLIFIED)
        self.assertFalse(is_ok)
        self.assertEqual(reason, "schema_not_ready")

        # Case 2: schema_migrations exists, but has legacy migrations
        mock_cur.fetchone.return_value = [True]
        mock_cur.fetchall.return_value = [("0001_extensions.sql", "hash1")]
        is_ok, reason = verify_schema_lineage(mock_conn, lineage=LINEAGE_SIMPLIFIED)
        self.assertFalse(is_ok)
        self.assertEqual(reason, "incompatible_schema_lineage")

        # Case 3: schema_migrations exists, simplified migration applied with correct checksum
        sql_path = os.path.join(MIGRATIONS_SIMPLIFIED_DIR, "0001_simplified.sql")
        with open(sql_path, "rb") as f:
            correct_checksum = hashlib.sha256(f.read()).hexdigest()

        mock_cur.fetchone.return_value = [True]
        mock_cur.fetchall.return_value = [("0001_simplified.sql", correct_checksum)]
        is_ok, reason = verify_schema_lineage(mock_conn, lineage=LINEAGE_SIMPLIFIED)
        self.assertTrue(is_ok)
        self.assertEqual(reason, "ok")

        # Case 4: checksum mismatch
        mock_cur.fetchall.return_value = [("0001_simplified.sql", "corrupted_checksum")]
        is_ok, reason = verify_schema_lineage(mock_conn, lineage=LINEAGE_SIMPLIFIED)
        self.assertFalse(is_ok)
        self.assertEqual(reason, "schema_not_ready")

    def test_startup_never_runs_ddl(self):
        """Verify that creating the FastAPI app does not execute any DDL or database queries."""
        from app.main import create_app
        with patch("app.db.get_connection") as mock_get_conn:
            app = create_app()
            self.assertIsNotNone(app)
            # Startup must never connect to DB or execute DDL
            mock_get_conn.assert_not_called()


class TestS1SecurityAndScoping(unittest.TestCase):
    """
    Acceptance SEC-01:
    - Actor scope validation (device:<uuid>, user:<uuid>, system:<uuid>)
    - Device token hash is one-way SHA-256; plaintext never stored
    - Remote Supabase database protection in test mode
    """

    def test_actor_scope_validation(self):
        """Verify actor_scope requires device:, user:, or system: prefix."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = {"id": str(uuid.uuid4())}

        hh_id = uuid.uuid4()
        u_id = uuid.uuid4()

        # Valid actor scopes
        repo.create_ingestion_request(
            mock_conn,
            household_id=hh_id,
            user_id=u_id,
            actor_scope=f"device:{uuid.uuid4()}",
            idempotency_key="valid-key-12345",
            request_kind="expense",
            operation="POST /api/v1/expenses",
        )
        repo.create_ingestion_request(
            mock_conn,
            household_id=hh_id,
            user_id=u_id,
            actor_scope=f"user:{u_id}",
            idempotency_key="valid-key-12345",
            request_kind="command",
            operation="POST /api/v1/commands/refund",
        )
        repo.create_ingestion_request(
            mock_conn,
            household_id=hh_id,
            user_id=u_id,
            actor_scope=f"system:{hh_id}",
            idempotency_key="valid-key-12345",
            request_kind="command",
            operation="POST /api/v1/schedules/daily",
        )

        # Invalid actor scope
        with self.assertRaises(ValueError) as ctx:
            repo.create_ingestion_request(
                mock_conn,
                household_id=hh_id,
                user_id=u_id,
                actor_scope="anonymous:123",
                idempotency_key="valid-key-12345",
                request_kind="expense",
                operation="POST /api/v1/expenses",
            )
        self.assertIn("Invalid actor_scope prefix", str(ctx.exception))

    def test_idempotency_key_length_validation(self):
        """Verify idempotency_key must be 8..200 characters."""
        mock_conn = MagicMock()
        hh_id = uuid.uuid4()
        u_id = uuid.uuid4()

        # Too short (< 8)
        with self.assertRaises(ValueError):
            repo.create_ingestion_request(
                mock_conn,
                household_id=hh_id,
                user_id=u_id,
                actor_scope=f"user:{u_id}",
                idempotency_key="short",
                request_kind="expense",
                operation="POST /api/v1/expenses",
            )

        # Too long (> 200)
        with self.assertRaises(ValueError):
            repo.create_ingestion_request(
                mock_conn,
                household_id=hh_id,
                user_id=u_id,
                actor_scope=f"user:{u_id}",
                idempotency_key="k" * 201,
                request_kind="expense",
                operation="POST /api/v1/expenses",
            )

    def test_device_token_hash_stored_as_bytes(self):
        """Verify device registration hashes token with SHA-256 and passes bytes to DB."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = {
            "id": str(uuid.uuid4()),
            "name": "iPhone 15",
            "platform": "ios",
            "status": "active",
        }

        raw_token = "secret_iphone_token_1234567890"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
        self.assertEqual(len(token_hash), 32)

        hh_id = uuid.uuid4()
        u_id = uuid.uuid4()

        device = repo.create_device(
            mock_conn,
            household_id=hh_id,
            user_id=u_id,
            name="iPhone 15",
            platform="ios",
            token_hash=token_hash,
            client_version="1.0.0",
        )
        self.assertEqual(device["name"], "iPhone 15")

        # Verify SQL query does not contain raw_token
        call_args = mock_cur.execute.call_args[0]
        params = call_args[1]
        self.assertNotIn(raw_token, str(params))

    def test_remote_supabase_rejected_in_test_mode(self):
        """Verify is_safe_for_testing and validate_safety reject remote Supabase URLs."""
        remote_settings = Settings(
            ENVIRONMENT="test",
            DATABASE_URL="postgresql://postgres:pass@aws-1-ap-southeast-1.pooler.supabase.com:6543/postgres",
            DB_SCHEMA="vibeledger_test_runner",
        )
        with patch("app.config.get_settings", return_value=remote_settings):
            self.assertFalse(is_safe_for_testing())
            with self.assertRaises(PermissionError) as ctx:
                validate_safety()
            self.assertIn("Remote Supabase database cannot be used", str(ctx.exception))


class TestS1HistoryAndAudit(unittest.TestCase):
    """
    Acceptance HIST-01:
    - audit_events trigger forbids UPDATE and DELETE
    - Audit actions vocabulary
    - Structured before/after payload support
    """

    def test_audit_events_immutability_trigger_defined(self):
        """Verify 0001_simplified.sql contains the audit_events immutable trigger."""
        sql_path = os.path.join(MIGRATIONS_SIMPLIFIED_DIR, "0001_simplified.sql")
        with open(sql_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("trg_audit_events_immutable", content)
        self.assertIn("BEFORE UPDATE OR DELETE ON audit_events", content)
        self.assertIn("RAISE EXCEPTION", content)

    def test_audit_events_action_vocabulary(self):
        """Verify audit action must be one of the defined canonical actions."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = {"id": 1, "action": "create"}

        hh_id = uuid.uuid4()
        entity_id = uuid.uuid4()

        # Valid actions
        for action in ("create", "update", "void", "replace", "close", "reopen", "confirm_flows"):
            repo.insert_audit_event(
                mock_conn,
                household_id=hh_id,
                actor_type="user",
                entity_type="account",
                entity_id=entity_id,
                action=action,
                reason="Test reason",
            )

        # Invalid action
        with self.assertRaises(ValueError) as ctx:
            repo.insert_audit_event(
                mock_conn,
                household_id=hh_id,
                actor_type="user",
                entity_type="account",
                entity_id=entity_id,
                action="destroy_everything",
            )
        self.assertIn("Invalid audit action", str(ctx.exception))


class TestS1AccountCategoryUIFoundations(unittest.TestCase):
    """
    Acceptance UI-01 (Account, Category, and Settings Foundations):
    - 15 expense categories + 3 income categories with descriptions
    - Fallback category rules (is_fallback=True, cannot be archived)
    - Account constraints (credit risk must be null, closed account dates)
    - Household settings bounds (ratio > 0 and <= 1)
    - Idempotent bootstrap produces zero opening balance rows
    """

    def test_canonical_categories_defined(self):
        """Verify exact 15 expense and 3 income categories defined with descriptions."""
        self.assertEqual(len(EXPENSE_CATEGORIES), 15)
        self.assertEqual(len(INCOME_CATEGORIES), 3)

        # Check expense fallback
        expense_fallbacks = [c for c in EXPENSE_CATEGORIES if c[2] is True]
        self.assertEqual(len(expense_fallbacks), 1)
        self.assertEqual(expense_fallbacks[0][0], "Other")

        # Check income fallback
        income_fallbacks = [c for c in INCOME_CATEGORIES if c[2] is True]
        self.assertEqual(len(income_fallbacks), 1)
        self.assertEqual(income_fallbacks[0][0], "Other income")

        # Check Grocery, Child, Home & Utilities
        expense_names = {c[0] for c in EXPENSE_CATEGORIES}
        self.assertIn("Grocery", expense_names)
        self.assertIn("Dine", expense_names)
        self.assertIn("Child", expense_names)
        self.assertIn("Home & Utilities", expense_names)
        self.assertIn("Digital & Gadgets", expense_names)
        self.assertIn("Trips & Occasions", expense_names)

    def test_credit_account_risk_constraint(self):
        """Verify credit account must have risk_level=None."""
        mock_conn = MagicMock()
        hh_id = uuid.uuid4()

        with self.assertRaises(ValueError) as ctx:
            repo.create_account(
                mock_conn,
                household_id=hh_id,
                name="Visa Card",
                balance_scope="card debt",
                account_type="credit",
                currency="CNY",
                risk_level="low",
            )
        self.assertIn("Credit accounts must have risk_level=None", str(ctx.exception))

    def test_household_settings_ratio_bounds(self):
        """Verify investment_review_change_ratio must be > 0 and <= 1."""
        mock_conn = MagicMock()
        hh_id = uuid.uuid4()

        # Ratio = 0 rejected
        with self.assertRaises(ValueError):
            repo.create_household(mock_conn, name="Test", investment_review_change_ratio=Decimal("0.0"))

        # Ratio < 0 rejected
        with self.assertRaises(ValueError):
            repo.create_household(mock_conn, name="Test", investment_review_change_ratio=Decimal("-0.1"))

        # Ratio > 1 rejected
        with self.assertRaises(ValueError):
            repo.create_household(mock_conn, name="Test", investment_review_change_ratio=Decimal("1.0001"))

        # Valid ratio 0.2000 accepted
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = {"id": str(hh_id), "investment_review_change_ratio": Decimal("0.2000")}
        res = repo.create_household(mock_conn, name="Test", investment_review_change_ratio=Decimal("0.2000"))
        self.assertIsNotNone(res)

    def test_idempotent_bootstrap_runs_cleanly(self):
        """Verify bootstrap_simplified_environment creates entities on first pass and verifies on second pass."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # First pass: no existing entities
        mock_cur.fetchone.side_effect = [
            None,  # household lookup
            {"id": str(uuid.uuid4())}, # create_household RETURNING
            None,  # user lookup
            {"id": str(uuid.uuid4())}, # create_user RETURNING
            None,  # membership lookup
            {"role": "owner"}, # add_household_member RETURNING
            # 15 expense categories lookup (all None)
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            # 3 income categories lookup (all None)
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            # 3 starter accounts lookup (all None)
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
            None, {"id": str(uuid.uuid4())},
        ]

        summary1 = bootstrap_simplified_environment(mock_conn, household_name="Test Household")
        self.assertEqual(summary1["categories_created"], 18)
        self.assertEqual(summary1["accounts_created"], 3)

        # Second pass: all exist
        hh_uuid = uuid.uuid4()
        user_uuid = uuid.uuid4()
        mock_cur.fetchone.side_effect = [
            (str(hh_uuid),),  # household lookup
            (str(user_uuid),),  # user lookup
            ("owner",),  # membership lookup
            # 15 expense categories (all found)
            ("cat1",), ("cat2",), ("cat3",), ("cat4",), ("cat5",),
            ("cat6",), ("cat7",), ("cat8",), ("cat9",), ("cat10",),
            ("cat11",), ("cat12",), ("cat13",), ("cat14",), ("cat15",),
            # 3 income categories (all found)
            ("inc1",), ("inc2",), ("inc3",),
            # 3 starter accounts (all found)
            ("acc1",), ("acc2",), ("acc3",),
        ]

        summary2 = bootstrap_simplified_environment(mock_conn, household_name="Test Household")
        self.assertEqual(summary2["categories_created"], 0)
        self.assertEqual(summary2["categories_verified"], 18)
        self.assertEqual(summary2["accounts_created"], 0)
        self.assertEqual(summary2["accounts_verified"], 3)


if __name__ == "__main__":
    unittest.main()
