# Walkthrough: VibeLedger Phase 0 & Phase 1 Foundation Hardening

This document records the exact implementation and verification outcomes of the **Phase 0 & Phase 1 Foundation Hardening Patch** on branch `implementation/phase-0-1-hardening`.

---

## 1. Summary of Hardening Changes

### 1.1 PostgreSQL Extension Schema Portability
- **Catalog-Driven Extension Discovery**: Replaced hard-coded `public.citext` and `public.gin_trgm_ops` with dynamic catalog discovery (`pg_extension` joined with `pg_namespace`).
- **Unified Bootstrap Ownership**: Removed duplicate and conflicting `CREATE EXTENSION ... SCHEMA public` logic from migration SQL files. Extension prerequisite verification is owned exclusively by `migrations/runner.py`.
- **Dynamic DDL Templating**: SQL migration scripts use tokens (`__CITEXT_TYPE__` and `__GIN_TRGM_OPS__`) which the migration runner resolves to the discovered extension namespace (`<schema>.citext` / `<schema>.gin_trgm_ops`) at execution time.
- **Strict Isolation Maintained**: `search_path` remains strictly set to `<target_schema>` with no `public` fallback for table resolution.

### 1.2 Migration Checksum & Drift Protection
- **Tracking Table Schema**: `schema_migrations` now tracks `migration_name TEXT PRIMARY KEY`, `checksum_sha256 TEXT NOT NULL`, and `applied_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
- **SHA-256 Checksum Calculation**: The migration runner computes SHA-256 from exact migration file bytes.
- **Drift Rejection**: If a recorded migration file's checksum differs on rerun, `runner.MigrationChecksumMismatch` is raised immediately without executing or overwriting.

### 1.3 Conservative Foreign Key Deletion Policy
Reviewed all 20 business target tables and enforced conservative persistence semantics:
- **Durable Historical / Ledger Evidence (`RESTRICT`)**:
  - `accounts.household_id` -> `RESTRICT`
  - `categories.household_id` -> `RESTRICT`
  - `transactions.household_id`, `from_account_id`, `to_account_id`, `category_id` -> `RESTRICT`
  - `transaction_links.source_transaction_id`, `target_transaction_id` -> `RESTRICT`
  - `account_snapshots.household_id`, `account_id` -> `RESTRICT`
  - `credit_card_snapshots.household_id`, `account_id` -> `RESTRICT`
  - `investment_pnl_periods.household_id`, `account_id`, `opening_snapshot_id`, `closing_snapshot_id` -> `RESTRICT`
  - `installment_plans.household_id`, `credit_account_id` -> `RESTRICT`
  - `reconciliation_batches.household_id`, `account_id` -> `RESTRICT`
  - `audit_events.household_id` -> `RESTRICT` (prevents cascade deletion of append-only audit trail)
  - `ingestion_requests.device_id` -> `RESTRICT`
- **Lifecycle-Owned / Disposable Children (`CASCADE`)**:
  - `household_members.household_id`, `user_id` -> `CASCADE`
  - `devices.user_id` -> `CASCADE`
  - `account_state.account_id` -> `CASCADE`
  - `account_aliases.account_id` -> `CASCADE`
  - `installment_periods.plan_id` -> `CASCADE`
  - `statement_lines.batch_id` -> `CASCADE`
  - `reconciliation_candidates.batch_id`, `statement_line_id` -> `CASCADE`
- **Nullable Context / Actor References (`SET NULL`)**:
  - Actor references (`owner_user_id`, `created_by_user_id`, `actor_user_id`, etc.) -> `SET NULL`
  - Source batch / request links (`source_request_id`, `statement_batch_id`, `reconciliation_batch_id`) -> `SET NULL`

### 1.4 Foundation Repository Signature Tightening
- `accounts.create_household`: Made `ledger_start_date: date` a required Python parameter without default `None`.
- `audit.insert_audit_event`: Made `entity_type: str`, `entity_id: UUID`, and `action: str` required parameters without empty string defaults.
- `accounts.create_account`: Aligned argument ordering and default values with DB column nullability.

### 1.5 Test Database Destructive Cleanup Safety
- `config.validate_test_schema()`: Ensures destructive `DROP SCHEMA` operations are only permitted when `ENVIRONMENT=test` and schema name matches regex `^vibeledger_test_[a-zA-Z0-9_]+$`.
- Protected schemas (`public`, `vibeledger_target`, `extensions`, `pg_catalog`, `information_schema`, `vault`) are strictly rejected.

---

## 2. Test Verification Report

### Test Command
```bash
.\venv_backend\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

### Test Count and Execution Results
- **Exact Test Count**: 22 tests across 4 test modules.
- **Failures / Errors**: 0 failures, 0 errors.
- **Execution Time**: ~93.6 seconds (integration testing with live PostgreSQL / Supabase instance).

