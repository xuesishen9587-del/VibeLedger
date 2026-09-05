# VibeLedger Target Architecture Implementation Plan

> Status: **Frozen Target Implementation Plan (Final consistency review complete)**
>
> Authority:
>
> 1. `TARGET_DOMAIN_MODEL.md` — approved business truth
> 2. `docs/architecture/PHYSICAL_SCHEMA.md` — target persistence contract
> 3. `docs/architecture/API_CONTRACT.md` — target public API contract
> 4. `docs/architecture/RECONCILIATION_ENGINE.md` — target reconciliation/matching contract
> 5. This document — implementation sequencing and verification gates
> 6. `docs/architecture/TEST_PLAN.md` — testing and regression rules
>
> Principle: **Phased, test-driven, greenfield rebuild.**
>
> Core rule: **Each Phase MUST have automated test acceptance criteria before proceeding to the next Phase.**
>
> Goal: migrate the current legacy implementation to the target architecture **incrementally, testably, and without a one-shot rewrite**.
>
> Data migration: **not required**. The target system may start with a fresh ledger and explicit opening balances.

---

# 1. Current Baseline

Current backend:

```text
ai-ledger-backend/
  main.py
  database.py
  db_migration.py
  test_client.py
  test_idempotency.py
```

Current characteristics:

```text
FastAPI
Gemini structured output
PostgreSQL / Supabase
psycopg2
accounts + transactions core model
accounts.current_balance mutable source
TABLE_SUFFIX environment isolation
hard-coded account/category Literal
single POST /api/record
request idempotency stored on transactions
future installment transactions
adjustment overloaded for reconciliation/investment
```

Current Dashboard:

```text
ai-ledger-dashboard/
  app.py
  database.py
```

Current characteristics:

```text
Streamlit directly reads PostgreSQL
Streamlit directly writes reconciliation adjustments
backend/database logic duplicated
Dashboard recalculates credit-card state itself
investment adjustment is remapped to income in Pandas
```

Useful existing capabilities to retain conceptually:

```text
FastAPI
PostgreSQL / Supabase
Gemini structured extraction
Docker deployment
atomic balance-update lessons
deterministic transfer lock ordering
cross-currency two-leg concept
client idempotency concept
Streamlit UI as Product v1 Dashboard technology
```

Legacy implementation details that MUST NOT define the new model:

```text
accounts.current_balance as authoritative truth
hard-coded account/category Literal
generic adjustment
investment adjustment -> income
future installment transaction generation
single generic /api/record contract
Dashboard direct database access
float-based financial calculations
startup-time ALTER TABLE migration strategy
TABLE_SUFFIX as long-term schema-management mechanism
```

---

# 2. Migration Strategy

Use a **strangler migration**:

```text
Legacy system remains runnable
        +
Target backend is built beside it
        ↓
Target Expense path becomes usable
        ↓
Snapshot/Reconciliation path becomes usable
        ↓
Statement/Investment paths become usable
        ↓
Dashboard switches to Backend API
        ↓
Fresh production cutover
        ↓
Legacy code removed
```

Do not continuously mutate the old two-table model into the target model.

Because old production data does not need migration:

```text
build target schema in isolated development environment
test completely
create explicit opening balances at cutover
start new ledger
```

---

# 3. Environment and Database Rule

During implementation:

```text
production legacy database/tables
MUST NOT be used by automated tests
MUST NOT be modified by target migrations
```

Recommended target isolation:

```text
separate Supabase dev project
OR
separate PostgreSQL database/schema
```

Preferred configuration:

```text
DATABASE_URL
DB_SCHEMA
ENVIRONMENT
```

Do not create new target architecture by expanding:

```text
TABLE_SUFFIX=_dev
```

across every new table.

`TABLE_SUFFIX` may remain only for the untouched legacy runtime until cutover.

---

# 4. Target Backend Structure

Do not rebuild the entire project into a large framework.

Recommended minimal structure:

```text
ai-ledger-backend/
  app/
    main.py
    config.py
    db.py

    api/
      deps.py
      errors.py
      routes/
        health.py
        expenses.py
        ingestion.py
        accounts.py
        categories.py
        transactions.py
        snapshots.py
        statements.py
        reconciliation.py
        investments.py
        dashboard.py

    domain/
      money.py
      accounts.py
      transactions.py
      installments.py
      reconciliation/
        models.py
        normalizer.py
        matcher.py
        scoring.py
        transfers.py
        refunds.py
        residuals.py
        investments.py
        commit.py

    repositories/
      accounts.py
      transactions.py
      ingestion.py
      snapshots.py
      reconciliation.py
      audit.py

    services/
      expense_service.py
      snapshot_service.py
      statement_service.py
      investment_service.py
      dashboard_service.py
      gemini_service.py

  migrations/
  tests/
```

Rules:

```text
api       -> request/response only
services  -> workflow orchestration
domain    -> deterministic business rules
repository-> SQL/persistence
```

Do not put the target system back into one large `database.py`.

---

# 5. Technology Decisions

## Keep

```text
FastAPI
PostgreSQL / Supabase
Streamlit
Gemini
Docker
```

## Change

### Financial values

Use:

```text
Decimal
```

throughout backend domain code.

Never convert persisted financial values to `float`.

### Schema migrations

Use explicit versioned SQL migrations.

Acceptable:

