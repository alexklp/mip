# МІП — Semantic Search v1: статус и калибровочный прогон

Дата: 24.08.2026
Статус: доставлен, скомпилирован, откалиброван на живых данных. Read-only —
миграций и применений к БД не требует (ничего не пишет).

## 1. Цель и scope

Минимальный read-only semantic search поверх уже существующих BGE-M3
embeddings для operator/demo use. Два независимых scope (`content` по
`embeddings`/`content_items`, `claims` по `claim_embeddings`/`claims`) и
режим `all`, запускающий оба. Существующий pipeline не менялся, DDL не
добавлялось.

## 2. Файл

`experiments/semantic_search/semantic_search.py` — 358 строк, 15428 байт,
md5 `7238b68b0f1847b6c6fdeaa3dc2753d2`, compile OK.

## 3. Ключевые решения (по факту реальной схемы, сверенной перед кодом)

Перед написанием кода схема была сверена вживую (`\d` по всем задействованным
таблицам на `mip_dev`), а не взята из архитектурных доков:

- `content_items.title`/`text_content`/`first_seen_at` — есть, используются
  напрямую для title/snippet/first_observed_at.
- `contour_id` — на `sources`, не на `item_occurrences` (это уже было видно
  по существующим воркерам, подтверждено `\d sources`). Выводится как
  source-level metadata, без переименования в "Content Contour
  Classification" — по требованию ТЗ.
- occurrence count — отдельного поля-счётчика в схеме нет, считается через
  `COUNT` по `item_occurrences`.
- `claim → canonical_event` — путь через реальную схему `event_candidates`
  слоя: `claims.claim_id` → `event_verification_members` (`included=true`)
  → `event_verifications` (`status='valid' AND event_decision='accepted_seed'`)
  → `event_candidate_id` → `canonical_event_members` → `canonical_events`.
  Отсутствие цепочки → `canonical_event: null`, ожидаемый результат.
- Ranking — cosine distance через pgvector `<=>` прямо в SQL
  (`similarity = 1 - distance`), НЕ через python-side numpy (в отличие от
  `claim_candidate_scan.py`, где полная all-pairs матрица оправдана для
  небольшого набора claims). Здесь top-K по одному query-вектору — SQL
  быстрее и естественнее, плюс сразу использует существующие индексы.
- Без явного cast query под partial HNSW-индекс (`idx_*_hnsw_bge_m3`) — на
  текущем объёме корпуса (тысячи content, сотни claims) seq scan + сортировка
  достаточно быстрые; если станет медленно — отдельная задача на explicit
  expression-cast под индекс, не сейчас (без преждевременной оптимизации).
- Query кодируется тем же вызовом, что весь MIP embedding pipeline:
  `model.encode([text], normalize_embeddings=True)`, без query:/passage:
  префиксов, CPU, `local_files_only=True`. Fail-fast звірка
  `embedding_model_id=1` — тот же `verify_registered_model` паттерн, что в
  `embed_worker.py`/`routing_worker.py`/`claim_embedding_worker.py`.
- Explicit non-goals по ТЗ подтверждены в реализации: без LLM/RAG, без query
  expansion, без reranker, без hybrid BM25/FTS, без relevance thresholds
  (только deterministic top-K), без web UI, без persistence search history.

## 4. Инцидент при доставке (пойман и исправлен)

При передаче base64-блока 2/5 закралась однобайтовая порча (символ `t`
вместо `d` в одной base64-строке) — прошла незамеченной, потому что после
блоков 1-4 проверялся только `wc -l`, а md5 всего файла сверялся лишь в
конце. Обнаружено на финальном сравнении md5 полного файла (271 строка
совпадала, md5 — нет). Найдено без перезаливки — побитовым md5 по диапазонам
строк каждого блока, затем точечный `sed -i` на конкретной строке. Урок:
на многоблочных передачах стоит сверять md5 по каждому блоку сразу после
отправки, не только финальный.

## 5. Калибровочный прогон (журнал реальных результатов)

### 5.1 "ракетний удар по Печенігах" (`--scope all --limit 5`)

Content: 5/5 релевантны, similarity 0.615–0.659, все source contour=3.
Claims: 5/5 релевантны, similarity 0.778–0.848, все `canonical_event: (none)`.

### 5.2 Проверка JOIN на конкретном claim (санity-check)

Прямым SQL найдены реальные claim_id, входящие в canonical_event
`112734e4` (Печеніги) через `event_verification_members`/`event_verifications`
/`canonical_event_members`. Запрос `"10 людей загинули"` вернул один из этих
claim_id первым результатом (`similarity=1.0000` — точное совпадение текста)
с корректно резолвленным `canonical_event` (id + полный summary).
**Вывод:** `(none)` в п. 5.1 — не баг, а следствие того, что top-K по
similarity не совпал с конкретными anchor/neighbor claims, реально
попавшими в seed. На небольшом корпусе это ожидаемо.

### 5.3 "Путін відвідав Курильські острови" (`--scope all --limit 5`)

Content: 5/5 релевантны, similarity 0.655–0.737 (contour 2/3/4 — разные
источники, включая российские, отработало кросс-source покрытие).
Claims: топ-2 (similarity 0.952/0.963) корректно резолвят
`canonical_event=9c9a59a8` с полным summary; остальные 3 — `(none)`
(другие claims про тот же визит, но не входящие в конкретный seed).

### 5.4 "дрони атакували Москву" (`--scope all --limit 5`)

Content: 5/5 релевантны, similarity 0.707–0.771, contour 3/4.
Claims: 5/5 релевантны, similarity 0.771–0.925, все `canonical_event: (none)`
— ожидаемо по той же логике (top-K не пересёкся с конкретными seed-claims
для canonical_event `c6883957`).

### 5.5 "погода в Києві завтра" (заведомо нерелевантный, `--scope all --limit 5`)

Content: similarity просел до 0.608–0.630 (реальные погодные статьи —
семантика сработала, embeddings отличили тему).
Claims: similarity 0.560–0.583, результаты тематически рассыпались
(ДТП, тривога, опалювальний сезон — просто "Київ"-совпадения, не про
погоду). **Вывод:** без relevance threshold (по ТЗ) ranking всё равно
даёт наблюдаемый разрыв между relevant (0.65–0.96) и irrelevant
(0.56–0.63) диапазонами — полезный сигнал для будущей калибровки порогов,
если она когда-нибудь понадобится, но сейчас порог сознательно не вводится.

### 5.6 Формат JSON + одиночные scope

`--scope content --format json` и `--scope claims --format json` дали
валидный JSON, `content_results`/`claim_results` корректно `null` для
незапрошенного scope, структура полей соответствует ТЗ (content_id/
similarity/title/snippet/first_observed_at/sources/occurrence_count для
content; claim_id/similarity/claim_text/epistemic_status/evidence_span/
parent_content/sources/observed_at/canonical_event для claims).

## 6. Итог

6 запросов (5 калибровочных + 1 sanity-check на конкретном claim_id),
оба scope по отдельности и `all`, оба формата (`text`/`json`) — все прошли
вручную проверенными. `canonical_event` JOIN подтверждён на реальном
positive-case, не только код-ревью. Готово к operator/demo использованию.
