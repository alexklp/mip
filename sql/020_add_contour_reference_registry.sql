-- 020_add_contour_reference_registry.sql
--
-- Content Contours v2: reference registry.
--
-- ВАЖЛИВО:
-- sources.contour_id залишається legacy/source-level provenance metadata.
-- Ця схема НЕ використовує і НЕ змінює sources.contour_id.
--
-- content_contour_assignments тут навмисно НЕ створюються:
-- спочатку reference registry -> calibration scan -> validation,
-- лише після цього persistence класифікації контенту.


-- 1. Чотири стратегічні моніторингові контури.
CREATE TABLE monitoring_contours (
    monitoring_contour_id  smallint PRIMARY KEY
        CHECK (monitoring_contour_id BETWEEN 1 AND 4),

    code                   text NOT NULL UNIQUE,
    name                   text NOT NULL,
    description            text,

    active                 boolean NOT NULL DEFAULT true,
    created_at             timestamptz NOT NULL DEFAULT now(),

    CHECK (length(btrim(code)) > 0),
    CHECK (length(btrim(name)) > 0)
);


-- 2. Канонічні об'єкти моніторингу.
--
-- Приклади класів об'єктів:
-- military_unit, organization, person, institution, location, programme.
--
-- object_type навмисно не обмежений ENUM/CHECK:
-- реальна таксономія буде уточнюватися reference data без зміни DDL.
CREATE TABLE contour_reference_objects (
    object_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    monitoring_contour_id  smallint NOT NULL
        REFERENCES monitoring_contours(monitoring_contour_id),

    object_type            text NOT NULL,
    canonical_name         text NOT NULL,

    active                 boolean NOT NULL DEFAULT true,
    source_note            text,
    created_at             timestamptz NOT NULL DEFAULT now(),

    CHECK (length(btrim(object_type)) > 0),
    CHECK (length(btrim(canonical_name)) > 0),

    UNIQUE (monitoring_contour_id, object_type, canonical_name),
    UNIQUE (object_id, monitoring_contour_id)
);


-- 3. Reference entries.
--
-- alias:
--   точна/словникова форма конкретного object_id.
--
-- semantic_prototype:
--   семантичний reference text для retrieval/classification.
--
-- facet:
--   тема/ознака всередині контуру
--   (movement, phishing, military_aid, mobilisation тощо).
--
-- object_id nullable:
-- тематичний reference може належати контуру без конкретного object.
CREATE TABLE contour_reference_entries (
    reference_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    monitoring_contour_id  smallint NOT NULL
        REFERENCES monitoring_contours(monitoring_contour_id),

    object_id              bigint,

    entry_type             text NOT NULL
        CHECK (entry_type IN ('alias', 'semantic_prototype', 'facet')),

    match_mode             text NOT NULL
        CHECK (match_mode IN ('exact', 'semantic', 'both')),

    facet_code             text,
    reference_text         text NOT NULL,

    active                 boolean NOT NULL DEFAULT true,
    reference_version      integer NOT NULL DEFAULT 1
        CHECK (reference_version > 0),

    source_note            text,
    created_at             timestamptz NOT NULL DEFAULT now(),

    CHECK (length(btrim(reference_text)) > 0),

    -- Alias завжди повинен вказувати на конкретний canonical object.
    CHECK (entry_type <> 'alias' OR object_id IS NOT NULL),

    -- Якщо object_id заданий, він повинен належати тому самому контуру.
    FOREIGN KEY (object_id, monitoring_contour_id)
        REFERENCES contour_reference_objects(object_id, monitoring_contour_id)
);


-- Унікальність object-bound reference entries.
CREATE UNIQUE INDEX uq_contour_reference_entry_object
    ON contour_reference_entries (
        monitoring_contour_id,
        entry_type,
        object_id,
        lower(reference_text),
        reference_version
    )
    WHERE object_id IS NOT NULL;


-- Унікальність contour-level reference entries без object_id.
CREATE UNIQUE INDEX uq_contour_reference_entry_nonobject
    ON contour_reference_entries (
        monitoring_contour_id,
        entry_type,
        lower(reference_text),
        reference_version
    )
    WHERE object_id IS NULL;


-- Швидкий deterministic alias/reference lookup.
CREATE INDEX idx_contour_reference_entries_exact
    ON contour_reference_entries (lower(reference_text))
    WHERE active AND match_mode IN ('exact', 'both');


-- 4. Embeddings reference entries.
--
-- Той самий pattern, що embeddings / claim_embeddings:
-- generic vector + embedding_model_id.
--
-- HNSW навмисно НЕ створюємо зараз:
-- reference registry очікується малим порівняно з corpus,
-- необхідність ANN-індексу спочатку має бути виміряна.
CREATE TABLE contour_reference_embeddings (
    reference_id           bigint NOT NULL
        REFERENCES contour_reference_entries(reference_id)
        ON DELETE CASCADE,

    embedding_model_id     smallint NOT NULL
        REFERENCES embedding_models(embedding_model_id),

    embedding              vector NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (reference_id, embedding_model_id)
);
