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

Start **S0** in the implementation plan: preserve accepted expense wire fixtures and
the actual Shortcut behavior, record a safe runnable baseline, and specify/test the
interrupted pending-key cancellation race. Then S1 fresh schema/identity, S2 spending,
metadata review and monthly schedules, S3 balances/wealth and statement import,
S4 investment estimates/confirmation/Dashboard/login, S5 removal and staging acceptance.
S6 production cutover is later and separately authorized. Do not implement the old
proposed `0010_asset_model_freeze.sql` first or rewrite applied migration bytes.

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
