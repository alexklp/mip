-- 041_add_segment_claim_scope.sql
--
-- Extend the existing claim-extraction persistence model with optional
-- segment-level evidence while preserving the current whole-content path.
--
-- Scope contract:
--
--   whole-content run:
--     content_id = parent content
--     segment_id = NULL
--
--   segment run:
--     content_id = parent content provenance
--     segment_id = concrete content_segments.segment_id
--
-- Claims remain in the existing claims table.
-- evidence_start/evidence_end are relative to the evidence text selected
-- by the run scope:
--   segment_id IS NULL     -> content_items.text_content
--   segment_id IS NOT NULL -> content_segments.text_content
--
-- Application contract for segment runs:
-- worker MUST verify that segment_id resolves through
-- content_segments -> segmentation_runs -> occurrence_content ->
-- item_occurrences to the same content_id stored on the run.
--
-- No existing rows are rewritten. All pre-041 runs remain whole-content
-- runs because segment_id is NULL.

ALTER TABLE claim_extraction_runs
    ADD COLUMN segment_id uuid
        REFERENCES content_segments(segment_id);

-- The old uniqueness contract cannot remain: two different segments of the
-- same content need independent attempt_no sequences.
ALTER TABLE claim_extraction_runs
    DROP CONSTRAINT
        claim_extraction_runs_content_id_llm_model_id_prompt_id_att_key;

-- Preserve the exact legacy identity for whole-content runs.
CREATE UNIQUE INDEX uq_claim_extraction_runs_content_scope
    ON claim_extraction_runs (
        content_id,
        llm_model_id,
        prompt_id,
        attempt_no
    )
    WHERE segment_id IS NULL;

-- Segment-level identity.
CREATE UNIQUE INDEX uq_claim_extraction_runs_segment_scope
    ON claim_extraction_runs (
        segment_id,
        llm_model_id,
        prompt_id,
        attempt_no
    )
    WHERE segment_id IS NOT NULL;

CREATE INDEX idx_claim_extraction_runs_segment_id
    ON claim_extraction_runs(segment_id)
    WHERE segment_id IS NOT NULL;
