-- VibeLedger Migration: 0009_indexes
-- Authority: docs/architecture/PHYSICAL_SCHEMA.md
-- This file creates all database indexes and uniqueness constraints (no FKs here).
-- Using public.gin_trgm_ops for GIN indexes since search_path does not fall back to public.

CREATE INDEX ix_household_members_user ON household_members (user_id);

CREATE INDEX ix_accounts_owner ON accounts (owner_user_id);
CREATE INDEX ix_accounts_linked_cash ON accounts (linked_cash_account_id);

CREATE INDEX ix_accounts_household_type ON accounts (household_id, account_type, status);
CREATE UNIQUE INDEX uq_accounts_active_name ON accounts (household_id, lower(name)) WHERE status = 'active';

CREATE UNIQUE INDEX uq_account_alias ON account_aliases (account_id, normalized_alias) WHERE deleted_at IS NULL;
CREATE INDEX ix_account_alias_trgm ON account_aliases USING GIN (normalized_alias public.gin_trgm_ops) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX uq_categories_active ON categories (household_id, category_type, lower(name)) WHERE status = 'active';

CREATE INDEX ix_ingestion_device_status ON ingestion_requests (device_id, status);
CREATE INDEX ix_ingestion_status_updated ON ingestion_requests (status, updated_at);

CREATE INDEX ix_transactions_from_date ON transactions (from_account_id, occurred_on DESC) WHERE deleted_at IS NULL;
CREATE INDEX ix_transactions_to_date ON transactions (to_account_id, occurred_on DESC) WHERE deleted_at IS NULL;
CREATE INDEX ix_transactions_household_date ON transactions (household_id, occurred_on DESC) WHERE deleted_at IS NULL;
CREATE INDEX ix_transactions_type_date ON transactions (household_id, transaction_type, occurred_on DESC) WHERE deleted_at IS NULL;
CREATE INDEX ix_transactions_statement_batch ON transactions (statement_batch_id) WHERE statement_batch_id IS NOT NULL;
CREATE INDEX ix_transactions_request ON transactions (source_request_id) WHERE source_request_id IS NOT NULL;
CREATE INDEX ix_transactions_merchant_trgm ON transactions USING GIN (merchant_normalized public.gin_trgm_ops) WHERE deleted_at IS NULL AND merchant_normalized IS NOT NULL;

CREATE UNIQUE INDEX uq_transaction_link_source_relation ON transaction_links (source_transaction_id, relation_type);
CREATE INDEX ix_transaction_links_target ON transaction_links (target_transaction_id, relation_type);

CREATE INDEX ix_account_snapshots_account_date ON account_snapshots (account_id, as_of DESC);
CREATE UNIQUE INDEX uq_snapshot_per_batch ON account_snapshots (reconciliation_batch_id, account_id, snapshot_type) WHERE reconciliation_batch_id IS NOT NULL;

CREATE INDEX ix_credit_snapshots_account_date ON credit_card_snapshots (account_id, as_of DESC);
CREATE UNIQUE INDEX uq_credit_snapshot_per_batch ON credit_card_snapshots (reconciliation_batch_id, account_id) WHERE reconciliation_batch_id IS NOT NULL;

CREATE INDEX ix_investment_pnl_account_period ON investment_pnl_periods (account_id, period_end DESC);

CREATE INDEX ix_installment_periods_month_status ON installment_periods (recognition_month, status);

CREATE INDEX ix_reconciliation_account_date ON reconciliation_batches (account_id, created_at DESC);
CREATE INDEX ix_reconciliation_household_status ON reconciliation_batches (household_id, status, created_at DESC);

CREATE INDEX ix_statement_lines_batch_status ON statement_lines (batch_id, match_status);
CREATE INDEX ix_statement_lines_amount_date ON statement_lines (currency, amount, transaction_on);
CREATE INDEX ix_statement_description_trgm ON statement_lines USING GIN (description_normalized public.gin_trgm_ops) WHERE description_normalized IS NOT NULL;

CREATE INDEX ix_reconciliation_candidates_batch_status ON reconciliation_candidates (batch_id, status);
CREATE INDEX ix_reconciliation_candidates_line ON reconciliation_candidates (statement_line_id);

CREATE INDEX ix_audit_household_date ON audit_events (household_id, created_at DESC);
CREATE INDEX ix_audit_entity ON audit_events (entity_type, entity_id, created_at DESC);
