-- 037_add_content_segmentation.sql
--
-- Persistence for accepted segmentation v1:
-- occurrence_content(success)
--   -> deterministic sectionize
--   -> Mamay adjacent-boundary same/new
--   -> deterministic contiguous segment assembly
--
-- Additive only. No scheduler, backfill or downstream rewiring.
--
-- APPLICATION CONTRACTS:
--
-- 1. Eligibility:
--    segmentation is allowed ONLY for occurrence_content.status='success'.
--
-- 2. Atomic success persistence:
--    a status='success' segmentation_run and ALL content_segments belonging
--    to it MUST be inserted in ONE DB transaction.
--
-- 3. Deterministic assembly validation before INSERT:
--    content_segments MUST form an ordered, contiguous, non-overlapping
--    partition of section ids 0..section_count-1:
--      - segment_index = 0..segment_count-1;
--      - ranges ordered by section_start;
--      - first section_start = 0;
--      - next.section_start = previous.section_end + 1;
--      - final section_end = section_count - 1.
--    These cross-row invariants are intentionally application-level in v1,
--    matching the existing claim_extraction run+children persistence pattern.
--
-- 4. A document producing zero analytical sections is NOT a successful
--    segmentation; it is persisted as sectionize_error.
--
-- 5. latency_ms is total wall-clock latency for the whole segmentation run.
--
-- 6. content_segments.text_hash = SHA-256 of UTF-8 text_content.

CREATE TABLE segmentation_prompts (
    prompt_id       smallint PRIMARY KEY,
    prompt_name     text NOT NULL,
    prompt_version  text NOT NULL,
    schema_version  text NOT NULL,
    prompt_text     text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (prompt_name, prompt_version)
);


CREATE TABLE segmentation_runs (
    segmentation_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    occurrence_content_id uuid NOT NULL
        REFERENCES occurrence_content(occurrence_content_id),

    sectionizer_version text NOT NULL,

    boundary_method text NOT NULL
        DEFAULT 'mamay_adjacent_boundary',

    llm_model_id smallint NOT NULL
        REFERENCES llm_models(llm_model_id),

    prompt_id smallint NOT NULL
        REFERENCES segmentation_prompts(prompt_id),

    attempt_no smallint NOT NULL DEFAULT 1
        CHECK (attempt_no > 0),

    status text NOT NULL
        CHECK (status IN (
            'success',
            'sectionize_error',
            'boundary_error',
            'assembly_error'
        )),

    section_count integer
        CHECK (section_count IS NULL OR section_count > 0),

    segment_count integer
        CHECK (segment_count IS NULL OR segment_count > 0),

    CHECK (
        segment_count IS NULL
        OR section_count IS NULL
        OR segment_count <= section_count
    ),

    -- Ordered array: one object per adjacent section boundary.
    --
    -- For a valid boundary decision the worker persists at least:
    --   left_section_id
    --   right_section_id
    --   boundary ('same'|'new')
    --   raw_response
    --
    -- Telemetry such as latency_ms, finish_reason and completion_tokens
    -- may be stored in the same object.
    --
    -- On boundary_error the array may contain valid preceding decisions
    -- plus the failed boundary including its raw_response/error metadata.
    boundary_decisions jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(boundary_decisions) = 'array'),

    errors jsonb
        CHECK (errors IS NULL OR jsonb_typeof(errors) = 'array'),

    code_revision text NOT NULL
        CHECK (btrim(code_revision) <> ''),

    -- Total wall-clock latency of the whole segmentation run.
    latency_ms integer
        CHECK (latency_ms IS NULL OR latency_ms >= 0),

    created_at timestamptz NOT NULL DEFAULT now(),

    CHECK (
        status <> 'success'
        OR (
            section_count IS NOT NULL
            AND segment_count IS NOT NULL
            AND jsonb_array_length(boundary_decisions)
                = GREATEST(section_count - 1, 0)
            AND errors IS NULL
        )
    ),

    CHECK (
        status = 'success'
        OR errors IS NOT NULL
    ),

    UNIQUE (
        occurrence_content_id,
        sectionizer_version,
        boundary_method,
        llm_model_id,
        prompt_id,
        attempt_no
    )
);


CREATE INDEX idx_segmentation_runs_occurrence_content_status
    ON segmentation_runs(
        occurrence_content_id,
        sectionizer_version,
        boundary_method,
        llm_model_id,
        prompt_id,
        status
    );


CREATE TABLE content_segments (
    segment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    segmentation_run_id uuid NOT NULL
        REFERENCES segmentation_runs(segmentation_run_id),

    -- Zero-based order. Writer validates that indexes are exactly
    -- 0..segment_count-1 and correspond to section_start ordering.
    segment_index integer NOT NULL
        CHECK (segment_index >= 0),

    -- Inclusive deterministic section range.
    section_start integer NOT NULL
        CHECK (section_start >= 0),

    section_end integer NOT NULL
        CHECK (section_end >= section_start),

    -- Frozen downstream representation.
    text_content text NOT NULL
        CHECK (btrim(text_content) <> ''),

    -- Lowercase SHA-256 hex of UTF-8 text_content.
    text_hash text NOT NULL
        CHECK (text_hash ~ '^[0-9a-f]{64}$'),

    created_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (segmentation_run_id, segment_index),
    UNIQUE (segmentation_run_id, section_start, section_end)
);


CREATE INDEX idx_content_segments_run
    ON content_segments(segmentation_run_id);
