-- VibeLedger Migration: 0007_installments
-- Authority: docs/architecture/PHYSICAL_SCHEMA.md

CREATE TABLE installment_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE RESTRICT,
    credit_account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    purchase_occurred_on DATE NOT NULL,
    merchant TEXT,
    original_amount NUMERIC(20,6) NOT NULL,
    original_currency CHAR(3) NOT NULL,
    account_principal_amount NUMERIC(20,6),
    account_currency CHAR(3) NOT NULL,
    total_periods SMALLINT NOT NULL,
    first_statement_month DATE,
    status TEXT NOT NULL DEFAULT 'pending_first_bill',
    source_request_id UUID REFERENCES ingestion_requests(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_inst_plans_orig_amount CHECK (original_amount > 0),
    CONSTRAINT chk_inst_plans_principal CHECK (account_principal_amount IS NULL OR account_principal_amount > 0),
    CONSTRAINT chk_inst_plans_periods CHECK (total_periods BETWEEN 2 AND 120),
    CONSTRAINT chk_inst_plans_orig_currency CHECK (original_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_inst_plans_account_currency CHECK (account_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_inst_plans_status CHECK (status IN ('pending_first_bill', 'active', 'completed', 'cancelled'))
);

CREATE TABLE installment_periods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES installment_plans(id) ON DELETE CASCADE,
    period_no SMALLINT NOT NULL,
    recognition_month DATE,
    scheduled_amount NUMERIC(20,6) NOT NULL,
    currency CHAR(3) NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    statement_line_id UUID REFERENCES statement_lines(id) ON DELETE SET NULL,
    expense_transaction_id UUID REFERENCES transactions(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_installment_periods_plan_no UNIQUE (plan_id, period_no),
    CONSTRAINT chk_inst_periods_period_no CHECK (period_no > 0),
    CONSTRAINT chk_inst_periods_amount CHECK (scheduled_amount > 0),
    CONSTRAINT chk_inst_periods_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_inst_periods_status CHECK (status IN ('scheduled', 'billed', 'cancelled')),
    CONSTRAINT chk_inst_periods_expense_consistency CHECK (
        (status = 'billed' AND expense_transaction_id IS NOT NULL)
        OR
        (status IN ('scheduled', 'cancelled') AND expense_transaction_id IS NULL)
    )
);
