# PostgreSQL Least-Privilege Role Model

Status: **Astra-Simplified Target Architecture (S1)**.
Reference: [CONTRACTS.md](CONTRACTS.md) Section 2.

## 1. Role Division

| Role Name | Scope | Privileges | Prohibited Actions |
|---|---|---|---|
| `vibeledger_operator` | Migrations, initial bootstrap, operator maintenance | `USAGE, CREATE` on target schema; `ALL PRIVILEGES` on tables/sequences | Running ordinary application web requests |
| `vibeledger_runtime` | Web API application server (`app/main.py`) | `USAGE` on target schema; `SELECT, INSERT, UPDATE, DELETE` on application tables; `SELECT, INSERT` only on `audit_events`; `SELECT` only on `schema_migrations` | Schema DDL (`CREATE`, `ALTER`, `DROP`); `UPDATE` or `DELETE` on `audit_events`; modifying `schema_migrations` |
| `anon` / `authenticated` | Supabase Data API clients | None (`REVOKE ALL`) | Any direct connection or query against private finance schema tables |

## 2. Invariant Safeguards

1. **No Runtime Schema DDL**:
   The runtime role cannot execute `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`, or `CREATE TRIGGER`. Startup of the FastAPI service never runs DDL.
2. **Audit Immutability**:
   In addition to the database trigger `trg_audit_events_immutable` (which raises an exception on any `UPDATE` or `DELETE`), the runtime role is granted `SELECT` and `INSERT` privileges only. `UPDATE` and `DELETE` privileges are explicitly revoked.
3. **No Direct Supabase Data API Exposure**:
   Financial tables are kept in the private target schema (`vibeledger_target` or `vibeledger_staging`). Permissions for `PUBLIC`, `anon`, and `authenticated` roles are revoked, ensuring all financial traffic must pass through the authenticated FastAPI application layer.
4. **Schema Migrations Protection**:
   `schema_migrations` is readable by runtime (for `/ready` lineage verification) but only writable by the operator role running `migrations/runner.py`.
