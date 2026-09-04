# МІП — Segment Embeddings + Routing v1

**Дата:** 04.09.2026
**Статус:** vertical slice PASS.

## Контекст

Після завершення Content Segmentation v1 наступним кроком було перевірити,
чи можна виконувати semantic routing не на whole `content_item`, а на
аналітичних `content_segments`.

Це не змінює базовий інваріант:

**segment != event.**

Segment — contiguous analytical region однієї публікації, який може містити
один або декілька claims/events.

## Segment embeddings

### DDL

Migration `039_add_segment_embeddings.sql` додала:

- `segment_embeddings`;
- PK `(segment_id, embedding_model_id)`;
- FK на `content_segments`;
- partial HNSW cosine index для `embedding_model_id=1`.

Використовується існуючий embedding contract:

- model: `BAAI/bge-m3`;
- embedding_model_id = `1`;
- dimension = `1024`;
- metric = cosine;
- normalized embeddings;
- pinned model revision;
- sentence-transformers `5.7.0`.

### Worker

`experiments/segment_embeddings/segment_embedding_worker.py`

Поточний scope:

- explicit `--segmentation-run-id`;
- тільки successful segmentation runs;
- тільки missing embeddings;
- registry fail-fast;
- перевірка dimension = 1024;
- atomic persistence;
- повторний запуск без missing rows → `SKIP`;
- без scheduler/backlog/batching.

Live verification підтвердила:

- multi-segment run: `3/3`;
- single-segment run: `1/1`;
- idempotency PASS;
- усі calibration segments успішно embedded.

## Segment routing calibration

Для score function повторно використано existing content-routing prototypes:

`experiments/content_routing/prototypes_v1.json`

Score contract залишився:

`max cosine(relevant prototypes) - max cosine(irrelevant prototypes)`

Але whole-content thresholds не були автоматично перенесені на segments.

### Negative calibration

Перевірено 3 нерелевантні full-text publications:

- ТСН, recipes — deep negative;
- 24 Канал, mushroom/forest clothing — near old skip threshold;
- УНІАН, supermarket prices — near old skip threshold.

Після segmentation отримано 7 segments.

Segment scores:

- ТСН:
  - `-0.2407`
  - `-0.2255`
  - `-0.1975`
- 24 Канал:
  - `-0.1170`
  - `-0.1637`
- УНІАН:
  - `-0.0571`
  - `-0.0752`

Висновок:

`T_SKIP = -0.07` не показав практичної причини для зміни.

Один near-boundary negative segment перейшов у `maybe`, але не в `analyze`,
що відповідає ролі buffer zone.

## Analyze / maybe calibration

Додатково перевірено 4 full-text publications:

- BBC News Україна;
- УНІАН;
- Еспресо;
- 24 Канал.

Отримано 9 segments.

Ключові scores:

- BBC:
  - `0.0438`
  - `0.0156`
  - `0.0828`
  - `0.0938`
- УНІАН:
  - `0.0986`
  - `0.1178`
  - `0.0466`
- Еспресо:
  - `0.1581`
- 24 Канал:
  - `0.1259`

Whole-content `T_ANALYZE = 0.10` виявився трохи завищеним для segment
granularity: релевантний segment з score `0.0986` залишався б у `maybe`.

Для Segment Routing v1 прийнято:

- `T_SKIP = -0.07`;
- `T_ANALYZE = 0.095`.

Це **pilot-calibrated implementation detail**, а не універсальний statistical
threshold. Він може бути переглянутий після більшого human-labeled sample.

## Routing persistence

Migration `040_add_segment_routing.sql` додала:

`segment_routing_decisions`

Контракт:

- FK на `content_segments`;
- `routing_version`;
- `embedding_model_id`;
- decision `analyze | maybe | skip`;
- raw score;
- `code_revision`;
- UNIQUE `(segment_id, routing_version)`.

Content-level `content_routing_decisions` не змінювались.

## Segment routing worker

`experiments/segment_routing/segment_routing_worker.py`

Поточний scope:

- point-run через `--segmentation-run-id`;
- successful segmentation run only;
- BGE-M3 model registry verification;
- повне coverage check:
  - persisted segments == `segment_count`;
  - embedded segments == `segment_count`;
- worker відмовляється працювати з partial routing state;
- використовує existing prototypes v1;
- thresholds:
  - skip `-0.07`;
  - analyze `0.095`;
- усі decisions одного run записуються транзакційно;
- повторний completed run → `SKIP`.

## Live persistence verification

На УНІАН multi-segment run ручний score-only pilot давав:

- segment 0: `0.0986`;
- segment 1: `0.1178`;
- segment 2: `0.0466`.

Persisted worker result:

- `0.0986 → analyze`;
- `0.1178 → analyze`;
- `0.0466 → maybe`.

Результат точно співпав з ручним calculation.

Повторний запуск:

- `SKIP: current routing identity already complete`;
- persisted routing rows залишились `3`.

Idempotency PASS.

## Підсумок

Підтверджено vertical slice:

`occurrence_content`
→ `content_segments`
→ `segment_embeddings`
→ `segment_routing_decisions`

Segment-level semantic routing технічно працює end-to-end і не потребує
нового architecture-review циклу перед наступним implementation step.

Не входили в цей milestone:

- scheduler;
- production batching;
- automatic backlog processing;
- заміна current live content routing;
- claims downstream rewiring.

## Наступний крок

Наступний architectural/implementation junction:

визначити minimal contract для **claim extraction з routed segments** і його
співіснування з поточним content-level claim pipeline.

Перед зміною production path потрібно спочатку перевірити існуючі claim DDL,
worker та provenance contract і зробити additive segment-aware vertical slice,
не ламаючи чинний live pipeline.