```text
Alembic
or
ordered SQL migration files
```

For this small project, ordered SQL migrations are sufficient if they are:

```text
versioned
reviewable
repeatable
never executed implicitly against production on application import
```

Recommended:

```text
migrations/
  0001_extensions.sql
  0002_identity_accounts.sql
  0003_ingestion_transactions.sql
  ...
```

### DDL execution

Remove:

```text
database.init_db()
```

from application import/startup once target schema is active.

Schema creation is deployment work, not request-runtime work.

---

# 6. Phase 0 — Architecture Freeze and Test Safety

## Goal

Make the repository safe to start implementation.

No business behavior changes.

## Work

Create/commit:

```text
docs/architecture/
  README.md
  PHYSICAL_SCHEMA.md
  API_CONTRACT.md
  RECONCILIATION_ENGINE.md
  IMPLEMENTATION_PLAN.md
  TEST_PLAN.md
```

Update:

```text
PROJECT_CONTEXT.md
docs/legacy/README.md
docs/legacy/development_doc.md
docs/legacy/remediation_summary.md
```

Mark:

```text
TARGET_DOMAIN_MODEL.md = target business truth
docs/architecture/*    = target architecture contracts
docs/legacy/*          = legacy/current implementation reference
```

Create:

```text
.env.example
```

with explicit:

```text
ENVIRONMENT=development
DATABASE_URL=
DB_SCHEMA=
GEMINI_API_KEY=
```

Add test safety guard:

```text
tests must refuse to run when ENVIRONMENT=production
tests must refuse known production DB/schema
```

Disable/delete any integration test behavior that writes shared remote production-like tables by default.

## Acceptance

- architecture documents are committed;
- documentation hierarchy is explicit;
- running unit tests cannot touch production;
- no target schema has been created in production.

## Rollback

Documentation/config-only phase.

---

# 7. Phase 1 — Target Database Foundation

## Goal

Create the physical schema and persistence primitives without exposing new user features.

## Build

Implement migrations for:

```text
households
users
household_members
devices

accounts
account_state
account_aliases
categories

ingestion_requests

transactions
transaction_links

account_snapshots
credit_card_snapshots

investment_pnl_periods

installment_plans
installment_periods

reconciliation_batches
statement_lines
reconciliation_candidates

audit_events
```

Create:

```text
app/config.py
app/db.py
repositories/*
domain/money.py
```

`domain/money.py` owns:

```text
Decimal parsing
currency validation
minor-unit quantization
FX-rate validation
```

Seed only development/test data.

Do not migrate legacy rows.

## Important constraints

Implement:

```text
PK / FK
UNIQUE
CHECK
indexes
audit immutability
```

from `PHYSICAL_SCHEMA.md`.

Create one `account_state` row when account is created.

## Do not implement yet

```text
Gemini
new Expense API
Statement parsing
Dashboard migration
```

## Tests

Schema tests:

```text
invalid currency rejected
invalid account type rejected
cross-table FK works
duplicate active account name rejected
duplicate device idempotency key rejected
audit event cannot be updated/deleted
Decimal precision preserved
```

Repository tests:

```text
create/get account
create aliases/categories
lock account_state
transaction rollback
```

## Acceptance

- target schema can be created from zero with one command;
- schema can be recreated repeatedly in dev/test;
- all tests use isolated DB;
- no legacy table is required.

## Rollback

Drop target dev schema/database only.

Legacy runtime remains untouched.

---

# 8. Phase 2 — Core Ledger Domain

## Goal

Implement committed financial events and concurrency-safe account projections.

## Build

Domain operations:

```text
expense
cash_income
transfer
fee
refund
opening_balance
reconciliation_adjustment
soft delete / void
```

Implement:

```text
services/ledger_service.py
repositories/transactions.py
repositories/accounts.py
repositories/audit.py
```

or equivalent minimal service split.

Rules:

```text
transaction legs positive
asset state positive
credit-card debt negative

same-currency transfer:
  explicit from/to legs

cross-currency transfer:
  both actual legs required
  effective_fx_rate = from / to

fee:
  separate transaction

refund:
  separate transaction
  refund_of link

void:
  reverse account_state atomically
  retain transaction/audit history
```

Lock account-state rows in sorted UUID order for multi-account operations.

## Replace legacy concepts

Do not reuse:

```text
insert_single_transaction_in_tx()
apply_adjustment()
```

as target domain entry points.

Their useful concurrency ideas may be reimplemented cleanly.

## Tests

```text
expense updates asset account
expense increases credit-card debt
income
same-currency transfer
cross-currency transfer
missing cross-currency leg rejected
fee separate from transfer
partial refund
over-refund blocked
soft-delete projection reversal
concurrent writes no lost update
opposite concurrent transfers no deadlock
opening balance not reported as income
```

## Acceptance

Given only target tables:

```text
all basic ledger events produce correct account_state
all events are auditable
all writes are atomic
all financial calculations use Decimal
```

## Rollback

Target code can be disabled without affecting legacy runtime.

---

# 9. Phase 3 — Request Idempotency and Expense API

## Goal

Create the first production-ready target user path:

```text
iPhone Expense Shortcut
```

supporting:
- A. Normal one-off expense capture;
- B. Foreign-currency card expense with reference FX estimation;
- C. Installment plan schedule capture.

## Build

Implement:

