-- VibeLedger Migration: 0002_identity_accounts
-- Authority: docs/architecture/PHYSICAL_SCHEMA.md

CREATE TABLE households (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    reporting_currency CHAR(3) NOT NULL DEFAULT 'CNY',
    ledger_start_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_households_currency CHECK (reporting_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_households_status CHECK (status IN ('active', 'archived'))
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_subject TEXT NOT NULL UNIQUE,
    email __CITEXT_TYPE__ UNIQUE,
    display_name TEXT NOT NULL,
    default_currency CHAR(3) NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_users_currency CHECK (default_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_users_status CHECK (status IN ('active', 'disabled'))
);

CREATE TABLE household_members (
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (household_id, user_id),
    CONSTRAINT chk_household_members_role CHECK (role IN ('owner', 'member'))
);

CREATE TABLE devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    token_hash BYTEA NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    client_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT chk_devices_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT chk_devices_revoked_consistency CHECK (
        (status = 'active' AND revoked_at IS NULL)
        OR
        (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

CREATE TABLE accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    institution TEXT,
    account_type TEXT NOT NULL,
    currency CHAR(3) NOT NULL,
    owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    linked_cash_account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
    billing_day SMALLINT,
    due_day SMALLINT,
    status TEXT NOT NULL DEFAULT 'active',
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_accounts_type CHECK (account_type IN ('cash', 'savings', 'credit', 'investment')),
    CONSTRAINT chk_accounts_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_accounts_billing_day CHECK (billing_day IS NULL OR (billing_day BETWEEN 1 AND 31)),
    CONSTRAINT chk_accounts_due_day CHECK (due_day IS NULL OR (due_day BETWEEN 1 AND 31)),
    CONSTRAINT chk_accounts_status CHECK (status IN ('active', 'inactive')),
    CONSTRAINT chk_accounts_linked_cash_not_self CHECK (linked_cash_account_id IS NULL OR linked_cash_account_id <> id),
    CONSTRAINT chk_accounts_credit_billing_days CHECK (
        account_type = 'credit'
        OR (billing_day IS NULL AND due_day IS NULL)
    )
);

CREATE TABLE account_state (
    account_id UUID PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    ledger_balance NUMERIC(20,6) NOT NULL DEFAULT 0.000000,
    initialized_at TIMESTAMPTZ,
    last_transaction_at TIMESTAMPTZ,
    last_authoritative_snapshot_at TIMESTAMPTZ,
    row_version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    alias_text TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    CONSTRAINT chk_account_aliases_status CHECK (status IN ('active', 'inactive'))
);

CREATE TABLE categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    category_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_categories_type CHECK (category_type IN ('expense', 'income')),
    CONSTRAINT chk_categories_status CHECK (status IN ('active', 'inactive'))
);
