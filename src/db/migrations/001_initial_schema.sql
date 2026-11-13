-- Migration: 001_initial_schema.sql
-- Description: Core relational schema, composite keys, indexes, and immutability triggers

-- 1. Ingestion Runs (Batch drops)
CREATE TABLE IF NOT EXISTS ingestion_runs (
    drop_id VARCHAR(128) PRIMARY KEY,
    ingest_order INT NOT NULL UNIQUE CHECK (ingest_order >= 1),
    status VARCHAR(32) NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 2. Ingestion File Checkpoints
CREATE TABLE IF NOT EXISTS ingestion_file_checkpoints (
    id BIGSERIAL PRIMARY KEY,
    drop_id VARCHAR(128) NOT NULL REFERENCES ingestion_runs(drop_id) ON DELETE CASCADE,
    file_type VARCHAR(32) NOT NULL CHECK (file_type IN ('LEAD_CSV', 'EVENT_JSONL')),
    file_path VARCHAR(512) NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    file_hash CHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('PROCESSED', 'SKIPPED')),
    records_count INT NOT NULL DEFAULT 0 CHECK (records_count >= 0),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_drop_file_checkpoint UNIQUE (drop_id, file_path)
);

-- 3. Dispositions Catalogue
CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id VARCHAR(64) NOT NULL,
    code VARCHAR(64) NOT NULL,
    label VARCHAR(255) NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    is_success BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (workflow_id, code)
);

-- 4. Leads Master Table
CREATE TABLE IF NOT EXISTS leads (
    lead_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id VARCHAR(64) NOT NULL,
    phone_bidx CHAR(64) NOT NULL,
    phone_encrypted TEXT NOT NULL,
    customer_name_encrypted TEXT NOT NULL,
    external_ref VARCHAR(128) NOT NULL,
    batch_id VARCHAR(128) NOT NULL,
    policy_expiry DATE,
    premium_amount NUMERIC(12, 2) CHECK (premium_amount IS NULL OR premium_amount >= 0.00),
    order_date DATE,
    product VARCHAR(128),
    preferred_language VARCHAR(16) NOT NULL DEFAULT 'en',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_leads_identity UNIQUE (workflow_id, phone_bidx)
);

CREATE INDEX IF NOT EXISTS idx_leads_workflow_phone ON leads (workflow_id, phone_bidx);

-- 5. Lead Rejections
CREATE TABLE IF NOT EXISTS lead_rejections (
    id BIGSERIAL PRIMARY KEY,
    drop_id VARCHAR(128) NOT NULL REFERENCES ingestion_runs(drop_id) ON DELETE CASCADE,
    workflow_id VARCHAR(64) NOT NULL,
    batch_id VARCHAR(128) NOT NULL,
    row_number INT NOT NULL CHECK (row_number >= 1),
    reason VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_rejection_row UNIQUE (drop_id, workflow_id, batch_id, row_number)
);

