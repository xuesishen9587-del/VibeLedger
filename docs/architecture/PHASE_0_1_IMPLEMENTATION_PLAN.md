# Implementation Plan: VibeLedger Phase 0 & Phase 1

This plan covers the implementation of **Implementation Phase 0 (Architecture Freeze & Test Safety)** and **Implementation Phase 1 (Target Database Foundation)** for VibeLedger, strictly adhering to the frozen target architecture (`TARGET_DOMAIN_MODEL.md`, `PHYSICAL_SCHEMA.md`, `API_CONTRACT.md`, `RECONCILIATION_ENGINE.md`, `IMPLEMENTATION_PLAN.md`, `TEST_PLAN.md`).

Phase 2 financial business logic (expense mutation, cash income mutation, transfer service, fee service, refund service, opening balance service, reconciliation adjustment service, void/reprojection service, etc.) is **strictly out of scope** and will NOT be implemented in this increment.

---

## User Review Required

> [!IMPORTANT]
> **Database Isolation Policy**:
> Target migrations and tests will run strictly inside explicit, isolated PostgreSQL schemas via `search_path` (e.g. `DB_SCHEMA=vibeledger_target` for dev and `vibeledger_test_<uuid>` for tests). No legacy production tables in `public` will be modified, altered, renamed, or migrated.
> 
> **Safety Guard**: Any attempt to run tests or migrations when `ENVIRONMENT=production`, or when `DB_SCHEMA` is empty or resolves to `public`, will be immediately aborted.

---

## Proposed Architecture & File Structure

```text
ai-ledger-backend/
├── app/
│   ├── __init__.py
│   ├── config.py                 # Target configuration layer & safety guards
│   ├── db.py                     # Connection & transaction context management with schema search_path
│   ├── domain/
│   │   ├── __init__.py
│   │   └── money.py              # Decimal parsing, currency validation, quantization, FX validation
│   └── repositories/
│       ├── __init__.py
│       ├── accounts.py           # Household, User, Device, Account, AccountState, Aliases, Categories
│       ├── ingestion.py          # IngestionRequest persistence
│       └── audit.py              # Append-only AuditEvents persistence
├── migrations/
│   ├── __init__.py
│   ├── runner.py                 # Explicit deterministic SQL migration runner
│   ├── 0001_extensions.sql       # pgcrypto, pg_trgm, citext
│   ├── 0002_identity_accounts.sql# households, users, household_members, devices, accounts, account_state, aliases, categories
│   ├── 0003_ingestion_batches.sql# ingestion_requests, reconciliation_batches
│   ├── 0004_transactions.sql     # transactions, transaction_links
│   ├── 0005_snapshots_invest.sql # account_snapshots, credit_card_snapshots, investment_pnl_periods
│   ├── 0006_statement_candidates.sql # statement_lines, reconciliation_candidates
│   ├── 0007_installments.sql     # installment_plans, installment_periods
│   ├── 0008_audit_events.sql     # audit_events & immutable update/delete trigger
│   └── 0009_deferred_fks_indexes.sql # cross-table foreign keys, trigram GIN indexes, partial indexes
├── tests/
│   ├── __init__.py
│   ├── test_safety.py            # Phase 0 safety guard tests (production reject, public schema reject)
│   ├── test_money.py             # Phase 1 domain money unit tests (exact Decimal arithmetic, currencies, quantization)
│   ├── test_migrations.py        # Schema migration determinism & isolated schema execution tests
│   └── test_schema.py            # PostgreSQL schema constraints, NOT NULLs, triggers, row locks, transaction rollback
├── .env.example                  # Safe placeholders for ENVIRONMENT, DATABASE_URL, DB_SCHEMA, GEMINI_API_KEY
```

---

## Proposed Changes

### 1. Phase 0: Configuration & Safety Foundation

#### [NEW] [config.py](file:///Users/shenxs/Desktop/Vibe%20Coding/Finance%20Ledger/ai-ledger-backend/app/config.py)
- Defines typed `Settings` using Pydantic / python-dotenv:
  - `ENVIRONMENT`: `development` | `test` | `production`
  - `DATABASE_URL`: PostgreSQL connection string
  - `DB_SCHEMA`: Target PostgreSQL schema (defaults to `vibeledger_target` in development, required)
  - `GEMINI_API_KEY`: Placeholder/config (NOT used in Phase 0/1)
- Implements safety verification functions:
  - `validate_safety()`: Refuses execution if `ENVIRONMENT == "production"` during tests/migrations.
  - `validate_schema()`: Refuses execution if `DB_SCHEMA` is empty, whitespace, or `"public"`.
  - `is_safe_for_testing()`: Refuses destructive test operations if `ENVIRONMENT != "test"`.

