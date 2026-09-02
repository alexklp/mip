# МІП Claim-Extraction: Persistence Layer + Batch Worker — 20.08.2026

Статус: закрытый рабочий день. Все пункты ТЗ этой сессии выполнены и проверены на живой БД. Продолжение — следующая сессия.

## Контекст (для быстрого старта завтра)

Baseline claim-extraction (eval harness, prompt v2, frozen run4): коммиты `fc2b305` / `4706b98`. Quality tuning (prompt v3, root-cause not_found — Тип 1 trailing-punct / Тип 2 spliced-span, задокументировано в `claude/07_MIP_ClaimExtraction_EvalHarness_Findings_20.08.2026.md`) — **на паузе**, не трогать без отдельной команды.

Сегодня — новый vertical slice: `content_items → Mamay+prompt v2 → existing adapter/validator → PostgreSQL persistence`, от DDL до рабочего batch-worker'а.

## Что сделано и закоммичено

**Коммит `963ffaf`** — "Add claim-extraction persistence layer: schema, registry seed, single-item pilot":
- `sql/007_add_claim_extraction.sql` — 4 таблицы: `llm_models`, `claim_extraction_prompts`, `claim_extraction_runs`, `claims`. Ключевые инварианты: run-level all-or-nothing валидность (validator.py валидирует весь response атомарно), идемпотентность через `UNIQUE(content_id, llm_model_id, prompt_id, attempt_no)`, offsets zero-based/`evidence_end` exclusive, `code_revision` отдельно от `model_id`/`prompt_id` (баг-фикс кода vs другая модель/промпт). Transaction-инвариант — application-level, не DB-триггер: `status='valid'` → run+claims одной транзакцией; `status IN ('invalid','transport_error')` → только run, claims никогда не пишутся для невалидного run.
- `sql/008_seed_claim_extraction_registry.sql` — зарегистрированы `llm_model_id=1` (MamayLM-Gemma-3-27B-IT, `model_revision='v2.0/Q4_K_M/7677fe7e2df2'`, framework llama.cpp b9642) и `prompt_id=1` (`claim_extractor` v2, текст побайтово = `prompt_claim_extractor_v2.txt`).
- `experiments/claim_extraction/persist_single_run.py` — pilot-скрипт на одном hardcoded content_id. Верифицирован живьём: `content_id=4c472a60-b9fe-4e55-90b0-109f909b6e3e` (источник "Военкор Котенок Z", контур 4) → VALID, 6/6 claims resolved, `not_found=0`, `ambiguous=0`, provenance подтверждён SQL.

**Коммит `1ae0410`** — "feat(claim-extraction): add persisted batch worker":
- `experiments/claim_extraction/claim_extract_worker.py` (267 строк) — минимальный batch worker поверх pilot-скрипта. Отличия от pilot: (1) выборка кандидатов через `NOT EXISTS` против `claim_extraction_runs` (`content_items` JOIN `item_occurrences` по паттерну `embed_worker.py`), `--limit N` обязательный CLI-параметр без дефолта; (2) per-item изоляция ошибок — один плохой item (transport error или неожиданное исключение при записи в БД) не валит весь batch, это сознательный отход от all-or-nothing философии `embed_worker.py`; (3) транзакция per-item (commit/rollback на каждом content_id отдельно), не на весь batch; (4) `code_revision` честно отражает dirty working tree (`git status --porcelain`), не только short SHA; (5) лог по каждому item + summary в конце batch (valid/invalid/transport_error/error counts).
- Без изменений: `run_eval.py`, `validator.py`, `persist_single_run.py`.

## Живой прогон и верификация

`--limit 5` на реальных `content_items` (контур не фильтровался явно — общий eligibility pool):

```
batch: 5 content_id(s) eligible (limit=5), code_revision=963ffaf+dirty
5/5 VALID, 0 invalid, 0 transport_error, 0 error
latency: 19.0–34.6s per item
offset_stats: resolved=claim_count, not_found=0, ambiguous=0 на всех 5
```

Provenance verified SQL (`claim_count` из `claim_extraction_runs` == `count(*)` из `claims` по `run_id`) — совпало 1:1 на всех 5 строках.

Идемпотентность verified SQL: тот же eligibility-запрос сразу после прогона вернул 5 **других** content_id, ни одного пересечения с уже обработанными.

`worker_run1.log` в git не попал (`*.log` в `.gitignore`) — это ожидаемо, лог runtime-артефакт, не код.

## Открытые пункты на следующую сессию

- Дальнейшие batch-прогоны `claim_extract_worker.py` на большем `--limit` — infrastructure готова, вопрос только объёма/цели (накопление данных под будущий анализ vs что-то ещё).
- Prompt v3 (root-cause not_found: Тип 2 spliced-span, 4/12 в run4, нарушение explicit anti-splice правила v2) — по-прежнему на паузе, ждёт отдельного решения.
- Per-claim (non-run-level) persistence, normalization, entities, relations, event clustering, contradiction detection, RAG, Hermes, queue/broker, systemd, parallel inference — вне scope, не начинать без явной команды.
- Идея на будущее (не запланирована): cross-source RU-vs-UA narrative comparison (контур 4 vs контур 1/3) — залогировано в `01_PROJECT_CONTEXT.md`, раздел "Ідеї на майбутнє".

## Текущий git HEAD

`1ae0410` (main), working tree чистый после этого коммита (кроме возможных runtime-логов, которые gitignored).
