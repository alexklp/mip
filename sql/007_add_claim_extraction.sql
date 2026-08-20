-- 007_add_claim_extraction.sql
-- Claim-extraction persistence vertical slice: llm_models / claim_extraction_prompts /
-- claim_extraction_runs / claims. Мінімальна модель під технічний pilot 20.08.2026:
-- run-level all-or-nothing валідність (validator.py валідує весь response evidence
-- атомарно) — якщо run.status='invalid', жоден claim з нього НЕ персистується,
-- навіть якщо окремі claims індивідуально пройшли б перевірку. Per-claim persistence
-- свідомо не вводиться в цьому slice.
--
-- Ідемпотентність: UNIQUE(content_id, llm_model_id, prompt_id, attempt_no) —
-- attempt_no дозволяє повторний прогін того самого content_id/model/prompt
-- (напр. після фікса worker'а) без конфлікту з попереднім run, зберігаючи обидва
-- як історію, а не перезаписуючи.
--
-- code_revision фіксує версію adapter/validator/worker (git short SHA), окремо від
-- prompt_id/llm_model_id — щоб відрізнити "інший результат через іншу модель/промпт"
-- від "інший результат через баг-фікс в коді".
--
-- model_revision у llm_models — це НЕ просто quantization-тег (напр. "Q4_K_M"),
-- а immutable identity конкретного model artifact: model-version/quantization/
-- artifact-hash. Два різних GGUF з однаковим написом "Q4_K_M" не повинні виглядати
-- в БД однією моделлю. Точний формат фіксується при seed.
--
-- Offsets у claims: zero-based, evidence_end EXCLUSIVE (як Python slicing
-- text[evidence_start:evidence_end]) — те саме, що resolve_offsets() в run_eval.py.
--
-- Implementation contract (application-level, НЕ DDL/тригер): при status='valid'
-- run + усі claims вставляються ОДНІЄЮ DB-транзакцією (BEGIN → run+claims → COMMIT).
-- При status IN ('invalid','transport_error') — тільки run, без claims. Це запобігає
-- сценарію "run записаний як valid, процес впав до вставки claims, після рестарту
-- worker бачить existing run і скіпає" — БД назавжди отримує valid run без claims.
-- Тригер на заборону claims для invalid-run зараз свідомо не додається (pilot-scope,
-- цей invariant тримає worker).

CREATE TABLE llm_models (
    llm_model_id      smallint PRIMARY KEY,
    model_name        text NOT NULL,
    model_revision    text NOT NULL,
    framework         text NOT NULL,
    framework_version text NOT NULL,
    context_window    integer NOT NULL CHECK (context_window > 0),
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_name, model_revision, framework, framework_version)
);

CREATE TABLE claim_extraction_prompts (
    prompt_id      smallint PRIMARY KEY,
    prompt_name    text NOT NULL,
    prompt_version text NOT NULL,
    schema_version text NOT NULL,
    prompt_text    text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (prompt_name, prompt_version)
);

CREATE TABLE claim_extraction_runs (
    run_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content_id     uuid NOT NULL REFERENCES content_items(content_id),
    llm_model_id   smallint NOT NULL REFERENCES llm_models(llm_model_id),
    prompt_id      smallint NOT NULL REFERENCES claim_extraction_prompts(prompt_id),
    attempt_no     smallint NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
    status         text NOT NULL CHECK (status IN ('valid', 'invalid', 'transport_error')),
    claim_count    integer CHECK (claim_count IS NULL OR claim_count >= 0),
    errors         jsonb CHECK (errors IS NULL OR jsonb_typeof(errors) = 'array'),
    raw_response   text,
    code_revision  text NOT NULL CHECK (btrim(code_revision) <> ''),
    latency_ms     integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (content_id, llm_model_id, prompt_id, attempt_no)
);

CREATE TABLE claims (
    claim_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id           uuid NOT NULL REFERENCES claim_extraction_runs(run_id),
    claim_local_id   text NOT NULL,
    claim_text       text NOT NULL CHECK (btrim(claim_text) <> ''),
    evidence_span    text NOT NULL CHECK (evidence_span <> ''),
    evidence_start   integer NOT NULL CHECK (evidence_start >= 0),
    evidence_end     integer NOT NULL CHECK (evidence_end > evidence_start),
    epistemic_status text NOT NULL CHECK (epistemic_status IN
        ('asserted', 'reported', 'alleged', 'estimated', 'opinion', 'uncertain', 'denied', 'not_reported')),
    claim_time_text  text,
    attribution_text text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (evidence_end = evidence_start + char_length(evidence_span)),
    UNIQUE (run_id, claim_local_id)
);