```text
POST /api/v1/expenses

GET /api/v1/ingestion-requests/by-key/{key}

POST /api/v1/ingestion-requests/{id}/confirm
POST /api/v1/ingestion-requests/{id}/revise
POST /api/v1/ingestion-requests/{id}/reject
```

Create:

```text
api/deps.py (minimum device-token Bearer authentication)
services/expense_service.py
services/reference_fx_service.py
services/gemini_service.py
repositories/ingestion.py
repositories/installments.py
api/routes/expenses.py
api/routes/ingestion.py
```

Implement minimum device-token authentication required by the Expense API:
- Validate incoming `Authorization: Bearer <device-token>`;
- Lookup active device by token hash in `devices` table;
- Resolve owning user and household context;
- Update `last_seen_at`.
(Full browser user login and admin revocation workflows are completed in Phase 10).

Gemini prompt becomes **expense-only**.

Remove from new prompt:

```text
transfer intent classification
investment adjustment
multi-account balance adjustment
generic income detection
```

Account/category options come from DB, not `Literal`.

Add deterministic validation after AI output.

## Reference FX Capability

Add minimal reference-FX service required only for Product v1 presentation/estimation:
- current/T-1 reference FX lookup;
- Decimal calculation only;
- foreign-card settlement estimation (`from_amount` computed, `account_leg_status = 'estimated'`);
- updates `account_state` with estimated debt.

> **CRITICAL INVARIANT**: Reference FX MUST NEVER fabricate missing cross-currency transfer legs. Cross-currency transfers strictly require both real legs.

## Installment Expense Extraction & Plan Creation

Expense-only structured output includes `payment_mode`:
- `one_off`
- `installment`

If `installment`:
- extracts `total_amount`, `currency`, `total_periods`, `merchant`, `from_account`;
- high-confidence capture atomically creates `ingestion_request` + `installment_plan` (`status = 'pending_first_bill'`) + `installment_periods` schedules (`status = 'scheduled'`) + audit log;
- creates **NO transaction** and **NO `account_state` balance mutation**;
- returns replayable plan summary (`installment_plan_id`, `plan_status = 'pending_first_bill'`, `total_periods`, `display_summary`);
- low-confidence installment capture enters `needs_confirmation`.

> Note: Statement billing recognition (`recognize_installment`) transitions plan status (`pending_first_bill -> active`, and `active -> completed` on final period) in subsequent reconciliation/Statement phases and is NOT executed in Phase 3.

## Idempotency

Implement:

```text
(device_id, idempotency_key) UNIQUE
request_hash
stored response
needs_confirmation state
```

Same key/same payload:

```text
replay existing result
```

Same key/different payload:

```text
409 IDEMPOTENCY_KEY_REUSE
```

Idempotency applies to one-off expenses and installment-plan captures identically.

## Confidence

AI confidence alone is not the whole decision.

Force confirmation at least when:

```text
account unresolved
multiple account candidates
amount/currency unclear
category unresolved
deterministic validation conflict
```

## Image handling

Validate:

```text
base64
MIME
decoded size
image decode
```

Do not persist high-confidence screenshot after processing.

Pending image lifetime should be minimal and bounded.

## Tests

```text
expense high-confidence commit
foreign-card estimated settlement (account_leg_status = estimated, account_state reflects estimate)
installment plan creation (creates plan + schedules, zero transactions, zero balance mutation)
installment retry creates no duplicate plan
low-confidence produces no transaction or plan until confirmed
confirm commits once
confirm twice is idempotent
revise maintains same request
reject produces no financial write

same idempotency same body
same idempotency different body

server committed/client lost response -> status recovery
invalid image
invalid account
invalid category
Gemini timeout/dependency unavailable
```

## Acceptance

Target Expense API can run end-to-end independently of legacy `/api/record`, supporting one-off expenses, foreign-card estimation, and installment plan capture.

## Rollback

Shortcut still points to legacy `/api/record`; target endpoint may be disabled independently.

---

# 10. Phase 4 — Account / Category / Dashboard Read APIs

## Goal

Make Backend the owner of account configuration and reporting reads before moving Dashboard writes.

## Build

Implement:

```text
GET/POST/PATCH accounts
deactivate account

account aliases CRUD

categories CRUD

GET transactions
GET transaction

GET dashboard/overview
GET dashboard/cash-flow
GET dashboard/investments
GET dashboard/account-freshness

GET credit-cards/{id}/state
GET installments
```

Add:

```text
services/dashboard_service.py
```

Backend owns:

```text
asset/liability calculations
cash-flow semantics
FX presentation rules
credit-card state
investment P&L separation
```

No Dashboard-side accounting calculations should be considered authoritative after this phase.

## Tests

```text
transfers excluded from income/expense
refund reduces net expense correctly
reconciliation adjustment excluded
opening balance excluded
investment P&L excluded from cash income
credit-card debt sign correct
credit overpayment treated as asset
account filtering
multi-currency reporting
```

## Acceptance

Every number required by current Dashboard can be obtained from Backend APIs.

Dashboard itself may still temporarily use legacy DB until Phase 11.

---

# 11. Phase 5 — Snapshot Reconciliation

## Goal

Support the low-friction reconciliation path before building PDF Statements.

This is intentionally earlier than Statement parsing because real usage does not require monthly Statements.

## Build

Implement:

