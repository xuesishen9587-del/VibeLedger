-- VibeLedger Migration: 0004_transactions
-- Authority: docs/architecture/PHYSICAL_SCHEMA.md

CREATE TABLE transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    transaction_type TEXT NOT NULL,
    occurred_on DATE NOT NULL,
    occurred_at TIMESTAMPTZ,
    posted_on DATE,
    from_account_id UUID REFERENCES accounts(id) ON DELETE RESTRICT,
    to_account_id UUID REFERENCES accounts(id) ON DELETE RESTRICT,
    original_amount NUMERIC(20,6) NOT NULL,
    original_currency CHAR(3) NOT NULL,
    from_amount NUMERIC(20,6),
    from_currency CHAR(3),
    to_amount NUMERIC(20,6),
    to_currency CHAR(3),
    effective_fx_rate NUMERIC(24,12),
    account_leg_status TEXT,
    reporting_amount NUMERIC(20,6),
    reporting_currency CHAR(3),
    reporting_fx_rate NUMERIC(24,12),
    reporting_fx_locked_at TIMESTAMPTZ,
    category_id UUID REFERENCES categories(id) ON DELETE RESTRICT,
    merchant TEXT,
    merchant_normalized TEXT,
    remarks TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'committed',
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    confidence NUMERIC(5,4),
    source_request_id UUID REFERENCES ingestion_requests(id) ON DELETE SET NULL,
    statement_batch_id UUID REFERENCES reconciliation_batches(id) ON DELETE SET NULL,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_by_device_id UUID REFERENCES devices(id) ON DELETE SET NULL,
    row_version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    deleted_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    delete_reason TEXT,
    CONSTRAINT chk_transactions_type CHECK (transaction_type IN (
        'expense',
        'cash_income',
        'refund',
        'transfer',
        'fee',
        'reconciliation_adjustment',
        'opening_balance'
    )),
    CONSTRAINT chk_transactions_source CHECK (source IN (
        'shortcut',
        'statement',
        'dashboard_manual',
        'reconciliation',
        'installment',
        'system'
    )),
    CONSTRAINT chk_transactions_status CHECK (status IN ('committed', 'voided')),
    CONSTRAINT chk_transactions_lifecycle CHECK (
        (status = 'committed' AND deleted_at IS NULL AND delete_reason IS NULL)
        OR
        (status = 'voided' AND deleted_at IS NOT NULL AND delete_reason IS NOT NULL)
    ),
    CONSTRAINT chk_transactions_leg_status CHECK (account_leg_status IS NULL OR account_leg_status IN ('estimated', 'authoritative')),
    CONSTRAINT chk_transactions_verification_status CHECK (verification_status IN (
        'unverified',
        'user_confirmed',
        'statement_confirmed',
        'manual_confirmed',
        'system_confirmed'
    )),
    CONSTRAINT chk_transactions_confidence CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 1)),
    CONSTRAINT chk_transactions_orig_amount CHECK (original_amount > 0),
    CONSTRAINT chk_transactions_from_amount CHECK (from_amount IS NULL OR from_amount > 0),
    CONSTRAINT chk_transactions_to_amount CHECK (to_amount IS NULL OR to_amount > 0),
    CONSTRAINT chk_transactions_fx_rate CHECK (effective_fx_rate IS NULL OR effective_fx_rate > 0),
    CONSTRAINT chk_transactions_orig_currency CHECK (original_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_transactions_from_currency CHECK (from_currency IS NULL OR from_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_transactions_to_currency CHECK (to_currency IS NULL OR to_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_transactions_rep_currency CHECK (reporting_currency IS NULL OR reporting_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_transactions_accounts_different CHECK (from_account_id IS NULL OR to_account_id IS NULL OR from_account_id <> to_account_id)
);

CREATE TABLE transaction_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_transaction_id UUID NOT NULL REFERENCES transactions(id) ON DELETE RESTRICT,
    target_transaction_id UUID NOT NULL REFERENCES transactions(id) ON DELETE RESTRICT,
    relation_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_trans_links_not_self CHECK (source_transaction_id <> target_transaction_id),
    CONSTRAINT chk_trans_links_relation_type CHECK (relation_type IN ('refund_of', 'reversal_of', 'installment_of'))
);
