-- 033_add_content_contour_assignments_v1.sql
--
-- Content Contours v1 persistence.
--
-- Contract:
--   - content may belong to multiple strategic monitoring contours;
--   - only positive assignments are persisted;
--   - absence of an assignment row means NONE;
--   - status is CONFIRMED or CANDIDATE;
--   - semantic-reference evidence alone is CANDIDATE only;
--   - sources.contour_id remains source-level provenance metadata.
--
-- Evidence v1 is stored directly on the assignment row.
-- If multiple persistent evidence records become necessary, evidence can be
-- normalized later without changing assignment identity.


-- Required for same-contour composite FK below.
CREATE UNIQUE INDEX uq_contour_reference_entry_contour
    ON contour_reference_entries (
        reference_id,
        monitoring_contour_id
    );


CREATE TABLE content_contour_assignments (
    assignment_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    content_id             uuid NOT NULL
        REFERENCES content_items(content_id),

    monitoring_contour_id  smallint NOT NULL
        REFERENCES monitoring_contours(monitoring_contour_id),

    assignment_version     smallint NOT NULL
        CHECK (assignment_version > 0),

    status                 text NOT NULL
        CHECK (status IN ('confirmed', 'candidate')),

    -- Optional semantic scope inside a contour.
    object_id              bigint,

    facet_code             text
        CHECK (facet_code IS NULL OR btrim(facet_code) <> ''),

    -- Winning evidence for this assignment.
    evidence_type          text NOT NULL
        CHECK (
            evidence_type IN (
                'exact_reference',
                'semantic_reference',
                'deterministic_rule'
            )
        ),

    reference_id           bigint,

    -- Required only for semantic-reference evidence.
    embedding_model_id     smallint,

    rule_code              text
        CHECK (rule_code IS NULL OR btrim(rule_code) <> ''),

    -- Raw cosine similarity.
    -- Ranking/evidence value, not probability/confidence.
    score                  real
        CHECK (score IS NULL OR score BETWEEN -1 AND 1),

    reason                 text,

    code_revision          text NOT NULL
        CHECK (btrim(code_revision) <> ''),

    created_at             timestamptz NOT NULL DEFAULT now(),

    -- object_id must belong to the same strategic contour.
    FOREIGN KEY (object_id, monitoring_contour_id)
        REFERENCES contour_reference_objects (
            object_id,
            monitoring_contour_id
        ),

    -- reference_id must belong to the same strategic contour.
    FOREIGN KEY (reference_id, monitoring_contour_id)
        REFERENCES contour_reference_entries (
            reference_id,
            monitoring_contour_id
        ),

    -- For semantic evidence, prove that this content embedding exists.
    FOREIGN KEY (content_id, embedding_model_id)
        REFERENCES embeddings (
            content_id,
            embedding_model_id
        ),

    -- For semantic evidence, prove that the reference embedding exists
    -- under the same embedding model.
    FOREIGN KEY (reference_id, embedding_model_id)
        REFERENCES contour_reference_embeddings (
            reference_id,
            embedding_model_id
        ),

    -- Evidence-shape contract.
    CHECK (
        (
            evidence_type = 'exact_reference'
            AND reference_id IS NOT NULL
            AND embedding_model_id IS NULL
            AND rule_code IS NULL
            AND score IS NULL
        )
        OR
        (
            evidence_type = 'semantic_reference'
            AND status = 'candidate'
            AND reference_id IS NOT NULL
            AND embedding_model_id IS NOT NULL
            AND rule_code IS NULL
            AND score IS NOT NULL
        )
        OR
        (
            evidence_type = 'deterministic_rule'
            AND reference_id IS NULL
            AND embedding_model_id IS NULL
            AND rule_code IS NOT NULL
            AND score IS NULL
        )
    )
);


-- One result for the same semantic scope and classifier contract.
CREATE UNIQUE INDEX uq_content_contour_assignment_scope
    ON content_contour_assignments (
        content_id,
        monitoring_contour_id,
        assignment_version,
        facet_code,
        object_id
    )
    NULLS NOT DISTINCT;


CREATE INDEX idx_content_contour_assignments_content
    ON content_contour_assignments (content_id);


CREATE INDEX idx_content_contour_assignments_contour_status
    ON content_contour_assignments (
        monitoring_contour_id,
        status
    );
