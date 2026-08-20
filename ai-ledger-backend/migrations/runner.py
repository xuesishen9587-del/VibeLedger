import os
import re
import hashlib
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
    schema-qualified CREATE EXTENSION ... SCHEMA "extensions".
    Falls back to 'public' if 'extensions' creation is not permitted, but NEVER installs
    into a disposable target/test DB_SCHEMA.
    Returns mapping from extension name to its discovered/installed schema name.
    """
    with conn.cursor() as cur:
        # 1. Discover already installed extensions and their namespaces
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

        # 2. Attempt installation of missing extensions into shared schema 'extensions'
        if missing:
            print(f"INFO: Missing extensions at database level: {missing}. Attempting installation into shared schema 'extensions'...")
            
            # Determine shared extension schema (prefer 'extensions', fallback to 'public')
            target_ext_schema = "extensions"
            try:
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=sql.Identifier(target_ext_schema)))
                conn.commit()
            except Exception as schema_err:
                conn.rollback()
                print(f"WARNING: Could not create/verify 'extensions' schema ({schema_err}). Trying 'public'...")
                target_ext_schema = "public"

            for ext in missing:
                try:
                    cur.execute(
                        sql.SQL("CREATE EXTENSION IF NOT EXISTS {ext} SCHEMA {schema};").format(
                            ext=sql.Identifier(ext),
                            schema=sql.Identifier(target_ext_schema)
                        )
                    )
                    conn.commit()
                    print(f"SUCCESS: Successfully created extension '{ext}' in schema '{target_ext_schema}'")
                except Exception as e:
                    conn.rollback()
                    raise ExtensionBootstrapError(
                        f"Required database extension '{ext}' is missing and could not be installed into shared schema '{target_ext_schema}'. "
                        f"Ensure database superuser/admin permissions allow extension creation or pre-install it. "
                        f"Error: {e}"
                    )

            # Re-discover after installation
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
    """
    validate_safety()
    settings = get_settings()
    
    if schema is None:
        schema = settings.DB_SCHEMA
        
    validate_schema(schema)
    print(f"LOG: Starting database migrations for schema: {schema}")
    
    conn = get_connection(schema)
    try:
        # 1. Ensure target schema exists
        with conn.cursor() as cur:
            quoted_schema = sql.Identifier(schema)
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=quoted_schema))
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
                
    finally:
        conn.close()

if __name__ == "__main__":
    try:
        run_migrations()
    except Exception as ex:
        print(f"ERROR: Migration failed: {ex}")
        exit(1)
