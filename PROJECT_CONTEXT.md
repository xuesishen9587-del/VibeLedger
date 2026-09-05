# VibeLedger Project Context & Agent Handoff

> Current stage:
> **Phases 0–11 implemented, verified, reviewed, and merged to `main`.**
> **Phase 11.5 (Pre-production Deployment & Runtime Readiness) staging runtime accepted.**
> **Phase 12 (Real iPhone Shortcut v2 acceptance) completed in staging.**
> **Phase 12.5 (Account / Asset Model Architecture Re-Freeze & Implementation) in progress.**
> **Phase 13 (Production Fresh Cutover) strictly blocked until Phase 12.5 passes.**

---

## 1. Project Goal

VibeLedger is a dedicated AI financial ledger for a two-person household:
- **Daily Ingestion**: iPhone Shortcut captures single-expense screenshots, which Gemini parses into structured financial records.
- **Asset Calibration**: Periodic bank/credit card Statement PDFs, Single Snapshots, and Multi-Account Asset Overview Captures provide ground-truth calibration to prevent ledger drift.
- **Reporting Focus**: Holistic family balance sheet, cash flows, liabilities, risk distribution, and investment valuations. No couple AA, split-billing, or intra-household debt tracking.

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
14. **Account Semantics & Risk Allocation**: `account_type` is strictly `cash`, `savings`, `credit`, `investment`. No `institution` entity or column is maintained; single institutions are modeled as multiple independent accounts. `asset_class`, `liquidity_level`, and account hierarchies are prohibited. `Account.risk_level` is nullable user metadata; credit accounts MUST have `risk_level = NULL` and are strictly excluded from risk distribution. `Category.description` carries semantic classification rules; category `priority` is prohibited.
15. **Multi-Account Asset Capture**: `POST /api/v1/asset-captures` is a dedicated Product v1 business intent for extracting balances across multiple accounts from a single banking/investment screenshot. Gemini extraction uses static Pydantic schemas without dynamic dicts or `additionalProperties`. Displayed aggregate totals are cross-check only and NEVER create a snapshot (exact quantized comparison; `ASSET_TOTAL_MISMATCH` on non-zero discrepancy). One Asset Capture produces 1 `ingestion_request` (`needs_confirmation` on ambiguity) + 0..N account-scoped `reconciliation_batches` (`needs_review`). Dashboard edits draft via `PATCH /draft` (`observations: [{account_id, observed_balance, currency}]`) and confirms via bodyless `POST /confirm` (replaying stored Asset Capture response on repeated calls). Persistence locks affected `account_state` rows in ascending UUID order and atomically writes snapshots, adjustments, and investment P&L in a single DB transaction (ALL OR NOTHING).

---

## 6. Current Next Steps

1. **Phase 12.5 (Account / Asset Model & Multi-Account Asset Capture)**:
   - 12.5A: Architecture re-freeze and documentation (Current).
   - 12.5B: Schema migration (`0010_asset_model_freeze.sql`: `risk_level`, `description`, `asset_capture` request_kind, and `DROP COLUMN institution` with backend deployment sequencing).
   - 12.5C: Backend domain, repositories, and APIs (`Account` risk_level, `Category` description, `POST /api/v1/asset-captures`, static Gemini transport, polymorphic `PATCH /draft` & bodyless `POST /confirm`, dedicated single-account snapshot endpoint cleanup).
   - 12.5D: Dashboard UI (Risk distribution chart/table, category descriptions, asset capture review & draft correction).
   - 12.5E: Dedicated iOS Asset Capture Shortcut.
   - 12.5F: Automated test matrix and staging acceptance.
2. **Phase 13 (Production Fresh Cutover)**: Deploy target backend and database to production, establish real opening balances, provision production devices, switch daily Shortcut, and archive legacy systems. **Strictly blocked until Phase 12.5 passes.**