```text
POST /api/v1/accounts/{id}/snapshots (ordinary cash / savings / credit balance snapshots)

GET reconciliation batch
GET reconciliation preview
commit reconciliation
```

Build deterministic:

```text
ledger_balance_as_of()
residual calculation
ordinary <=200 CNY rule
```

For normal account:

```text
authoritative Snapshot
↓
residual
↓
<=200 -> ready with adjustment candidate
>200  -> needs_review
```

Create authoritative snapshot only at batch commit.

## Opening balance

Implement fresh-ledger initialization:

```text
household.ledger_start_date

first account observation
→ opening_balance / initial authoritative baseline
```

No historical migration.

## Tests

```text
first opening balance
snapshot exact match
small residual
large residual
historical as-of balance
concurrent transaction during preview
commit revalidation
commit twice
rollback entire batch
```

## Acceptance

A user can keep an account accurate using only:

```text
Expense Shortcut
+
occasional balance Snapshot/manual balance
```

No Statement is required.

---

# 12. Phase 6 — Reconciliation Matching Engine

## Goal

Implement the deterministic engine before PDF parsing.

## Build

Implement:

```text
domain/reconciliation/
  models.py
  normalizer.py
  matcher.py
  scoring.py
  transfers.py
  refunds.py
  residuals.py
  commit.py
```

Use synthetic `NormalizedStatementLine` fixtures first.

Implement:

```text
account hard gate
direction gate
±5-day window
exact amount evidence
original-currency evidence
merchant trigram similarity
type compatibility
score + margin
mutual-best matching
candidate assignment
```

Constants:

```text
AUTO_MATCH_SCORE = 80
AUTO_MATCH_MARGIN = 15
```

as documented, but keep them configuration constants.

## Transfer

Implement:

```text
match existing transfer first
counter-account evidence
same-currency two-side matching
cross-currency explicit two legs
unresolved destination -> review
```

## Refund

Implement:

```text
180-day original-expense lookup
partial refunds
refund remaining amount
```

## Installment

Implement:

```text
installment plans/schedules
recognize only billed period
```

## Replay safety

Test repeated normalized Statement batch even before PDF support.

## Tests

Use fixtures for all matrix cases from `RECONCILIATION_ENGINE.md`.

## Acceptance

Given deterministic normalized lines, engine produces stable expected:

```text
matches
candidates
review reasons
residual
```

No AI/PDF dependency is required to validate core matching correctness.

---

# 13. Phase 7 — Statement PDF Pipeline

## Goal

Add PDF as an evidence source on top of the already-tested reconciliation engine.

## Build

Implement:

```text
POST /api/v1/accounts/{id}/statements
GET reconciliation batch/status
GET statement lines
candidate accept/edit/reject
commit
```

Create:

```text
services/statement_service.py
services/statement_parser.py
api/routes/statements.py
api/routes/reconciliation.py
```

Parser:

```text
PDF
→ document metadata
→ normalized statement lines
```

User chooses account before upload.

Parser does not guess account.

## File lifecycle

```text
upload
↓
temporary file
↓
parse
↓
success: delete immediately

failure:
temporary retention <=24h
```

Password:

```text
memory/current job only
never DB
never logs
```

## Processing

Prefer simple execution first.

Do not introduce Celery/Redis unless real document processing latency requires it.

If async processing is needed, use the minimum supported job mechanism.

## Tests

```text
text PDF
password-required PDF
wrong password
parser invalid values
statement line normalization
same Statement uploaded twice
Statement with missing expense
Statement with ambiguous match
Statement with transfer
Statement with refund
Statement batch atomic rollback
PDF deleted after success
password not persisted
```

## Acceptance

Statement is an optional advanced reconciliation route.

Repeated upload creates no duplicate financial facts.

---

# 14. Phase 8 — Credit Card and Installment Completion

## Goal

Replace the legacy computed-credit-card approximation with first-class Statement state.

## Build

Use:

```text
credit_card_snapshots

statement_balance
remaining_statement_due
unbilled_balance
current_outstanding
```

Implement:

```text
foreign-card original amount matching
settlement amount enrichment
historical reporting FX freeze
repayment as transfer
```

Finalize installment behavior:

```text
first recognition when first billed
only current billed period becomes expense
future periods remain schedules
last period absorbs rounding
```

## Remove target dependency on legacy

Do not use:

```text
get_credit_card_statement_info()
```

as an authoritative target calculation.

It may remain temporarily for legacy UI only.

## Tests

```text
statement balance vs repayments
remaining due
unbilled
current outstanding

JPY original -> USD settlement
historical reporting FX freeze
later CNY repayment does not rewrite expense

12-installment schedule
first bill
next bill
rounding final period
```

## Acceptance

Credit-card Dashboard values come from target facts/snapshots, not inferred historical approximations alone.

---

# 15. Phase 9 — Investment Workflow

## Goal

Implement investment valuation without contaminating household cash income.

## Build

Implement:

```text
POST /api/v1/investment-accounts/{id}/snapshots
GET  /api/v1/investment-accounts/{id}/performance
account snapshot investment_valuation
investment_pnl_periods
contribution/withdrawal matching
```

Formula:

```text
P&L =
closing value
- opening value
- contributions
+ withdrawals
```

Investment Statement parser Product v1 extracts only:

```text
total asset value
clear deposits
clear withdrawals
```

Ignore:

```text
positions
trades
cost basis
```

