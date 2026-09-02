# MIP — Event Candidate / Event Builder v1: pilot зафиксирован как успешный

Дата: 24.08.2026
Статус: pilot завершён, признан успешным на калибровочной выборке. Массовый rerun не проводился — по протоколу требуется отдельное решение.

## 1. Закрытие relation_judgment v3 (предшествующий слой)

`relation_judgment v3` (referent gate + verbatim-quote grounding) зафиксирован как завершённый pilot. Prompt tuning прекращён.

**Известная лимитация (постоянная, не устранённая полностью на уровне v3):** отдельные generic/шаблонные claims всё ещё могут давать false-positive `confirmed`/`same_fact`|`same_event` на уровне relation_judgments — модель иногда самоцитирует общую (не идентифицирующую) фразу как "verbatim evidence", формально проходя grounding-check, но нарушая смысловое правило против generic-phrase confirmation. Зафиксировано на калибровочном прогоне (30 pairs, 20 backlog + 10 new): ~5-6/23 confirmed pairs несли этот паттерн, включая флагманский случай `995e6ae3` (self-quote шаблонной фразы "emergency services at site").

**Архитектурное следствие:** relation edges — сигнал, не доказанная истина. Именно поэтому следующий слой (event candidate) спроектирован так, чтобы НЕ наследовать upstream confirmed слепо (см. §2, §4).

## 2. Архитектура Event Candidate / Event Builder v1

Расширение пайплайна: `claims → candidate_pairs → relation_judgments → event candidate → event verification`.

Жёсткие ограничения (по требованию, без исключений в v1):
- НЕ connected components / transitive closure по relation edges.
- `same_fact`/`same_event` НЕ считается автоматически истинным event membership.
- Deterministic-код формирует event seed вокруг anchor claim + ограниченного числа связанных claims (one-hop, без рекурсивного расширения графа).
- Seed включает полный evidence packet: claim text, evidence span, parent content (title/snippet), source, contour, observed time, relation score/judgment.
- LLM verifier получает весь seed целиком, решает какие claims реально относятся к одному событию — включая ЧАСТИЧНОЕ исключение отдельных claims из seed, а не только accept/reject seed целиком.
- Contradiction сохраняется как отношение внутри одного возможного event, автоматически не разделяет claims.
- Никакого GraphRAG, entity resolution, human labeling, Hermes orchestration — вне рамок этого среза.
- Thresholds не оптимизировались без измерений.

**Seed formation v1 (самый простой вариант, по требованию):** anchor claim + top-K=5 прямых (one-hop) соседей, без рекурсии. `SEED_VERSION=1` фиксирует `TOP_K=5` как часть identity-контракта — смена K требует нового `seed_version`, поэтому `--top-k` не вынесен в CLI.

**Anchor selection v1:** любой claim с ≥1 qualifying edge становится anchor. Один `event_candidate` на anchor, без дедупликации кластеров — seeds могут пересекаться (claim может быть anchor в одном seed и neighbor в другом). Кластеризация/мердж confirmed events — future work, вне этого среза.

**Qualifying edge:** `relation_judgments.status='valid' AND relation_label IN ('same_fact','same_event','contradiction') AND shared_referent_status='confirmed'`, под зафиксированным relation-judgment контрактом v3 (`llm_model_id=1, prompt_id=3, attempt_no=1`). `related`/`unrelated` edges не создают.

**event_decision:** `accepted_seed` | `rejected_seed` (переименовано из `confirmed_event`/`rejected_seed` — verifier проверяет coherence/membership кандидата, не устанавливает объективную истинность события). `accepted_seed` валиден только если `anchor.included=true` И ≥1 `neighbor.included=true` — иначе `rejected_seed`. Проверка односторонняя (тот же принцип, что referent gate в relation_judgment v3): модель может выбрать `rejected_seed` холистически даже если per-member счётчик формально удовлетворял бы accepted-условию.

**event_verification_members** сохраняются для ЛЮБОЙ valid verification, включая `rejected_seed` (все `included=false`, но `member_rationale` сохраняется всегда).

**Известное ограничение upstream явно задокументировано в самом prompt verifier'а** — модель предупреждена, что `relation_label`/`shared_referent_status` иногда ложно подтверждают generic/шаблонные claims через self-quoted boilerplate, и проинструктирована не доверять этому автоматически.

## 3. DDL (sql/016_add_event_candidates.sql)

5 таблиц: `event_candidates`, `event_candidate_members`, `event_verification_prompts` (отдельный namespace prompt_id от `relation_judgment_prompts`), `event_verifications`, `event_verification_members`.

