# Live Segment Pipeline v1 — checkpoint 04.09.2026

## Статус

Bounded live path для occurrence-level full text → segmentation → segment embeddings → segment routing → segment claim extraction реализован и проверен на живых данных.

Это операционный milestone, а не изменение архитектурного принципа.

## Реализовано

### Orchestrator

Добавлен:

`experiments/segment_pipeline/segment_pipeline_once.py`

Основная политика v1:

- parent content routing: `analyze` only;
- RSS occurrence full-text;
- deepest partial state first;
- используются существующие point-run workers без дублирования processing logic;
- Mamay workloads сериализуются через общий `mamay.lock`;
- segment claims запускаются только для `analyze`;
- explicit occurrence/claim budgets;
- oversized claim inputs ограничиваются отдельным operational guard;
- scheduler внутри Python orchestrator отсутствует.

Приоритет work items:

1. `resume_downstream`;
2. `fulltext_ready`;
3. `needs_fulltext`.

## Первый bounded live run

Параметры:

- occurrence limit: 3;
- claim limit: 5.

Результат:

- 3 существующих segmented occurrences успешно resumed;
- routing:
  - `analyze`;
  - `maybe`;
  - `analyze`;
- `maybe` корректно не передавался в claim extraction;
- один analyze segment дал 16 VALID claims;
- один длинный analyze segment дал 47 claims, но run стал INVALID из-за одного evidence span.

### Negative result

Для длинного segment input:

- text length: 8629 chars;
- latency: 428856 ms;
- claims: 47;
- offsets resolved: 46;
- not_found: 1.

Проблемный evidence span оказался почти дословным, но не exact:

- модель изменила грамматическую форму имени;
- также изменила whitespace/paragraph formatting;
- aligned similarity была около 0.98.

Решение:

**не вводить fuzzy grounding в strict validator.**

Exact evidence grounding остаётся инвариантом. Перефразированный span не должен автоматически приниматься как цитата.

## Claim input size guard

На момент измерения было 4 `analyze` segments с длинами примерно:

- 636;
- 1607;
- 3697;
- 8629 chars.

Введён временный operational guard:

`--max-claim-chars 4000`

Это:

- не limitation Mamay;
- не архитектурный constraint;
- не semantic threshold;
- а reversible live safety limit.

Oversized segments не должны удерживать `resume_downstream` queue.

## Второй bounded live run

Параметры:

- occurrence limit: 3;
- claim limit: 5;
- max claim chars: 4000.

Полный результат:

### Resume occurrence

3 segments:

- analyze → 17 VALID claims;
- analyze → 19 VALID claims;
- maybe → claim extraction не запускался.

### Fulltext-ready occurrence

Segmentation:

- 3 sections;
- 3 segments.

Routing:

- analyze;
- maybe;
- maybe.

Analyze segment:

- 561 chars;
- 14 VALID claims;
- all offsets resolved.

### Fresh needs-fulltext occurrence

Полностью прошёл новый path:

`RSS → occurrence_content → segmentation → segment embedding → segment routing → segment claim extraction`

Результат:

- full text success;
- 1 segment;
- routing `analyze`;
- 1712 chars;
- 10 VALID claims;
- all offsets resolved.

Итого во втором live run:

- 4 claim attempts;
- 4 VALID;
- 60 claims;
- `not_found=0`;
- `ambiguous=0`.

## Performance observation

Measured claim latencies показали, что input character length не является достаточным predictor runtime.

Примеры:

- 2207 chars → ~164 s;
- 1185 chars → ~205 s;
- 561 chars → ~104 s;
- 1712 chars → ~91 s.

Поэтому `max_claim_chars=4000` сохраняется как safety guard, но не рассматривается как runtime budget.

Mamay остаётся строго serial resource.

## Ops wrapper

Добавлен:

`ops/run_segment_pipeline_once.sh`

Live limits v1:

- occurrence limit: 2;
- claim limit: 2;
- max claim chars: 4000.

Wrapper:

- использует отдельный `segment-pipeline.lock`;
- загружает runtime environment;
- пишет дневной log;
- не удерживает `mamay.lock` на весь pipeline;
- Mamay lock берётся непосредственно LLM workers.

## Wrapper verification

Ручной запуск wrapper:

- full-text extraction успешно выполнился;
- на segmentation общий `mamay.lock` оказался занят существующим contour workload;
- pipeline корректно завершился через SKIP;
- exit code 0;
- partial state остался resumable.

Это подтверждает ожидаемое coexistence с существующими live Mamay jobs.

## Live scheduling

Добавлен cron:

`17 * * * * $HOME/mip/ops/run_segment_pipeline_once.sh`

Текущее расписание Mamay workloads разнесено по времени, а общий `mamay.lock` остаётся окончательной защитой от overlap.

Первый автоматический cron cycle на момент этого checkpoint ещё не проверен по log.

## Commits

- `34db38e` — bounded segment pipeline orchestrator;
- `f02121e` — max segment claim input guard;
- `ab0e027` — bounded segment pipeline ops runner.

## Что НЕ входит в этот milestone

Не решены и сознательно не включены:

- oversized segment processing strategy;
- segment claim embedding automation;
- relation candidate production;
- relation semantic calibration;
- candidate generation scaling;
- event construction.

Relation production остаётся отдельным downstream этапом.

## Следующий практический шаг

После первого cron cycle:

1. проверить только новый segment-pipeline log;
2. убедиться, что automatic bounded resume/live processing проходит без неожиданного overlap;
3. после этого считать live segment pipeline v1 operationally accepted.

Не расширять budgets или `maybe` policy без новых измерений.
