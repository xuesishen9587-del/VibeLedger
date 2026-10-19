-- Descriptive metadata only; historical records deliberately remain unfilled.
ALTER TABLE transactions
    ADD COLUMN expense_content varchar(160),
    ADD COLUMN expense_content_source text,
    ADD CONSTRAINT transactions_expense_content_check CHECK (
        (expense_content IS NULL AND expense_content_source IS NULL) OR
        (expense_content IS NOT NULL AND btrim(expense_content) <> '' AND
         expense_content_source IS NOT NULL AND expense_content_source IN ('manual','screenshot','user_note','statement','schedule')));

ALTER TABLE spending_schedules
    ADD COLUMN expense_content varchar(160),
    ADD COLUMN expense_content_source text,
    ADD CONSTRAINT schedules_expense_content_check CHECK (
        (expense_content IS NULL AND expense_content_source IS NULL) OR
        (expense_content IS NOT NULL AND btrim(expense_content) <> '' AND
         expense_content_source IS NOT NULL AND expense_content_source IN ('manual','screenshot','user_note','statement','schedule')));

-- Each materialized period owns a snapshot; plan edits affect future periods only.
ALTER TABLE schedule_occurrences
    ADD COLUMN expense_content varchar(160),
    ADD COLUMN expense_content_source text,
    ADD CONSTRAINT occurrences_expense_content_check CHECK (
        (expense_content IS NULL AND expense_content_source IS NULL) OR
        (expense_content IS NOT NULL AND btrim(expense_content) <> '' AND
         expense_content_source IS NOT NULL AND expense_content_source IN ('manual','screenshot','user_note','statement','schedule')));