#### [NEW] [.env.example](file:///Users/shenxs/Desktop/Vibe%20Coding/Finance%20Ledger/.env.example) & [ai-ledger-backend/.env.example](file:///Users/shenxs/Desktop/Vibe%20Coding/Finance%20Ledger/ai-ledger-backend/.env.example)
- Canonical example variables with safe placeholders only (no real credentials).

---

### 2. Database Connection & Transaction Layer

#### [NEW] [db.py](file:///Users/shenxs/Desktop/Vibe%20Coding/Finance%20Ledger/ai-ledger-backend/app/db.py)
- Connection provider using `psycopg2`.
- Schema scoping: every connection executes `SET search_path = {schema}, public` or `SET search_path = {schema}` upon acquisition to ensure complete isolation.
- Context managers:
  - `get_connection(schema=None)`: yields connection scoped to target schema.
  - `transaction(conn=None, schema=None)`: context manager managing `BEGIN`, `COMMIT`, and `ROLLBACK` cleanly without hidden auto-commits.
- Supports explicit row locking: `SELECT ... FOR UPDATE`.

---

### 3. Domain Money Primitives

#### [NEW] [domain/money.py](file:///Users/shenxs/Desktop/Vibe%20Coding/Finance%20Ledger/ai-ledger-backend/app/domain/money.py)
- `parse_decimal(value)`: safely converts string/int/Decimal to `Decimal`, strictly rejecting float types to prevent precision loss.
- `quantize_money(amount: Decimal, currency: str) -> Decimal`: quantizes to currency minor units (e.g. JPY $\to$ `1`, CNY/USD/EUR/SGD $\to$ `0.01`).
- `validate_currency_code(code: str) -> str`: validates regex `^[A-Z]{3}$` and uppercase normalization.
- `validate_fx_rate(rate: Decimal) -> Decimal`: validates positive `NUMERIC(24,12)` bounds.
- `quantize_reporting(amount: Decimal) -> Decimal`: standard `NUMERIC(20,6)` scale representation.

---

### 4. Physical Database Migrations

Explicit versioned SQL migrations based on `PHYSICAL_SCHEMA.md`:

#### [NEW] `migrations/0001_extensions.sql`
- `CREATE EXTENSION IF NOT EXISTS pgcrypto;`
- `CREATE EXTENSION IF NOT EXISTS pg_trgm;`
- `CREATE EXTENSION IF NOT EXISTS citext;`

#### [NEW] `migrations/0002_identity_accounts.sql`
- `households` (UUID PK, reporting_currency CHAR(3), ledger_start_date DATE, status CHECK active/archived)
- `users` (UUID PK, auth_subject TEXT UNIQUE, email CITEXT UNIQUE, display_name TEXT, default_currency CHAR(3), status CHECK active/disabled)
- `household_members` (PK household_id + user_id, role CHECK owner/member)
- `devices` (UUID PK, user_id FK, device_name, platform, token_hash BYTEA UNIQUE, status CHECK active/revoked, revoked_at consistency CHECK)
- `accounts` (UUID PK, household_id FK, name, institution, account_type CHECK cash/savings/credit/investment, currency CHAR(3), owner_user_id FK, linked_cash_account_id FK, billing_day/due_day credit checks, status CHECK active/inactive, uq_accounts_active_name)
- `account_state` (PK account_id FK -> accounts, ledger_balance NUMERIC(20,6) default 0, initialized_at TIMESTAMPTZ nullable, row_version BIGINT default 0)
- `account_aliases` (UUID PK, account_id FK, alias_text, normalized_alias, status, uq_account_alias)
- `categories` (UUID PK, household_id FK, name, category_type CHECK expense/income, status, uq_categories_active)

#### [NEW] `migrations/0003_ingestion_batches.sql`
- `ingestion_requests` (UUID PK, device_id FK, idempotency_key TEXT, request_kind CHECK expense/transfer/snapshot, request_hash BYTEA, status CHECK received/processing/needs_confirmation/committed/rejected/failed, UNIQUE(device_id, idempotency_key))
- `reconciliation_batches` (UUID PK, household_id FK, account_id FK, batch_type CHECK statement/snapshot/manual, status CHECK processing/ready/needs_review/committed/rejected/failed, currency CHAR(3), engine_version NOT NULL default '1', counts >= 0, committed_at check)

