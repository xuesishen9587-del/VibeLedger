# VibeLedger Project Context & Agent Handoff

> Current stage:  
> **Phases 0–11 implemented, verified, reviewed, and merged to `main`.**  
> **Phase 11.5 (Pre-production Deployment & Runtime Readiness) in progress.**  
> **Phase 12 (Real iPhone Shortcut v2 acceptance) ready to execute once Staging is deployed.**  
> **Phase 13 (Production Fresh Cutover) scheduled after Phase 12 verification.**

---

## 1. Project Goal

VibeLedger is a dedicated AI financial ledger for a two-person household:
- **Daily Ingestion**: iPhone Shortcut captures single-expense screenshots, which Gemini parses into structured financial records.
- **Authoritative Calibration**: Periodic bank/credit card Statement PDFs and Account Snapshots provide ground-truth reconciliation to prevent ledger drift.
- **Reporting Focus**: Holistic family balance sheet, cash flows, liabilities, and investment valuations. No couple AA, split-billing, or intra-household debt tracking.

---

## 2. Current Legacy Runtime Summary

The existing codebase (`ai-ledger-backend` and `ai-ledger-dashboard`) is a **legacy prototype**:

```text
iPhone Shortcut
  -> POST Base64 image + note + idempotency_key
  -> FastAPI (/api/record in main.py)
  -> Gemini 3.1 Flash-Lite
  -> PostgreSQL (accounts + transactions)

Streamlit Dashboard (app.py)
  -> Directly connects to PostgreSQL (psycopg2)
  -> Directly updates balances and writes reconciliation adjustments
```

Key legacy characteristics:
- Two-table schema (`accounts`, `transactions`) where `accounts.current_balance` is mutable scalar truth.
- Hard-coded Python `Literal` for accounts and categories.
- Overloaded `adjustment` transaction type (used for both manual balance calibration and investment gains).
- Installments prematurely generate $N$ future transactions and alter global balances.
- Database isolation via `TABLE_SUFFIX` and startup-time `database.init_db()` DDL.
- Legacy tests in `test_idempotency.py` assume a remote database environment.

---

## 3. Target Architecture Summary

The target system is a greenfield architectural design documented under `docs/architecture/`:

```text
iPhone Shortcut / Dashboard UI
        ↓ REST (/api/v1/* with Bearer token)
FastAPI Backend (app/)
  ├── api/           (REST routes, validation, serialization)
  ├── services/      (Orchestration, Gemini service, Statement parsing)
  ├── domain/        (Deterministic ledger rules, Decimal math, scoring)
  └── repositories/  (PostgreSQL persistence, row-level locks)
        ↓
PostgreSQL Database
  ├── Identity & Config : households, users, devices, accounts, categories, account_aliases
  ├── Idempotency       : ingestion_requests (device-scoped request lifecycle)
  ├── Durable Ledger    : transactions, transaction_links, audit_events (append-only)
  ├── Projections       : account_state (rebuildable derived cache, initialized_at baseline)
  ├── Authoritative Obs : account_snapshots, credit_card_snapshots
  ├── Derived Analytics : investment_pnl_periods (calculated P&L between valuation baselines)
  ├── Installments      : installment_plans, installment_periods (scheduled vs billed)
  └── Reconciliation    : reconciliation_batches, statement_lines, reconciliation_candidates
```

---

## 4. Authoritative Documentation Hierarchy

All future development must follow this strict reading and authority order:

1. [`TARGET_DOMAIN_MODEL.md`](./TARGET_DOMAIN_MODEL.md) — **Approved business & domain source of truth**
2. [`docs/architecture/PHYSICAL_SCHEMA.md`](./docs/architecture/PHYSICAL_SCHEMA.md) — **Target PostgreSQL persistence contract**
3. [`docs/architecture/API_CONTRACT.md`](./docs/architecture/API_CONTRACT.md) — **Target external REST API contract**
4. [`docs/architecture/RECONCILIATION_ENGINE.md`](./docs/architecture/RECONCILIATION_ENGINE.md) — **Target reconciliation & matching engine contract**
5. [`docs/architecture/IMPLEMENTATION_PLAN.md`](./docs/architecture/IMPLEMENTATION_PLAN.md) — **Implementation roadmap & phase sequencing**
6. [`docs/architecture/TEST_PLAN.md`](./docs/architecture/TEST_PLAN.md) — **Verification contract & test matrix**
7. [`PROJECT_CONTEXT.md`](./PROJECT_CONTEXT.md) — **This handoff document**
8. [`docs/legacy/*`](./docs/legacy/README.md) — **Historical reference only**

