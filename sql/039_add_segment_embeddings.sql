-- 039_add_segment_embeddings.sql
--
-- Embeddings for persisted analytical content segments.
-- Reuses embedding_models registry and the existing BGE-M3 model identity.
--
-- Additive only. Does not alter content-level or claim-level embeddings.

CREATE TABLE segment_embeddings (
    segment_id           uuid NOT NULL
        REFERENCES content_segments(segment_id),

    embedding_model_id   smallint NOT NULL
        REFERENCES embedding_models(embedding_model_id),

    embedding            vector NOT NULL,

    created_at           timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (segment_id, embedding_model_id)
);

-- Same BGE-M3 / 1024-dim cosine pattern as content and claim embeddings.
CREATE INDEX idx_segment_embeddings_hnsw_bge_m3
    ON segment_embeddings
    USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
    WHERE embedding_model_id = 1;
