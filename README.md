# VibeLedger

A personal finance system for a two-person household: capture everyday expenses
with an iPhone Shortcut, periodically update account balances, and see household
assets, debts, net worth, investment gains and risk distribution.

The FastAPI backend, PostgreSQL data layer and Streamlit REST Dashboard already
exist. Staging runtime and the real iPhone Expense Shortcut have passed acceptance,
as reported in the household handoff. Production fresh cutover has not happened.

The **simplified architecture revised on 2026-09-06 is being implemented: S1/S2
are accepted and S3 remains in progress**.
It keeps the working one-request expense experience and replaces projected account
balances and general reconciliation with dated balance observations. Spending and
wealth are independent. Monthly spending schedules and selected-account statement
imports feed these records. Review exposes missing accounts, uncertain categories
and unusual investment changes. Missing capital flows default to zero for estimated
gains; user-confirmed gains are labelled separately.

Start with [the architecture index](docs/architecture/README.md). Its three documents
cover [product rules](TARGET_DOMAIN_MODEL.md), [schema and APIs](docs/architecture/CONTRACTS.md),
and [implementation and acceptance](docs/architecture/IMPLEMENTATION_PLAN.md).
[PROJECT_CONTEXT](PROJECT_CONTEXT.md) records the current handoff and next slice.

| Directory | Contents |
|---|---|
| [ai-ledger-backend](ai-ledger-backend/README.md) | Implemented FastAPI app in app/; previous architecture's migrations/tests; legacy root entry point retained pending replacement |
| [ai-ledger-dashboard](ai-ledger-dashboard/README.md) | Implemented Streamlit app and authenticated REST client |
| [docs/architecture](docs/architecture/README.md) | Current simplified target and transition/testing contract |
| [docs/deployment](docs/deployment/STAGING_DEPLOYMENT.md) | Historical accepted staging setup; must be updated during simplified implementation |
| [docs/legacy](docs/legacy/README.md) | Prototype history, not target requirements |

Keep Cloud Run backend/Dashboard services in asia-southeast1 and Supabase PostgreSQL.
Runtime secrets remain outside Git. The Dashboard does not access financial tables
directly. No historical legacy-data migration is required. Implement in an isolated
fresh schema; production deployment is a later, separately authorized action.
