import os
import re
import psycopg2
from psycopg2 import sql
from app.config import get_settings, validate_safety
from app.db import get_connection, transaction

MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))

def get_migration_files():
    """
    Returns a sorted list of migration SQL files in the migrations directory.
    """
    files = []
    for f in os.listdir(MIGRATIONS_DIR):
        if f.endswith(".sql") and re.match(r"^\d{4}_.*\.sql$", f):
            files.append(f)
    return sorted(files)

def run_migrations(schema: str = None):
    """
    Runs all pending target database migrations within the specified schema.
    """
    validate_safety()
    settings = get_settings()
    
    if schema is None:
        schema = settings.DB_SCHEMA
        
    print(f"LOG: Starting database migrations for schema: {schema}")
    
    # 1. Establish connection and initialize schema if needed
    conn = get_connection(schema)
    try:
        # Create schema if it doesn't exist
        with conn.cursor() as cur:
            quoted_schema = sql.Identifier(schema)
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=quoted_schema))
        conn.commit()
        
        # 2. Check/create extensions at database level (in public schema)
        with conn.cursor() as cur:
            cur.execute("SELECT extname FROM pg_extension WHERE extname IN ('pgcrypto', 'pg_trgm', 'citext');")
            installed = {row[0] for row in cur.fetchall()}
            
            required = {'pgcrypto', 'pg_trgm', 'citext'}
            missing = required - installed
            
            if missing:
                print(f"INFO: Missing extensions at database level: {missing}. Attempting to install...")
                for ext in missing:
                    try:
                        # Force extensions to be created in public schema so they are persistent and shared
                        cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext} SCHEMA public;")
                        print(f"SUCCESS: Successfully installed extension '{ext}' in public schema")
                    except Exception as e:
                        conn.rollback()
                        raise RuntimeError(
                            f"Required database extension '{ext}' is missing and could not be installed. "
                            f"Ensure the database user has superuser/create privilege or the extensions are "
                            f"installed pre-emptively. Error: {e}"
                        )
                conn.commit()
                
        # 3. Create schema_migrations tracking table inside the scoped target schema
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
        conn.commit()
        
        # 4. Fetch already applied migrations
        with conn.cursor() as cur:
            cur.execute("SELECT migration_name FROM schema_migrations;")
            applied = {row[0] for row in cur.fetchall()}
            
        # 5. Apply missing migrations sequentially
        migration_files = get_migration_files()
        for filename in migration_files:
            if filename in applied:
                print(f"SKIP: Migration '{filename}' is already applied. Skipping.")
                continue
                
            filepath = os.path.join(MIGRATIONS_DIR, filename)
            print(f"RUN: Applying migration '{filename}'...")
            with open(filepath, "r", encoding="utf-8") as f:
                sql_content = f.read()
                
            try:
                with transaction(conn):
                    with conn.cursor() as cur:
                        cur.execute(sql_content)
                        cur.execute(
                            "INSERT INTO schema_migrations (migration_name) VALUES (%s);",
                            (filename,)
                        )
                print(f"SUCCESS: Successfully applied migration '{filename}'")
            except Exception as e:
                print(f"ERROR: Error applying migration '{filename}': {e}")
                raise
                
    finally:
        conn.close()

if __name__ == "__main__":
    # Allow running migration runner from command line for development
    try:
        run_migrations()
    except Exception as ex:
        print(f"ERROR: Migration failed: {ex}")
        exit(1)
