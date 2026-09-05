# VibeLedger Physical PostgreSQL Schema

> Status: **Frozen Target Physical Schema (Final consistency review complete)**
>
> Authority: `TARGET_DOMAIN_MODEL.md` is the business source of truth. This document translates it into the target PostgreSQL persistence model.
>
> Scope: greenfield target schema. **No legacy production-data compatibility is required.**
>
> Core rule: **Account metadata, financial Transactions, authoritative Snapshots, and Reconciliation workflow are separate persistence concerns.**

---

## 1. Physical Design Principles

### 1.1 Source of truth vs projection

The following tables contain durable business facts:

- `transactions`
- `transaction_links`
- `account_snapshots`
- `credit_card_snapshots`
- `investment_pnl_periods`
- `installment_plans`
- `installment_periods`
- `reconciliation_batches`
- `statement_lines`
- `reconciliation_candidates`
- `audit_events`

`account_state` is a **derived current-state projection** for fast reads and concurrency-safe balance updates. It MUST be rebuildable from durable ledger facts and authoritative snapshots.

### 1.2 IDs

Use UUID primary keys for all business entities:

```sql
UUID PRIMARY KEY DEFAULT gen_random_uuid()
```

`audit_events.id` uses `BIGINT GENERATED ALWAYS AS IDENTITY` for ordered append-only audit history.

### 1.3 Time

Use:

- `DATE` for business/reporting dates;
- `TIMESTAMPTZ` for real timestamps;
- never use timezone-naive `TIMESTAMP`.

For ordinary expense reporting:

```text
occurred_on = authoritative reporting date
posted_on   = bank posting date, matching/audit only
```

Optional `occurred_at` preserves an exact timestamp when available.

### 1.4 Money and FX

PostgreSQL `MONEY` MUST NOT be used.

Canonical types:

```text
amount / balance / P&L / residual  NUMERIC(20,6)
FX rate                            NUMERIC(24,12)
confidence                         NUMERIC(5,4)
currency                           CHAR(3)
```

Application mapping:

```text
PostgreSQL NUMERIC <-> Python Decimal
```

`float` MUST NOT be used for persisted financial calculations.

Currency codes MUST be uppercase 3-letter codes:

```sql
CHECK (currency ~ '^[A-Z]{3}$')
```

### 1.5 Sign convention

Transaction leg amounts are always **positive**:

```text
from_amount > 0
to_amount   > 0
original_amount > 0
```

Direction comes from `from_account_id` / `to_account_id`.

Signed values are used only where the sign itself has business meaning:

```text
account_state.ledger_balance
account_snapshots.balance
investment_pnl_periods.pnl_amount
reconciliation_batches.residual_amount
reconciliation_batches.adjustment_amount
```

Account-state convention:

```text
asset balance       positive
credit-card debt    negative
credit overpayment  positive
```

### 1.6 FX convention

For a cross-currency transfer:

```text
effective_fx_rate = from_amount / to_amount
```

Meaning: units of `from_currency` paid per 1 unit of `to_currency`.

Example:

```text
7250 CNY -> 1000 USD
effective_fx_rate = 7.25 CNY per USD
```

---

## 2. Required PostgreSQL Extensions

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;
```

---

# 3. Household and Identity

## 3.1 `households`

Lightweight security/ownership root. Product v1 normally contains one household.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `name` | TEXT | NOT NULL |
| `reporting_currency` | CHAR(3) | NOT NULL default `CNY` |
| `ledger_start_date` | DATE | NOT NULL |
| `status` | TEXT | NOT NULL default `active`; `active / archived` |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

Checks:

```sql
CHECK (reporting_currency ~ '^[A-Z]{3}$');
CHECK (status IN ('active', 'archived'));
```

---

## 3.2 `users`

Application users. Authentication SHOULD be delegated to an external identity provider; do not store raw passwords here.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `auth_subject` | TEXT | NOT NULL UNIQUE |
| `email` | CITEXT | nullable, UNIQUE when present |
| `display_name` | TEXT | NOT NULL |
| `default_currency` | CHAR(3) | NOT NULL default `CNY` |
| `status` | TEXT | NOT NULL default `active`; `active / disabled` |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

Checks:

```sql
CHECK (default_currency ~ '^[A-Z]{3}$');
CHECK (status IN ('active', 'disabled'));
```

---

## 3.3 `household_members`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `household_id` | UUID | FK -> households, NOT NULL |
| `user_id` | UUID | FK -> users, NOT NULL |
| `role` | TEXT | NOT NULL default `member`; `owner / member` |
| `joined_at` | TIMESTAMPTZ | NOT NULL default now() |

```sql
PRIMARY KEY (household_id, user_id);
CHECK (role IN ('owner', 'member'));
CREATE INDEX ix_household_members_user ON household_members (user_id);
```

---

## 3.4 `devices`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `user_id` | UUID | FK -> users, NOT NULL |
| `device_name` | TEXT | NOT NULL |
| `platform` | TEXT | NOT NULL; Product v1 `ios_shortcuts` |
| `token_hash` | BYTEA | NOT NULL UNIQUE |
| `status` | TEXT | NOT NULL default `active`; `active / revoked` |
| `client_version` | TEXT | nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `last_seen_at` | TIMESTAMPTZ | nullable |
| `revoked_at` | TIMESTAMPTZ | nullable |

Checks:

```sql
CHECK (status IN ('active', 'revoked'));
CHECK (
  (status = 'active' AND revoked_at IS NULL)
  OR
  (status = 'revoked' AND revoked_at IS NOT NULL)
);
```

Raw API tokens MUST never be persisted.

---

# 4. Accounts and Configuration

## 4.1 `accounts`

Account metadata only.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `name` | TEXT | NOT NULL |
| `account_type` | TEXT | NOT NULL; `cash / savings / credit / investment` |
| `currency` | CHAR(3) | NOT NULL |
| `risk_level` | TEXT | nullable; `very_low / low / medium / high / NULL` |
| `owner_user_id` | UUID | FK -> users, nullable |
| `linked_cash_account_id` | UUID | self FK -> accounts, nullable |
| `billing_day` | SMALLINT | nullable, 1..31 |
| `due_day` | SMALLINT | nullable, 1..31 |
| `status` | TEXT | NOT NULL default `active`; `active / inactive` |
| `row_version` | BIGINT | NOT NULL default 0 |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

> [!IMPORTANT]
> **Migration 0010 & Column Removal**:
> The target domain model has no `Account.institution`. Phase 12.5 migration `0010_asset_model_freeze.sql` is responsible for bringing the existing staging schema to this target by executing the equivalent of:
> ```sql
> ALTER TABLE accounts DROP COLUMN institution;
> ```
> The final migrated schema after 0010 MUST NOT contain `accounts.institution`.
>
> **Zero-Downtime Deployment Sequencing Requirement**:
> 1. Runtime backend code must first remove all references and dependencies on `accounts.institution` (entities, queries, inserts, serializers) as part of the same Phase 12.5 staging upgrade.
> 2. Deploy the compatible backend revision first (or coordinate deployment and migration atomically), ensuring no running backend revision issues queries selecting or inserting `accounts.institution` after the column has been dropped.
> 3. Execute `0010_asset_model_freeze.sql` to drop the column.
> A single real institution/platform is modeled as multiple independent Accounts (e.g. `ABC_Debit`, `ABC_Term`, `ABC_Wealth`). No `institution` entity or column is maintained in the target schema.

Checks:

```sql
CHECK (account_type IN ('cash', 'savings', 'credit', 'investment'));
CHECK (currency ~ '^[A-Z]{3}$');
CHECK (risk_level IS NULL OR risk_level IN ('very_low', 'low', 'medium', 'high'));
CHECK (account_type <> 'credit' OR risk_level IS NULL);
CHECK (billing_day IS NULL OR billing_day BETWEEN 1 AND 31);
CHECK (due_day IS NULL OR due_day BETWEEN 1 AND 31);
CHECK (status IN ('active', 'inactive'));
CHECK (linked_cash_account_id IS NULL OR linked_cash_account_id <> id);
CHECK (
  account_type = 'credit'
  OR (billing_day IS NULL AND due_day IS NULL)
);
```

Indexes:

```sql
CREATE INDEX ix_accounts_household_type
ON accounts (household_id, account_type, status);

