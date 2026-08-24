-- 018_add_event_merge.sql
-- Event Merge / Dedup v1: объединение пересекающихся accepted_seed
-- event_candidates (из event_candidate_builder.py + event_verifier_worker.py)
-- в canonical_events -- claims -> candidate_pairs -> relation_judgments ->
-- event_candidates -> event_verifications -> (цей шар) -> canonical_events.
--
-- Ключові рішення (зафіксовано в діалозі, не переглядати без причини):
--
-- 1. Merge candidate генерується ДЕТЕРМІНІСТИЧНО (event_merge_candidate_builder.py)
--    лише для ДВОХ accepted_seed event_candidates, якщо у них є хоча б один
--    спільний claim з included=true в обох. Сам overlap НЕ означає merge --
--    це лише кандидат для LLM merge-verifier (event_merge_worker.py).
--
-- 2. НЕ connected components по merge edges автоматично. Якщо seed вже
--    входить у canonical_event (з N членів), новий претендент звіряється
--    проти ПОВНОГО поточного canonical_event packet (union всіх included=true
--    claims усіх членів), а не лише проти одного seed, через який він прийшов.
--    Це прямо забороняє ланцюжок: seed A<->seed B + seed B<->seed C =>
--    автоматичне злиття A+B+C без окремої перевірки C проти вже об'єднаного A+B.
--
-- 3. canonical<->canonical merge (обидва seeds пари вже належать РІЗНИМ
--    canonical_events) в v1 НЕ обробляється. Такі пари -- deferred_both_canonical,
--    залишаються для окремого v2 pass. Жодного автоматичного злиття
--    canonical_events один в одного в цьому шарі немає.
--
-- 4. different_event і insufficient зберігаються як валідні результати
--    (event_merges.status='valid'), НЕ відкидаються -- аналогічно
--    rejected_seed на попередньому шарі.
--
-- 5. Eligibility (event_merge_worker.py) виключає deterministic-skip випадки
--    на рівні SQL-запиту (обидва seeds вже в ОДНОМУ canonical_event --
--    already_same_canonical; обидва в РІЗНИХ -- deferred_both_canonical).
--    Для цих випадків НЕ створюється fake-рядок у event_merges -- лічильники
--    в SUMMARY, не БД-записи. Це свідоме рішення: event_merges фіксує лише
--    реальні LLM-виклики і їх результати, а не бухгалтерію по skip-станам.
--
-- 6. event_merges.input_payload зберігає ТОЧНИЙ JSON packet, що реально
--    пішов у LLM (side_a/side_b snapshot на момент виклику) -- side_a_ref/
--    side_b_ref лишаються як легкі покажчики (kind+id), але НЕ замінюють
--    повний snapshot, оскільки canonical_event packet змінюється у часі.
--
-- sql/016_add_event_candidates.sql та sql/017_seed_event_verification_registry_v1.sql
-- МАЮТЬ бути застосовані ДО цієї міграції.

CREATE TABLE event_merge_prompts (
    prompt_id      smallint PRIMARY KEY,
    prompt_name    text NOT NULL,
    prompt_version text NOT NULL,
    schema_version text NOT NULL,
    prompt_text    text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (prompt_name, prompt_version)
);

CREATE TABLE event_merge_candidates (
    merge_candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seed_a_id          uuid NOT NULL REFERENCES event_candidates(event_candidate_id),
    seed_b_id          uuid NOT NULL REFERENCES event_candidates(event_candidate_id),
    shared_claim_ids   jsonb NOT NULL CHECK (jsonb_typeof(shared_claim_ids) = 'array'),
    code_revision      text NOT NULL CHECK (btrim(code_revision) <> ''),
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (seed_a_id < seed_b_id),  -- канонічний порядок, без дублів (A,B)/(B,A)
    CHECK (jsonb_array_length(shared_claim_ids) > 0),  -- non-empty invariant
    UNIQUE (seed_a_id, seed_b_id)
);

CREATE TABLE canonical_events (
    canonical_event_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_event_summary text NOT NULL CHECK (btrim(canonical_event_summary) <> ''),
    founding_merge_id       uuid,  -- FK додається нижче (event_merges ще не існує на цьому рядку)
    code_revision            text NOT NULL CHECK (btrim(code_revision) <> ''),
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE event_merges (
    merge_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merge_candidate_id   uuid NOT NULL REFERENCES event_merge_candidates(merge_candidate_id),
    llm_model_id         smallint NOT NULL REFERENCES llm_models(llm_model_id),
    prompt_id            smallint NOT NULL REFERENCES event_merge_prompts(prompt_id),
    attempt_no           smallint NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
    status               text NOT NULL CHECK (status IN ('valid', 'invalid', 'transport_error')),
    decision             text CHECK (decision IS NULL OR decision IN ('same_event', 'different_event', 'insufficient')),
    merged_event_summary text CHECK (merged_event_summary IS NULL OR btrim(merged_event_summary) <> ''),
    rationale_text       text CHECK (rationale_text IS NULL OR btrim(rationale_text) <> ''),
    input_payload         jsonb NOT NULL,  -- точний JSON packet, реально відправлений LLM (side_a+side_b snapshot)
    side_a_ref             jsonb,  -- {"kind": "seed"|"canonical_event", "id": "..."} -- легкий покажчик, не заміняє input_payload
    side_b_ref             jsonb,
    errors                 jsonb CHECK (errors IS NULL OR jsonb_typeof(errors) = 'array'),
    raw_response            text,
    code_revision            text NOT NULL CHECK (btrim(code_revision) <> ''),
    latency_ms                integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at                 timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'valid') = (decision IS NOT NULL)),
    CHECK ((status = 'valid') = (rationale_text IS NOT NULL)),
    CHECK ((decision = 'same_event') = (merged_event_summary IS NOT NULL)),
    UNIQUE (merge_candidate_id, llm_model_id, prompt_id, attempt_no)
);

ALTER TABLE canonical_events
    ADD CONSTRAINT canonical_events_founding_merge_id_fkey
    FOREIGN KEY (founding_merge_id) REFERENCES event_merges(merge_id);

CREATE TABLE canonical_event_members (
    canonical_event_id  uuid NOT NULL REFERENCES canonical_events(canonical_event_id),
    event_candidate_id  uuid NOT NULL REFERENCES event_candidates(event_candidate_id),
    added_via_merge_id  uuid NOT NULL REFERENCES event_merges(merge_id),
    PRIMARY KEY (canonical_event_id, event_candidate_id),
    UNIQUE (event_candidate_id)  -- один accepted_seed належить максимум одному canonical_event у v1
);
