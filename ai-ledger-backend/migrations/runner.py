import os
import re
import hashlib
import time
from typing import Dict, List, Optional
import psycopg2
from psycopg2 import sql
from app.config import get_settings, validate_safety, validate_schema
from app.db import get_connection, transaction

MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))
REQUIRED_EXTENSIONS = {"pgcrypto", "pg_trgm", "citext"}

class MigrationChecksumMismatch(Exception):
    """Raised when an already applied migration's content does not match its recorded checksum."""
    pass

class ExtensionBootstrapError(Exception):
    """Raised when a required PostgreSQL extension is missing and cannot be installed."""
    pass

def get_migration_files() -> List[str]:
    """
    Returns a sorted list of migration SQL files in the migrations directory.
    """
    files = []
    for f in os.listdir(MIGRATIONS_DIR):
        if f.endswith(".sql") and re.match(r"^\d{4}_.*\.sql$", f):
            files.append(f)
    return sorted(files)

def ensure_extensions(conn) -> Dict[str, str]:
    """
    Queries PostgreSQL catalogs to discover installed extension namespaces.
    If any required extension is missing, attempts safe installation into the dedicated
    shared 'extensions' schema (creating 'extensions' schema if needed) using
    schema-qualified CREATE EXTENSION ... SCHEMA "extensions", or explicit fallback
    to SCHEMA "public".
    NEVER runs schema-less CREATE EXTENSION or installs into disposable target/test DB_SCHEMA.
    Raises ExtensionBootstrapError if any required extension remains uninstalled.
    Returns mapping from extension name to its discovered/installed schema name.
    """
    with conn.cursor() as cur:
        # Check currently installed extensions and their namespaces
        cur.execute(
            """
            SELECT e.extname, n.nspname 
            FROM pg_extension e 
            JOIN pg_namespace n ON e.extnamespace = n.oid 
            WHERE e.extname IN ('pgcrypto', 'pg_trgm', 'citext');
            """
        )
        installed = {row[0]: row[1] for row in cur.fetchall()}
        missing = REQUIRED_EXTENSIONS - set(installed.keys())

        if missing:
            # First try creating shared 'extensions' schema
            try:
                with transaction(conn):
                    with conn.cursor() as cur_ext:
                        cur_ext.execute('CREATE SCHEMA IF NOT EXISTS "extensions";')
                preferred_schema = "extensions"
            except Exception:
                preferred_schema = "public"

            for ext in sorted(missing):
                installed_ok = False
                # Try preferred schema ("extensions" or "public")
                try:
                    with transaction(conn):
                        with conn.cursor() as cur_ext:
                            cur_ext.execute(
                                sql.SQL('CREATE EXTENSION IF NOT EXISTS {ext} SCHEMA {schema};').format(
                                    ext=sql.Identifier(ext),
                                    schema=sql.Identifier(preferred_schema)
                                )
                            )
                    installed_ok = True
                except Exception as ex:
                    if preferred_schema == "extensions":
                        # Explicit fallback ONLY to explicit 'public' schema
                        try:
                            with transaction(conn):
                                with conn.cursor() as cur_ext:
                                    cur_ext.execute(
                                        sql.SQL('CREATE EXTENSION IF NOT EXISTS {ext} SCHEMA "public";').format(
                                            ext=sql.Identifier(ext)
                                        )
                                    )
                            installed_ok = True
                        except Exception as pub_ex:
                            print(f"WARN: Could not install extension '{ext}' in 'public' schema: {pub_ex}")
                    else:
                        print(f"WARN: Could not install extension '{ext}' in '{preferred_schema}' schema: {ex}")

            # Re-check after installation attempts
            cur.execute(
                """
                SELECT e.extname, n.nspname 
                FROM pg_extension e 
                JOIN pg_namespace n ON e.extnamespace = n.oid 
                WHERE e.extname IN ('pgcrypto', 'pg_trgm', 'citext');
                """
            )
            installed = {row[0]: row[1] for row in cur.fetchall()}
            missing = REQUIRED_EXTENSIONS - set(installed.keys())
            if missing:
                raise ExtensionBootstrapError(
                    f"Required database extensions {missing} are still missing after installation attempt."
                )

        return installed

