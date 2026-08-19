-- VibeLedger Database Extensions Bootstrap
-- Extensions are database-level resources and are created once.
-- They must never be dropped during test schema cleanup.

CREATE EXTENSION IF NOT EXISTS pgcrypto SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public;
CREATE EXTENSION IF NOT EXISTS citext SCHEMA public;