CREATE INDEX ix_accounts_household_risk
ON accounts (household_id, risk_level)
WHERE status = 'active';

CREATE INDEX ix_accounts_owner
ON accounts (owner_user_id);

CREATE INDEX ix_accounts_linked_cash
ON accounts (linked_cash_account_id);

CREATE UNIQUE INDEX uq_accounts_active_name
ON accounts (household_id, lower(name))
WHERE status = 'active';
```

Service invariants:

- owner must belong to the same household;
- linked cash account must belong to same household;
- linked account SHOULD be `cash`;
- account currency becomes immutable once financial history exists.

---

## 4.2 `account_state`

Current derived projection. **Not source of truth.**

| Column | Type | Constraints / Meaning |
|---|---|---|
| `account_id` | UUID | PK, FK -> accounts |
| `ledger_balance` | NUMERIC(20,6) | NOT NULL default 0 |
| `initialized_at` | TIMESTAMPTZ | nullable; set when opening baseline is established |
| `last_transaction_at` | TIMESTAMPTZ | nullable |
| `last_authoritative_snapshot_at` | TIMESTAMPTZ | nullable |
| `row_version` | BIGINT | NOT NULL default 0 |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

Rules:

- one row per account;
- `initialized_at = NULL` represents an uninitialized technical default state (0 balance is not an observed fact);
- `initialized_at IS NOT NULL` indicates the account has been formally initialized with an `opening_balance`, authoritative Snapshot, or first Statement baseline;
- every committed financial mutation locks this row first;
- balance is denominated in `accounts.currency`;
- table must be rebuildable from durable facts.

---

## 4.3 `account_aliases`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `account_id` | UUID | FK -> accounts, NOT NULL |
| `alias_text` | TEXT | NOT NULL |
| `normalized_alias` | TEXT | NOT NULL |
| `status` | TEXT | NOT NULL default `active`; `active / inactive` |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `deleted_at` | TIMESTAMPTZ | nullable |

```sql
CHECK (status IN ('active', 'inactive'));

CREATE UNIQUE INDEX uq_account_alias
ON account_aliases (account_id, normalized_alias)
WHERE deleted_at IS NULL;

CREATE INDEX ix_account_alias_trgm
ON account_aliases
USING GIN (normalized_alias gin_trgm_ops)
WHERE deleted_at IS NULL;
```

Do not enforce global alias uniqueness. Ambiguous aliases are valid and must cause confirmation.

---

## 4.4 `categories`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `name` | TEXT | NOT NULL |
| `category_type` | TEXT | NOT NULL; `expense / income` |
| `description` | TEXT | nullable; semantic classification policy |
| `status` | TEXT | NOT NULL default `active`; `active / inactive` |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

> [!NOTE]
> **Product v1 Expense Category Taxonomy**:
> Product v1 freezes exactly 14 canonical active Expense categories (`Grocery`, `Dine`, `Child`, `Home & Utilities`, `Digital & Gadgets`, `Clothing`, `Beauty`, `Transportation`, `Health`, `Education`, `Gift & Socials`, `Parents`, `Fun & Games`, `Trips & Occasions`), initialized and seeded via migration or bootstrap with canonical descriptions.
> Arbitrary creation, renaming, or deactivation of Expense categories is not supported in Product v1.
> `description` provides semantic classification guidelines for AI (e.g. child spending policy). No `priority` column is added.
> Income categories remain customizable.

```sql
CHECK (category_type IN ('expense', 'income'));
CHECK (status IN ('active', 'inactive'));

CREATE UNIQUE INDEX uq_categories_active
ON categories (household_id, category_type, lower(name))
WHERE status = 'active';
```

---

# 5. Client Request / Idempotency State

## 5.1 `ingestion_requests`

Request-level idempotency belongs here, not on transaction rows.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `device_id` | UUID | FK -> devices, NOT NULL |
| `idempotency_key` | TEXT | NOT NULL |
| `request_kind` | TEXT | NOT NULL; `expense / transfer / snapshot / asset_capture` |
| `request_hash` | BYTEA | NOT NULL |
| `status` | TEXT | NOT NULL default `received`; see below |
| `captured_at` | TIMESTAMPTZ | nullable |
| `client_version` | TEXT | nullable |
| `draft_payload` | JSONB | nullable |
| `response_payload` | JSONB | nullable |
| `failure_code` | TEXT | nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |
| `committed_at` | TIMESTAMPTZ | nullable |

Status values:

```text
received
processing
needs_confirmation
committed
rejected
failed
```

Constraints:

```sql
UNIQUE (device_id, idempotency_key);

