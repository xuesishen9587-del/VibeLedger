# Implementation Tasks (Phase 0 & Phase 1)

## Phase 0 — Configuration & Safety Foundation
- [x] Add `pydantic-settings` to `ai-ledger-backend/requirements.txt`
- [x] Create `ai-ledger-backend/app/config.py` (Pydantic v2 Settings + safety checks)
- [x] Create `.env.example` in repo root and `ai-ledger-backend/.env.example`
- [x] Create `ai-ledger-backend/tests/test_safety.py` (safety guard tests)

## Phase 1 — Database Connection & Domain Money Primitives
- [x] Create `ai-ledger-backend/app/db.py` (connection, transaction managers, search path scoping, Identifier quoting)
- [x] Create `ai-ledger-backend/app/domain/money.py` (Decimal parsing, currency and FX validation, quantization)
- [x] Create `ai-ledger-backend/tests/test_money.py` (domain money unit tests)

## Phase 1 — Physical Database Migrations
- [x] Create `ai-ledger-backend/migrations/0001_extensions.sql` (DB-level extensions setup, non-destructive, never dropped)
- [x] Create `ai-ledger-backend/migrations/0002_identity_accounts.sql` (inline FKs)
- [x] Create `ai-ledger-backend/migrations/0003_ingestion_batches.sql` (inline FKs)
- [x] Create `ai-ledger-backend/migrations/0004_transactions.sql` (inline FKs)
- [x] Create `ai-ledger-backend/migrations/0005_snapshots_invest.sql` (inline FKs)
- [x] Create `ai-ledger-backend/migrations/0006_statement_candidates.sql` (inline FKs)
- [x] Create `ai-ledger-backend/migrations/0007_installments.sql` (inline FKs)
- [x] Create `ai-ledger-backend/migrations/0008_audit_events.sql` (inline trigger)
- [x] Create `ai-ledger-backend/migrations/0009_indexes.sql` (performance and trigram GIN indexes only)
- [x] Create `ai-ledger-backend/migrations/runner.py` (safety-aware migration runner)
- [x] Create `ai-ledger-backend/tests/test_migrations.py` (migration runner tests)

## Phase 1 — Repository Layer
- [x] Create `ai-ledger-backend/app/repositories/accounts.py`
- [x] Create `ai-ledger-backend/app/repositories/ingestion.py`
- [x] Create `ai-ledger-backend/app/repositories/audit.py`

## Phase 1 — Verification & Parity Checking
- [x] Create `ai-ledger-backend/tests/test_schema.py` (PostgreSQL integration tests)
- [x] Run full automated test suite (`python -m unittest discover`) and resolve any errors
- [x] Perform table-by-table schema parity check against `docs/architecture/PHYSICAL_SCHEMA.md`
