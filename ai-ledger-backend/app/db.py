import psycopg2
import psycopg2.extras
from psycopg2 import sql
from contextlib import contextmanager
from app.config import get_settings, validate_schema

# Register UUID adapter for psycopg2 globally
psycopg2.extras.register_uuid()

def get_connection(schema: str = None) -> psycopg2.extensions.connection:
    """
    Establishes a PostgreSQL database connection and strictly scopes its search_path
    to the configured target schema (without public schema fallback).
    """
    settings = get_settings()
    db_url = settings.DATABASE_URL
    
    if schema is None:
        schema = settings.DB_SCHEMA
        
    validate_schema(schema)
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = psycopg2.connect(
                db_url,
                connect_timeout=10,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5
            )
            # Configure connection schema scoping safely using Identifier to prevent SQL injection
            with conn.cursor() as cur:
                quoted_schema = sql.Identifier(schema)
                query = sql.SQL("SET search_path = {schema}").format(schema=quoted_schema)
                cur.execute(query)
            return conn
        except psycopg2.OperationalError:
            if attempt == max_retries - 1:
                raise
            import time
            time.sleep(0.5)

@contextmanager
def get_db_connection(schema: str = None):
    """
    Context manager that yields a connection and ensures it is closed afterwards.
    """
    conn = get_connection(schema)
    try:
        yield conn
    finally:
        conn.close()

@contextmanager
def transaction(conn=None, schema: str = None):
    """
    Context manager that wraps a block in a database transaction.
    If an existing connection is provided, it uses it and does not close it.
    If no connection is provided, it creates a new one, scopes it, manages
    the commit/rollback lifecycle, and closes it.
    """
    should_close = False
    if conn is None:
        conn = get_connection(schema)
        should_close = True
        
    try:
        yield conn
        if not conn.closed:
            conn.commit()
    except Exception:
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if should_close and not conn.closed:
            conn.close()