#### [NEW] `migrations/0004_transactions.sql`
- `transactions` (UUID PK, household_id FK, transaction_type CHECK expense/cash_income/refund/transfer/fee/reconciliation_adjustment/opening_balance, occurred_on DATE, occurred_at TIMESTAMPTZ, posted_on DATE, from_account_id FK, to_account_id FK, original_amount > 0 NUMERIC(20,6), original_currency CHAR(3), from_amount, from_currency, to_amount, to_currency, effective_fx_rate NUMERIC(24,12), account_leg_status CHECK estimated/authoritative, reporting_amount, reporting_currency, reporting_fx_rate, category_id FK, merchant, merchant_normalized, remarks, source CHECK shortcut/statement/dashboard_manual/reconciliation/installment/system, status CHECK committed/voided, verification_status CHECK unverified/user_confirmed/statement_confirmed/manual_confirmed/system_confirmed, confidence BETWEEN 0 AND 1, source_request_id FK, statement_batch_id FK, lifecycle CHECK status=committed <=> deleted_at IS NULL AND delete_reason IS NULL, status=voided <=> deleted_at IS NOT NULL AND delete_reason IS NOT NULL)
- `transaction_links` (UUID PK, source_transaction_id FK, target_transaction_id FK, relation_type CHECK refund_of/reversal_of/installment_of, source <> target, uq_transaction_link_source_relation)

#### [NEW] `migrations/0005_snapshots_invest.sql`
- `account_snapshots` (UUID PK, household_id FK, account_id FK, as_of TIMESTAMPTZ, balance NUMERIC(20,6), currency CHAR(3), snapshot_type CHECK balance/investment_valuation, source CHECK shortcut/statement/dashboard_manual, reconciliation_batch_id FK, uq_snapshot_per_batch)
- `credit_card_snapshots` (UUID PK, household_id FK, account_id FK, as_of TIMESTAMPTZ, statement_period_start/end, statement_balance, remaining_statement_due, unbilled_balance, current_outstanding, currency CHAR(3), source CHECK statement/dashboard_manual/system_derived, uq_credit_snapshot_per_batch)
- `investment_pnl_periods` (UUID PK, household_id FK, account_id FK, opening_snapshot_id FK, closing_snapshot_id FK, period_start, period_end, contributions_amount >= 0, withdrawals_amount >= 0, pnl_amount signed, currency CHAR(3), status CHECK provisional/confirmed, calculation_version, UNIQUE(account_id, closing_snapshot_id))

#### [NEW] `migrations/0006_statement_candidates.sql`
- `statement_lines` (UUID PK, batch_id FK, source_page_no, source_row_no, transaction_on DATE, posted_on DATE, description_raw, description_normalized, amount > 0 NUMERIC(20,6), currency CHAR(3), direction CHECK debit/credit/unknown, line_type CHECK expense/income/transfer/refund/fee/unknown, match_status CHECK unmatched/matched/new_candidate/ambiguous/ignored, matched_transaction_id FK, confidence BETWEEN 0 AND 1, line_fingerprint BYTEA non-unique)
- `reconciliation_candidates` (UUID PK, batch_id FK, statement_line_id FK, candidate_type CHECK match/create_transaction/create_transfer/refund/adjustment/snapshot/investment_pnl/recognize_installment, status CHECK proposed/needs_review/accepted/rejected/applied, target_transaction_id FK, payload JSONB NOT NULL, confidence BETWEEN 0 AND 1, reason_code, reason_detail, resolved_by_user_id FK, resolved_at, applied_transaction_id FK)

#### [NEW] `migrations/0007_installments.sql`
- `installment_plans` (UUID PK, household_id FK, credit_account_id FK, purchase_occurred_on DATE, merchant, original_amount > 0, original_currency CHAR(3), account_principal_amount > 0, account_currency CHAR(3), total_periods BETWEEN 2 AND 120, first_statement_month DATE, status CHECK pending_first_bill/active/completed/cancelled)
- `installment_periods` (UUID PK, plan_id FK, period_no > 0, recognition_month DATE, scheduled_amount > 0 NUMERIC(20,6), currency CHAR(3), status CHECK scheduled/billed/cancelled, statement_line_id FK, expense_transaction_id FK, UNIQUE(plan_id, period_no), status=billed <=> expense_transaction_id IS NOT NULL)

#### [NEW] `migrations/0008_audit_events.sql`
- `audit_events` (id BIGINT GENERATED ALWAYS AS IDENTITY PK, household_id FK, actor_type CHECK user/device/system, actor_user_id FK, actor_device_id FK, request_id FK, reconciliation_batch_id FK, entity_type TEXT NOT NULL, entity_id UUID NOT NULL, action CHECK create/update/soft_delete/restore/confirm/reject/commit/reconcile/void, before_data JSONB, after_data JSONB, metadata JSONB, created_at TIMESTAMPTZ NOT NULL default now())
- Immutability trigger: `BEFORE UPDATE OR DELETE ON audit_events` raising an exception (`audit_events is append-only: updates and deletes are forbidden`).

