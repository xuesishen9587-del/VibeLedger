-- VibeLedger Migration: 0005_snapshots_invest
-- Authority: docs/architecture/PHYSICAL_SCHEMA.md

CREATE TABLE account_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    as_of TIMESTAMPTZ NOT NULL,
    balance NUMERIC(20,6) NOT NULL,
    currency CHAR(3) NOT NULL,
    snapshot_type TEXT NOT NULL,
    source TEXT NOT NULL,
    reconciliation_batch_id UUID REFERENCES reconciliation_batches(id) ON DELETE SET NULL,
    source_request_id UUID REFERENCES ingestion_requests(id) ON DELETE SET NULL,
    is_authoritative BOOLEAN NOT NULL DEFAULT true,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_snapshots_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_snapshots_type CHECK (snapshot_type IN ('balance', 'investment_valuation')),
    CONSTRAINT chk_snapshots_source CHECK (source IN ('shortcut', 'statement', 'dashboard_manual'))
);

CREATE TABLE credit_card_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    as_of TIMESTAMPTZ NOT NULL,
    statement_period_start DATE,
    statement_period_end DATE,
    statement_balance NUMERIC(20,6),
    remaining_statement_due NUMERIC(20,6),
    unbilled_balance NUMERIC(20,6),
    current_outstanding NUMERIC(20,6),
    currency CHAR(3) NOT NULL,
    source TEXT NOT NULL,
    reconciliation_batch_id UUID REFERENCES reconciliation_batches(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_cc_snapshots_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_cc_snapshots_source CHECK (source IN ('statement', 'dashboard_manual', 'system_derived')),
    CONSTRAINT chk_cc_snapshots_statement_balance CHECK (statement_balance IS NULL OR statement_balance >= 0),
    CONSTRAINT chk_cc_snapshots_remaining_due CHECK (remaining_statement_due IS NULL OR remaining_statement_due >= 0),
    CONSTRAINT chk_cc_snapshots_unbilled CHECK (unbilled_balance IS NULL OR unbilled_balance >= 0),
    CONSTRAINT chk_cc_snapshots_outstanding CHECK (current_outstanding IS NULL OR current_outstanding >= 0),
    CONSTRAINT chk_cc_snapshots_period CHECK (
        statement_period_start IS NULL
        OR statement_period_end IS NULL
        OR statement_period_end >= statement_period_start
    )
);

CREATE TABLE investment_pnl_periods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    opening_snapshot_id UUID NOT NULL REFERENCES account_snapshots(id) ON DELETE RESTRICT,
    closing_snapshot_id UUID NOT NULL REFERENCES account_snapshots(id) ON DELETE RESTRICT,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    contributions_amount NUMERIC(20,6) NOT NULL DEFAULT 0.000000,
    withdrawals_amount NUMERIC(20,6) NOT NULL DEFAULT 0.000000,
    pnl_amount NUMERIC(20,6) NOT NULL,
    currency CHAR(3) NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    calculation_version INTEGER NOT NULL DEFAULT 1,
    reconciliation_batch_id UUID REFERENCES reconciliation_batches(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_investment_pnl_closing_snap UNIQUE (account_id, closing_snapshot_id),
    CONSTRAINT chk_inv_pnl_different_snapshots CHECK (opening_snapshot_id <> closing_snapshot_id),
    CONSTRAINT chk_inv_pnl_period CHECK (period_end > period_start),
    CONSTRAINT chk_inv_pnl_contributions CHECK (contributions_amount >= 0),
    CONSTRAINT chk_inv_pnl_withdrawals CHECK (withdrawals_amount >= 0),
    CONSTRAINT chk_inv_pnl_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_inv_pnl_status CHECK (status IN ('provisional', 'confirmed'))
);
