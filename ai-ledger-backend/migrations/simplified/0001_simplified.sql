-- VibeLedger Astra-Simplified PostgreSQL Baseline Migration
-- S1: 16 Application Tables + Constraints, Composite Foreign Keys, Indexes, and Immutability Trigger.

-- 1. households
CREATE TABLE IF NOT EXISTS households (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    reporting_currency VARCHAR(3) NOT NULL DEFAULT 'CNY',
    started_on DATE NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Singapore',
    investment_review_change_ratio NUMERIC(6,4) NOT NULL DEFAULT 0.2000,
    status TEXT NOT NULL DEFAULT 'active',
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_households_ratio CHECK (investment_review_change_ratio > 0 AND investment_review_change_ratio <= 1),
    CONSTRAINT chk_households_status CHECK (status IN ('active', 'archived')),
    CONSTRAINT uq_households_id_composite UNIQUE (id)
);

-- 2. users
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_subject TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_users_status CHECK (status IN ('active', 'disabled')),
    CONSTRAINT uq_users_id_composite UNIQUE (id)
);

-- 3. household_members
CREATE TABLE IF NOT EXISTS household_members (
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (household_id, user_id),
    CONSTRAINT chk_household_members_role CHECK (role IN ('owner', 'member')),
    CONSTRAINT uq_household_members_composite UNIQUE (household_id, user_id)
);

-- 4. devices
CREATE TABLE IF NOT EXISTS devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    token_hash BYTEA NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    client_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT chk_devices_platform CHECK (platform IN ('ios', 'macos', 'web', 'ios_shortcuts', 'other')),
    CONSTRAINT chk_devices_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT fk_devices_household_user FOREIGN KEY (household_id, user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_devices_household_id UNIQUE (household_id, id)
);

