-- VibeLedger Migration: 0008_audit_events
-- Authority: docs/architecture/PHYSICAL_SCHEMA.md

CREATE TABLE audit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    actor_type TEXT NOT NULL,
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_device_id UUID REFERENCES devices(id) ON DELETE SET NULL,
    request_id UUID REFERENCES ingestion_requests(id) ON DELETE SET NULL,
    reconciliation_batch_id UUID REFERENCES reconciliation_batches(id) ON DELETE SET NULL,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    action TEXT NOT NULL,
    before_data JSONB,
    after_data JSONB,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_audit_actor_type CHECK (actor_type IN ('user', 'device', 'system')),
    CONSTRAINT chk_audit_action CHECK (action IN (
        'create',
        'update',
        'soft_delete',
        'restore',
        'confirm',
        'reject',
        'commit',
        'reconcile',
        'void'
    ))
);

-- Immutable audit_events trigger
CREATE OR REPLACE FUNCTION reject_audit_events_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: updates and deletes are forbidden';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_events_immutability
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW
EXECUTE FUNCTION reject_audit_events_mutation();