#### [NEW] `migrations/0009_deferred_fks_indexes.sql`
- Cross-table foreign keys and indexes:
  - Indexes on `accounts (household_id, account_type, status)`, `transactions (from_account_id, occurred_on DESC)`, `transactions (to_account_id, occurred_on DESC)`, `transactions (household_id, occurred_on DESC)`, `transactions (household_id, transaction_type, occurred_on DESC)`.
  - GIN trigram indexes on `transactions.merchant_normalized`, `statement_lines.description_normalized`, `account_aliases.normalized_alias`.
  - Foreign key linking `installment_periods.expense_transaction_id -> transactions(id)`.
  - Foreign key linking `statement_lines.matched_transaction_id -> transactions(id)`.
  - Foreign key linking `reconciliation_candidates.target_transaction_id -> transactions(id)`.
  - Foreign key linking `reconciliation_candidates.applied_transaction_id -> transactions(id)`.

#### [NEW] `migrations/runner.py`
- Deterministic migration engine that tracks applied migrations in `schema_migrations` table inside the configured `DB_SCHEMA`.
- Validates safety constraints before executing.
- Executes migrations sequentially in a single transaction per migration file.

---

### 5. Repository Layer (Phase 1 Persistence Primitives)

#### [NEW] [repositories/accounts.py](file:///Users/shenxs/Desktop/Vibe%20Coding/Finance%20Ledger/ai-ledger-backend/app/repositories/accounts.py)
- Household: `create_household()`, `get_household()`
- User: `create_user()`, `get_user()`
- Membership: `add_household_member()`, `get_household_members()`
- Device: `create_device()`, `get_device()`, `get_device_by_token_hash()`
- Account & State:
  - `create_account()`: atomically creates `accounts` row and its associated `account_state` row (`ledger_balance = Decimal('0.000000')`, `initialized_at = None`).
  - `get_account()`, `get_account_state()`, `list_accounts()`
  - `lock_account_state(conn, account_id)`: executes `SELECT * FROM account_state WHERE account_id = %s FOR UPDATE`.
- Aliases & Categories:
  - `create_account_alias()`, `list_account_aliases()`
  - `create_category()`, `list_categories()`

#### [NEW] [repositories/ingestion.py](file:///Users/shenxs/Desktop/Vibe%20Coding/Finance%20Ledger/ai-ledger-backend/app/repositories/ingestion.py)
- `create_ingestion_request()`, `get_ingestion_request()`, `get_by_device_and_key()`, `update_ingestion_request_status()`

#### [NEW] [repositories/audit.py](file:///Users/shenxs/Desktop/Vibe%20Coding/Finance%20Ledger/ai-ledger-backend/app/repositories/audit.py)
- `insert_audit_event()`, `list_audit_events_for_entity()`

---

## Verification Plan

### Automated Tests

1. **Safety Guard Tests (`tests/test_safety.py`)**:
   - `ENVIRONMENT=production` causes immediate abort.
   - Empty or `public` `DB_SCHEMA` causes immediate abort.
   - Destructive operations outside `ENVIRONMENT=test` are rejected.
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

4. **Schema & Constraint PostgreSQL Integration Tests (`tests/test_schema.py`)**:
   - **Identity & Devices**: Duplicate `auth_subject` rejected; duplicate `token_hash` rejected; device status/revocation consistency constraint.
   - **Accounts & Account State**: Invalid `account_type` rejected; invalid `currency` rejected; duplicate active name in household rejected; same name in different household allowed; inactive account name allows new active account.
   - **Atomicity**: Creating an account atomically creates `account_state` with `ledger_balance = 0` and `initialized_at = NULL`.
   - **Locking & Transactions**: `lock_account_state` executes `SELECT ... FOR UPDATE`; failure in multi-step operation rolls back cleanly leaving zero partial rows.
   - **Categories**: Duplicate active category per type rejected.
   - **Ingestion Requests**: `(device_id, idempotency_key)` uniqueness enforced; invalid status rejected.
   - **Transactions**: Invalid `transaction_type` rejected; negative `original_amount` rejected; lifecycle constraints (`status=committed <=> deleted_at IS NULL AND delete_reason IS NULL`, `status=voided <=> deleted_at IS NOT NULL AND delete_reason IS NOT NULL`) enforced; explicit NULLs on required columns rejected.
   - **Installments**: `status=billed <=> expense_transaction_id IS NOT NULL` enforced; periods between 2 and 120.
   - **Reconciliation**: Batch status checks, counts >= 0, `engine_version` NOT NULL enforced.
   - **Audit Immutability**: `INSERT` into `audit_events` succeeds; `UPDATE` raises error from trigger; `DELETE` raises error from trigger.

### Execution Command
```bash
ai-ledger-backend/venv_backend/bin/python -m unittest discover -s ai-ledger-backend/tests -p "test_*.py"
```
*(Integration tests that require isolated PostgreSQL will run in BypassSandbox mode using a dedicated test schema `vibeledger_test_<uuid>`, clean up after themselves, and confirm that legacy `public` tables remain 100% untouched).*