CHECK (length(idempotency_key) BETWEEN 8 AND 200);
CHECK (request_kind IN ('expense', 'transfer', 'snapshot', 'asset_capture'));
CHECK (status IN (
  'received',
  'processing',
  'needs_confirmation',
  'committed',
  'rejected',
  'failed'
));
```

Indexes:

```sql
CREATE INDEX ix_ingestion_device_status
ON ingestion_requests (device_id, status);

CREATE INDEX ix_ingestion_status_updated
ON ingestion_requests (status, updated_at);
```

Rule:

- same key + same `request_hash` -> return stored state/response;
- same key + different hash -> reject as `IDEMPOTENCY_KEY_REUSE`.

Multi-Account Asset Capture Workflow Rules:
- A single Asset Capture produces exactly 1 `ingestion_requests` row (`request_kind = 'asset_capture'`).
- `needs_confirmation` is an `ingestion_requests.status` value only (indicating client/AI draft awaiting user confirmation).
- While in `needs_confirmation`, zero financial facts (snapshots, transactions, adjustments, investment P&L) are written or partially committed.
- The `ingestion_request` serves as the overall multi-account workflow and grouping boundary.
- Once confirmed, all associated account-scoped `reconciliation_batches` and the `ingestion_request` transition to `committed` within the same single database transaction.
- If rejected, `ingestion_requests.status` becomes `rejected`, leaving zero financial facts.

---

# 6. Financial Transactions

## 6.1 `transactions`

Committed financial/economic events only. Pending drafts never enter this table.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `transaction_type` | TEXT | NOT NULL; see below |
| `occurred_on` | DATE | NOT NULL; report attribution date |
| `occurred_at` | TIMESTAMPTZ | nullable |
| `posted_on` | DATE | nullable |
| `from_account_id` | UUID | FK -> accounts, nullable |
| `to_account_id` | UUID | FK -> accounts, nullable |
| `original_amount` | NUMERIC(20,6) | NOT NULL, >0 |
| `original_currency` | CHAR(3) | NOT NULL |
| `from_amount` | NUMERIC(20,6) | nullable, >0 |
| `from_currency` | CHAR(3) | nullable |
| `to_amount` | NUMERIC(20,6) | nullable, >0 |
| `to_currency` | CHAR(3) | nullable |
| `effective_fx_rate` | NUMERIC(24,12) | nullable, >0 |
| `account_leg_status` | TEXT | nullable; `estimated / authoritative` |
| `reporting_amount` | NUMERIC(20,6) | nullable; frozen reporting value |
| `reporting_currency` | CHAR(3) | nullable |
| `reporting_fx_rate` | NUMERIC(24,12) | nullable |
| `reporting_fx_locked_at` | TIMESTAMPTZ | nullable |
| `category_id` | UUID | FK -> categories, nullable |
| `merchant` | TEXT | nullable |
| `merchant_normalized` | TEXT | nullable |
| `remarks` | TEXT | nullable |
| `source` | TEXT | NOT NULL; see below |
| `status` | TEXT | NOT NULL default `committed`; `committed / voided` |
| `verification_status` | TEXT | NOT NULL default `unverified`; see below |
| `confidence` | NUMERIC(5,4) | nullable |
| `source_request_id` | UUID | FK -> ingestion_requests, nullable |
| `statement_batch_id` | UUID | FK -> reconciliation_batches, nullable |
| `created_by_user_id` | UUID | FK -> users, nullable |
| `created_by_device_id` | UUID | FK -> devices, nullable |
| `row_version` | BIGINT | NOT NULL default 0 |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |
| `deleted_at` | TIMESTAMPTZ | nullable |
| `deleted_by_user_id` | UUID | FK -> users, nullable |
| `delete_reason` | TEXT | nullable |

Transaction types:

```text
expense
cash_income
refund
transfer
fee
reconciliation_adjustment
opening_balance
```

Sources:

```text
shortcut
statement
dashboard_manual
reconciliation
installment
system
```

Verification statuses:

```text
unverified
user_confirmed
statement_confirmed
manual_confirmed
system_confirmed
```

Core checks:

```sql
CHECK (transaction_type IN (
  'expense',
  'cash_income',
  'refund',
  'transfer',
  'fee',
  'reconciliation_adjustment',
  'opening_balance'
));

CHECK (source IN (
  'shortcut',
  'statement',
  'dashboard_manual',
  'reconciliation',
  'installment',
  'system'
));

CHECK (status IN ('committed', 'voided'));

CHECK (
  (status = 'committed' AND deleted_at IS NULL AND delete_reason IS NULL)
  OR
  (status = 'voided' AND deleted_at IS NOT NULL AND delete_reason IS NOT NULL)
);

CHECK (account_leg_status IS NULL OR account_leg_status IN ('estimated', 'authoritative'));

CHECK (verification_status IN (
  'unverified',
  'user_confirmed',
  'statement_confirmed',
  'manual_confirmed',
  'system_confirmed'
));

CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1);
CHECK (original_amount > 0);
CHECK (from_amount IS NULL OR from_amount > 0);
CHECK (to_amount IS NULL OR to_amount > 0);
CHECK (effective_fx_rate IS NULL OR effective_fx_rate > 0);
CHECK (original_currency ~ '^[A-Z]{3}$');
CHECK (from_currency IS NULL OR from_currency ~ '^[A-Z]{3}$');
CHECK (to_currency IS NULL OR to_currency ~ '^[A-Z]{3}$');
CHECK (reporting_currency IS NULL OR reporting_currency ~ '^[A-Z]{3}$');
CHECK (from_account_id IS NULL OR to_account_id IS NULL OR from_account_id <> to_account_id);
```

Transaction shape:

```text
expense:
  from_account required
  to_account null
  from_amount required (estimated if foreign card, authoritative otherwise)
  category_id required (must reference an active expense-type category)

fee:
  from_account required
  to_account null
  from_amount required
  category_id required (must reference an active expense-type category)

cash_income:
  from_account null
  to_account required
  to_amount required
  category_id required (must reference an active income-type category)

refund:
  from_account null
  to_account required
  to_amount required
  category_id nullable (may inherit original expense category)

transfer:
  from_account required
  to_account required
  from_amount required
  to_amount required
  effective_fx_rate required
  category_id null

reconciliation_adjustment:
  exactly one account side
  corresponding leg amount required
  category_id null

