# Implementation Tasks (Phase 0 & Phase 1 Foundation Hardening)

## Phase 0 — Configuration & Safety Foundation
- [x] Add `pydantic-settings` to `ai-ledger-backend/requirements.txt`
- [x] Create `ai-ledger-backend/app/config.py` (Pydantic v2 Settings + safety checks)
- [x] Create `.env.example` in repo root and `ai-ledger-backend/.env.example`
- [x] Implement strict test schema validation in `app/config.py` (`validate_test_schema`)
- [x] Create `ai-ledger-backend/tests/test_safety.py` (safety guard tests including test schema destruction checks)

## Phase 1 — Database Connection & Domain Money Primitives
- [x] Create `ai-ledger-backend/app/db.py` (connection, transaction managers, search path scoping, Identifier quoting)
- [x] Register UUID adapter in `app/db.py` for psycopg2 parameter binding
- [x] Create `ai-ledger-backend/app/domain/money.py` (Decimal parsing, currency and FX validation, quantization)
- [x] Create `ai-ledger-backend/tests/test_money.py` (domain money unit tests)

## Phase 1 — Physical Database Migrations & Runner Hardening
- [x] Hardened `0001_extensions.sql` to eliminate conflicting duplicate extension DDL
- [x] Hardened `0002_identity_accounts.sql` with portable citext and conservative RESTRICT FKs
- [x] Hardened `0003_ingestion_batches.sql` with conservative RESTRICT FKs
- [x] Hardened `0004_transactions.sql` with conservative RESTRICT FKs on financial history
- [x] Hardened `0005_snapshots_invest.sql` with conservative RESTRICT FKs on snapshots and P&L
- [x] Hardened `0006_statement_candidates.sql` with explicit cascade rules on batch drafts
- [x] Hardened `0007_installments.sql` with conservative RESTRICT FKs on plans
- [x] Hardened `0008_audit_events.sql` with RESTRICT on household_id and immutability trigger
- [x] Hardened `0009_indexes.sql` with portable `__GIN_TRGM_OPS__` token
- [x] Implement SHA-256 migration checksum tracking and drift rejection in `runner.py`
- [x] Implement dynamic catalog extension namespace discovery in `runner.py`
- [x] Create `ai-ledger-backend/tests/test_migrations.py` (testing runner, checksum drift, extension discovery)

## Phase 1 — Repository Layer
- [x] Tighten `ai-ledger-backend/app/repositories/accounts.py` signatures
- [x] Tighten `ai-ledger-backend/app/repositories/ingestion.py` signatures
- [x] Tighten `ai-ledger-backend/app/repositories/audit.py` signatures

## Phase 1 — Comprehensive Parity & Regression Verification
- [x] Create exhaustive data-driven contract parity test in `tests/test_schema.py` for all 20 business tables
- [x] Add NOT NULL constraint rejection tests across all tables
- [x] Add transaction lifecycle invariant tests (`committed` vs `voided`)
- [x] Add installment invariant tests (`pending_first_bill`, `billed <=> expense_transaction_id`)
- [x] Add reconciliation batch invariant tests (counts, period ordering, committed status)
- [x] Add conservative FK `ON DELETE RESTRICT` rejection tests
- [x] Add audit trigger immutability tests
- [x] Run full automated test suite (`22 tests passed in ~93.6s`)
