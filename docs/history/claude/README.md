# Claude project history

Цей каталог зберігає **історичну інженерну пам'ять проєкту МІП**, експортовану з Claude Project, який використовувався паралельно з розробкою на сервері.

Файли `claude_03`–`claude_22` залишаються у вихідному вигляді як provenance/history: handoff-и, результати експериментів, калібрування, негативні результати, rationale технічних рішень і зафіксовані на той момент стани системи.

## Як використовувати цей каталог

Це **reference/history, а не автоматично поточний baseline**.

При розбіжностях пріоритет такий:

1. фактичні результати тестів і вимірювань;
2. поточний код, DDL та перевірена реалізація в актуальній гілці;
3. новіші перевірені engineering artifacts ближче до реалізації;
4. ці історичні документи;
5. старі припущення та пропозиції.

Архітектурні принципи з історичних документів можуть залишатися актуальними, навіть якщо конкретні implementation details уже змінилися.

**Не відновлювати поточний стан сервера, Git HEAD, порти, backlog, кількість джерел або runtime-параметри лише з historical snapshot.** Перед практичною дією звіряти поточний код/БД/runtime.

## Індекс

| Файл | Тип | Як трактувати зараз |
|---|---|---|
| `claude_03_MIP_Server_State_18_08_2026.md` | server/runtime handoff | **Historical snapshot.** Корисний для походження рішень і вже проведених діагностик; конкретні версії, endpoint-и та стан сервера могли змінитися. |
| `claude_04_TZ_Monitoring_Requirements.md` | requirements reference | **Reference.** Конспект вихідних вимог і змісту 4 стратегічних контурів. Не замінює оригінальний ТЗ/HLD. |
| `claude_05_MIP_Collectors_Pilot_Status_18_08_2026.md` | ingestion/collectors handoff | **Historical snapshot.** Фіксує становлення raw/content/occurrence contract, RSS/TG-web та scoped VPN-підходу. Поточний стан дивитися в коді та БД. |
| `claude_06_MIP_Embeddings_ClaimExtraction_Handoff_19_08_2026.md` | architecture + handoff | **Високоцінний rationale.** Фіксує модульний шлях `evidence → claims → linking → relations → events → graph → RAG/GraphRAG`, evidence lineage та принцип «детерміноване кодом, семантичне LLM». Конкретний task/status історичний. |
| `claude_07_MIP_ClaimExtraction_EvalHarness_Findings_20_08_2026.md` | eval/research findings | **Актуальний reference failure modes.** Зокрема `not_found`, reconstructed/spliced evidence span, trailing punctuation, atomicity і epistemic-modality guards. Не вважати кожен старий run поточним quality baseline. |
| `claude_08_MIP_ClaimExtraction_Persistence_Worker_20_08_2026.md` | implementation milestone | **Historical.** Початковий persistence/batch-worker contract; поточний worker уже еволюціонував (ordering, retry semantics, DB lifecycle тощо). |
| `claude_09_MIP_ContentRouting_v1_21_08_2026.md` | decision rationale | **Still relevant.** Пояснює conservative Routing v1, високу ціну false-negative та причину широкої `maybe`-зони. Поточний код/thresholds завжди перевіряти окремо. |
| `claude_10_MIP_FirstAnalyticCorpus_21_08_2026.md` | pilot snapshot | **Historical milestone.** Перший persisted analytical corpus і ранні invalid-rate спостереження. |
| `claude_11_MIP_EventCandidate_Builder_v1_24_08_2026.md` | event-layer design/pilot | **Високоцінний rationale.** One-hop bounded seeds, відмова від connected-components/transitive closure та незалежний event verifier, який не успадковує relation edges як істину. |
| `claude_12_MIP_EventMerge_Dedup_v1_24_08_2026.md` | canonicalization design/pilot | **Високоцінний rationale.** Live canonical-membership check, anti-chaining, canonical-event merge semantics і відкладені v2 питання. Поточну реалізацію звіряти з кодом. |
| `claude_13_MIP_SemanticSearch_v1_24_08_2026.md` | operator/demo capability | **Still relevant reference.** Read-only semantic search по content/claims і зв'язок з canonical events; наявність/CLI перевіряти в актуальному repo. |
| `claude_14_MIP_Routing_v2_NoiseLeakage_Findings_25_08_2026.md` | routing research | **Negative-result chain, part 1.** Діагностика noise leakage/weather confound. |
| `claude_15_MIP_RoutingV2_ShadowRun_Verdict_25_08_2026.md` | routing shadow-run verdict | **Negative-result chain, part 2.** V2 prototype expansion суттєво розширила `analyze` і внесла новий noise; `routing_version=1` не змінювався. |
| `claude_16_MIP_RoutingV2_Ablation_Verdict_25_08_2026.md` | routing ablation verdict | **Negative-result chain, part 3 / важливий stop-condition.** Гіпотеза «винен один широкий prototype» спростована; косметичну prototype surgery зупинено без нової системної гіпотези. |
| `claude_17_MIP_ContentContours_v2_AdversarialReview_25_08_2026.md` | contour design review | **Research/rationale.** Важливе розділення source provenance, deterministic anchors та strategic content classification. Частина конкретного proposed design пізніше superseded реалізацією content-contour classifier. |
| `claude_18_MIP_ContentContours_v2_CalibrationPlan_and_Results_25_08_2026.md` | contour calibration plan | **Historical research package.** Зафіксував `source_group_set` vs future `content_contour_set` та calibration methodology. Конкретний план/статус не вважати current без звірки з новішими результатами. |
| `claude_19_MIP_Mamay_Performance_Diagnostics_01_09_2026.md` | performance baseline | **Current reference until superseded by newer measurements.** Закриває повторне безпричинне тестування power/clock, `np=1` vs `np=4`, context-size latency і prompt-cache. CUDA-vs-Vulkan та realistic shared-prefix concurrency були залишені відкритими, а не доведеними. |
| `claude_20_MIP_DownstreamPipeline_ReliabilityAudit_02_09_2026.md` | downstream reliability audit | **Important pre-live blocker reference.** F1: hardcoded attempt semantics роблять `transport_error` terminal; F4: `event_candidate_builder` не бачить successful retry >1; F2: DB connection/transaction lifecycle через Mamay inference. F1+F4 мають випускатися атомарно; DDL/semantic contracts змін не потребують. |
| `claude_21_MIP_CandidatePairGeneration_ScaleReview_02_09_2026.md` | candidate-generation scale review | **Research/rationale.** Підтвердив, що global exact top-N v1 придатний як reference/profile, але не як live relation queue; bottleneck — Mamay judgment budget. Blockwise exact виправданий як вимірювальний інструмент, а live v2 має бути bounded/per-claim після вимірювань. |
| `claude_22_MIP_RelationStage_AdversarialReview_02_09_2026.md` | adversarial relation-stage review | **Current semantic review checkpoint.** Критикує overfit до 5 regression pairs і premature signatures/ontology; рекомендує human gold set до нових prompt/threshold рішень, розділяє retrieval, cheap deterministic discrimination, referent evidence та Mamay relation semantics. |

