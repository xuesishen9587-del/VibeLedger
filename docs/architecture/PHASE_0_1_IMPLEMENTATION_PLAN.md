# Implementation Plan: VibeLedger Phase 0 & Phase 1

This plan covers the implementation of **Implementation Phase 0 (Architecture Freeze & Test Safety)** and **Implementation Phase 1 (Target Database Foundation)** for VibeLedger, strictly adhering to the frozen target architecture (`TARGET_DOMAIN_MODEL.md`, `PHYSICAL_SCHEMA.md`, `API_CONTRACT.md`, `RECONCILIATION_ENGINE.md`, `IMPLEMENTATION_PLAN.md`, `TEST_PLAN.md`).

Phase 2 financial business logic (expense mutation, cash income mutation, transfer service, fee service, refund service, opening balance service, reconciliation adjustment service, void/reprojection service, etc.) is **strictly out of scope** and will NOT be implemented in this increment.

---

## User Review Required

> [!IMPORTANT]
> **Database Isolation Policy**:
> Target migrations and tests will run strictly inside explicit, isolated PostgreSQL schemas via `search_path` (e.g. `DB_SCHEMA=vibeledger_target` for dev and `vibeledger_test_<uuid>` for tests). No legacy production tables in `public` will be modified, altered, renamed, or migrated.
> 
> **Explicit Schema Scoping (No Public Fallback)**:
> Database connection scoping executes strictly inside `DB_SCHEMA` using `SET search_path = {schema}`. The legacy `public` fallback is **strictly forbidden** in target query paths to prevent target SQL from accidentally falling back to legacy tables in the `public` schema.
> 
> **Safety Guard**: Any attempt to run tests or migrations when `ENVIRONMENT=production`, or when `DB_SCHEMA` is empty, resolves to `public`, or is unspecified, will be immediately aborted.

---

## Implementation Rules & Authority

> [!IMPORTANT]
> **Schema Authority Rule**:
> The migration descriptions in this document are high-level summaries only. **`docs/architecture/PHYSICAL_SCHEMA.md` is the absolute authority** for all table specifications: columns, types (`NUMERIC(20,6)` / `NUMERIC(24,12)`), NULLability, defaults, CHECK constraints, FK constraints, UNIQUE constraints, and index details.
> 
> Before declaring Phase 1 complete, a comprehensive **table-by-table schema parity check** must be performed against `PHYSICAL_SCHEMA.md` to ensure absolute correctness.

---

## Proposed Architecture & File Structure

```text
ai-ledger-backend/
├── app/
│   ├── __init__.py
│   ├── config.py                 # Target configuration layer (Pydantic v2 Settings) & safety guards
│   ├── db.py                     # Connection & transaction context management with schema search_path & Identifier quoting
│   ├── domain/
│   │   ├── __init__.py
│   │   └── money.py              # Decimal parsing, currency validation, quantization, FX rate validation
│   └── repositories/
│       ├── __init__.py
│       ├── accounts.py           # Household, User, Device, Account, AccountState, Aliases, Categories
│       ├── ingestion.py          # IngestionRequest persistence
│       └── audit.py              # Append-only AuditEvents persistence
├── migrations/
│   ├── __init__.py
│   ├── runner.py                 # Explicit deterministic SQL migration runner (validates safety)
│   ├── 0001_extensions.sql       # Database-level extension bootstrap logic (non-destructive)
│   ├── 0002_identity_accounts.sql# households, users, household_members, devices, accounts, account_state, aliases, categories (inline FKs)
│   ├── 0003_ingestion_batches.sql# ingestion_requests, reconciliation_batches (inline FKs)
│   ├── 0004_transactions.sql     # transactions, transaction_links (inline FKs)
│   ├── 0005_snapshots_invest.sql # account_snapshots, credit_card_snapshots, investment_pnl_periods (inline FKs)
│   ├── 0006_statement_candidates.sql # statement_lines, reconciliation_candidates (inline FKs)
│   ├── 0007_installments.sql     # installment_plans, installment_periods (inline FKs)
│   ├── 0008_audit_events.sql     # audit_events & immutable update/delete trigger
│   └── 0009_indexes.sql          # trigram GIN indexes, partial indexes, and performance indexes only
├── tests/
│   ├── __init__.py
│   ├── test_safety.py            # Phase 0 safety guard tests (production reject, public schema reject, public fallback reject)
│   ├── test_money.py             # Phase 1 domain money unit tests (exact Decimal arithmetic, currencies, quantization)
│   ├── test_migrations.py        # Schema migration determinism & isolated schema execution tests
│   └── test_schema.py            # PostgreSQL schema constraints, NOT NULLs, triggers, row locks, transaction rollback
├── .env.example                  # Safe placeholders for ENVIRONMENT, DATABASE_URL, DB_SCHEMA, GEMINI_API_KEY
```