opening_balance:
  exactly one account side
  corresponding leg amount required
  category_id null
```

Category service rules:

```text
expense / fee -> expense category required
cash_income   -> income category required
refund        -> may inherit original expense category
transfer / opening_balance / reconciliation_adjustment -> category null
```

Cross-table service rules:

```text
from_currency == from_account.currency
to_currency   == to_account.currency
```

### Foreign Currency Credit Card Expense & Estimation

For an expense where `original_currency <> from_account.currency`:
1. **Shortcut Ingestion**:
   - `original_amount` / `original_currency` preserved permanently;
   - `from_amount` is computed using current / T-1 reference FX and stored;
   - `account_leg_status = 'estimated'`;
   - `account_state` projection is updated with this estimated debt.
2. **Statement Reconciliation Settlement**:
   - `from_amount` is updated to the authoritative card settlement amount;
   - `account_leg_status = 'authoritative'`;
   - `account_state` receives the exact projection delta:
     $$\text{projection\_delta} = \text{projection\_effect}(\text{after}) - \text{projection\_effect}(\text{before}) = -68.20 - (-68.90) = +0.70\text{ USD}$$
     moving `account_state.ledger_balance` from $-68.90$ to $-68.20$;
   - `posted_on` is recorded;
   - `reporting_amount` and `reporting_fx_rate` are frozen;
   - `audit_events` records the transition with before/after state.

> **CRITICAL INVARIANT**: Estimation applies ONLY to foreign credit-card expense settlement legs. Cross-currency internal transfers (`transfer`) strictly require both real settlement amounts (`from_amount` and `to_amount`) and MUST NEVER invent a transfer leg using reference FX.

### Historical FX freeze

Before Statement settlement:

```text
reporting_amount = NULL
```

Dashboard may use current/T-1 reference FX.

After authoritative Statement settlement, persist:

```text
reporting_amount
reporting_currency
reporting_fx_rate
reporting_fx_locked_at
```

Later repayment FX MUST NOT rewrite these fields.

Indexes:

```sql
CREATE INDEX ix_transactions_from_date
ON transactions (from_account_id, occurred_on DESC)
WHERE deleted_at IS NULL;

CREATE INDEX ix_transactions_to_date
ON transactions (to_account_id, occurred_on DESC)
WHERE deleted_at IS NULL;

CREATE INDEX ix_transactions_household_date
ON transactions (household_id, occurred_on DESC)
WHERE deleted_at IS NULL;

CREATE INDEX ix_transactions_type_date
ON transactions (household_id, transaction_type, occurred_on DESC)
WHERE deleted_at IS NULL;

CREATE INDEX ix_transactions_statement_batch
ON transactions (statement_batch_id)
WHERE statement_batch_id IS NOT NULL;

CREATE INDEX ix_transactions_request
ON transactions (source_request_id)
WHERE source_request_id IS NOT NULL;

CREATE INDEX ix_transactions_merchant_trgm
ON transactions
USING GIN (merchant_normalized gin_trgm_ops)
WHERE deleted_at IS NULL AND merchant_normalized IS NOT NULL;
```

---

## 6.2 `transaction_links`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `source_transaction_id` | UUID | FK -> transactions, NOT NULL |
| `target_transaction_id` | UUID | FK -> transactions, NOT NULL |
| `relation_type` | TEXT | NOT NULL; `refund_of / reversal_of / installment_of` |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |

```sql
CHECK (source_transaction_id <> target_transaction_id);
CHECK (relation_type IN ('refund_of', 'reversal_of', 'installment_of'));

CREATE UNIQUE INDEX uq_transaction_link_source_relation
ON transaction_links (source_transaction_id, relation_type);

CREATE INDEX ix_transaction_links_target
ON transaction_links (target_transaction_id, relation_type);
```

Multiple partial refunds may target the same original expense.

---

# 7. Authoritative Snapshots

## 7.1 `account_snapshots`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `account_id` | UUID | FK -> accounts, NOT NULL |
| `as_of` | TIMESTAMPTZ | NOT NULL |
| `balance` | NUMERIC(20,6) | NOT NULL, signed |
| `currency` | CHAR(3) | NOT NULL |
| `snapshot_type` | TEXT | NOT NULL; `balance / investment_valuation` |
| `source` | TEXT | NOT NULL; `shortcut / statement / dashboard_manual` |
| `reconciliation_batch_id` | UUID | FK -> reconciliation_batches, nullable |
| `source_request_id` | UUID | FK -> ingestion_requests, nullable |
| `is_authoritative` | BOOLEAN | NOT NULL default true |
| `created_by_user_id` | UUID | FK -> users, nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |

```sql
CHECK (currency ~ '^[A-Z]{3}$');
CHECK (snapshot_type IN ('balance', 'investment_valuation'));
CHECK (source IN ('shortcut', 'statement', 'dashboard_manual'));

CREATE INDEX ix_account_snapshots_account_date
ON account_snapshots (account_id, as_of DESC);

CREATE UNIQUE INDEX uq_snapshot_per_batch
ON account_snapshots (reconciliation_batch_id, account_id, snapshot_type)
WHERE reconciliation_batch_id IS NOT NULL;
```

Service invariant:

```text
currency == account.currency
```

---

## 7.2 `credit_card_snapshots`

Liability fields are stored as non-negative amounts owed.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `account_id` | UUID | FK -> accounts, NOT NULL |
| `as_of` | TIMESTAMPTZ | NOT NULL |
| `statement_period_start` | DATE | nullable |
| `statement_period_end` | DATE | nullable |
| `statement_balance` | NUMERIC(20,6) | nullable, >=0 |
| `remaining_statement_due` | NUMERIC(20,6) | nullable, >=0 |
| `unbilled_balance` | NUMERIC(20,6) | nullable, >=0 |
| `current_outstanding` | NUMERIC(20,6) | nullable, >=0 |
| `currency` | CHAR(3) | NOT NULL |
| `source` | TEXT | NOT NULL; `statement / dashboard_manual / system_derived` |
| `reconciliation_batch_id` | UUID | FK -> reconciliation_batches, nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |

```sql
CHECK (currency ~ '^[A-Z]{3}$');
CHECK (source IN ('statement', 'dashboard_manual', 'system_derived'));
CHECK (statement_balance IS NULL OR statement_balance >= 0);
CHECK (remaining_statement_due IS NULL OR remaining_statement_due >= 0);
CHECK (unbilled_balance IS NULL OR unbilled_balance >= 0);
CHECK (current_outstanding IS NULL OR current_outstanding >= 0);
CHECK (
  statement_period_start IS NULL
  OR statement_period_end IS NULL
  OR statement_period_end >= statement_period_start
);