## Ambiguity

If capital movement is unclear:

```text
needs_review
```

Never use automatic ±200 investment adjustment.

## Tests

```text
initial snapshot creates no prior P&L
100k -> 160k + 50k contribution = 10k P&L
withdrawal case
negative P&L
ambiguous capital movement
investment P&L absent from cash income
```

## Acceptance

Investment valuation updates net worth and investment analytics only.

---

# 16. Phase 10 — Authentication & Authorization Hardening

## Goal

Harden identity, browser user authentication, household authorization boundaries, and device management before Dashboard cutover.

Note: Schema tables (`users`, `household_members`, `devices`) are created in Phase 1; minimum device-token authentication for the Expense API is implemented in Phase 3.

## Build

Implement:

```text
api/auth/ (browser authentication provider integration)
auth_subject resolution -> User -> Household
Household member access control middleware
Device management & token revocation endpoints
Production auth security test suite
```

Dashboard authentication:

```text
external auth
→ auth_subject
→ user
→ household
```

Shortcut:

```text
Bearer device token (hashing and revocation verified)
```

Per-device revocation:
- Revoked devices receive `401 Unauthorized` with `DEVICE_REVOKED`
- Token plaintext is never logged, leaked, or returned after creation

Do not accept:

```text
recorded_by
user_id
household_id
```

from an untrusted Shortcut body as authorization truth. Resolve identity exclusively from the authenticated token context.

## Tests

```text
valid device token accepts request
revoked device token rejected immediately
user not household member rejected with 403
two users both see household data cleanly
device mapped to correct user and household
device token never returned or logged in plaintext
```

## Acceptance

All target REST endpoints reject unauthorized or cross-household requests; device tokens support immediate revocation.

---

# 17. Phase 11 — Dashboard Migration to Backend

## Goal

Remove Dashboard direct ownership of accounting/database logic and migrate it to Backend REST APIs using Phase 10 authenticated sessions.

## Build

Create Dashboard API client:

```text
ai-ledger-dashboard/
  api_client.py
```

Replace direct database queries:

```text
from database import ...
```

with Backend HTTP calls against `/api/v1/*`.

Migrate page-by-page:

```text
1. asset/liability overview
2. cash-flow/statistics
3. credit-card view
4. investment view
5. account/category management
6. snapshot/manual reconciliation
7. work queue
8. Statement upload/review
9. audit/history
10. transaction correction / void UI
```

Delete Dashboard-side:

```text
get_db_connection()
apply_adjustment()
get_credit_card_statement_info()
investment adjustment -> income remapping
direct SELECT/UPDATE/INSERT
```

Move all reporting and FX calculations to Backend.

## Keep

Streamlit presentation code may remain.

Do not rewrite UI technology unless required.

## Tests

Backend API tests remain authoritative.

Dashboard tests focus on:

```text
API response rendering
form submission
error display
review workflow navigation
```

## Acceptance

Dashboard container does not require:

```text
DATABASE_URL
```

It only needs Backend URL and authentication configuration.

Search repository:

```text
ai-ledger-dashboard
```

must contain zero direct PostgreSQL business access.

---

# 17.5. Phase 11.5 — Pre-production Deployment & Runtime Readiness

## Goal

Deploy the target system (`Dockerfile.target`) to an isolated, non-authoritative staging environment (`ENVIRONMENT=staging`), apply target migrations, bootstrap initial staging identity and accounts, and verify runtime readiness before Phase 12 real-device testing.

## Staging Architecture

```text
Target FastAPI Backend (Dockerfile.target -> app.main:app)
        ↓
Isolated Staging PostgreSQL Database / Schema (vibeledger_staging)
        ↓
Target Migrations (0001 -> 0009 with SHA256 checksums)
        ↓
Staging Bootstrap (bootstrap_staging.py, account_state.initialized_at=NULL)
        ↓
Staging Browser Auth (generate_staging_browser_token.py, HS256 HMAC signing)
        ↓
Staging iPhone Device Token (POST /api/v1/devices)
        ↓
Staging Dashboard (BACKEND_URL only, zero direct DB access)
```

## Key Deliverables

1. `ai-ledger-backend/Dockerfile.target` — Dedicated target application container definition booting `uvicorn app.main:app --port 7860`. (Legacy `ai-ledger-backend/Dockerfile` remains unchanged).
2. `ai-ledger-backend/app/config.py` — Support for `ENVIRONMENT=staging` (permits migrations, strictly restricts destructive cleanups to `ENVIRONMENT=test`).
3. `ai-ledger-backend/scripts/bootstrap_staging.py` & `staging_seed.example.json` — Idempotent staging data setup using natural keys and consistency verification.
4. `ai-ledger-backend/scripts/generate_staging_browser_token.py` — Staging-only HS256 HMAC-signed Browser JWT generation.
5. `docs/deployment/STAGING_DEPLOYMENT.md` — 15-step staging operational runbook.

## Acceptance Criteria

1. Target container boots `app.main:app`.
2. `GET /health` returns HTTP 200 with target service identity.
3. `GET /ready` returns HTTP 200 with `database=ok` AND `gemini=ok`.
4. Target migrations applied and checksums verified on `vibeledger_staging`.
5. Staging bootstrap executes idempotently and sets `account_state.initialized_at = NULL`.
6. Staging Browser JWT authenticates against `POST /api/v1/devices` and provisions a staging iPhone device token.
7. Provisioned device token authenticates against target `/api/v1/*` endpoints.
8. Dashboard connects to staging Backend without `DATABASE_URL`.
9. Zero production data or legacy production endpoints modified.

