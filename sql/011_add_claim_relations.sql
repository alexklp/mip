-- 011_add_claim_relations.sql
-- Claim relations vertical slice: candidate_pairs (deterministic, append-only) +
-- relation_judgment_prompts + relation_judgments (LLM-based, per-item).
--
-- candidate_pairs — append-only множина колись-небудь відібраних top-N кандидатів.
-- Ranking (candidate_pair_generator.py) рахується по ПОВНОМУ поточному corpus
-- claim_embeddings КОЖЕН запуск, без попереднього виключення вже існуючих пар;
-- --top-n — runtime budget конкретного запуску, НЕ матеріалізований "поточний
-- топ-N". claim_id_a < claim_id_b — канонічний порядок, щоб та сама пара не
-- могла потрапити в таблицю двічі у двох напрямках.
--
-- candidate_version — immutable identity контракту генерації (алгоритм +
-- embedding_model_id + правило cross-contour фільтрації, варіант 1: повністю
-- неперетинні contour_set). Зміна --top-n НЕ вимагає нової версії. Зміна
-- алгоритму/фільтрації — вимагає.
--
-- relation_judgments — LLM judgment окремої candidate_pair. Той самий
-- registry-verification та all-or-nothing per-attempt контракт, що
-- claim_extraction_runs: status='valid' <=> relation_label і rationale_text
-- обидва NOT NULL; 'invalid'/'transport_error' — обидва NULL. attempt_no
-- дозволяє повторний прогін тієї самої пари (напр. після фікса промпту) без
-- перезапису попереднього attempt.

CREATE TABLE candidate_pairs (
    candidate_pair_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id_a         uuid NOT NULL REFERENCES claims(claim_id),
    claim_id_b         uuid NOT NULL REFERENCES claims(claim_id),
    candidate_version  smallint NOT NULL CHECK (candidate_version > 0),
    embedding_model_id smallint NOT NULL REFERENCES embedding_models(embedding_model_id),
    score              real NOT NULL CHECK (score BETWEEN -1 AND 1),
    code_revision      text NOT NULL CHECK (btrim(code_revision) <> ''),
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (claim_id_a < claim_id_b),
    UNIQUE (claim_id_a, claim_id_b, candidate_version)
);

CREATE TABLE relation_judgment_prompts (
    prompt_id      smallint PRIMARY KEY,
    prompt_name    text NOT NULL,
    prompt_version text NOT NULL,
    schema_version text NOT NULL,
    prompt_text    text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (prompt_name, prompt_version)
);

CREATE TABLE relation_judgments (
    judgment_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_pair_id  uuid NOT NULL REFERENCES candidate_pairs(candidate_pair_id),
    llm_model_id       smallint NOT NULL REFERENCES llm_models(llm_model_id),
    prompt_id          smallint NOT NULL REFERENCES relation_judgment_prompts(prompt_id),
    attempt_no         smallint NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
    status             text NOT NULL CHECK (status IN ('valid', 'invalid', 'transport_error')),
    relation_label     text CHECK (relation_label IS NULL OR relation_label IN
                            ('same_fact', 'same_event', 'contradiction', 'related', 'unrelated')),
    rationale_text     text CHECK (rationale_text IS NULL OR btrim(rationale_text) <> ''),
    errors             jsonb CHECK (errors IS NULL OR jsonb_typeof(errors) = 'array'),
    raw_response       text,
    code_revision      text NOT NULL CHECK (btrim(code_revision) <> ''),
    latency_ms         integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'valid') = (relation_label IS NOT NULL)),
    CHECK ((status = 'valid') = (rationale_text IS NOT NULL)),
    UNIQUE (candidate_pair_id, llm_model_id, prompt_id, attempt_no)
);