CREATE INDEX ix_credit_snapshots_account_date
ON credit_card_snapshots (account_id, as_of DESC);

CREATE UNIQUE INDEX uq_credit_snapshot_per_batch
ON credit_card_snapshots (reconciliation_batch_id, account_id)
WHERE reconciliation_batch_id IS NOT NULL;
```

Service invariants:

```text
account.account_type == credit
currency == account.currency
```

---

# 8. Investment P&L

## 8.1 `investment_pnl_periods`

Formula:

```text
P&L = closing_value - opening_value - contributions + withdrawals
```

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `account_id` | UUID | FK -> accounts, NOT NULL |
| `opening_snapshot_id` | UUID | FK -> account_snapshots, NOT NULL |
| `closing_snapshot_id` | UUID | FK -> account_snapshots, NOT NULL |
| `period_start` | TIMESTAMPTZ | NOT NULL |
| `period_end` | TIMESTAMPTZ | NOT NULL |
| `contributions_amount` | NUMERIC(20,6) | NOT NULL default 0 |
| `withdrawals_amount` | NUMERIC(20,6) | NOT NULL default 0 |
| `pnl_amount` | NUMERIC(20,6) | NOT NULL, signed |
| `currency` | CHAR(3) | NOT NULL |
| `status` | TEXT | NOT NULL default `confirmed`; `provisional / confirmed` |
| `calculation_version` | INTEGER | NOT NULL default 1 |
| `reconciliation_batch_id` | UUID | FK -> reconciliation_batches, nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

```sql
CHECK (opening_snapshot_id <> closing_snapshot_id);
CHECK (period_end > period_start);
CHECK (contributions_amount >= 0);
CHECK (withdrawals_amount >= 0);
CHECK (currency ~ '^[A-Z]{3}$');
CHECK (status IN ('provisional', 'confirmed'));
UNIQUE (account_id, closing_snapshot_id);

CREATE INDEX ix_investment_pnl_account_period
ON investment_pnl_periods (account_id, period_end DESC);
```

Service invariants:

- account must be `investment`;
- both snapshots belong to this account and are `investment_valuation`;
- known capital flows come from committed transfers / Statement-confirmed flows;
- ambiguous capital movement => reconciliation batch remains `needs_review`;
- Invariant: A reconciliation batch in `processing`, `ready`, or `needs_review` MUST NOT insert uncommitted rows into `investment_pnl_periods`. Pending/provisional calculations belong strictly in `reconciliation_candidates.payload` until atomic batch commit;
- While `status = provisional` is retained in the schema for future extensibility, it is not used to persist uncommitted reconciliation drafts in Product v1 and must never affect normal reporting.

Investment P&L MUST NOT be counted as cash income.

---

# 9. Installments

## 9.1 `installment_plans`

Future committed transactions are forbidden. Scheduling is separate.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `credit_account_id` | UUID | FK -> accounts, NOT NULL |
| `purchase_occurred_on` | DATE | NOT NULL |
| `merchant` | TEXT | nullable |
| `original_amount` | NUMERIC(20,6) | NOT NULL |
| `original_currency` | CHAR(3) | NOT NULL |
| `account_principal_amount` | NUMERIC(20,6) | nullable |
| `account_currency` | CHAR(3) | NOT NULL |
| `total_periods` | SMALLINT | NOT NULL |
| `first_statement_month` | DATE | nullable |
| `status` | TEXT | NOT NULL default `pending_first_bill`; see below |
| `source_request_id` | UUID | FK -> ingestion_requests, nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

Statuses:

```text
pending_first_bill
active
completed
cancelled
```

```sql
CHECK (original_amount > 0);
CHECK (account_principal_amount IS NULL OR account_principal_amount > 0);
CHECK (total_periods BETWEEN 2 AND 120);
CHECK (original_currency ~ '^[A-Z]{3}$');
CHECK (account_currency ~ '^[A-Z]{3}$');
CHECK (status IN ('pending_first_bill', 'active', 'completed', 'cancelled'));
```

Lifecycle invariants:

- newly captured installment plan starts as `pending_first_bill`;
- first successful `recognize_installment` atomic commit transitions plan to `active`;
- subsequent periods maintain `active` status;
- billing the final scheduled period transitions plan to `completed`;
- a `completed` plan must have zero remaining `scheduled` periods;
- a `cancelled` plan cannot recognize new installment expenses.

---

## 9.2 `installment_periods`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `plan_id` | UUID | FK -> installment_plans, NOT NULL |
| `period_no` | SMALLINT | NOT NULL |
| `recognition_month` | DATE | nullable |
| `scheduled_amount` | NUMERIC(20,6) | NOT NULL |
| `currency` | CHAR(3) | NOT NULL |
| `status` | TEXT | NOT NULL default `scheduled`; `scheduled / billed / cancelled` |
| `statement_line_id` | UUID | FK -> statement_lines, nullable |
| `expense_transaction_id` | UUID | FK -> transactions, nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

```sql
CHECK (period_no > 0);
CHECK (scheduled_amount > 0);
CHECK (currency ~ '^[A-Z]{3}$');
CHECK (status IN ('scheduled', 'billed', 'cancelled'));
CHECK (
  (status = 'billed' AND expense_transaction_id IS NOT NULL)
  OR
  (status IN ('scheduled', 'cancelled') AND expense_transaction_id IS NULL)
);
UNIQUE (plan_id, period_no);

