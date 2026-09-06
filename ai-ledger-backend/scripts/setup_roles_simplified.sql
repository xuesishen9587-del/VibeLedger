-- VibeLedger Astra-Simplified Least-Privilege Database Role Configuration
-- Defines separate operator role (DDL + bootstrap) and runtime role (DML only, no DDL, immutable audit).
-- Restricts Supabase Data API (anon / authenticated) from direct financial table access.

-- Parameterized schema placeholder: __DB_SCHEMA__

-- 1. Create Roles if they do not exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'vibeledger_operator') THEN
        CREATE ROLE vibeledger_operator WITH LOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'vibeledger_runtime') THEN
        CREATE ROLE vibeledger_runtime WITH LOGIN NOINHERIT;
    END IF;
END
$$;

-- 2. Schema Permissions
-- Operator can manage and create schema objects (for migrations and bootstrap DDL)
GRANT USAGE, CREATE ON SCHEMA "__DB_SCHEMA__" TO vibeledger_operator;

-- Runtime has USAGE only (DML access, strictly NO CREATE / NO DDL on schema)
GRANT USAGE ON SCHEMA "__DB_SCHEMA__" TO vibeledger_runtime;
REVOKE CREATE ON SCHEMA "__DB_SCHEMA__" FROM vibeledger_runtime;

-- 3. Table Permissions for vibeledger_operator
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA "__DB_SCHEMA__" TO vibeledger_operator;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "__DB_SCHEMA__" TO vibeledger_operator;
GRANT ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA "__DB_SCHEMA__" TO vibeledger_operator;

-- 4. Table Permissions for vibeledger_runtime (Least Privilege)
-- Application tables: SELECT, INSERT, UPDATE, DELETE
GRANT SELECT, INSERT, UPDATE, DELETE ON
    "__DB_SCHEMA__".households,
    "__DB_SCHEMA__".users,
    "__DB_SCHEMA__".household_members,
    "__DB_SCHEMA__".devices,
    "__DB_SCHEMA__".accounts,
    "__DB_SCHEMA__".account_aliases,
    "__DB_SCHEMA__".categories,
    "__DB_SCHEMA__".ingestion_requests,
    "__DB_SCHEMA__".spending_schedules,
    "__DB_SCHEMA__".schedule_occurrences,
    "__DB_SCHEMA__".transactions,
    "__DB_SCHEMA__".account_snapshots,
    "__DB_SCHEMA__".investment_period_inputs,
    "__DB_SCHEMA__".fx_quotes,
    "__DB_SCHEMA__".statement_lines
TO vibeledger_runtime;

-- audit_events: SELECT and INSERT only (UPDATE and DELETE are strictly REVOKED)
GRANT SELECT, INSERT ON "__DB_SCHEMA__".audit_events TO vibeledger_runtime;
REVOKE UPDATE, DELETE, TRUNCATE ON "__DB_SCHEMA__".audit_events FROM vibeledger_runtime;

-- schema_migrations: SELECT only for runtime readiness check (NO DML/DDL)
GRANT SELECT ON "__DB_SCHEMA__".schema_migrations TO vibeledger_runtime;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON "__DB_SCHEMA__".schema_migrations FROM vibeledger_runtime;

-- Grant sequence usage for identity columns
GRANT USAGE ON ALL SEQUENCES IN SCHEMA "__DB_SCHEMA__" TO vibeledger_runtime;

-- 5. Supabase Data API Protection
-- Ensure public, anon, and authenticated roles have ZERO permissions on the private schema
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON SCHEMA "__DB_SCHEMA__" FROM anon;
        REVOKE ALL ON ALL TABLES IN SCHEMA "__DB_SCHEMA__" FROM anon;
    END IF;
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON SCHEMA "__DB_SCHEMA__" FROM authenticated;
        REVOKE ALL ON ALL TABLES IN SCHEMA "__DB_SCHEMA__" FROM authenticated;
    END IF;
END
$$;

REVOKE ALL ON SCHEMA "__DB_SCHEMA__" FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA "__DB_SCHEMA__" FROM PUBLIC;
