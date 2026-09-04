# МІП — Content Segmentation v1: persistence pilot

**Дата:** 04.09.2026  
**Статус:** vertical slice PASS.

## Рішення

Для v1 підтверджено робочий persistence path:

`occurrence_content(success)`
→ deterministic `sectionize()`
→ Mamay adjacent-boundary `same|new`
→ deterministic contiguous segment assembly
→ `segmentation_runs`
→ `content_segments`

Segment залишається аналітичним фрагментом публікації, а не event.

## Реалізовано

### DDL

Migration `037_add_content_segmentation.sql`:

- `segmentation_prompts`;
- `segmentation_runs`;
- `content_segments`.

Ключові контракти:

- segmentation дозволена лише для успішного `occurrence_content`;
- zero sections = `sectionize_error`;
- successful run + усі child segments записуються однією DB-транзакцією;
- partition ranges перевіряються application-level:
  ordered, contiguous, без gaps/overlaps;
- `segment_index` послідовний;
- segment text hash = SHA-256 UTF-8;
- boundary decisions зберігають raw model response та runtime metadata.

Migration `038_seed_segmentation_registry.sql`:

- зареєстровано `adjacent_boundary / v1`;
- schema `adjacent-boundary/1`;
- використовується існуючий Mamay model registry entry.

### Worker

`experiments/content_segmentation/segmentation_worker.py`

Поточний scope навмисно мінімальний:

- тільки explicit `--occurrence-id`;
- без batch;
- без scheduler;
- без backfill;
- без downstream rewiring.

Worker:

- resolve поточного successful `occurrence_content`;
- читає prompt із registry;
- не тримає DB connection під час Mamay inference;
- виконує pairwise boundary classification;
- deterministic assembly;
- перевіряє partition перед persistence;
- success run + всі segments пише atomically;
- повторний запуск тієї самої segmentation identity робить `SKIP`.

## Live verification

### Single-section case

Перевірено шлях без LLM boundary calls:

- sections = 1;
- segments = 1;
- boundaries = 0;
- persisted child segments = 1;
- status = `success`.

Це підтвердило DB persistence contract окремо від Mamay inference.

### Multi-section case

На раніше перевіреному multi-topic digest:

- sections = 4;
- Mamay boundaries:
  - `same`
  - `new`
  - `new`
- deterministic result:
  - `(0,1)`
  - `(2,2)`
  - `(3,3)`
- segments persisted = 3;
- status = `success`.

Результат точно відтворив попередній pairwise technical pilot.

### Idempotency

Повторний запуск тієї самої segmentation identity:

- worker повернув `SKIP`;
- кількість successful runs залишилась `1`.

## Висновок

Content Segmentation v1 vertical slice підтверджено end-to-end:

`full article extraction → structured XML → sectionize → Mamay boundaries → deterministic segments → persistence`.

Немає потреби в новому prompt-tuning або architecture-review циклі перед наступним implementation step.

Наступні речі НЕ входили в цей milestone:

- scheduler;
- backfill;
- production batching;
- downstream embeddings/routing/claims rewiring;
- source-specific extraction hardening.