CREATE INDEX ix_installment_periods_month_status
ON installment_periods (recognition_month, status);
```

Rounding rule:

> final period absorbs the exact remainder so total recognized amount equals plan principal.

---

# 10. Reconciliation

## 10.1 `reconciliation_batches`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `account_id` | UUID | FK -> accounts, NOT NULL |
| `batch_type` | TEXT | NOT NULL; `statement / snapshot / manual` |
| `period_start` | DATE | nullable |
| `period_end` | DATE | nullable |
| `status` | TEXT | NOT NULL default `processing`; see below |
| `currency` | CHAR(3) | NOT NULL |
| `authoritative_balance` | NUMERIC(20,6) | nullable, signed |
| `statement_balance` | NUMERIC(20,6) | nullable, >=0 |
| `current_outstanding` | NUMERIC(20,6) | nullable, >=0 |
| `unbilled_balance` | NUMERIC(20,6) | nullable, >=0 |
| `residual_amount` | NUMERIC(20,6) | nullable, signed |
| `adjustment_amount` | NUMERIC(20,6) | nullable, signed |
| `matched_count` | INTEGER | NOT NULL default 0 |
| `created_count` | INTEGER | NOT NULL default 0 |
| `pending_count` | INTEGER | NOT NULL default 0 |
| `parser_version` | TEXT | nullable; Statement batches only |
| `engine_version` | TEXT | NOT NULL default '1' |
| `source_request_id` | UUID | FK -> ingestion_requests, nullable |
| `created_by_user_id` | UUID | FK -> users, nullable |
| `row_version` | BIGINT | NOT NULL default 0 |
| `failure_code` | TEXT | nullable |
| `failure_detail` | TEXT | nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |
| `committed_at` | TIMESTAMPTZ | nullable |

Statuses:

```text
processing
ready
needs_review
committed
rejected
failed
```

```sql
CHECK (batch_type IN ('statement', 'snapshot', 'manual'));
CHECK (status IN ('processing', 'ready', 'needs_review', 'committed', 'rejected', 'failed'));
CHECK (currency ~ '^[A-Z]{3}$');
CHECK (statement_balance IS NULL OR statement_balance >= 0);
CHECK (current_outstanding IS NULL OR current_outstanding >= 0);
CHECK (unbilled_balance IS NULL OR unbilled_balance >= 0);
CHECK (matched_count >= 0);
CHECK (created_count >= 0);
CHECK (pending_count >= 0);
CHECK (period_start IS NULL OR period_end IS NULL OR period_end >= period_start);
CHECK ((status = 'committed' AND committed_at IS NOT NULL) OR status <> 'committed');

CREATE INDEX ix_reconciliation_account_date
ON reconciliation_batches (account_id, created_at DESC);

CREATE INDEX ix_reconciliation_household_status
ON reconciliation_batches (household_id, status, created_at DESC);
```

Rules:

- original PDF is not persisted;
- parse success => delete immediately;
- failed temporary file retention <=24h;
- PDF password never persisted;
- repeated upload creates a new batch;
- replay safety comes from transaction matching, not PDF deduplication.

Status and Scoping Invariants:
- `needs_review` is a `reconciliation_batches.status` value only (indicating reconciliation variance awaiting human resolution).
- `reconciliation_batches` NEVER take `needs_confirmation` status.
- Each `reconciliation_batch` remains strictly scoped to exactly ONE account (`account_id NOT NULL`).
- For multi-account Asset Captures, 0..N account-scoped `reconciliation_batches` are created, each linked to the parent ingestion request via `source_request_id = ingestion_requests.id`.
- There is NO parent reconciliation batch table, NO `asset_capture_batches` table, and NO account hierarchy.

---

## 10.2 `statement_lines`

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `batch_id` | UUID | FK -> reconciliation_batches, NOT NULL |
| `source_page_no` | INTEGER | nullable |
| `source_row_no` | INTEGER | nullable |
| `transaction_on` | DATE | nullable |
| `posted_on` | DATE | nullable |
| `description_raw` | TEXT | NOT NULL |
| `description_normalized` | TEXT | nullable |
| `amount` | NUMERIC(20,6) | NOT NULL |
| `currency` | CHAR(3) | NOT NULL |
| `direction` | TEXT | NOT NULL; `debit / credit / unknown` |
| `line_type` | TEXT | NOT NULL; see below |
| `match_status` | TEXT | NOT NULL default `unmatched`; see below |
| `matched_transaction_id` | UUID | FK -> transactions, nullable |
| `confidence` | NUMERIC(5,4) | nullable |
| `line_fingerprint` | BYTEA | nullable, non-unique |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |

Line types:

```text
expense
income
transfer
refund
fee
unknown
```

Match statuses:

```text
unmatched
matched
new_candidate
ambiguous
ignored
```

```sql
CHECK (amount > 0);
CHECK (currency ~ '^[A-Z]{3}$');
CHECK (direction IN ('debit', 'credit', 'unknown'));
CHECK (line_type IN ('expense', 'income', 'transfer', 'refund', 'fee', 'unknown'));
CHECK (match_status IN ('unmatched', 'matched', 'new_candidate', 'ambiguous', 'ignored'));
CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1);
CHECK (source_page_no IS NULL OR source_page_no > 0);
CHECK (source_row_no IS NULL OR source_row_no > 0);

CREATE INDEX ix_statement_lines_batch_status
ON statement_lines (batch_id, match_status);

CREATE INDEX ix_statement_lines_amount_date
ON statement_lines (currency, amount, transaction_on);

CREATE INDEX ix_statement_description_trgm
ON statement_lines
USING GIN (description_normalized gin_trgm_ops)
WHERE description_normalized IS NOT NULL;
```

`line_fingerprint` MUST NOT be unique.

---

## 10.3 `reconciliation_candidates`

Temporary heterogeneous proposals before atomic commit.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | UUID | PK |
| `batch_id` | UUID | FK -> reconciliation_batches, NOT NULL |
| `statement_line_id` | UUID | FK -> statement_lines, nullable |
| `candidate_type` | TEXT | NOT NULL; see below |
| `status` | TEXT | NOT NULL default `proposed`; see below |
| `target_transaction_id` | UUID | FK -> transactions, nullable |
| `payload` | JSONB | NOT NULL |
| `confidence` | NUMERIC(5,4) | nullable |
| `reason_code` | TEXT | nullable |
| `reason_detail` | TEXT | nullable |
| `resolved_by_user_id` | UUID | FK -> users, nullable |
| `resolved_at` | TIMESTAMPTZ | nullable |
| `applied_transaction_id` | UUID | FK -> transactions, nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |
| `updated_at` | TIMESTAMPTZ | NOT NULL default now() |

Candidate types:

```text
match
create_transaction
create_transfer
refund
adjustment
snapshot
investment_pnl
recognize_installment
```

Statuses:

```text
proposed
needs_review
accepted
rejected
applied
```

```sql
CHECK (candidate_type IN (
  'match',
  'create_transaction',
  'create_transfer',
  'refund',
  'adjustment',
  'snapshot',
  'investment_pnl',
  'recognize_installment'
));