-- 6. Interaction Ledger
CREATE TABLE IF NOT EXISTS interaction_ledger (
    event_id VARCHAR(128) PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    workflow_id VARCHAR(64) NOT NULL,
    call_id VARCHAR(128) NOT NULL,
    phone_bidx CHAR(64),
    phone_encrypted TEXT,
    disposition_code VARCHAR(64),
    occurred_at TIMESTAMPTZ NOT NULL,
    payload_json JSONB NOT NULL,
    lead_id UUID REFERENCES leads(lead_id) ON DELETE RESTRICT,
    is_orphan BOOLEAN NOT NULL DEFAULT FALSE,
    drop_id VARCHAR(128) NOT NULL REFERENCES ingestion_runs(drop_id) ON DELETE CASCADE,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ledger_disposition FOREIGN KEY (workflow_id, disposition_code)
        REFERENCES dispositions(workflow_id, code) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_ledger_call_id ON interaction_ledger (call_id);
CREATE INDEX IF NOT EXISTS idx_ledger_lead_id ON interaction_ledger (lead_id);
CREATE INDEX IF NOT EXISTS idx_ledger_workflow_phone ON interaction_ledger (workflow_id, phone_bidx);
CREATE INDEX IF NOT EXISTS idx_ledger_is_orphan ON interaction_ledger (is_orphan) WHERE is_orphan = TRUE;
CREATE INDEX IF NOT EXISTS idx_ledger_occurred_at ON interaction_ledger (occurred_at);

-- 7. Immutability Trigger Functions and Triggers

-- 7.1 Orphan-to-Linked State Transition Trigger (NULL-safe)
CREATE OR REPLACE FUNCTION fn_enforce_ledger_immutability()
RETURNS TRIGGER AS $$
DECLARE
    v_lead_workflow VARCHAR(64);
    v_lead_phone_bidx CHAR(64);
    v_call_lead_count INT;
    v_associated_lead_id UUID;
BEGIN
    -- 1. Strictly forbid modifying already-linked events
    IF OLD.is_orphan = FALSE OR OLD.lead_id IS NOT NULL THEN
        RAISE EXCEPTION 'Illegal ledger modification: Already-linked events are permanently immutable (event_id: %).', OLD.event_id;
    END IF;

    -- 2. Verify that this update is strictly an orphan-to-linked transition
    IF NOT (OLD.lead_id IS NULL AND OLD.is_orphan = TRUE AND NEW.lead_id IS NOT NULL AND NEW.is_orphan = FALSE) THEN
        RAISE EXCEPTION 'Illegal ledger transition: Only permitted transition is (lead_id NULL, is_orphan TRUE) -> (lead_id NOT NULL, is_orphan FALSE).';
    END IF;

    -- 3. Forbid modification of any core event attributes, metadata, or PII columns (NULL-safe comparison)
    IF (OLD.event_id IS DISTINCT FROM NEW.event_id OR
        OLD.event_type IS DISTINCT FROM NEW.event_type OR
        OLD.workflow_id IS DISTINCT FROM NEW.workflow_id OR
        OLD.call_id IS DISTINCT FROM NEW.call_id OR
        OLD.phone_bidx IS DISTINCT FROM NEW.phone_bidx OR
        OLD.phone_encrypted IS DISTINCT FROM NEW.phone_encrypted OR
        OLD.disposition_code IS DISTINCT FROM NEW.disposition_code OR
        OLD.occurred_at IS DISTINCT FROM NEW.occurred_at OR
        OLD.payload_json IS DISTINCT FROM NEW.payload_json OR
        OLD.drop_id IS DISTINCT FROM NEW.drop_id OR
        OLD.ingested_at IS DISTINCT FROM NEW.ingested_at) THEN
        RAISE EXCEPTION 'Immutable ledger violation: Event content and metadata cannot be modified during linking (event_id: %).', OLD.event_id;
    END IF;

    -- 4. Validate target lead existence and workflow alignment
    SELECT workflow_id, phone_bidx INTO v_lead_workflow, v_lead_phone_bidx
    FROM leads
    WHERE lead_id = NEW.lead_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Target lead not found: lead_id % does not exist.', NEW.lead_id;
    END IF;

    IF v_lead_workflow <> NEW.workflow_id THEN
        RAISE EXCEPTION 'Cross-workflow violation: Target lead workflow (%) does not match event workflow (%) for event_id %.',
            v_lead_workflow, NEW.workflow_id, OLD.event_id;
    END IF;

    -- 5. Standard events: phone blind index must match target lead phone blind index
    IF NEW.phone_bidx IS NOT NULL THEN
        IF v_lead_phone_bidx <> NEW.phone_bidx THEN
            RAISE EXCEPTION 'Phone mismatch violation: Event phone_bidx does not match target lead phone_bidx for event_id %.', OLD.event_id;
        END IF;
    END IF;

    -- 6. Override events without phone: must resolve through call_id to the unique lead associated with the call
    IF NEW.event_type = 'disposition.override' AND NEW.phone_bidx IS NULL THEN
        -- Retrieve count of distinct leads for this (workflow_id, call_id)
        SELECT COUNT(DISTINCT lead_id)
        INTO v_call_lead_count
        FROM interaction_ledger
        WHERE call_id = NEW.call_id
          AND workflow_id = NEW.workflow_id
          AND lead_id IS NOT NULL;

        IF v_call_lead_count = 0 THEN
            RAISE EXCEPTION 'Override association violation: No preceding event for call_id % in workflow % is linked to any lead.',
                NEW.call_id, NEW.workflow_id;
        ELSIF v_call_lead_count > 1 THEN
            RAISE EXCEPTION 'Ambiguous call association: Multiple conflicting leads (%) associated with call_id % in workflow %.',
                v_call_lead_count, NEW.call_id, NEW.workflow_id;
        END IF;

        -- Retrieve the unique associated lead_id (clean single-row lookup without UUID aggregate)
        SELECT lead_id INTO v_associated_lead_id
        FROM interaction_ledger
        WHERE call_id = NEW.call_id
          AND workflow_id = NEW.workflow_id
          AND lead_id IS NOT NULL
        LIMIT 1;

        -- Explicitly verify that the retrieved unique associated lead matches NEW.lead_id
        IF v_associated_lead_id IS DISTINCT FROM NEW.lead_id THEN
            RAISE EXCEPTION 'Override association mismatch: Target lead_id % does not match the unique lead (%) associated with call_id % in workflow %.',
                NEW.lead_id, v_associated_lead_id, NEW.call_id, NEW.workflow_id;
        END IF;
    END IF;

    -- All integrity invariants satisfied: allow transition
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ledger_immutability ON interaction_ledger;
CREATE TRIGGER trg_ledger_immutability
BEFORE UPDATE ON interaction_ledger
FOR EACH ROW
EXECUTE FUNCTION fn_enforce_ledger_immutability();

-- 7.2 Append-Only Deletion Prevention Trigger
CREATE OR REPLACE FUNCTION fn_prevent_ledger_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Append-only ledger violation: Deletion of records from interaction_ledger is strictly prohibited (event_id: %).', OLD.event_id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ledger_prevent_delete ON interaction_ledger;
CREATE TRIGGER trg_ledger_prevent_delete
BEFORE DELETE ON interaction_ledger
FOR EACH ROW
EXECUTE FUNCTION fn_prevent_ledger_delete();

-- 7.3 Append-Only Truncation Prevention Trigger
CREATE OR REPLACE FUNCTION fn_prevent_ledger_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Append-only ledger violation: TRUNCATE on interaction_ledger is strictly prohibited.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ledger_prevent_truncate ON interaction_ledger;
CREATE TRIGGER trg_ledger_prevent_truncate
BEFORE TRUNCATE ON interaction_ledger
FOR EACH STATEMENT
EXECUTE FUNCTION fn_prevent_ledger_truncate();
