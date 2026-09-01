-- 034_add_contour_llm_classification.sql
--
-- LLM classification persistence for strategic content contours.
--
-- Contract:
--   - raw model run is stored separately from positive assignments;
--   - effective_response contains deterministic post-processing
--     (currently C4 provenance gate);
--   - only positive effective decisions are materialized into
--     content_contour_assignments;
--   - LLM assignments are CANDIDATE in v1.

CREATE TABLE contour_classification_prompts (
    prompt_id      smallint PRIMARY KEY,
    prompt_name    text NOT NULL,
    prompt_version text NOT NULL,
    schema_version text NOT NULL,
    prompt_text    text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),

    UNIQUE (prompt_name, prompt_version)
);


CREATE TABLE contour_classification_runs (
    run_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    content_id          uuid NOT NULL
        REFERENCES content_items(content_id),

    llm_model_id        smallint NOT NULL
        REFERENCES llm_models(llm_model_id),

    prompt_id           smallint NOT NULL
        REFERENCES contour_classification_prompts(prompt_id),

    attempt_no          smallint NOT NULL DEFAULT 1
        CHECK (attempt_no > 0),

    status              text NOT NULL
        CHECK (status IN ('valid', 'invalid', 'transport_error')),

    errors              jsonb
        CHECK (errors IS NULL OR jsonb_typeof(errors) = 'array'),

    -- Exact model output before deterministic business rules.
    raw_response        text,

    -- Validated result after deterministic business rules
    -- such as the C4 provenance eligibility gate.
    effective_response  jsonb
        CHECK (
            effective_response IS NULL
            OR jsonb_typeof(effective_response) = 'object'
        ),

    c4_eligible         boolean,
    c4_overridden       boolean NOT NULL DEFAULT false,

    finish_reason       text,

    completion_tokens   integer
        CHECK (
            completion_tokens IS NULL
            OR completion_tokens >= 0
        ),

    code_revision       text NOT NULL
        CHECK (btrim(code_revision) <> ''),

    latency_ms          integer
        CHECK (latency_ms IS NULL OR latency_ms >= 0),

    created_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (
        content_id,
        llm_model_id,
        prompt_id,
        attempt_no
    ),

    UNIQUE (run_id, content_id)
);


ALTER TABLE content_contour_assignments
    ADD COLUMN classification_run_id uuid;

ALTER TABLE content_contour_assignments
    ADD CONSTRAINT content_contour_assignments_classification_run_fkey
    FOREIGN KEY (classification_run_id, content_id)
        REFERENCES contour_classification_runs (
            run_id,
            content_id
        );


-- Replace v1 evidence constraints with the same contract plus
-- llm_classification evidence.
ALTER TABLE content_contour_assignments
    DROP CONSTRAINT content_contour_assignments_evidence_type_check,
    DROP CONSTRAINT content_contour_assignments_check;


ALTER TABLE content_contour_assignments
    ADD CONSTRAINT content_contour_assignments_evidence_type_check
    CHECK (
        evidence_type IN (
            'exact_reference',
            'semantic_reference',
            'deterministic_rule',
            'llm_classification'
        )
    );


ALTER TABLE content_contour_assignments
    ADD CONSTRAINT content_contour_assignments_check
    CHECK (
        (
            evidence_type = 'exact_reference'
            AND reference_id IS NOT NULL
            AND embedding_model_id IS NULL
            AND rule_code IS NULL
            AND score IS NULL
            AND classification_run_id IS NULL
        )
        OR
        (
            evidence_type = 'semantic_reference'
            AND status = 'candidate'
            AND reference_id IS NOT NULL
            AND embedding_model_id IS NOT NULL
            AND rule_code IS NULL
            AND score IS NOT NULL
            AND classification_run_id IS NULL
        )
        OR
        (
            evidence_type = 'deterministic_rule'
            AND reference_id IS NULL
            AND embedding_model_id IS NULL
            AND rule_code IS NOT NULL
            AND score IS NULL
            AND classification_run_id IS NULL
        )
        OR
        (
            evidence_type = 'llm_classification'
            AND status = 'candidate'
            AND reference_id IS NULL
            AND embedding_model_id IS NULL
            AND rule_code IS NULL
            AND score IS NULL
            AND classification_run_id IS NOT NULL
        )
    );


CREATE INDEX idx_content_contour_assignments_classification_run
    ON content_contour_assignments (classification_run_id)
    WHERE classification_run_id IS NOT NULL;
