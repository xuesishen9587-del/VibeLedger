-- VibeLedger Migration: 0001_extensions
-- Authority: docs/architecture/PHYSICAL_SCHEMA.md
-- PostgreSQL extensions (pgcrypto, pg_trgm, citext) are database-level prerequisites
-- verified and bootstrapped dynamically by the migration engine prior to execution.
-- This migration step formally marks the extension prerequisites as fulfilled in migration history.

SELECT 1;