CHECK (status IN ('proposed', 'needs_review', 'accepted', 'rejected', 'applied'));
CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1);

CREATE INDEX ix_reconciliation_candidates_batch_status
ON reconciliation_candidates (batch_id, status);

CREATE INDEX ix_reconciliation_candidates_line
ON reconciliation_candidates (statement_line_id);
```

JSONB is intentional here because candidates are temporary. Final committed facts must be normalized.

---

# 11. Audit and Soft Delete

## 11.1 `audit_events`

Append-only immutable log.

| Column | Type | Constraints / Meaning |
|---|---|---|
| `id` | BIGINT | identity PK |
| `household_id` | UUID | FK -> households, NOT NULL |
| `actor_type` | TEXT | NOT NULL; `user / device / system` |
| `actor_user_id` | UUID | FK -> users, nullable |
| `actor_device_id` | UUID | FK -> devices, nullable |
| `request_id` | UUID | FK -> ingestion_requests, nullable |
| `reconciliation_batch_id` | UUID | FK -> reconciliation_batches, nullable |
| `entity_type` | TEXT | NOT NULL |
| `entity_id` | UUID | NOT NULL |
| `action` | TEXT | NOT NULL; see below |
| `before_data` | JSONB | nullable |
| `after_data` | JSONB | nullable |
| `metadata` | JSONB | nullable |
| `created_at` | TIMESTAMPTZ | NOT NULL default now() |

Actions:

```text
create
update
soft_delete
restore
confirm
reject
commit
reconcile
void
```

```sql
CHECK (actor_type IN ('user', 'device', 'system'));
CHECK (action IN (
  'create',
  'update',
  'soft_delete',
  'restore',
  'confirm',
  'reject',
  'commit',
  'reconcile',
  'void'
));

CREATE INDEX ix_audit_household_date
ON audit_events (household_id, created_at DESC);

CREATE INDEX ix_audit_entity
ON audit_events (entity_type, entity_id, created_at DESC);
```

Application DB role:

```text
audit_events: INSERT + SELECT only
```

Add a trigger rejecting UPDATE/DELETE.

---

## 11.2 Soft-delete policy

### Transactions

Soft delete and void are the **identical financial lifecycle operation** in Product v1:

- `status = 'committed'` $\iff$ `deleted_at IS NULL` AND `delete_reason IS NULL`
- `status = 'voided'` $\iff$ `deleted_at IS NOT NULL` AND `delete_reason IS NOT NULL`
- `deleted_by_user_id` is nullable (set for user actions; null for automated system compensations)

```text
deleted_at
deleted_by_user_id
delete_reason
```

Voiding must atomically reverse the transaction's `account_state` projection exactly once and append an immutable `audit_events` row.

Statement-confirmed transactions require explicit two-step correction/void workflows.

### Accounts

Never hard-delete after history exists:

```text
status = inactive
```

### Categories

```text
status = inactive
```

### Statement/reconciliation history

Retain normalized evidence and batch history. Only original PDF bytes are deleted.

---

# 12. Critical Cross-Table Invariants

## 12.1 Household consistency

All entities in one financial operation must belong to the same household.

## 12.2 Account currency consistency

```text
transaction.from_currency == from_account.currency
transaction.to_currency   == to_account.currency
snapshot.currency         == account.currency
credit_snapshot.currency  == credit_account.currency
```

`original_currency` may differ from account currency.

Example:

```text
original: 10,000 JPY
card posting: 68.20 USD

original_amount/currency = 10000 JPY
from_amount/currency     = 68.20 USD
```

## 12.3 Transfer

One internal transfer = one transaction.

Cross-currency requires both legs and real FX rate.

Fee = separate `fee` transaction.

## 12.4 Refund

Refund is an independent transaction linked with:

```text
refund_of
```

Partial refunds allowed.

Aggregate refunds exceeding original refundable amount require reviewed override.

## 12.5 Investment

Market-value change is never cash income.

Investment automatic ±200 reconciliation adjustment is forbidden.

## 12.6 Reconciliation threshold

Non-investment account:

```text
abs(residual converted to household reporting currency) <= 200 CNY
```

may create automatic `reconciliation_adjustment`.

Otherwise:

```text
needs_review
```

---

# 13. Concurrency and Transaction Boundaries

Default PostgreSQL isolation may remain `READ COMMITTED`.

Correctness relies on:

- explicit row locks;
- deterministic lock order;
- unique constraints;
- final revalidation inside commit.

Every financial write path locks `account_state` before changing account projection.

## 13.1 Expense / income / fee / one-sided adjustment

```text
BEGIN

lock ingestion_request if request-driven
lock account_state FOR UPDATE

re-check idempotency
validate account/currency
insert transaction
update account_state
insert audit_event
persist response/status

COMMIT
```

Any failure => full rollback.

## 13.2 Transfer

Lock both account-state rows in sorted `account_id` order:

```text
BEGIN

lock request
lock account_state A
lock account_state B
validate both legs
insert one transfer
update both states
insert optional fee transaction
audit
commit request

COMMIT
```

## 13.3 Multi-Account Asset Capture

One Asset Capture image may observe balances across multiple canonical accounts.
All observations MUST be committed atomically within a single database transaction:

```text
BEGIN

lock ingestion_request (idempotency boundary)

1. resolve and validate all candidate accounts (must be active, same household, unique match)
2. sort all affected canonical account_ids in deterministic ascending UUID order:
   account_ids = sorted([id_1, id_2, ..., id_k])

3. lock every affected account_state row in sorted order:
   FOR account_id IN account_ids:
       SELECT * FROM account_state WHERE account_id = :account_id FOR UPDATE

4. validate all account states and currencies
5. cross-check sum of observations against displayed total (where currencies match)

6. FOR each observation:
   IF account_type IN ('cash', 'savings'):
       INSERT INTO account_snapshots (snapshot_type='balance', ...)
       execute reconciliation adjustment if residual within policy
   IF account_type == 'investment':
       INSERT INTO account_snapshots (snapshot_type='investment_valuation', ...)
       calculate and INSERT INTO investment_pnl_periods if applicable
   UPDATE account_state
     SET ledger_balance = ..., last_authoritative_snapshot_at = ...
   INSERT INTO audit_events

7. UPDATE ingestion_requests SET status = 'committed', committed_at = now()

