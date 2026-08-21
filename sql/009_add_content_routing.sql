-- 009_add_content_routing.sql
-- Content Routing v1: дешевий semantic pre-filter поверх існуючих BGE-M3
-- embeddings, ДО дорогого Mamay claim extraction. Zero-shot: cosine similarity
-- content-вектора до невеликого набору relevant/irrelevant textual prototypes
-- (experiments/content_routing/prototypes_v1.json), без training pipeline,
-- без окремої ML-моделі.
--
-- routing_version — immutable identity конкретного routing contract: якщо
-- змінюється scoring logic, набір prototypes, thresholds чи embedding_model_id —
-- це вже нова версія, не UPDATE існуючих рядків. Без окремої registry-таблиці
-- (на відміну від llm_models/embedding_models) — тут немає live-процесу, чию
-- identity треба fail-fast звіряти при кожному запуску, просто версія в коді.
--
-- embedding_model_id — явна FK-залежність: routing навчений/порахований на
-- конкретних BGE-M3 векторах, зміна embedding-моделі в майбутньому без нової
-- routing_version дала б розсинхрон, який тут неможливий завдяки FK.
--
-- score — raw margin (max cos_sim до relevant prototypes) - (max cos_sim до
-- irrelevant), НЕ нормований в [0,1]. CHECK (-2..2) — вільна sanity-межа
-- (математична межа різниці двох cosine similarity), не семантичний контракт.
--
-- Ідемпотентність: UNIQUE(content_id, routing_version) — повторний прогін
-- worker'а для тієї ж routing_version не дублює рядки.

CREATE TABLE content_routing_decisions (
    routing_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_id           uuid NOT NULL REFERENCES content_items(content_id),
    routing_version       smallint NOT NULL CHECK (routing_version > 0),
    embedding_model_id    smallint NOT NULL REFERENCES embedding_models(embedding_model_id),
    decision              text NOT NULL CHECK (decision IN ('analyze', 'maybe', 'skip')),
    score                 real NOT NULL CHECK (score BETWEEN -2 AND 2),
    reason                text,
    code_revision         text NOT NULL CHECK (btrim(code_revision) <> ''),
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (content_id, routing_version)
);