def run_migrations(schema: Optional[str] = None) -> None:
    """
    Runs all pending target database migrations sequentially within the specified schema.
    Enforces migration checksum validation and extension schema portability.
    Includes transient connection dropout retries for remote pooler stability.
    """
    validate_safety()
    settings = get_settings()
    
    if schema is None:
        schema = settings.DB_SCHEMA
        
    validate_schema(schema)
    print(f"LOG: Starting database migrations for schema: {schema}")

    for attempt in range(1, 4):
        conn = None
        try:
            conn = get_connection(schema)
            # 1. Ensure target schema exists
            with conn.cursor() as cur:
                quoted_schema = sql.Identifier(schema)
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=quoted_schema))
                cur.execute(sql.SQL("SET search_path = {schema}").format(schema=quoted_schema))
            conn.commit()
            
            # 2. Discover / verify extensions and their namespaces at DB level
            ext_schemas = ensure_extensions(conn)
            
            # 3. Create or upgrade schema_migrations tracking table with checksum_sha256
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        migration_name TEXT PRIMARY KEY,
                        checksum_sha256 TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                """)
            conn.commit()
            
            # 4. Fetch applied migrations and checksums
            with conn.cursor() as cur:
                cur.execute("SELECT migration_name, checksum_sha256 FROM schema_migrations;")
                applied_migrations = {row[0]: row[1] for row in cur.fetchall()}
                
            # 5. Process migration files sequentially
            migration_files = get_migration_files()
            for filename in migration_files:
                filepath = os.path.join(MIGRATIONS_DIR, filename)
                with open(filepath, "rb") as f:
                    raw_bytes = f.read()
                    
                file_checksum = hashlib.sha256(raw_bytes).hexdigest()
                
                # Check for existing migration
                if filename in applied_migrations:
                    recorded_checksum = applied_migrations[filename]
                    if recorded_checksum == file_checksum:
                        print(f"SKIP: Migration '{filename}' is already applied (checksum verified).")
                        continue
                    else:
                        raise MigrationChecksumMismatch(
                            f"Drift detected! Migration '{filename}' checksum mismatch. "
                            f"Recorded: {recorded_checksum}, Current: {file_checksum}. "
                            "Previously applied migrations are immutable."
                        )
                
                # Apply unapplied migration
                print(f"RUN: Applying migration '{filename}'...")
                sql_content = raw_bytes.decode("utf-8")
                
                # Substitute extension namespaces dynamically using safely quoted schema identifiers
                citext_nsp = ext_schemas.get("citext", "public")
                trgm_nsp = ext_schemas.get("pg_trgm", "public")
                sql_content = sql_content.replace("__CITEXT_TYPE__", f'"{citext_nsp}".citext')
                sql_content = sql_content.replace("__GIN_TRGM_OPS__", f'"{trgm_nsp}".gin_trgm_ops')
                
                try:
                    with transaction(conn):
                        with conn.cursor() as cur:
                            cur.execute(sql_content)
                            cur.execute(
                                """
                                INSERT INTO schema_migrations (migration_name, checksum_sha256) 
                                VALUES (%s, %s);
                                """,
                                (filename, file_checksum)
                            )
                    print(f"SUCCESS: Successfully applied migration '{filename}'")
                except Exception as e:
                    print(f"ERROR: Error applying migration '{filename}': {e}")
                    raise
            break
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as net_err:
            if attempt < 3:
                print(f"WARN: Database connection dropped during migrations (attempt {attempt}/3): {net_err}. Retrying in 1s...")
                time.sleep(1.0)
                continue
            raise
        finally:
            if conn and not conn.closed:
                conn.close()

if __name__ == "__main__":
    try:
        run_migrations()
    except Exception as ex:
        print(f"ERROR: Migration failed: {ex}")
        exit(1)