COMMIT
```

Invariants:
- **ALL OR NOTHING**: If any account fails validation, any residual requires review, or any unexpected error occurs, the entire transaction is rolled back (`ROLLBACK ALL`). No partial screenshot effects.
- **Deadlock Avoidance**: Enforced by locking affected `account_state` rows strictly in ascending UUID order across all concurrent captures and transactions.

## 13.4 Shortcut confirmation

Low-confidence request:

```text
ingestion_requests.status = needs_confirmation
draft_payload stored
NO transaction
NO account_state mutation
```

Only confirm commits financial state.

## 13.5 Reconciliation preview

May create:

```text
reconciliation_batch
statement_lines
reconciliation_candidates
```

without a long transaction.

It MUST NOT mutate committed ledger state.

## 13.6 Reconciliation atomic commit

```text
BEGIN

lock batch FOR UPDATE
verify not already committed

lock affected account_state rows in sorted order

re-read committed transactions
revalidate matches
recompute residual
re-evaluate threshold

apply accepted candidates
create transactions/snapshots/P&L
recognize billed installments
update statement matches
update verification status
update account_state
mark candidates applied
mark batch committed
write audit

COMMIT
```

Any failure => rollback entire batch.

Second commit attempt on committed batch => no-op/readback.

## 13.6 Concurrent Shortcut vs Statement commit

Both lock the same `account_state` row.

After acquiring the lock, reconciliation MUST re-read/revalidate so a transaction created during preview cannot cause duplicate compensation.

## 13.7 Investment snapshot / P&L

```text
BEGIN

lock investment account_state
re-read previous authoritative investment snapshot
re-read known contributions/withdrawals

if ambiguous capital movement:
    keep batch needs_review
else:
    create closing snapshot
    create confirmed investment P&L
    set investment account_state to authoritative valuation
    audit

COMMIT
```

## 13.8 Statement-confirmed transaction correction boundary

```text
BEGIN

lock transaction row FOR UPDATE
verify row_version matches caller token
lock affected account_state rows in sorted order

compute projection deltas (before vs after amounts, accounts, dates)
apply delta updates to account_state
update transaction fields (amount, category, merchant, remarks, row_version + 1)

write audit_events (action = 'update', before_state, after_state)

COMMIT
```

Correction never mutates immutable Statement evidence (`statement_lines`).

## 13.9 Foreign-currency credit card settlement delta reconciliation boundary

```text
BEGIN

lock target transaction FOR UPDATE
verify account_leg_status == 'estimated'
lock credit-card account_state FOR UPDATE

calculate projection_delta:
  projection_effect_before = -estimated_settlement_amount
  projection_effect_after  = -authoritative_settlement_amount
  projection_delta = projection_effect_after - projection_effect_before
  (e.g., -68.20 - (-68.90) = +0.70 USD)

apply projection_delta to account_state.ledger_balance:
  ledger_balance := ledger_balance + projection_delta (debt changes from -68.90 to -68.20)

update transaction:
  from_amount = authoritative_settlement_amount
  account_leg_status = 'authoritative'
  posted_on = statement_line.post_date
  reporting_amount = locked historical reporting value
  reporting_fx_locked_at = now()
  statement_batch_id = batch.id
  verification_status = 'statement_confirmed'

write audit_events (action = 'reconcile', before: estimated, after: authoritative)

COMMIT
```

---

# 14. High-Value Index Summary

```text
accounts:
  household + type + status
  active unique name

account_aliases:
  account + normalized alias
  trigram alias

transactions:
  from_account + occurred_on
  to_account + occurred_on
  household + occurred_on
  type + occurred_on
  statement_batch
  request
  trigram merchant

account_snapshots:
  account + as_of DESC

credit_card_snapshots:
  account + as_of DESC

investment_pnl_periods:
  account + period_end DESC

installment_periods:
  recognition_month + status

reconciliation_batches:
  account + created_at DESC
  household + status

statement_lines:
  batch + match_status
  currency + amount + transaction_on
  trigram description

reconciliation_candidates:
  batch + status

audit_events:
  household + created_at DESC
  entity + created_at DESC

ingestion_requests:
  UNIQUE device + idempotency_key
  device + status
```

Do not add speculative indexes before real query evidence exists.

---

# 15. Recommended DDL Order

```text
1. extensions
2. households
3. users
4. household_members
5. devices
6. accounts
7. account_state
8. account_aliases
9. categories
10. ingestion_requests
11. reconciliation_batches
12. transactions
13. transaction_links
14. account_snapshots
15. credit_card_snapshots
16. investment_pnl_periods
17. statement_lines
18. reconciliation_candidates
19. installment_plans
20. installment_periods
21. audit_events
22. deferred/circular foreign keys
23. indexes
24. immutable-audit trigger
```

Where circular references exist, create tables first, then add FKs with `ALTER TABLE`.

---

# 16. Intentional Differences from Legacy Schema

Remove these legacy assumptions:

```text
accounts.current_balance as source of truth
transactions alone representing all financial state
hard-coded account/category literals
investment adjustment mapped into income
future installment transactions created immediately
single generic adjustment semantic
request idempotency only on transaction rows
Dashboard owning accounting write logic
Statement parsing directly mutating ledger
```

Target replacement:

```text
accounts                -> metadata
account_state           -> rebuildable current projection
transactions            -> committed financial events
snapshots               -> authoritative observed state
investment_pnl_periods  -> non-cash investment return
reconciliation_*        -> staged atomic workflow
ingestion_requests      -> idempotency + recovery + confirmation
installment_*           -> schedule first, transaction only when billed
audit_events            -> immutable history
```

---

# 17. Product v1 Non-Goals

Do not model yet:

- individual securities / positions;
- tax lots / investment cost basis;
- AA / couple receivables;
- credit-card delinquency/minimum-payment workflows;
- early installment payoff;
- exact realized/unrealized FX accounting;
- legacy data migration;
- mandatory monthly close;
- `institution` table or column (single institutions are modeled as multiple independent Accounts);
- `asset_class`, `liquidity_level`, `parent_account_id`, or account hierarchy;
- category `priority`.

---

# 18. Agent Implementation Rule

This document is the physical persistence contract.

If implementation convenience conflicts with `TARGET_DOMAIN_MODEL.md`, the Target Domain Model wins.

If a technical detail is unspecified and does not change business semantics, choose a reasonable default and document it instead of asking the user.

Never collapse:

```text
Transaction
Snapshot
Investment P&L
Reconciliation
```

back into one generic transaction/adjustment model.