---

## Proposed Changes

### 1. Phase 0: Configuration & Safety Foundation

#### [MODIFY] [requirements.txt](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/ai-ledger-backend/requirements.txt)
- Adds `pydantic-settings>=2.2.0` to backend dependencies.

#### [NEW] [config.py](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/ai-ledger-backend/app/config.py)
- Defines explicit Settings using Pydantic v2 `BaseSettings` (from `pydantic_settings`):
  - `ENVIRONMENT`: `development` | `test` | `production` (strictly validated, no implicit default)
  - `DATABASE_URL`: PostgreSQL connection string (strictly validated)
  - `DB_SCHEMA`: Target PostgreSQL schema (required for migrations and tests; must NOT be default or public)
  - `GEMINI_API_KEY`: Placeholder/config (optional, not used in Phase 0/1)
- Implements safety verification functions:
  - `validate_safety()`: Refuses execution if `ENVIRONMENT == "production"` during tests/migrations.
  - `validate_schema()`: Refuses execution if `DB_SCHEMA` is empty, whitespace, or `"public"`.
  - `is_safe_for_testing()`: Refuses destructive test operations if `ENVIRONMENT != "test"`.

#### [NEW] [.env.example](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/.env.example) & [ai-ledger-backend/.env.example](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/ai-ledger-backend/.env.example)
- Canonical example variables with safe placeholders only (no real credentials).

---

### 2. Database Connection & Transaction Layer

#### [NEW] [db.py](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/ai-ledger-backend/app/db.py)
- Connection provider using `psycopg2`.
- Schema scoping: every connection executes `SET search_path = {schema}` upon acquisition (strictly omitting `, public` fallback to prevent target code from querying legacy public tables).
- Safe Identifier Quoting: schema names are safely parameterized using `psycopg2.sql.Identifier(schema)` to prevent SQL injection during dynamic DDL search path statements.
- Context managers:
  - `get_connection(schema=None)`: yields connection scoped to target schema.
  - `transaction(conn=None, schema=None)`: context manager managing `BEGIN`, `COMMIT`, and `ROLLBACK` cleanly without hidden auto-commits.
- Supports explicit row locking: `SELECT ... FOR UPDATE`.

---

### 3. Domain Money Primitives

#### [NEW] [domain/money.py](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/ai-ledger-backend/app/domain/money.py)
- `parse_decimal(value)`: safely converts string/int/Decimal to `Decimal`, strictly rejecting float types to prevent precision loss.
- `quantize_money(amount: Decimal, currency: str) -> Decimal`: quantizes to currency minor units (e.g. JPY $\to$ `1`, CNY/USD/EUR/SGD $\to$ `0.01`).
- `validate_currency_code(code: str) -> str`: validates regex `^[A-Z]{3}$` and uppercase normalization.
- `validate_fx_rate(rate: Decimal) -> Decimal`: validates positive `NUMERIC(24,12)` bounds.
- `quantize_reporting(amount: Decimal) -> Decimal`: standard `NUMERIC(20,6)` scale representation.

---

### 4. Physical Database Migrations

Explicit versioned SQL migrations based on `PHYSICAL_SCHEMA.md`. All foreign keys are created inline in the migrations where both referenced tables exist:

#### [NEW] `migrations/0001_extensions.sql`
- Treated as database-level bootstrap prerequisites.
- Safely detects and creates extensions if needed and permissions allow:
  `CREATE EXTENSION IF NOT EXISTS pgcrypto;`
  `CREATE EXTENSION IF NOT EXISTS pg_trgm;`
  `CREATE EXTENSION IF NOT EXISTS citext;`
- These are never dropped or recreated during `vibeledger_test_<uuid>` cleanup.
- If unavailable and the current DB user cannot install them, execution aborts with a clear error.

