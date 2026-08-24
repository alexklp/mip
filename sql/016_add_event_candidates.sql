-- 016_add_event_candidates.sql
-- Event Candidate / Event Builder v1: claims -> candidate_pairs -> relation_judgments
-- -> event_candidates (deterministic seed) -> event_verifications (LLM, member-level).
--
-- Не connected components, не transitive closure. event_candidate_builder.py
-- будує seed навколо ОДНОГО anchor claim + TOP_K=5 прямих сусідів по
-- qualifying edges (один hop, без рекурсивного розширення графа). seed_version
-- фіксує TOP_K=5 як частину identity контракту (seed_version=1); зміна K =
-- новий seed_version, а не runtime-прапорець.
--
-- qualifying edge = relation_judgments.status='valid' AND relation_label IN
-- ('same_fact','same_event','contradiction') під зафіксованим relation-
-- judgment контрактом (llm_model_id=1, prompt_id=3, attempt_no=1 -- поточний
-- v3, referent gate). related/unrelated НЕ створюють edges.
--
-- Anchor selection v1: БУДЬ-ЯКИЙ claim з >=1 qualifying edge стає anchor.
-- Один event_candidate на anchor, без дедуплікації кластерів -- seed'и можуть
-- перетинатися (claim може бути anchor в одному seed і neighbor в іншому).
-- Кластеризація/мердж confirmed events -- поза межами цього зрізу.
--
-- contradiction НЕ виключає claim із seed -- це один з типів qualifying edge,
-- видимий verifier'у як є; verifier сам вирішує, чи claims з розбіжними
-- даними лишаються в одній події.
--
-- Відоме обмеження, яке передається у verifier явно (в промпті): upstream
-- relation_label -- це сигнал евристики, а не доведена істина. Виміряно, що
-- v3 інколи хибно підтверджує спільний референт для generic/шаблонних claims
-- (self-quote шаблонної фрази як "evidence"). verifier зобов'язаний
-- переоцінювати кожен claim самостійно за наданими даними, а не довіряти
-- relation_label автоматично.

CREATE TABLE event_candidates (
    event_candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    anchor_claim_id    uuid NOT NULL REFERENCES claims(claim_id),
    seed_version       smallint NOT NULL CHECK (seed_version > 0),
    neighbor_count     smallint NOT NULL CHECK (neighbor_count >= 0),
    code_revision      text NOT NULL CHECK (btrim(code_revision) <> ''),
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (anchor_claim_id, seed_version)
);

-- role='anchor' <=> source_candidate_pair_id/source_relation_judgment_id/
-- rank_in_seed усі NULL (anchor не має "джерела" -- він сам є точкою відліку
-- seed'а). role='neighbor' <=> усі три поля заповнені (яка саме edge
-- привела цього claim у seed, і на якому ранзі за score).
CREATE TABLE event_candidate_members (
    event_candidate_id          uuid NOT NULL REFERENCES event_candidates(event_candidate_id),
    claim_id                    uuid NOT NULL REFERENCES claims(claim_id),
    role                        text NOT NULL CHECK (role IN ('anchor', 'neighbor')),
    source_candidate_pair_id    uuid REFERENCES candidate_pairs(candidate_pair_id),
    source_relation_judgment_id uuid REFERENCES relation_judgments(judgment_id),
    rank_in_seed                smallint CHECK (rank_in_seed IS NULL OR rank_in_seed > 0),
    PRIMARY KEY (event_candidate_id, claim_id),
    CHECK ((role = 'anchor') = (source_candidate_pair_id IS NULL)),
    CHECK ((role = 'anchor') = (source_relation_judgment_id IS NULL)),
    CHECK ((role = 'anchor') = (rank_in_seed IS NULL))
);

-- Реєстр промптів верифікації -- той самий паттерн, що relation_judgment_prompts.
-- Окремий namespace prompt_id (своя послідовність із 1), окрема таблиця --
-- це інший контракт/інша схема виходу, не варіант relation_judge.
CREATE TABLE event_verification_prompts (
    prompt_id      smallint PRIMARY KEY,
    prompt_name    text NOT NULL,
    prompt_version text NOT NULL,
    schema_version text NOT NULL,
    prompt_text    text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (prompt_name, prompt_version)
);

-- event_decision: 'accepted_seed' | 'rejected_seed' -- НЕ 'confirmed_event',
-- бо verifier перевіряє coherence/membership кандидата, а не встановлює
-- об'єктивну істинність події. status='valid' <=> event_decision NOT NULL
-- <=> rationale_text NOT NULL (той самий all-or-nothing контракт, що
-- relation_judgments). event_summary NOT NULL <=> event_decision='accepted_seed'.
--
-- Правило "accepted_seed валідний лише якщо anchor.included=true І
-- >=1 neighbor.included=true, інакше rejected_seed" -- це ОДНОСТОРОННЯ
-- узгодженість, яку перевіряє validate_event_verification() в
-- event_verifier_worker.py (внутрішня суперечність відповіді моделі =
-- invalid, за тим самим принципом, що referent gate у relation_judgments).
-- НЕ закодовано як DB CHECK тут, бо потребує даних з event_verification_members
-- (окрема таблиця) -- за рішенням не додавати DB trigger для цього пілоту,
-- exact-membership та ця узгодженість лишаються app-level інваріантами.
CREATE TABLE event_verifications (
    verification_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_candidate_id uuid NOT NULL REFERENCES event_candidates(event_candidate_id),
    llm_model_id        smallint NOT NULL REFERENCES llm_models(llm_model_id),
    prompt_id           smallint NOT NULL REFERENCES event_verification_prompts(prompt_id),
    attempt_no          smallint NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
    status               text NOT NULL CHECK (status IN ('valid', 'invalid', 'transport_error')),
    event_decision       text CHECK (event_decision IS NULL OR event_decision IN ('accepted_seed', 'rejected_seed')),
    event_summary        text CHECK (event_summary IS NULL OR btrim(event_summary) <> ''),
    rationale_text        text CHECK (rationale_text IS NULL OR btrim(rationale_text) <> ''),
    errors                jsonb CHECK (errors IS NULL OR jsonb_typeof(errors) = 'array'),
    raw_response          text,
    code_revision          text NOT NULL CHECK (btrim(code_revision) <> ''),
    latency_ms              integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at               timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'valid') = (event_decision IS NOT NULL)),
    CHECK ((status = 'valid') = (rationale_text IS NOT NULL)),
    CHECK ((event_decision = 'accepted_seed') = (event_summary IS NOT NULL)),
    UNIQUE (event_candidate_id, llm_model_id, prompt_id, attempt_no)
);

-- Зберігається для БУДЬ-ЯКОГО valid verification, включно з rejected_seed
-- (не лише accepted_seed) -- явне рішення: rejected_seed теж пише member-рядки
-- (усі included=false), rationale зберігається для кожного claim. Це дає
-- повну трасованість "чому саме цей claim виключено", а не лише факт відмови.
-- event_verification_members.claim_id МАЄ бути підмножиною (і точним
-- збігом множини) claim_id з event_candidate_members для цього
-- event_candidate_id -- app-level інваріант (validator), не DB constraint.
CREATE TABLE event_verification_members (
    verification_id  uuid NOT NULL REFERENCES event_verifications(verification_id),
    claim_id         uuid NOT NULL REFERENCES claims(claim_id),
    included         boolean NOT NULL,
    member_rationale text NOT NULL CHECK (btrim(member_rationale) <> ''),
    PRIMARY KEY (verification_id, claim_id)
);