---

## 5. Locked Major Product Decisions

1. **No Legacy Data Migration**: Old production data will NOT be migrated. The target system starts with a fresh schema at `ledger_start_date` initialized with explicit opening balances (`account_state.initialized_at`).
2. **Statement is Optional Evidence**: Statement PDF upload is an optional high-accuracy tool, NOT a mandatory monthly closing ritual. An account can remain calibrated purely via periodic balance Snapshots.
3. **Draft Safety & Confirmation**: High-confidence Shortcut expenses auto-commit; low-confidence inputs enter `needs_confirmation` (`ingestion_requests`) and create NO transaction or balance mutation until user approval. Reconciliation ambiguities enter `needs_review` on the batch.
4. **Idempotency Ownership**: Client `idempotency_key` is owned at the `ingestion_requests` level per device, not directly on `transactions`.
5. **Foreign Credit Card Estimation**: Shortcut captures foreign card purchases with reference FX (`account_leg_status = 'estimated'`) and updates `account_state` estimated debt. Statement reconciliation replaces it with authoritative settlement, applies the exact delta to `account_state`, freezes historical reporting FX, and audits the transition. Cross-currency transfers strictly require both real legs and never use estimated FX.
6. **Fee Reporting**: `fee` is a distinct `transaction_type` requiring an expense category. Household expense reporting includes ordinary expense + fee - applicable refunds.
7. **Refund & Void Semantics**: Refunds are independent transactions linked via `refund_of`, never deletions of original expenses. Voiding / soft delete (`status = 'voided'` $\iff$ `deleted_at IS NOT NULL` + `delete_reason`) atomically reverses `account_state` projections exactly once and preserves audit trails.
8. **Installment Schedules**: Purchasing an installment plan creates schedule records only; only the current billed period becomes an expense upon Statement arrival (`recognize_installment`). No future transactions exist in advance.
9. **Reconciliation Thresholds**: Unexplained residuals on ordinary accounts $\le 200\text{ CNY}$ may auto-generate `reconciliation_adjustment`; residuals $>200\text{ CNY}$ trigger `needs_review`. Investment accounts NEVER use the $200\text{ CNY}$ threshold.
10. **Investment Valuation**: Market gains/losses (`investment_pnl_periods`) update net worth and investment analytics, but are strictly excluded from household `cash_income`. Pending reconciliation calculations remain in candidate payload until atomic commit.
11. **Historical Correction Flow**: Statement-confirmed transactions can only be corrected through an explicit two-step preview/commit API with optimistic concurrency control (`row_version`), updating `account_state` deltas and writing append-only audit events without altering raw Statement evidence.
12. **Document Privacy**: Statement PDFs are deleted immediately upon successful parsing (max 24h retention on failure); PDF passwords are kept in memory only and never persisted.
13. **Backend Exclusivity**: Dashboard is a pure UI client consuming Backend REST APIs; it never holds direct database credentials or executes accounting business logic.

---

## 6. Current Next Steps

1. **Phase 11.5 (Pre-production Deployment & Runtime Readiness)**: Setup isolated staging environment (`ENVIRONMENT=staging`, `vibeledger_staging`), execute target migrations, bootstrap initial staging identity and accounts via `bootstrap_staging.py`, generate staging HS256 Browser JWT, verify `/health` and `/ready` (`database=ok`, `gemini=ok`), and provision staging iPhone device token.
2. **Phase 12 (iPhone Shortcut v2 Cutover)**: Test real-device Shortcut captures, draft confirmations, and recovery workflows against live staging HTTPS backend.
3. **Phase 13 (Production Fresh Cutover)**: Deploy target backend and database to production, establish real opening balances, provision production devices, switch daily Shortcut, and archive legacy systems.