#### [NEW] `migrations/0002_identity_accounts.sql`
- `households` (UUID PK, reporting_currency, ledger_start_date, status)
- `users` (UUID PK, auth_subject UNIQUE, email CITEXT UNIQUE, display_name, default_currency, status)
- `household_members` (PK household_id + user_id, role)
- `devices` (UUID PK, user_id FK -> users, device_name, platform, token_hash UNIQUE, status, revoked_at)
- `accounts` (UUID PK, household_id FK -> households, name, institution, account_type, currency, owner_user_id FK -> users, linked_cash_account_id FK -> accounts, billing_day/due_day, status)
- `account_state` (PK account_id FK -> accounts, ledger_balance NUMERIC(20,6), initialized_at, row_version)
- `account_aliases` (UUID PK, account_id FK -> accounts, alias_text, normalized_alias, status)
- `categories` (UUID PK, household_id FK -> households, name, category_type, status)

#### [NEW] `migrations/0003_ingestion_batches.sql`
- `ingestion_requests` (UUID PK, device_id FK -> devices, idempotency_key, request_kind, request_hash, status)
- `reconciliation_batches` (UUID PK, household_id FK -> households, account_id FK -> accounts, batch_type, status, currency, engine_version, counts, committed_at)

#### [NEW] `migrations/0004_transactions.sql`
- `transactions` (UUID PK, household_id FK -> households, transaction_type, occurred_on, occurred_at, posted_on, from_account_id FK -> accounts, to_account_id FK -> accounts, original_amount, original_currency, from_amount, from_currency, to_amount, to_currency, effective_fx_rate, account_leg_status, reporting_amount, reporting_currency, reporting_fx_rate, category_id FK -> categories, merchant, merchant_normalized, remarks, source, status, verification_status, confidence, source_request_id FK -> ingestion_requests, statement_batch_id FK -> reconciliation_batches)
- `transaction_links` (UUID PK, source_transaction_id FK -> transactions, target_transaction_id FK -> transactions, relation_type)

#### [NEW] `migrations/0005_snapshots_invest.sql`
- `account_snapshots` (UUID PK, household_id FK -> households, account_id FK -> accounts, as_of, balance, currency, snapshot_type, source, reconciliation_batch_id FK -> reconciliation_batches)
- `credit_card_snapshots` (UUID PK, household_id FK -> households, account_id FK -> accounts, as_of, statement_period_start/end, statement_balance, remaining_statement_due, unbilled_balance, current_outstanding, currency, source)
- `investment_pnl_periods` (UUID PK, household_id FK -> households, account_id FK -> accounts, opening_snapshot_id FK -> account_snapshots, closing_snapshot_id FK -> account_snapshots, period_start, period_end, contributions_amount, withdrawals_amount, pnl_amount, currency, status, calculation_version)

#### [NEW] `migrations/0006_statement_candidates.sql`
- `statement_lines` (UUID PK, batch_id FK -> reconciliation_batches, source_page_no, source_row_no, transaction_on, posted_on, description_raw, description_normalized, amount, currency, direction, line_type, match_status, matched_transaction_id FK -> transactions, confidence, line_fingerprint)
- `reconciliation_candidates` (UUID PK, batch_id FK -> reconciliation_batches, statement_line_id FK -> statement_lines, candidate_type, status, target_transaction_id FK -> transactions, payload, confidence, reason_code, reason_detail, resolved_by_user_id FK -> users, resolved_at, applied_transaction_id FK -> transactions)

#### [NEW] `migrations/0007_installments.sql`
- `installment_plans` (UUID PK, household_id FK -> households, credit_account_id FK -> accounts, purchase_occurred_on, merchant, original_amount, original_currency, account_principal_amount, account_currency, total_periods, first_statement_month, status)
- `installment_periods` (UUID PK, plan_id FK -> installment_plans, period_no, scheduled_amount, currency, status, statement_line_id FK -> statement_lines, expense_transaction_id FK -> transactions)

#### [NEW] `migrations/0008_audit_events.sql`
- `audit_events` (id BIGINT GENERATED ALWAYS AS IDENTITY PK, household_id FK -> households, actor_type, actor_user_id FK -> users, actor_device_id FK -> devices, request_id FK -> ingestion_requests, reconciliation_batch_id FK -> reconciliation_batches, entity_type, entity_id, action, before_data, after_data, metadata, created_at)
- Immutability trigger: `BEFORE UPDATE OR DELETE ON audit_events` raising an exception to enforce append-only audit trail.