Ключевые инварианты (constraint-уровень):
- `UNIQUE(anchor_claim_id, seed_version)` на `event_candidates`.
- `event_candidate_members`: `(role='anchor') = (source_candidate_pair_id IS NULL) = (source_relation_judgment_id IS NULL) = (rank_in_seed IS NULL)`.
- `event_verifications`: all-or-nothing status contract — `status='valid' <=> event_decision NOT NULL <=> rationale_text NOT NULL`; `event_decision='accepted_seed' <=> event_summary NOT NULL`.
- Cross-table exact member-set invariant (members[] в JSON ответе модели == expected claim_ids seed'а) — держится на validator/app-level, НЕ через DB trigger (явное решение — trigger избыточен для pilot).

## 4. Код

- `experiments/event_candidates/event_candidate_builder.py` — deterministic seed builder. Fail-fast регистри-проверка upstream identity (llm_models + relation_judgment_prompts), без побайтовой сверки текста промпта (не его job).
- `experiments/event_candidates/event_verifier_worker.py` — LLM verification worker. Полная регистри-проверка (model identity + prompt identity + побайтовая сверка prompt_text с файлом на диске). Минимальные pure helpers (`get_code_revision`, `build_context_snippet`, `fetch_claims_meta`) продублированы, не импортированы из `relation_judgment_worker.py` (явное решение — общий модуль выносится позже при реальной необходимости).
- `experiments/event_candidates/event_verifier_prompt_v1.txt` (`prompt_id=1`, `prompt_version=v1`, `schema_version=event-verification/1`).

Unit-tested локально до применения к БД (10/10 tests passed на первом прогоне для `validate_event_verification()`).

## 5. Pilot run (первый и единственный на момент фиксации)

`event_candidate_builder.py --limit 15`: 32 claims с ≥1 qualifying edge, построено 15 event_candidates (seed_version=1, top_k=5).

`event_verifier_worker.py --limit 15`: 15/15 valid, 0 invalid, 0 transport_error.

**Ручной построчный аудит (не только SUMMARY-счётчики) — 15/15 решений корректны:**

8 `accepted_seed`, все подтверждены легитимными:
- Москва, атака дронів, заява Собяніна (2 seed, взаимно anchor↔neighbor) — проверено на уровне content_items: оба claim цитируют одно и то же заявление мэра (620 БПЛА, эвакуация обломков) — реальная кросс-source корроборация одного инцидента, НЕ generic-совпадение (визуально похоже на флагманский FP-паттерн из v3, но при проверке первоисточника оказалось легитимным).
- Печеніги, ракетний удар, 10 загиблих (3 seed) — тройное независимое совпадение конкретной цифры (score 0.99/0.90/0.89).
- Путін на Курилах (2 seed, взаимно anchor↔neighbor) — специфичные сущности прямо в claim_text.
- Трамп погрожує Оману (1 seed) — специфичные сущности прямо в claim_text.

7 `rejected_seed`, все корректны — verifier поймал ровно тот класс ошибок, ради которого его строили: upstream `same_fact`/`same_event`+`confirmed` на generic-фразах («жертв немає», «екстрені служби на місці», «госпіталізовано одну людину») между заведомо разными инцидентами (Сахалін/Сирія/EVA/Уфа/Київ/Печеніги/Підмосков'я/Бровари/Москва-смартфон). Верифаер развёл их по source content, не унаследовав upstream confirmed слепо.

**Вывод:** архитектура (независимый verifier, per-claim re-evaluation, а не наследование upstream relation-статуса) работает как спроектирована на реальных данных первого прогона. Флагманский известный false positive (`995e6ae3`-стиль) в эту 15-seed выборку не попал — не подтверждение, что лимитация relation_judgment v3 устранена, а подтверждение, что механизм exclusion на уровне verifier функционален там, где он был протестирован.

**Оговорка:** выборка маленькая (15 seeds, единственный прогон). Это pilot-подтверждение работоспособности архитектуры, не статистически значимая оценка precision/recall на масштабе. Массовый rerun не проводился намеренно, thresholds не тюнились.

## 6. Статус на момент фиксации

- Миграция применена на `mip_dev` (`sql/016`, `sql/017`).
- Код в `experiments/event_candidates/` — задеплоен, компилируется, unit-tested, прогнан один раз на 15 seeds.
- Pilot зафиксирован как успешный.
- Следующий слой (event merging/clustering confirmed events, дальнейшая работа с accepted_seed как основой для чего-то выше) — НЕ определён на момент этой записи, требует отдельного решения.