---

# 18. Phase 12 — iPhone Shortcut v2 Cutover

## Goal

Move the real Shortcut from legacy `/api/record` to target Expense API.

## Shortcut behavior

```text
start
↓
check local pending idempotency key
↓
GET request status
↓
resolve old request
↓
generate new key
↓
persist pending key
↓
take screenshot
↓
resize/compress
↓
POST /api/v1/expenses
↓
committed / needs_confirmation
```

Confirmation menu:

```text
Confirm
Supplement information
Cancel
```

Do not add:

```text
transfer selection
investment selection
accounting calculations
confidence threshold logic
```

Expense Shortcut remains expense-only.

Optional later shortcuts:

```text
Vibe Transfer
Vibe Snapshot
```

are separate entry points.

## Acceptance

Test real device:

```text
normal expense
low-confidence expense
network loss after commit
rerun recovery
confirm
revise
reject
```

---

# 18.5. Phase 12.5 — Account / Asset Model & Multi-Account Asset Capture

## Goal

Refactor the account model, add account risk-level classification, add semantic category descriptions, and introduce dedicated multi-account asset screenshot capture (`POST /api/v1/asset-captures`) with atomic multi-account reconciliation, before fresh production cutover.

## Scope & Gating

> **CRITICAL GATING**: Phase 13 (Production Fresh Cutover) **MUST remain strictly blocked** until Phase 12.5 implementation and staging acceptance are fully complete.

Phase 12.5 spans six sub-phases:

### 18.5.1 Phase 12.5A — Architecture Re-Freeze & Documentation (Current)
- Freeze updated `TARGET_DOMAIN_MODEL.md` (Account semantics: cash/savings/credit/investment, risk_level, Category description, Asset Capture, Risk distribution).
- Freeze `PHYSICAL_SCHEMA.md` (accounts table, categories table, ingestion_requests request_kind, multi-account locking order).
- Freeze `API_CONTRACT.md` (POST /api/v1/asset-captures, static Gemini transport, Dashboard risk distribution, Accounts & Categories API updates).
- Freeze `RECONCILIATION_ENGINE.md` (multi-account asset capture reconciliation, aggregate total cross-check, atomicity).
- Freeze `IMPLEMENTATION_PLAN.md` & `TEST_PLAN.md`.

### 18.5.2 Phase 12.5B — Database Schema Migration
- Migration `0010_asset_model_freeze.sql`:
  - `ALTER TABLE accounts ADD COLUMN risk_level TEXT;`
  - Add check constraints: `risk_level IN ('very_low', 'low', 'medium', 'high')`, `account_type <> 'credit' OR risk_level IS NULL`.
  - Add index `ix_accounts_household_risk`.
  - `ALTER TABLE categories ADD COLUMN description TEXT;`
  - Update `chk_ingestion_kind` on `ingestion_requests` to include `'asset_capture'`.
  - Execute `ALTER TABLE accounts DROP COLUMN institution;` to bring staging schema to target domain model.
  - **Zero-Downtime Deployment Sequencing Requirement**:
    - Runtime backend code must first remove all `accounts.institution` dependencies (entities, queries, inserts, serializers) as part of the same Phase 12.5 staging upgrade.
    - Deploy compatible backend revision first (or coordinate deployment and migration atomically), ensuring no running backend revision issues queries referencing `accounts.institution` after the column is dropped.
    - Execute `0010_asset_model_freeze.sql` to drop the column.

### 18.5.3 Phase 12.5C — Backend Domain, Repositories, & APIs
- Update Account domain entities, repositories, and API endpoints to validate and persist `risk_level`.
- Update Category domain entities, repositories, and API endpoints to persist `description`.
- Update Gemini service system prompt to receive category `name` and `description` for expense classification.
- Define static Gemini transport schemas `AssetObservationTransport` and `AssetCaptureExtractionTransport` (strictly compatible with Gemini Developer API, no dynamic dicts, no `additionalProperties`).
- Implement `POST /api/v1/asset-captures` endpoint:
  - Device bearer authentication.
  - Idempotency handling via `ingestion_requests` (`request_kind = 'asset_capture'`).
  - Gemini asset extraction invocation.
  - Deterministic aggregate total cross-check with exact quantized comparison:
    1. Quantize observations and displayed total to minor currency units;
    2. Sum quantized constituent balances;
    3. Exact equality = pass;
    4. Non-zero difference = `ASSET_TOTAL_MISMATCH` -> `ingestion_request.status = needs_confirmation`;
    5. Skip aggregate check if currencies differ (do not fabricate FX).
  - Canonical account resolution with ambiguity protection for generic aliases.
  - Multi-account atomic locking: sort affected canonical `account_id`s in ascending UUID order, acquire `account_state FOR UPDATE`, commit snapshots, per-account reconciliation batches (`source_request_id`), adjustments, and investment P&L in ONE DB transaction. Rollback all on failure.
  - Auto-commit for high-confidence/unambiguous matches; `needs_confirmation` for mismatches/ambiguities.