#### [NEW] `migrations/0009_indexes.sql`
- Index structures only (no duplicate or deferred FKs):
  - Performance indexes on `accounts`, `transactions`, `audit_events`.
  - GIN trigram indexes on normalized text fields (`transactions.merchant_normalized`, `statement_lines.description_normalized`, `account_aliases.normalized_alias`).
  - Partial indexes for active records constraints.

#### [NEW] `migrations/runner.py`
- Deterministic migration engine that tracks applied migrations in `schema_migrations` table inside the configured `DB_SCHEMA`.
- Validates safety constraints and requires explicit schema configuration.
- Executes migrations sequentially in a single transaction per migration file.

---

### 5. Repository Layer (Phase 1 Persistence Primitives)

#### [NEW] [repositories/accounts.py](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/ai-ledger-backend/app/repositories/accounts.py)
- Household: `create_household()`, `get_household()`
- User: `create_user()`, `get_user()`
- Membership: `add_household_member()`, `get_household_members()`
- Device: `create_device()`, `get_device()`, `get_device_by_token_hash()`
- Account & State:
  - `create_account()`: atomically creates `accounts` row and its associated `account_state` row.
  - `get_account()`, `get_account_state()`, `list_accounts()`
  - `lock_account_state(conn, account_id)`: executes `SELECT * FROM account_state WHERE account_id = %s FOR UPDATE` under the schema search path.
- Aliases & Categories:
  - `create_account_alias()`, `list_account_aliases()`
  - `create_category()`, `list_categories()`

#### [NEW] [repositories/ingestion.py](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/ai-ledger-backend/app/repositories/ingestion.py)
- `create_ingestion_request()`, `get_ingestion_request()`, `get_by_device_and_key()`, `update_ingestion_request_status()`

#### [NEW] [repositories/audit.py](file:///c:/Users/Illidan/OneDrive/Vibe%20Coding/Vibe%20Ledger/ai-ledger-backend/app/repositories/audit.py)
- `insert_audit_event()`, `list_audit_events_for_entity()`

---

## Verification Plan

### Automated Tests

1. **Safety Guard Tests (`tests/test_safety.py`)**:
   - `ENVIRONMENT=production` causes immediate abort.
   - Empty or `public` `DB_SCHEMA` causes immediate abort.
   - Destructive operations outside `ENVIRONMENT=test` are rejected.
   - Database connections run without legacy `public` fallback in `search_path`.
   - Tests do not silently fall back to legacy database/schema.

2. **Money Domain Unit Tests (`tests/test_money.py`)**:
   - `0.1 + 0.2 == Decimal('0.3')` exact arithmetic.
   - Exact `NUMERIC(20,6)` scale and `NUMERIC(24,12)` FX rate representation.
   - Currency code validation (`^[A-Z]{3}$`), rejection of invalid codes.
   - Minor unit quantization (CNY `0.01`, USD `0.01`, JPY `1`).

3. **Migration Runner Tests (`tests/test_migrations.py`)**:
   - Run migrations against fresh isolated schema (`vibeledger_test_<uuid>`).
   - Verify all 9 migration files apply successfully in order.
   - Re-running migrations is an idempotent no-op.
   - Verify DB extensions (pgcrypto, pg_trgm, citext) are detected at DB level and NOT dropped/recreated during cleanup.

4. **Schema & Parity Check PostgreSQL Integration Tests (`tests/test_schema.py`)**:
   - Performs a field-by-field parity verification against `docs/architecture/PHYSICAL_SCHEMA.md`.
   - **Identity & Devices**: Duplicate `auth_subject` / `token_hash` rejected.
   - **Accounts & Account State**: Invalid `account_type` or `currency` rejected; duplicate active name in household rejected.
   - **Atomicity**: Creating an account atomically creates `account_state` with `ledger_balance = 0` and `initialized_at = NULL`.
   - **Locking & Transactions**: `lock_account_state` executes `SELECT ... FOR UPDATE`; failure in multi-step operation rolls back cleanly leaving zero partial rows.
   - **Audit Immutability**: `INSERT` into `audit_events` succeeds; `UPDATE` or `DELETE` raises database trigger errors.

### Execution Command
```bash
ai-ledger-backend/venv_backend/bin/python -m unittest discover -s ai-ledger-backend/tests -p "test_*.py"
```