```text
......................
----------------------------------------------------------------------
Ran 22 tests in 93.663s

OK
```

### Module Breakdown
1. **`tests.test_safety` (5 tests)**:
   - `test_production_environment_rejected`: Asserts `validate_safety` raises `PermissionError` when `ENVIRONMENT=production`.
   - `test_public_schema_rejected`: Asserts `validate_safety` and `validate_schema` reject `public` schema.
   - `test_empty_schema_rejected`: Asserts rejection of empty/whitespace schema strings.
   - `test_destructive_ops_outside_test_rejected`: Asserts `is_safe_for_testing()` is only True when `ENVIRONMENT=test`.
   - `test_validate_test_schema_safety`: Asserts rejection of protected schemas, non-matching test patterns, and non-test environments for schema teardown.

2. **`tests.test_money` (6 tests)**:
   - `test_float_rejection`: Asserts float inputs are rejected to prevent floating-point drift.
   - `test_currency_code_validation`: Asserts ISO 4217 uppercase 3-letter currency regex validation.
   - `test_quantize_minor_units`: Asserts accurate quantization for JPY (0 decimals), CNY/USD (2 decimals), and KWD (3 decimals).
   - `test_parse_and_validate_amount`: Asserts valid decimal string parsing and sign constraints.
   - `test_fx_rate_validation`: Asserts positive FX rates and scale quantization up to 12 decimal places.
   - `test_cross_currency_conversion`: Asserts exact Decimal math in cross-currency conversions.

3. **`tests.test_migrations` (3 tests)**:
   - `test_run_migrations_success`: Asserts all 9 migrations apply in order on fresh schema, verifies SHA-256 recording, asserts table presence, and verifies idempotent no-op on rerun.
   - `test_migration_checksum_drift_protection`: Simulates checksum tampering in `schema_migrations` and asserts `MigrationChecksumMismatch` is raised.
   - `test_extension_discovery_and_non_destruction`: Verifies discovery of `pgcrypto`, `pg_trgm`, and `citext` namespaces in catalog and asserts extension persistence.

4. **`tests.test_schema` (8 tests)**:
   - `test_exhaustive_schema_parity`: Data-driven catalog assertion covering all 20 business target tables, asserting exact columns, types, char lengths, numeric precision/scale, nullability, and primary keys against `PHYSICAL_SCHEMA.md`.
   - `test_required_not_null_column_rejections`: Asserts explicit NULL rejection across all required enum, status, and metadata columns.
   - `test_transaction_lifecycle_invariants`: Asserts lifecycle constraint (`status=committed <=> deleted_at IS NULL AND delete_reason IS NULL`, `status=voided <=> deleted_at IS NOT NULL AND delete_reason IS NOT NULL`).
   - `test_installment_invariants`: Asserts `pending_first_bill` default status, period range (2..120), and `status=billed <=> expense_transaction_id IS NOT NULL`.
   - `test_reconciliation_batch_invariants`: Asserts status checks, non-negative count constraints, period ordering, and `committed <=> committed_at IS NOT NULL`.
   - `test_conservative_foreign_key_semantics`: Asserts `ON DELETE RESTRICT` prevents accidental cascade deletion of financial transactions and audit events.
   - `test_audit_event_trigger_immutability`: Asserts `audit_events` allows INSERT but trigger raises `DatabaseError` on UPDATE or DELETE.
   - `test_accounts_atomicity_and_locking`: Asserts atomic creation of `accounts` and `account_state` (`ledger_balance = 0.000000`) and verifies `SELECT FOR UPDATE` locking and rollback.

---

## 3. Database Environment & Extension Status

- **Database Used**: Supabase PostgreSQL 15+ Pooler instance.
- **Extension Discovery Status**:
  - `pgcrypto`: Installed in `extensions` schema.
  - `citext`: Installed in `public` schema.
  - `pg_trgm`: Installed in `public` schema.
  - All three extensions were detected pre-existing at database level and discovered dynamically.
- **Target Schemas Used**:
  - Test Schemas: `vibeledger_test_<uuid>` (created dynamically, fully migrated, tested, and dropped).
  - Development Schema: `vibeledger_target` (rebuilt clean with checksum tracking and verified idempotent).
- **Legacy Public Tables**:
  - Confirmed `accounts_dev`, `ledger`, and `transactions_dev` in `public` remain 100% untouched.

---

## 4. Phase Scope Confirmation

- **Phase 0 Status**: Complete and Hardened.
- **Phase 1 Status**: Complete and Hardened.
- **Phase 2 Status**: NOT implemented. (Zero Phase 2 code, business services, Gemini integration, or transaction mutation APIs were introduced).