## Особливо важливі зафіксовані уроки

### Routing v2: не повторювати косметичний цикл без нової гіпотези

Документи `14 → 15 → 16` утворюють один experiment trail. Було перевірено noise leakage, виконано shadow-run і точкову ablation одного relevant prototype. Результат: проблема виявилася системною для zero-shot BGE-M3 margin-підходу на частині off-topic військово-дипломатичного контенту, а не помилкою одного формулювання.

Тому **не відновлювати prototype wording surgery просто тому, що її легко зробити**. Потрібна нова перевірювана гіпотеза, окремий shadow-run і вимірювання.

### Claim extraction: INVALID не дорівнює transport failure

Документ `07` показує відомі класи semantic/adapter failure, включно з неможливістю чесно відновити evidence offsets для reconstructed/spliced span. Validator має право відхилити такий run; це не означає падіння Mamay або PostgreSQL.

### Relations/events: edge — сигнал, не істина

Документи `11–12` фіксують принцип: semantic relation не стає event membership автоматично. Event verification і canonical merge повторно перевіряють coherence та membership, зберігаючи provenance і не дозволяючи неконтрольоване транзитивне злиття.

### Downstream reliability: retry attempt — це runtime history, а не model/prompt identity

Документ `20` фіксує критичне розділення: `attempt_no` не можна трактувати як частину identity relation contract. `transport_error` має залишатися retryable, successful retry повинен бути видимий downstream, а DB connection не повинен жити через тривалий Mamay inference. `relation_judgment_worker` retry-fix і attempt-aware `event_candidate_builder` потрібно змінювати разом.

### Relation semantics: не підміняти відсутність gold set новою онтологією

Документи `21–22` разом з новішими вимірюваннями показують, що claim cosine добре ловить proposition/template similarity, але не є доказом event/referent identity. Не заморожувати claim/content thresholds, hub cutoffs або нову signature/entity ontology без held-out human-labeled sample. Нові pairwise prompts та independent signatures не повинні замінювати вимірювання recall/precision.

### Mamay performance: не починати діагностику заново без нового симптому

Документ `19` уже містить вимірювання single-stream decode, prompt processing, power/clock, context sizing, parallel slots і prompt cache. Повторювати ці ж тести варто лише при новому симптомі, зміні runtime/model/backend або конкретній optimization hypothesis.

## Правило для майбутніх AI-сесій

Перед пропозицією зміни, яка стосується вже дослідженої області, спочатку перевірити цей archive на попередній experiment/verdict. Особливо це стосується:

- Routing v1/v2 і prototype tuning;
- claim-extraction prompt/offset failure modes;
- relation/event/canonicalization semantics;
- content contour provenance vs classification;
- candidate generation / relation semantic calibration;
- downstream retry/transaction lifecycle;
- Mamay/llama.cpp performance tuning.

Мета archive — не «заморозити» старі рішення, а **не повторювати вже проведені експерименти без нової причини**.