-- 5. accounts
CREATE TABLE IF NOT EXISTS accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    balance_scope TEXT NOT NULL,
    account_type TEXT NOT NULL,
    currency VARCHAR(3) NOT NULL,
    owner_user_id UUID,
    risk_level TEXT,
    opened_on DATE NOT NULL,
    closed_on DATE,
    status TEXT NOT NULL DEFAULT 'active',
    statement_import_enabled BOOLEAN NOT NULL DEFAULT false,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_accounts_type CHECK (account_type IN ('cash', 'savings', 'investment', 'credit')),
    CONSTRAINT chk_accounts_risk CHECK (risk_level IN ('very_low', 'low', 'medium', 'high') OR risk_level IS NULL),
    CONSTRAINT chk_accounts_credit_risk CHECK (account_type != 'credit' OR risk_level IS NULL),
    CONSTRAINT chk_accounts_status CHECK (status IN ('active', 'closed', 'cancelled')),
    CONSTRAINT chk_accounts_active_closed_on CHECK (status != 'active' OR closed_on IS NULL),
    CONSTRAINT chk_accounts_closed_on_dates CHECK (status != 'closed' OR (closed_on IS NOT NULL AND closed_on >= opened_on)),
    CONSTRAINT fk_accounts_household_owner FOREIGN KEY (household_id, owner_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_accounts_household_id UNIQUE (household_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_active_name ON accounts (household_id, LOWER(name)) WHERE status = 'active';

-- 6. account_aliases
CREATE TABLE IF NOT EXISTS account_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    account_id UUID NOT NULL,
    alias_text TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_account_aliases_status CHECK (status IN ('active', 'inactive')),
    CONSTRAINT fk_account_aliases_account FOREIGN KEY (household_id, account_id) REFERENCES accounts(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_account_aliases_household_id UNIQUE (household_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_account_aliases_active ON account_aliases (household_id, account_id, normalized_alias) WHERE status = 'active';

-- 7. categories
CREATE TABLE IF NOT EXISTS categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    category_type TEXT NOT NULL,
    description TEXT,
    is_fallback BOOLEAN NOT NULL DEFAULT false,
    status TEXT NOT NULL DEFAULT 'active',
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_categories_type CHECK (category_type IN ('expense', 'income')),
    CONSTRAINT chk_categories_status CHECK (status IN ('active', 'inactive')),
    CONSTRAINT uq_categories_household_id UNIQUE (household_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_categories_active_name ON categories (household_id, category_type, LOWER(name)) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS uq_categories_active_fallback ON categories (household_id, category_type) WHERE is_fallback = true AND status = 'active';

-- 8. ingestion_requests
CREATE TABLE IF NOT EXISTS ingestion_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    device_id UUID,
    actor_scope TEXT NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    request_kind TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash VARCHAR(64),
    image_sha256 VARCHAR(64),
    status TEXT NOT NULL,
    captured_at TIMESTAMPTZ,
    client_version TEXT,
    draft_payload JSONB,
    response_payload JSONB,
    response_http_status INTEGER,
    failure_code TEXT,
    row_version BIGINT NOT NULL DEFAULT 0,
    last_editor_scope TEXT,
    statement_account_id UUID,
    document_sha256 VARCHAR(64),
    period_start DATE,
    period_end DATE,
    parser_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    committed_at TIMESTAMPTZ,
    CONSTRAINT chk_ingestion_requests_key_len CHECK (char_length(idempotency_key) >= 8 AND char_length(idempotency_key) <= 200),
    CONSTRAINT chk_ingestion_requests_kind CHECK (request_kind IN ('expense', 'balance_capture', 'statement', 'command')),
    CONSTRAINT chk_ingestion_requests_status CHECK (status IN ('processing', 'needs_confirmation', 'committed', 'rejected', 'failed')),
    CONSTRAINT chk_ingestion_requests_committed_at CHECK ((status = 'committed' AND committed_at IS NOT NULL) OR (status != 'committed' AND committed_at IS NULL)),
    CONSTRAINT chk_ingestion_requests_hash CHECK (request_hash IS NOT NULL OR (request_kind = 'command' AND operation = 'cancel')),
    CONSTRAINT fk_ingestion_requests_user FOREIGN KEY (household_id, user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_ingestion_requests_device FOREIGN KEY (household_id, device_id) REFERENCES devices(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_ingestion_requests_statement_account FOREIGN KEY (household_id, statement_account_id) REFERENCES accounts(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_ingestion_requests_household_id UNIQUE (household_id, id),
    CONSTRAINT uq_ingestion_requests_key UNIQUE (household_id, actor_scope, idempotency_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_statement_doc_claim ON ingestion_requests (household_id, statement_account_id, document_sha256)
    WHERE request_kind = 'statement' AND status IN ('processing', 'needs_confirmation', 'committed') AND statement_account_id IS NOT NULL AND document_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ingestion_requests_household_status_created ON ingestion_requests (household_id, status, created_at);

-- 9. spending_schedules
CREATE TABLE IF NOT EXISTS spending_schedules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount_per_period NUMERIC(20,6) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    period_count INTEGER,
    start_month DATE NOT NULL,
    day_of_month INTEGER NOT NULL,
    merchant TEXT NOT NULL,
    category_id UUID NOT NULL,
    account_id UUID,
    status TEXT NOT NULL DEFAULT 'active',
    source_request_id UUID,
    created_by_device_id UUID,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_spending_schedules_kind CHECK (kind IN ('recurring', 'installment')),
    CONSTRAINT chk_spending_schedules_amount CHECK (amount_per_period > 0),
    CONSTRAINT chk_spending_schedules_period_count CHECK (
        (kind = 'installment' AND period_count >= 1 AND period_count <= 1200) OR
        (kind = 'recurring' AND (period_count IS NULL OR (period_count >= 1 AND period_count <= 1200)))
    ),
    CONSTRAINT chk_spending_schedules_start_month CHECK (start_month = date_trunc('month', start_month)::date),
    CONSTRAINT chk_spending_schedules_day CHECK (day_of_month >= 1 AND day_of_month <= 31),
    CONSTRAINT chk_spending_schedules_status CHECK (status IN ('active', 'paused', 'cancelled', 'completed')),
    CONSTRAINT fk_spending_schedules_creator FOREIGN KEY (household_id, created_by_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_spending_schedules_device FOREIGN KEY (household_id, created_by_device_id) REFERENCES devices(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_spending_schedules_category FOREIGN KEY (household_id, category_id) REFERENCES categories(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_spending_schedules_account FOREIGN KEY (household_id, account_id) REFERENCES accounts(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_spending_schedules_request FOREIGN KEY (household_id, source_request_id) REFERENCES ingestion_requests(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_spending_schedules_household_id UNIQUE (household_id, id)
);

-- 10. schedule_occurrences
CREATE TABLE IF NOT EXISTS schedule_occurrences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    schedule_id UUID NOT NULL,
    period_no INTEGER NOT NULL,
    due_on DATE NOT NULL,
    amount NUMERIC(20,6) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    category_id UUID NOT NULL,
    account_id UUID,
    status TEXT NOT NULL,
    skip_reason TEXT,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_schedule_occurrences_period_no CHECK (period_no >= 1),
    CONSTRAINT chk_schedule_occurrences_amount CHECK (amount > 0),
    CONSTRAINT chk_schedule_occurrences_status CHECK (status IN ('recorded', 'skipped', 'needs_confirmation')),
    CONSTRAINT fk_schedule_occurrences_schedule FOREIGN KEY (household_id, schedule_id) REFERENCES spending_schedules(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_schedule_occurrences_category FOREIGN KEY (household_id, category_id) REFERENCES categories(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_schedule_occurrences_account FOREIGN KEY (household_id, account_id) REFERENCES accounts(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_schedule_occurrences_household_id UNIQUE (household_id, id),
    CONSTRAINT uq_schedule_occurrences_period UNIQUE (household_id, schedule_id, period_no)
);

-- 11. transactions
CREATE TABLE IF NOT EXISTS transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    transaction_type TEXT NOT NULL,
    occurred_on DATE NOT NULL,
    occurred_at TIMESTAMPTZ,
    date_source TEXT NOT NULL,
    original_amount NUMERIC(20,6) NOT NULL,
    original_currency VARCHAR(3) NOT NULL,
    category_id UUID NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'committed',
    account_id UUID,
    merchant TEXT,
    merchant_normalized TEXT,
    remarks TEXT,
    refund_of_transaction_id UUID,
    payment_mode TEXT,
    schedule_occurrence_id UUID,
    category_uncertain BOOLEAN NOT NULL DEFAULT false,
    account_review_acknowledged BOOLEAN NOT NULL DEFAULT false,
    reporting_amount NUMERIC(20,6),
    reporting_currency VARCHAR(3),
    reporting_fx_rate NUMERIC(24,12),
    reporting_fx_as_of DATE,
    reporting_fx_source TEXT,
    reporting_fx_locked_at TIMESTAMPTZ,
    created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_by_device_id UUID,
    source_request_id UUID NOT NULL,
    source_item_key TEXT NOT NULL DEFAULT 'single',
    deleted_at TIMESTAMPTZ,
    deleted_by_user_id UUID,
    delete_reason TEXT,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_transactions_type CHECK (transaction_type IN ('expense', 'refund', 'cash_income')),
    CONSTRAINT chk_transactions_date_source CHECK (date_source IN ('receipt', 'capture_date', 'manual', 'statement', 'schedule')),
    CONSTRAINT chk_transactions_original_amount CHECK (original_amount > 0),
    CONSTRAINT chk_transactions_source CHECK (source IN ('shortcut', 'dashboard_manual', 'statement', 'scheduled')),
    CONSTRAINT chk_transactions_status CHECK (status IN ('committed', 'voided')),
    CONSTRAINT chk_transactions_payment_mode CHECK (payment_mode IS NULL OR transaction_type = 'expense'),
    CONSTRAINT chk_transactions_payment_mode_val CHECK (payment_mode IN ('one_off', 'installment') OR payment_mode IS NULL),
    CONSTRAINT chk_transactions_refund_self CHECK (refund_of_transaction_id IS NULL OR refund_of_transaction_id != id),
    CONSTRAINT chk_transactions_void_consistency CHECK (
        (status = 'voided' AND deleted_at IS NOT NULL AND deleted_by_user_id IS NOT NULL AND delete_reason IS NOT NULL) OR
        (status = 'committed' AND deleted_at IS NULL AND deleted_by_user_id IS NULL AND delete_reason IS NULL)
    ),
    CONSTRAINT chk_transactions_fx_consistency CHECK (
        (reporting_amount IS NULL AND reporting_currency IS NULL AND reporting_fx_rate IS NULL AND reporting_fx_as_of IS NULL AND reporting_fx_source IS NULL AND reporting_fx_locked_at IS NULL) OR
        (reporting_amount IS NOT NULL AND reporting_currency IS NOT NULL AND reporting_fx_rate IS NOT NULL AND reporting_fx_as_of IS NOT NULL AND reporting_fx_source IS NOT NULL AND reporting_fx_locked_at IS NOT NULL)
    ),
    CONSTRAINT fk_transactions_creator FOREIGN KEY (household_id, created_by_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_device FOREIGN KEY (household_id, created_by_device_id) REFERENCES devices(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_deleter FOREIGN KEY (household_id, deleted_by_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_category FOREIGN KEY (household_id, category_id) REFERENCES categories(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_account FOREIGN KEY (household_id, account_id) REFERENCES accounts(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_request FOREIGN KEY (household_id, source_request_id) REFERENCES ingestion_requests(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_refund_of FOREIGN KEY (household_id, refund_of_transaction_id) REFERENCES transactions(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_transactions_occurrence FOREIGN KEY (household_id, schedule_occurrence_id) REFERENCES schedule_occurrences(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_transactions_household_id UNIQUE (household_id, id),
    CONSTRAINT uq_transactions_request_item UNIQUE (source_request_id, source_item_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_occurrence ON transactions (household_id, schedule_occurrence_id) WHERE schedule_occurrence_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_transactions_household_occurred ON transactions (household_id, occurred_on DESC, id);
CREATE INDEX IF NOT EXISTS idx_transactions_household_refund ON transactions (household_id, refund_of_transaction_id) WHERE refund_of_transaction_id IS NOT NULL;

-- 12. account_snapshots
CREATE TABLE IF NOT EXISTS account_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    account_id UUID NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    time_basis TEXT NOT NULL,
    balance NUMERIC(20,6) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT,
    replaces_snapshot_id UUID,
    voided_at TIMESTAMPTZ,
    voided_by_user_id UUID,
    void_reason TEXT,
    created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_by_device_id UUID,
    source_request_id UUID NOT NULL,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_snapshots_time_basis CHECK (time_basis IN ('explicit', 'capture', 'date_only')),
    CONSTRAINT chk_snapshots_source CHECK (source IN ('screenshot', 'manual', 'statement')),
    CONSTRAINT chk_snapshots_status CHECK (status IN ('active', 'voided')),
    CONSTRAINT chk_snapshots_void_consistency CHECK (
        (status = 'voided' AND voided_at IS NOT NULL AND void_reason IS NOT NULL) OR
        (status = 'active' AND voided_at IS NULL AND void_reason IS NULL)
    ),
    CONSTRAINT fk_snapshots_creator FOREIGN KEY (household_id, created_by_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_snapshots_voider FOREIGN KEY (household_id, voided_by_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_snapshots_device FOREIGN KEY (household_id, created_by_device_id) REFERENCES devices(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_snapshots_account FOREIGN KEY (household_id, account_id) REFERENCES accounts(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_snapshots_request FOREIGN KEY (household_id, source_request_id) REFERENCES ingestion_requests(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_snapshots_replaces FOREIGN KEY (household_id, replaces_snapshot_id) REFERENCES account_snapshots(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_snapshots_household_id UNIQUE (household_id, id),
    CONSTRAINT uq_snapshots_request_account UNIQUE (source_request_id, account_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_snapshots_active_account_asof ON account_snapshots (household_id, account_id, as_of) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_snapshots_household_account_asof ON account_snapshots (household_id, account_id, as_of DESC) WHERE status = 'active';

-- 13. investment_period_inputs
CREATE TABLE IF NOT EXISTS investment_period_inputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    account_id UUID NOT NULL,
    opening_snapshot_id UUID NOT NULL,
    closing_snapshot_id UUID NOT NULL,
    contributions_amount NUMERIC(20,6) NOT NULL,
    withdrawals_amount NUMERIC(20,6) NOT NULL,
    confirmed_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    voided_at TIMESTAMPTZ,
    voided_by_user_id UUID,
    void_reason TEXT,
    source_request_id UUID,
    created_by_device_id UUID,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_investment_inputs_contrib CHECK (contributions_amount >= 0),
    CONSTRAINT chk_investment_inputs_withdraw CHECK (withdrawals_amount >= 0),
    CONSTRAINT chk_investment_inputs_status CHECK (status IN ('active', 'voided')),
    CONSTRAINT chk_investment_inputs_void_consistency CHECK (
        (status = 'voided' AND voided_at IS NOT NULL AND void_reason IS NOT NULL) OR
        (status = 'active' AND voided_at IS NULL AND void_reason IS NULL)
    ),
    CONSTRAINT chk_investment_inputs_snapshots_distinct CHECK (opening_snapshot_id != closing_snapshot_id),
    CONSTRAINT fk_investment_inputs_account FOREIGN KEY (household_id, account_id) REFERENCES accounts(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_investment_inputs_opening FOREIGN KEY (household_id, opening_snapshot_id) REFERENCES account_snapshots(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_investment_inputs_closing FOREIGN KEY (household_id, closing_snapshot_id) REFERENCES account_snapshots(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_investment_inputs_confirmer FOREIGN KEY (household_id, confirmed_by_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_investment_inputs_voider FOREIGN KEY (household_id, voided_by_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_investment_inputs_device FOREIGN KEY (household_id, created_by_device_id) REFERENCES devices(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_investment_inputs_request FOREIGN KEY (household_id, source_request_id) REFERENCES ingestion_requests(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_investment_inputs_household_id UNIQUE (household_id, id),
    CONSTRAINT uq_investment_inputs_pair UNIQUE (household_id, opening_snapshot_id, closing_snapshot_id)
);

-- 14. fx_quotes
CREATE TABLE IF NOT EXISTS fx_quotes (
    from_currency VARCHAR(3) NOT NULL,
    to_currency VARCHAR(3) NOT NULL,
    rate_as_of DATE NOT NULL,
    rate NUMERIC(24,12) NOT NULL,
    source TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_currency, to_currency, rate_as_of),
    CONSTRAINT chk_fx_quotes_rate CHECK (rate > 0)
);

-- 15. statement_lines
CREATE TABLE IF NOT EXISTS statement_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    request_id UUID NOT NULL,
    account_id UUID NOT NULL,
    row_no INTEGER NOT NULL,
    extracted_payload JSONB NOT NULL,
    applied_transaction_id UUID,
    final_action TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_statement_lines_row_no CHECK (row_no >= 1),
    CONSTRAINT chk_statement_lines_final_action CHECK (final_action IN ('create', 'link', 'skip') OR final_action IS NULL),
    CONSTRAINT fk_statement_lines_request FOREIGN KEY (household_id, request_id) REFERENCES ingestion_requests(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_statement_lines_account FOREIGN KEY (household_id, account_id) REFERENCES accounts(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_statement_lines_applied_txn FOREIGN KEY (household_id, applied_transaction_id) REFERENCES transactions(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_statement_lines_household_id UNIQUE (household_id, id),
    CONSTRAINT uq_statement_lines_row UNIQUE (request_id, row_no)
);

-- 16. audit_events
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    actor_type TEXT NOT NULL,
    actor_user_id UUID,
    actor_device_id UUID,
    source_request_id UUID,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    action TEXT NOT NULL,
    before_data JSONB,
    after_data JSONB,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_audit_events_actor_type CHECK (actor_type IN ('user', 'device', 'system')),
    CONSTRAINT chk_audit_events_action CHECK (action IN (
        'create', 'update', 'void', 'replace', 'close', 'reopen',
        'confirm_flows', 'fill_reporting_fx', 'acknowledge_metadata',
        'skip_period', 'link_statement', 'pause_schedule',
        'resume_schedule', 'cancel_schedule'
    )),
    CONSTRAINT fk_audit_events_user FOREIGN KEY (household_id, actor_user_id) REFERENCES household_members(household_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_audit_events_device FOREIGN KEY (household_id, actor_device_id) REFERENCES devices(household_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_audit_events_request FOREIGN KEY (household_id, source_request_id) REFERENCES ingestion_requests(household_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_audit_events_household_entity ON audit_events (household_id, entity_type, entity_id, id DESC);

-- Immutable audit events trigger
CREATE OR REPLACE FUNCTION trg_audit_events_immutable_func()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only and cannot be updated or deleted';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events;
CREATE TRIGGER trg_audit_events_immutable
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW
EXECUTE FUNCTION trg_audit_events_immutable_func();
