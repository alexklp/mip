-- 040_add_segment_routing.sql
--
-- Segment Routing v1.
--
-- Deterministic semantic routing over persisted content_segments embeddings.
-- Reuses:
--   - embedding_model_id=1 (BAAI/bge-m3)
--   - content_routing/prototypes_v1.json
--   - score = max(sim relevant) - max(sim irrelevant)
--
-- Segment-level thresholds are calibrated separately from whole-content routing:
--   T_SKIP    = -0.07
--   T_ANALYZE =  0.095
--
-- The threshold values themselves live in the worker contract and are
-- versioned by routing_version/code revision, matching content routing v1.
--
-- Additive only. Does not alter content_routing_decisions.

CREATE TABLE segment_routing_decisions (
    segment_routing_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    segment_id           uuid NOT NULL
        REFERENCES content_segments(segment_id),

    routing_version      smallint NOT NULL
        CHECK (routing_version > 0),

    embedding_model_id   smallint NOT NULL
        REFERENCES embedding_models(embedding_model_id),

    decision             text NOT NULL
        CHECK (decision IN ('analyze', 'maybe', 'skip')),

    score                real NOT NULL
        CHECK (score BETWEEN -2 AND 2),

    reason               text,

    code_revision        text NOT NULL
        CHECK (btrim(code_revision) <> ''),

    created_at           timestamptz NOT NULL DEFAULT now(),

    UNIQUE (segment_id, routing_version)
);

CREATE INDEX idx_segment_routing_decisions_decision
    ON segment_routing_decisions(routing_version, decision);
