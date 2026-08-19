-- 006_add_embeddings.sql
-- Embeddings vertical slice. embedding_model_id зафіксовано ВРУЧНУ (не IDENTITY) —
-- потрібен детермінований ID для BGE-M3 (=1), щоб зіслатись на нього в partial
-- HNSW-індексі нижче, в тому ж файлі. embedding — generic vector (без фіксованої
-- розмірності в типі колонки): кожна майбутня модель отримує власний partial
-- expression-індекс (свій cast + WHERE embedding_model_id=N), не займаючи чужий.
-- Побічний ефект: cast на "чужу" розмірність під WHERE-умовою впаде при INSERT/UPDATE —
-- це dimension-safety на рівні індексу, безкоштовно.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE embedding_models (
    embedding_model_id  smallint PRIMARY KEY,
    model_name           text NOT NULL,
    model_revision        text NOT NULL,
    framework              text NOT NULL,
    framework_version       text NOT NULL,
    dimension                 smallint NOT NULL,
    metric                     text NOT NULL CHECK (metric IN ('cosine', 'l2', 'inner_product')),
    encoding_params             jsonb NOT NULL,
    created_at                   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_name, model_revision, framework, framework_version)
);

INSERT INTO embedding_models
    (embedding_model_id, model_name, model_revision, framework, framework_version,
     dimension, metric, encoding_params)
VALUES (
    1,
    'BAAI/bge-m3',
    '5617a9f61b028005a4858fdac845db406aefb181',
    'sentence-transformers',
    '5.7.0',
    1024,
    'cosine',
    '{"normalize_embeddings": true, "input_field": "text_content"}'::jsonb
);

CREATE TABLE embeddings (
    content_id            uuid NOT NULL REFERENCES content_items(content_id),
    embedding_model_id    smallint NOT NULL REFERENCES embedding_models(embedding_model_id),
    embedding               vector NOT NULL,
    created_at                timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (content_id, embedding_model_id)
);

-- Partial expression HNSW тільки для BGE-M3 (embedding_model_id=1).
CREATE INDEX idx_embeddings_hnsw_bge_m3 ON embeddings
    USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
    WHERE embedding_model_id = 1;
