# VibeLedger project handoff

Updated: **2026-09-06**. Architecture baseline reviewed on
`refactor/astra-simplify-architecture` at `3ac0ed6`.
The accepted simplification is committed at `83e479a`; this focused documentation
revision adds the household's schedule, statement, metadata-review and gain requirements.

## Current state

* `ai-ledger-backend/app/` is a substantial implemented FastAPI application, not an
  unbuilt prototype. Its root `main.py` is the older prototype entry point.
* `ai-ledger-dashboard/app.py` already uses backend REST through `api_client.py`.
  It does not directly own PostgreSQL business logic.
* Migrations 0001–0009 implement the previous architecture (20 application tables).
  They do not implement the Phase 12.5 risk/category-description/multi-account
  capture proposal. Do not treat that proposal as a prerequisite anymore.
* The user reports staging runtime and real-device Expense Shortcut acceptance
  complete. The deployment runbook documents the earlier runtime gate; this
  architecture review did not rerun remote acceptance.
* Production fresh cutover has not happened. Historical legacy-data migration is
  not required. No production deployment is part of the architecture task.
* This handoff introduces a **documentation-only simplified target**. Existing
  runtime/schema/tests still reflect the preceding design until implementation.

## Source of truth

Read [TARGET_DOMAIN_MODEL](TARGET_DOMAIN_MODEL.md),
[CONTRACTS](docs/architecture/CONTRACTS.md), and
[IMPLEMENTATION_PLAN](docs/architecture/IMPLEMENTATION_PLAN.md).
[Architecture index](docs/architecture/README.md) explains consolidation and authority.
The old “frozen” Phase 12.5 documents are superseded, available in Git at `3ac0ed6`.

## Target in one paragraph

Keep reliable screenshot expense capture and independent dated account balances.
Wealth comes from the latest observed assets and debts, with freshness and coverage
visible. Spending does not move balances; balance updates do not invent spending.
Investment gain is the change in value minus net additions; absent flow inputs mean
zero assumed flow and an estimated gain, distinct from user-confirmed gain. Review
surfaces unusual estimates and saved expenses with unknown accounts or uncertain
Other categories. Support monthly recurring/installment spending and selected-account
statement import for batch spending plus a dated balance. Keep FastAPI, Streamlit,
Supabase, device tokens/receipts, Decimal, household auth and small change history.
Remove general reconciliation, balance projections, generic links and separate
audit/work-queue user interfaces.

## Next implementation work

**S0** (freeze working boundary and runnable baseline) is **completed** on commit
`f9ca292cfd0a7437fcc274d236e160344566daa5`. Sanitized wire fixtures, the Shortcut
compatibility boundary specification, migration checksums, and offline boundary
characterization tests are committed and verified.

**S1** (fresh database, identities and settings) implementation and remediation completed:
* 16-table simplified baseline under `migrations/simplified/0001_simplified.sql` with strict composite foreign keys on all household entities (`fk_ingestion_requests_device`, `fk_spending_schedules_device`, `fk_transactions_device`, `fk_transactions_deleter`, `fk_snapshots_device`, `fk_snapshots_voider`, `fk_audit_events_user`, `fk_audit_events_device`).
* Strict lineage selection (`LegacyMigrationLineageDetectedError`) and readiness check verification.
* Remote Supabase safety guards rejecting non-local databases in test mode.
* Simplified repositories with strict household scoping on all mutations, queries, and row locks (`lock_ingestion_request`, `touch_device`).
* Short household-scoped finance-write lock primitive (`acquire_household_finance_lock`, `lock_ingestion_requests_in_order`).
* Least-privilege operator/runtime role configuration (`scripts/setup_roles_simplified.sql` and `docs/architecture/DATABASE_ROLES.md`).
* Idempotent seed script with fail-fast configuration drift detection (`BootstrapDriftError` in `scripts/bootstrap_simplified.py`).
* Comprehensive offline unit test suite (24 tests in `tests/unit/test_s1_foundation_unit.py`, 215 tests backend total).
* Real PostgreSQL integration test suite (13 test methods in `tests/integration/test_s1_simplified_db.py` covering catalog inspection, constraints, composite FK isolation, audit immutability, two-connection concurrency, least-privilege roles, and drift detection).

**S1 acceptance gate status**: Implementation and remediation are complete; acceptance is **pending real PostgreSQL execution**.
On the current host environment, Docker / local PostgreSQL 17 is not installed/running, so tests requiring a live database fixture skip gracefully. In accordance with CONTRACTS safety rules, remote databases (e.g. Supabase) cannot be substituted for disposable test runs. Full S1 acceptance will complete once Docker or a local PostgreSQL 17 instance is available to run `ai-ledger-backend/scripts/run_local_integration.ps1`. Do not start S2 until accepted.

Consumer choices are specified, not blockers: last reported wealth, due-period
installment spending, editable seeded categories, statement preview before Save,
and estimated gains with an initial adjustable 20% unusual-change threshold.
Monthly days beyond a month's length use its last day. The four requested additions
do not reintroduce a reconciliation engine or projected account balances.

## Workspace and operating notes

Use PowerShell 7 (`pwsh.exe`) and UTF-8 on Windows. Never run legacy remote-dependent
tests or destructive test cleanup against inherited credentials. Use a disposable
local PostgreSQL test database/schema and the existing safety harness.
`ai-ledger-backend/cloudbuild.phase12.yaml` predates this revision and remains
untouched by this documentation work.
