-- VibeLedger Migration: 0006_statement_candidates
-- Authority: docs/architecture/PHYSICAL_SCHEMA.md

CREATE TABLE statement_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES reconciliation_batches(id) ON DELETE CASCADE,
    source_page_no INTEGER,
    source_row_no INTEGER,
    transaction_on DATE,
    posted_on DATE,
    description_raw TEXT NOT NULL,
    description_normalized TEXT,
    amount NUMERIC(20,6) NOT NULL,
    currency CHAR(3) NOT NULL,
    direction TEXT NOT NULL,
    line_type TEXT NOT NULL,
    match_status TEXT NOT NULL DEFAULT 'unmatched',
    matched_transaction_id UUID REFERENCES transactions(id) ON DELETE SET NULL,
    confidence NUMERIC(5,4),
    line_fingerprint BYTEA,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_stmt_lines_amount CHECK (amount > 0),
    CONSTRAINT chk_stmt_lines_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_stmt_lines_direction CHECK (direction IN ('debit', 'credit', 'unknown')),
    CONSTRAINT chk_stmt_lines_type CHECK (line_type IN ('expense', 'income', 'transfer', 'refund', 'fee', 'unknown')),
    CONSTRAINT chk_stmt_lines_match_status CHECK (match_status IN ('unmatched', 'matched', 'new_candidate', 'ambiguous', 'ignored')),
    CONSTRAINT chk_stmt_lines_confidence CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 1)),
    CONSTRAINT chk_stmt_lines_page_no CHECK (source_page_no IS NULL OR source_page_no > 0),
    CONSTRAINT chk_stmt_lines_row_no CHECK (source_row_no IS NULL OR source_row_no > 0)
);

CREATE TABLE reconciliation_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES reconciliation_batches(id) ON DELETE CASCADE,
    statement_line_id UUID REFERENCES statement_lines(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    target_transaction_id UUID REFERENCES transactions(id) ON DELETE SET NULL,
    payload JSONB NOT NULL,
    confidence NUMERIC(5,4),
    reason_code TEXT,
    reason_detail TEXT,
    resolved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    resolved_at TIMESTAMPTZ,
    applied_transaction_id UUID REFERENCES transactions(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_candidates_type CHECK (candidate_type IN (
        'match',
        'create_transaction',
        'create_transfer',
        'refund',
        'adjustment',
        'snapshot',
        'investment_pnl',
        'recognize_installment'
    )),
    CONSTRAINT chk_candidates_status CHECK (status IN ('proposed', 'needs_review', 'accepted', 'rejected', 'applied')),
    CONSTRAINT chk_candidates_confidence CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 1))
);
