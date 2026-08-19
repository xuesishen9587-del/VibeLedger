# VibeLedger Target Architecture Documentation

> Status: **Frozen Target Architecture**  
> Scope: **Product v1 / MVP**

This directory contains the authoritative technical architecture specifications, contracts, implementation sequences, and verification plans for the VibeLedger target system.

---

## 1. Document Authority Hierarchy

When resolving technical or business questions, use the following strict authority order:

```text
TARGET_DOMAIN_MODEL.md
    = approved business/domain truth (root)

docs/architecture/PHYSICAL_SCHEMA.md
    = target PostgreSQL persistence contract

docs/architecture/API_CONTRACT.md
    = target external REST API contract

docs/architecture/RECONCILIATION_ENGINE.md
    = target reconciliation & matching engine contract

docs/architecture/IMPLEMENTATION_PLAN.md
    = implementation roadmap and phase sequencing

docs/architecture/TEST_PLAN.md
    = verification contract & test matrix

PROJECT_CONTEXT.md
    = concise current-state / Agent handoff index

docs/legacy/*
    = historical/current-old implementation reference only
```

If legacy documentation or existing prototype code conflicts with target architecture, **target architecture wins**.

---

## 2. Architecture Document Map

| Document | Purpose | Primary Audience |
|---|---|---|
| [`TARGET_DOMAIN_MODEL.md`](../../TARGET_DOMAIN_MODEL.md) | Defines core business concepts, domain entities (`Account`, `Transaction`, `Snapshot`, `Reconciliation`), date rules, currency principles, and out-of-scope boundaries. | Domain logic, product invariants |
| [`PHYSICAL_SCHEMA.md`](./PHYSICAL_SCHEMA.md) | Defines the target PostgreSQL tables, UUID PKs, exact `NUMERIC(20,6)` types, foreign keys, check constraints, row-level locking strategies, derived projection (`account_state`), and append-only audit trail (`audit_events`). | Database engineering, persistence repositories |
| [`API_CONTRACT.md`](./API_CONTRACT.md) | Defines public `/api/v1` REST endpoints, decimal string JSON serialization, device bearer authentication, draft confirmation flow, and error envelopes. | API layer, Shortcut integration, Dashboard client |
| [`RECONCILIATION_ENGINE.md`](./RECONCILIATION_ENGINE.md) | Defines the deterministic matching algorithm, scoring gates, fuzzy merchant similarity, refund lookbacks, installment recognition, residual thresholds, and atomic batch commit algorithm. | Matching engine, reconciliation services |
| [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) | Defines the step-by-step strangler migration from Phase 0 to Phase 14, isolating development without touching legacy production data. | Agent task planning, execution sequencing |
| [`TEST_PLAN.md`](./TEST_PLAN.md) | Defines the multi-tiered test pyramid (Unit, DB Integration, API Integration, E2E), permanent regression invariants, and sandbox safety rules. | QA, test automation, phase acceptance |

---

## 3. Canonical Terminology & Glossary

### 3.1 Scope vs. Implementation Sequencing
- **`Product v1` / `MVP`**: Refers to the complete functional product scope defined by the frozen target architecture.
- **`Implementation Phase 0`, `Implementation Phase 1`, ...**: Refers strictly to the sequential technical milestones in [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md). Standalone "Phase 1" must never be used to ambiguously refer to product scope.

### 3.2 Date Fields
- **`occurred_on`** (`DATE`, required): Authoritative business/reporting date when the economic event occurred. Used for monthly budgeting and reports.
- **`occurred_at`** (`TIMESTAMPTZ`, optional): Exact timestamp when available (e.g., from digital receipts).
- **`posted_on`** (`DATE`, optional): Bank/Statement clearing date. Used for audit trail and reconciliation matching only, never for overriding reporting month.

### 3.3 Transaction Types vs Statement Line Types
- **`cash_income`**: Canonical transaction type for actual cash inflows into household accounts (salary, interest, reimbursements, gift money).
- **`income`**: May exist only at the `statement_lines.line_type` raw extraction layer. A Statement line of type `income` transforms into a `cash_income` transaction upon confirmation.
- **`reconciliation_adjustment`**: Automatic or manual adjustment applied to an ordinary account during reconciliation when residual $\le 200\text{ CNY}$. Modifies balance projection; excluded from income/expense statistics.
- **`investment_pnl`**: Non-cash market valuation change derived from `account_snapshots`. Persisted in `investment_pnl_periods`; tracked in net worth but strictly excluded from cash income.
- **`opening_balance`**: Initial ledger baseline transaction at `ledger_start_date`. Excluded from income and expense metrics.

### 3.4 Workflow Statuses
- **`needs_confirmation`**: Used for **ingestion requests** (e.g. Shortcut captures with low confidence, ambiguous accounts, or validation warnings) that require user approval before financial commit.
- **`needs_review`**: Used for **reconciliation batches and candidates** where automated matching is ambiguous, capital flow is unclear, or residual exceeds threshold.

### 3.5 Candidate Types (Reconciliation Enum)
The canonical set of reconciliation candidate types across all architecture contracts is:
1. `match` — Link Statement line to an existing committed transaction.
2. `create_transaction` — Propose creating a missing ordinary transaction (`expense`, `cash_income`, `fee`).
3. `create_transfer` — Propose creating an internal transfer between two household accounts.
4. `refund` — Propose creating a refund transaction linked via `refund_of` to an earlier expense.
5. `adjustment` — Propose an ordinary account `reconciliation_adjustment` ($\le 200\text{ CNY}$).
6. `snapshot` — Propose saving an authoritative account balance snapshot.
7. `investment_pnl` — Propose calculating and persisting investment P&L for a period.
8. `recognize_installment` — Propose recognizing a scheduled installment period as a billed expense.

### 3.6 Statement Match Statuses
The canonical match statuses on `statement_lines` are:
- `unmatched` — Initial state; line has not yet been processed by the matcher.
- `matched` — High-confidence unique match with an existing transaction.
- `new_candidate` — High-confidence proposal to create a new transaction/transfer/refund/installment.
- `ambiguous` — Multiple matches or unclear semantics requiring human review.
- `ignored` — Line intentionally skipped from ledger impact (e.g., non-household movement).

### 3.7 Foreign Credit Card Estimation & Account Leg Status
- **`account_leg_status = 'estimated'`**: Set at Shortcut ingestion time for foreign card expenses (`original_currency <> card.currency`) where settlement leg is estimated via reference FX. Updates `account_state` estimated debt.
- **`account_leg_status = 'authoritative'`**: Set upon Statement reconciliation atomic commit, replacing the estimate with actual settlement amount, applying the balance delta to `account_state`, and freezing historical reporting FX.
- **Transfer Invariant**: Cross-currency internal transfers strictly require both actual settlement legs and NEVER use estimated FX.

### 3.8 Fee & Category Invariants
- **`fee`**: Distinct transaction type representing household cash outflow; requires an active expense-type category (`category_id`).
- **Reporting Formula**: $\text{Household Expense} = \text{ordinary expense} + \text{fee} - \text{applicable refunds}$.

### 3.9 Void & Soft Delete Lifecycle
- **`status = 'committed'`** $\iff$ `deleted_at IS NULL` AND `delete_reason IS NULL`
- **`status = 'voided'`** $\iff$ `deleted_at IS NOT NULL` AND `delete_reason IS NOT NULL`
- Soft delete and void are the identical financial lifecycle operation; voiding atomically reverses `account_state` projections exactly once and preserves audit trails.
