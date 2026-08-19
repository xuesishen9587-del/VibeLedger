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
    
    conn = psycopg2.connect(db_url)
    
    # Configure connection schema scoping safely using Identifier to prevent SQL injection
    with conn.cursor() as cur:
        quoted_schema = sql.Identifier(schema)
        query = sql.SQL("SET search_path = {schema}").format(schema=quoted_schema)
        cur.execute(query)
        
    return conn

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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if should_close:
            conn.close()