- Implement Polymorphic Ingestion Confirm and Draft endpoints:
  - `PATCH /api/v1/ingestion-requests/{request_id}/draft`: polymorphic by `request_kind`. If `asset_capture`, accepts `{"observations": [{"account_id": "uuid", "observed_balance": "...", "currency": "..."}]}` so Dashboard can correct unmapped accounts or OCR errors prior to confirmation.
  - `POST /api/v1/ingestion-requests/{request_id}/confirm`: bodyless `{}`. Dispatched by `request_kind`. If `asset_capture`, revalidates current draft, sorts affected account UUIDs, locks `account_state` rows in ascending UUID order, atomically commits all snapshots, per-account reconciliation batches, adjustments, and investment P&L in ONE single DB transaction. Returns multi-account `results` response (NOT expense `transaction_id`). Repeated confirm replays stored response.
  - `POST /api/v1/ingestion-requests/{request_id}/reject`: marks `ingestion_request` as `rejected`, committing zero financial facts.
- Clean up Single-Account Snapshot endpoint:
  - `POST /api/v1/accounts/{account_id}/snapshots` is strictly dedicated to known-account numeric/manual authoritative snapshot entry.
  - Remove all image-based request handling from this single-account endpoint; all screenshot recognition MUST use `POST /api/v1/asset-captures`.
- Enforce Status and Scoping Isolation:
  - `needs_confirmation` belongs to `ingestion_requests.status` only.
  - `needs_review` belongs to `reconciliation_batches.status` only.
  - One Asset Capture is 1 `ingestion_request` + 0..N account-scoped `reconciliation_batches` linked by `source_request_id`. No parent reconciliation batch table or `asset_capture_batches` table.

### 18.5.4 Phase 12.5D — Dashboard UI Enhancement
- Risk Distribution Chart and Breakdown Table:
  - Displays portfolio risk breakdown (`very_low`, `low`, `medium`, `high`, `NULL` as unclassified).
  - Strictly excludes all credit accounts.
  - Converts non-reporting currencies using Reference FX.
- Category Management UI: Add and edit category `description`.
- Asset Capture Review / Confirmation UI:
  - Review draft asset captures in `needs_confirmation`.
  - Resolve unmapped observations or correct OCR errors via `PATCH /api/v1/ingestion-requests/{id}/draft`.
  - Confirm via bodyless `POST /api/v1/ingestion-requests/{id}/confirm` or reject via `POST /reject`.

### 18.5.5 Phase 12.5E — Dedicated iOS Asset Capture Shortcut
- Dedicated `ios-shortcut-asset-1.0` Shortcut (distinct from Expense Shortcut).
- Captures bank/brokerage asset overview screenshot.
- Calls `POST /api/v1/asset-captures`.
- Displays confirmed balances or informs user to confirm on Dashboard.

### 18.5.6 Phase 12.5F — Automated Testing & Staging Acceptance
- Unit tests: schema invariants, risk_level rules, category descriptions, static Gemini asset transport schema, exact quantized aggregate cross-check math, risk distribution calculation.
- Integration tests: database migrations (including 0010 column drop), multi-account row locking and atomicity, deadlock avoidance, idempotency replay, polymorphic draft/confirm endpoints.
- Staging acceptance: deploy to staging (`ENVIRONMENT=staging`), execute real asset capture with dedicated Shortcut, verify atomic snapshot creation, reconciliation adjustments, and Dashboard risk distribution.

## Acceptance Criteria
1. Automated unit test suite passes 100%.
2. Integration test suite passes 100%.
3. Static Gemini asset extraction schema verified free of dynamic dicts and `additionalProperties`.
4. Multi-account asset capture commits all accounts atomically or rolls back completely.
5. Aggregate total mismatch prevents auto-commit and triggers `needs_confirmation`.
6. Credit accounts cannot have risk_level and are excluded from risk distribution.
7. Category descriptions successfully guide Gemini expense classification.

---

# 19. Phase 13 — Production Fresh Cutover

## Preconditions

All required phases passed:

```text
target schema
ledger domain
Expense API
snapshot reconciliation
Statement reconciliation
credit-card behavior
investment
Dashboard API migration
auth/device tokens
Phase 11.5 staging runtime readiness passed
Phase 12 real-device Shortcut acceptance passed
Phase 12.5 Account / Asset Model & Multi-Account Asset Capture passed
```

## Cutover

Because historical migration is not required:

```text
1. choose ledger_start_date
2. deploy target backend/schema
3. create household/users/devices/accounts/categories/aliases
4. establish opening balances / initial snapshots
5. deploy target Dashboard
6. switch iPhone Shortcut endpoint
7. verify health/readiness
8. begin new ledger
```

Keep a backup/export of the legacy DB for reference, but do not load it into the new ledger.

## Smoke test

```text
create one controlled expense
verify transaction
verify account_state
verify Dashboard
void controlled transaction
verify reversal
submit snapshot
verify reconciliation
```

## Rollback boundary

Before meaningful target transactions accumulate:

```text
Shortcut may be pointed back to legacy endpoint
Dashboard legacy deployment may be restored
```

After production target ledger is actively used:

```text
do not split writes between legacy and target systems
```

Rollback must restore the entire target service state consistently.

---

# 20. Phase 14 — Legacy Removal

Only after stable target operation.

Delete:

