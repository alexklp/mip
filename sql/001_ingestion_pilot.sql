-- 001_ingestion_pilot.sql
-- Мінімальний вертикальний зріз: sources / raw_items / content_items / item_occurrences
-- Без partitioning, без contour_id, без soft-dedup, без embeddings — свідомо, це LLD pilot-scope.

CREATE TABLE sources (
    source_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text NOT NULL,
    url_or_handle  text NOT NULL,
    source_type    text NOT NULL CHECK (source_type IN ('rss', 'telegram', 'web', 'api')),
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE raw_items (
    raw_item_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id      uuid NOT NULL REFERENCES sources(source_id),
    collected_at   timestamptz NOT NULL DEFAULT now(),
    content_hash   text NOT NULL,
    payload        jsonb NOT NULL,
    status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'normalized', 'rejected'))
);
CREATE INDEX idx_raw_items_source_id     ON raw_items(source_id);
CREATE INDEX idx_raw_items_content_hash  ON raw_items(content_hash);
CREATE INDEX idx_raw_items_status        ON raw_items(status);

CREATE TABLE content_items (
    content_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_hash   text NOT NULL UNIQUE,
    language       text,
    title          text,
    text_content   text NOT NULL,
    first_seen_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE item_occurrences (
    occurrence_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_id     uuid NOT NULL REFERENCES content_items(content_id),
    raw_item_id    uuid NOT NULL,  -- traced, без formal FK (staging-шар, за рішенням HLD 4.2)
    source_id      uuid NOT NULL REFERENCES sources(source_id),
    external_ref   text NOT NULL,
    published_at   timestamptz,
    collected_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, external_ref)  -- ключ ідемпотентності повторного запуску collector'а
);
CREATE INDEX idx_item_occurrences_content_id ON item_occurrences(content_id);
CREATE INDEX idx_item_occurrences_source_id  ON item_occurrences(source_id);