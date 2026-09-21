# VibeLedger

The accepted hosted frontend is [React](ai-ledger-web/README.md); the existing
Streamlit service remains available as a fallback. S1–S4 are accepted. S5 removes
superseded runtime and provides accepted isolated staging operations; production cutover
has not happened. See [S5 evidence](docs/deployment/S5_ACCEPTANCE.md).

A personal finance system for a two-person household: capture everyday expenses
with an iPhone Shortcut, periodically update account balances, and see household
assets, debts, net worth, investment gains and risk distribution.

The FastAPI backend, PostgreSQL data layer and React REST Dashboard already
exist. Staging runtime and the real iPhone Expense Shortcut have passed acceptance,
as reported in the household handoff. Production fresh cutover has not happened.

The **simplified architecture revised on 2026-09-06 is being implemented: S1/S2
and S3/S4 are accepted; S5 has completed isolated staging acceptance**.
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
| [ai-ledger-backend](ai-ledger-backend/README.md) | Single FastAPI runtime in app/; simplified migrations and invariant tests |
| [ai-ledger-dashboard](ai-ledger-dashboard/README.md) | Implemented Streamlit app and authenticated REST client |
| [docs/architecture](docs/architecture/README.md) | Current simplified target and transition/testing contract |
| [docs/deployment](docs/deployment/STAGING_DEPLOYMENT.md) | Current isolated staging operation and recovery runbook |
| [docs/legacy](docs/legacy/README.md) | Prototype history, not target requirements |

Keep Cloud Run backend/Dashboard services in asia-southeast1 and Supabase PostgreSQL.
Runtime secrets remain outside Git. The Dashboard does not access financial tables
directly. No historical legacy-data migration is required. Implement in an isolated
fresh schema; production deployment is a later, separately authorized action.