```text
legacy /api/record
legacy main.py models/prompts
legacy database.py ledger methods
legacy db_migration.py
legacy TABLE_SUFFIX logic
legacy test_idempotency.py remote-DB assumptions

ai-ledger-dashboard/database.py
Dashboard direct DB code
investment adjustment -> income mapping
legacy future installment calculations
```

Update:

```text
docs/legacy/development_doc.md
```

to archive legacy implementation history.

Do not delete architecture documents.

---

# 21. Suggested Phase Boundaries for Antigravity / Codex

Do **not** give an implementation Agent:

```text
"Implement IMPLEMENTATION_PLAN.md"
```

as one task.

Give exactly one Phase at a time.

Recommended first coding prompt:

```text
Implement Phase 0 and Phase 1 only.
Do not begin Phase 2.
```

After review:

```text
Implement Phase 2 only.
```

Each Agent run must report:

```text
files changed
schema/migrations added
behavior changed
tests added
tests executed
remaining TODO within current phase
whether acceptance criteria passed
```

No unrelated refactors.

---

# 22. Phase Dependency Graph

```text
Phase 0  Architecture/Test Safety
   ↓
Phase 1  Database Foundation
   ↓
Phase 2  Core Ledger Domain
   ↓
Phase 3  Expense + Idempotency
   ├──────────────┐
   ↓              ↓
Phase 4         Phase 5
Read APIs       Snapshot Reconciliation
                  ↓
                Phase 6
                Matching Engine
                  ↓
                Phase 7
                Statement Pipeline
                  ↓
                Phase 8
                Credit Card / Installments
                  ↓
                Phase 9
                Investment
   └──────────────┬─────────────┘
                  ↓
                Phase 10
                Auth Hardening
                   ↓
                 Phase 11
                 Dashboard Migration
                    ↓
                 Phase 11.5
                 Pre-production Staging & Readiness
                    ↓
                  Phase 12
                  Shortcut v2
                    ↓
                  Phase 12.5
                  Asset Model & Multi-Account Asset Capture
                    ↓
                 Phase 13
                 Production Cutover
                   ↓
                Phase 14
                Legacy Removal
```

Phase 4 may proceed in parallel with Phase 5 after the Core Ledger is stable.

---

# 23. Definition of Done for Every Phase

A phase is complete only when:

```text
code is committed
tests exist
tests pass
no production data was unintentionally modified
architecture contract remains satisfied
documentation updated if behavior changed
acceptance criteria are demonstrated
```

"Code written" is not completion.

---

# 24. Test Strategy Across Phases

Three layers:

## Unit

Pure deterministic domain tests:

```text
Decimal
transaction effects
matching score
refund limits
residual
investment P&L
installment rounding
```

Fast; no network.

## Database integration

Use isolated PostgreSQL:

```text
constraints
transactions
FOR UPDATE behavior
concurrency
rollback
replay safety
```

Never shared production.

## API integration

Test FastAPI routes against isolated database.

Gemini should normally be mocked with fixed structured responses.

Only a small optional manual/integration suite should call real Gemini.

---

# 25. CI Minimum

On every change:

```text
lint/import validation
unit tests
isolated DB integration tests
API tests
```

CI MUST NOT require:

```text
production DATABASE_URL
real Gemini API
real bank Statement
```

Use sanitized fixtures.

---

# 26. What Not to Refactor Early

Do not spend early phases on:

```text
Streamlit visual redesign
chart styling
new frontend framework
background-job framework
Redis
microservices
event bus
ORM migration for its own sake
investment positions
advanced FX accounting
```

These do not unlock the target accounting model.

---

# 27. Critical Stop Conditions

Stop implementation and fix architecture violation if any Phase starts doing one of these:

```text
pending AI draft inserted into transactions

Snapshot represented as generic adjustment only

investment valuation mapped into cash income

future installment periods inserted as committed future transactions

Dashboard writes PostgreSQL directly in target path

Statement parser modifies account state during parsing

cross-currency transfer invents a missing leg using public FX

reconciliation commit is partial

financial arithmetic uses float

new target business configuration is hard-coded in Literal
```

---

# 28. Recommended First Development Milestone

The first meaningful milestone is **not** Statement PDF support.

It is:

```text
fresh target database
+
correct core ledger
+
Expense API
+
request recovery/confirmation
+
manual/Snapshot reconciliation
```

At that point the system already supports the highest-frequency real usage:

```text
daily low-friction expense capture
+
occasional account-balance calibration
```

Statement and Investment features can then be added without destabilizing daily bookkeeping.

---

# 29. Final Delivery Sequence

Recommended execution sequence:

```text
M1 Foundation
  Phase 0-2

M2 Daily Bookkeeping
  Phase 3-5

M3 Deep Reconciliation
  Phase 6-8

M4 Investment
  Phase 9

M5 Client Migration
  Phase 10-12

M6 Cutover
  Phase 13-14
```

The project should remain runnable at every milestone.

---

# 30. Agent Rule

When implementing any Phase:

1. Read all architecture documents first.
2. Implement only the assigned Phase.
3. Do not preserve legacy behavior if it conflicts with target architecture.
4. Do not refactor unrelated code.
5. Do not modify production data.
6. Prefer deterministic business logic over LLM decisions.
7. Add tests before declaring acceptance.
8. If a technical detail is unspecified but does not alter business semantics, choose a reasonable implementation and document it.
9. Only escalate a question when it changes an approved business rule.
