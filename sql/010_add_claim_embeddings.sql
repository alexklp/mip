CREATE TABLE claim_embeddings (
    claim_id            uuid NOT NULL REFERENCES claims(claim_id),
    embedding_model_id  smallint NOT NULL REFERENCES embedding_models(embedding_model_id),
    embedding           vector NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, embedding_model_id)
);

-- Partial expression HNSW тільки для BGE-M3 (embedding_model_id=1), той самий
-- pattern, що idx_embeddings_hnsw_bge_m3 в sql/006_add_embeddings.sql.
CREATE INDEX idx_claim_embeddings_hnsw_bge_m3 ON claim_embeddings
    USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
    WHERE embedding_model_id = 1;
