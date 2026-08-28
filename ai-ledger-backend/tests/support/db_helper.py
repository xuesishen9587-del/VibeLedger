import atexit
import os
import threading
import uuid
import psycopg2
from psycopg2 import sql
import unittest

os.environ["ENVIRONMENT"] = "test"
if "DB_SCHEMA" not in os.environ:
    os.environ["DB_SCHEMA"] = "vibeledger_test_runner"

from app import config
if config.settings is not None and config.settings.ENVIRONMENT != "test":
    try:
        config.settings = config.Settings()
    except Exception:
        pass

from app.db import get_connection
from migrations import runner

ALL_BUSINESS_TABLES = [
    "audit_events",
    "reconciliation_candidates",
    "statement_lines",
    "reconciliation_batches",
    "installment_periods",
    "installment_plans",
    "investment_pnl_periods",
    "credit_card_snapshots",
    "account_snapshots",
    "transaction_links",
    "transactions",
    "ingestion_requests",
    "account_aliases",
    "account_state",
    "accounts",
    "devices",
    "household_members",
    "users",
    "categories",
    "households",
]

_SHARED_SCHEMA_ENV = "VIBELEDGER_TEST_SHARED_SCHEMA"
_shared_schema_lock = threading.RLock()
_shared_schema_name: str | None = None
_truncate_statements: dict[str, sql.Composed] = {}


def _shared_schema_enabled() -> bool:
    return os.environ.get(_SHARED_SCHEMA_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _create_isolated_test_schema() -> str:
    schema_name = f"vibeledger_test_{uuid.uuid4().hex[:12]}"
    config.validate_test_schema(schema_name)
    runner.run_migrations(schema_name)
    return schema_name

def create_test_schema() -> str:
    """
    Creates a new isolated test schema and runs migrations 0001-0009 ONCE inside it.
    Strictly enforces testing safety assertions.
    """
    if not config.is_safe_for_testing():
        raise PermissionError("Destructive test operations are only allowed when ENVIRONMENT='test'.")
    
    if not _shared_schema_enabled():
        return _create_isolated_test_schema()

    global _shared_schema_name
    with _shared_schema_lock:
        if _shared_schema_name is None:
            _shared_schema_name = _create_isolated_test_schema()
        return _shared_schema_name

def drop_test_schema(schema_name: str) -> None:
    """
    Drops a test schema with CASCADE.
    Strictly validates that schema_name is an authorized temporary test schema.
    """
    if not config.is_safe_for_testing():
        raise PermissionError("Destructive test operations are only allowed when ENVIRONMENT='test'.")
    config.validate_test_schema(schema_name)
    
    settings = config.get_settings()
    conn = get_connection(settings.DB_SCHEMA)
    try:
        with conn.cursor() as cur:
            quoted = sql.Identifier(schema_name)
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {schema} CASCADE;").format(schema=quoted))
        conn.commit()
    except Exception as e:
        print(f"Warning: failed to drop test schema {schema_name}: {e}")
    finally:
        conn.close()
        with _shared_schema_lock:
            _truncate_statements.pop(schema_name, None)


def cleanup_shared_test_schema() -> None:
    """Drop the opt-in process-wide test schema, if one was created."""
    global _shared_schema_name
    with _shared_schema_lock:
        schema_name = _shared_schema_name
        _shared_schema_name = None
    if schema_name:
        drop_test_schema(schema_name)

def truncate_business_tables(conn, schema_name: str) -> None:
    """
    Truncates all business tables in the validated test schema in a single statement.
    Preserves schema_migrations.
    """
    if not config.is_safe_for_testing():
        raise PermissionError("Destructive test operations are only allowed when ENVIRONMENT='test'.")
    config.validate_test_schema(schema_name)

    # Clean transaction state if aborted/dirty
    if conn.status != psycopg2.extensions.STATUS_READY:
        conn.rollback()

    with conn.cursor() as cur:
        with _shared_schema_lock:
            truncate_stmt = _truncate_statements.get(schema_name)
        if truncate_stmt is None:
            # Migrations are immutable for a schema during an integration run,
            # so the safely quoted table list only needs to be discovered once.
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type = 'BASE TABLE'
                  AND table_name != 'schema_migrations';
                """,
                (schema_name,)
            )
            tables = [row[0] for row in cur.fetchall()]
            if tables:
                quoted_tables = [sql.SQL("{schema}.{tbl}").format(
                    schema=sql.Identifier(schema_name),
                    tbl=sql.Identifier(t)
                ) for t in tables]
                truncate_stmt = sql.SQL("TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE;").format(
                    tables=sql.SQL(", ").join(quoted_tables)
                )
                with _shared_schema_lock:
                    _truncate_statements[schema_name] = truncate_stmt
        if truncate_stmt is not None:
            cur.execute(truncate_stmt)
    conn.commit()


class BaseDbTestCase(unittest.TestCase):
    """
    Base test case for DB integration tests:
    - Runs migrations ONCE per test class in setUpClass
    - Truncates business tables before each test in setUp
    - Calls seed_test_data() hook for fresh fixtures
    - Drops test schema in tearDownClass
    """
    test_schema: str = ""
    conn = None

    @classmethod
    def setUpClass(cls):
        os.environ["ENVIRONMENT"] = "test"
        if not config.is_safe_for_testing():
            if config.settings is not None:
                config.settings = config.Settings()
        if not config.is_safe_for_testing():
            raise unittest.SkipTest("Skipping DB integration test. ENVIRONMENT must be 'test'.")
        
        cls.test_schema = create_test_schema()
        cls.conn = get_connection(cls.test_schema)
        cls.cls_setup()

    @classmethod
    def cls_setup(cls):
        """Hook for class-level setup after schema creation."""
        pass

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "conn") and cls.conn and not cls.conn.closed:
            cls.conn.close()
        if (
            hasattr(cls, "test_schema")
            and cls.test_schema
            and not _shared_schema_enabled()
        ):
            drop_test_schema(cls.test_schema)

    def setUp(self):
        if not config.is_safe_for_testing():
            self.skipTest("Skipping DB integration test. ENVIRONMENT must be 'test'.")
        
        if self.conn.status != psycopg2.extensions.STATUS_READY:
            self.conn.rollback()
        
        # Ensure search_path is set to test_schema
        with self.conn.cursor() as cur:
            quoted = sql.Identifier(self.test_schema)
            cur.execute(sql.SQL("SET search_path = {schema};").format(schema=quoted))
        self.conn.commit()

        truncate_business_tables(self.conn, self.test_schema)
        self.seed_test_data()

    def seed_test_data(self):
        """Hook for test-specific deterministic fixtures."""
        pass


atexit.register(cleanup_shared_test_schema)
