-- OPERATOR ONLY. Read S6_PRODUCTION_CUTOVER.md gates G0/G1 first.
-- Not called by application startup, migrations.runner, or hosted CI.
\set ON_ERROR_STOP on
SELECT :'authorization' = 'CREATE_NEW_VIBELEDGER_PROD_V1'
   AND current_database() = :'expected_database'
   AND session_user = :'expected_operator' AS permitted \gset
\if :permitted
\else
  \echo 'STOP: explicit creation authorization/database/operator mismatch'
  DO $$ BEGIN RAISE EXCEPTION 'S6 operator precondition failed; no changes applied'; END $$;
\endif
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('vibeledger:s6:prod:v1'));
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='vibeledger_prod_v1')
     OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('vibeledger_prod_owner','vibeledger_prod_runtime')) THEN
    RAISE EXCEPTION 'Target schema/role already exists: inspect; never overwrite or reset it';
  END IF;
  IF (SELECT count(*) FROM pg_extension WHERE extname IN ('pgcrypto','pg_trgm','citext')) != 3 THEN
    RAISE EXCEPTION 'Operator must review/install required extensions before this script';
  END IF;
END $$;
CREATE ROLE vibeledger_prod_owner NOLOGIN;
CREATE ROLE vibeledger_prod_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT vibeledger_prod_owner TO :"expected_operator";
CREATE SCHEMA vibeledger_prod_v1 AUTHORIZATION vibeledger_prod_owner;
SET LOCAL ROLE vibeledger_prod_owner;
SET LOCAL search_path TO vibeledger_prod_v1, pg_catalog;
CREATE TABLE schema_migrations (
  migration_name TEXT PRIMARY KEY,
  checksum_sha256 TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Operator must first check this input file's byte SHA256 against the accepted image.
\ir ../../../ai-ledger-backend/migrations/simplified/0001_simplified.sql
INSERT INTO schema_migrations(migration_name,checksum_sha256) VALUES
 ('0001_simplified.sql','bf8cc2ea6c46dd9ce66389784e499c1bbd7ff756ef532d1d9b23aa83d186651c');
REVOKE ALL ON SCHEMA vibeledger_prod_v1 FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA vibeledger_prod_v1 FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA vibeledger_prod_v1 FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA vibeledger_prod_v1 FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA vibeledger_prod_v1 REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
GRANT USAGE ON SCHEMA vibeledger_prod_v1 TO vibeledger_prod_runtime;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA vibeledger_prod_v1 TO vibeledger_prod_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vibeledger_prod_v1 TO vibeledger_prod_runtime;
REVOKE INSERT, UPDATE ON schema_migrations FROM vibeledger_prod_runtime;
REVOKE UPDATE ON audit_events FROM vibeledger_prod_runtime;
-- No DELETE, TRUNCATE, schema CREATE, ownership, role membership or DDL for runtime.
COMMIT;
\echo 'Fresh schema created; runtime remains NOLOGIN until separately provisioned'
