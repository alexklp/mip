# МІП Content Routing v1 — 21.08.2026

Статус: vertical slice закрыт и закоммичен. `HEAD=77cad07` (main), working tree чистый.

## Контекст и цель

Цель этапа: резко уменьшить объём контента перед дорогим Mamay claim extraction (десятки секунд на item), чтобы быстрее выйти на первый аналитический слой. Ingestion (`content_items`/`item_occurrences`) и BGE-M3 embeddings (`embedding_model_id=1`, 6211 eligible, backlog=0) уже стабильны — не трогались.

## Архитектурное решение

Не supervised classifier (LogisticRegression/sklearn — sklearn 1.9.0 в venv подтверждён, но не понадобился), а **zero-shot semantic routing** поверх уже существующих BGE-M3 embeddings: небольшой набор textual prototypes (relevant/irrelevant), закодированных той же моделью, cosine similarity к content-вектору → `score = max(cos_sim, relevant) − max(cos_sim, irrelevant)` → `analyze/maybe/skip` по порогам. Без training pipeline, без новой embedding-модели, без neural classifier — самое простое обратимое решение, соответствующее приоритету "conservative baseline быстро" над "идеальный classifier долго".

Ключевой prior conservative-приоритет: false negative (`relevant → skip`) намного хуже false positive. Поэтому skip-зона узкая, maybe-зона широкая (всё сомнительное остаётся в потоке на Mamay).

## Что сделано и закоммичено (`77cad07`)

**`sql/009_add_content_routing.sql`** — таблица `content_routing_decisions`:
- `routing_version` — immutable identity контракта (smallint, без отдельной registry-таблицы — здесь нет live-процесса для fail-fast сверки, просто версия в коде). Меняются prototypes/thresholds/embedding_model_id/scoring logic → новая версия, не UPDATE.
- `embedding_model_id` — явный FK на `embedding_models`, фиксирует зависимость routing от конкретного feature space (BGE-M3).
- `decision` — CHECK IN ('analyze','maybe','skip').
- `score` — raw margin, НЕ нормирован в [0,1], CHECK (-2..2) как свободная sanity-граница (не семантический контракт).
- `UNIQUE(content_id, routing_version)` — идемпотентность.

**`experiments/content_routing/`**:
- `prototypes_v1.json` — 12 relevant prototypes (бойові дії, санкції, заяви посадовців, мобілізація, атаки на інфраструктуру, дипломатія, пропаганда, кібератаки, окуповані території) / 10 irrelevant (рецепти, гороскопи, шоу-бізнес, спорт, lifestyle, реклама, погода, гумор).
- `routing_scan.py` — read-only розвідка: считает score/decision на всём eligible corpus, печатает перцентили, decision counts, примеры з трёх зон. Не пишет в БД.
- `audit_skip_keywords.py` — одноразовая лексическая подстраховка: keyword-grep по SKIP-зоне на предмет явно военной лексики в title (не production-компонент).
- `routing_worker.py` — idempotent backfill worker. Batch commit (по 500), fail-fast на ошибку (без per-item isolation — в отличие от `claim_extract_worker.py`, здесь нет внешнего непредсказуемого LLM-вызова, только детерминированное вычисление, так что паттерн ближе к `embed_worker.py`). `code_revision` с dirty-detection (тот же паттерн, что в `claim_extract_worker.py`).

## Калибровка порогов (важная часть работы)

Первый прогон: `T_SKIP=-0.05`, `T_ANALYZE=0.10` → `analyze=722 (11.6%)`, `maybe=4282 (68.9%)`, `skip=1207 (19.4%)`. Пять случайных примеров на зону выглядели чисто, НО keyword-audit по всей SKIP-зоне (1207 items) нашёл 9 хитов, из них **2 реальных false negative**: сообщения о реальных ударах дронов/ракет по Києву и Борисполю, score `-0.0527` и `-0.0571` — прямо на границе порога.

Важное наблюдение: оба false negative были МЕНЕЕ отрицательны, чем все корректно-skip примеры (от `-0.0627` и дальше) — чистая, безопасная граница для сдвига. Порог сдвинут на `T_SKIP=-0.07`. Пересчёт: `analyze=722 (11.6%, не изменился)`, `maybe=4662 (75.1%)`, `skip=827 (13.3%)`. Повторный keyword-audit: оба false negative ушли в maybe, остальные 6 хитов — false positive самого keyword-grep'а (подстрочные коллизии типа "війн" внутри "подвійна", "фронт"/"наступ" в контексте погоды).

Оговорка: keyword-audit — разовая, не исчерпывающая проверка (ограниченный список слов), не полноценный eval. Это осознанный компромисс — conservative baseline быстро, а не multi-day tuning.

## Backfill и интеграция

`routing_worker.py` прогнан на полном corpus: `6211/6211` distinct content_id, без дублей, распределение `722/4662/827` — совпадает с `routing_scan.py` детерминированно (одинаковый scoring, что и ожидалось).

`experiments/claim_extraction/claim_extract_worker.py` — точечный патч `fetch_batch()`: добавлен `AND EXISTS (... content_routing_decisions ... decision IN ('analyze','maybe') ...)`. Сам claim extraction contract (adapter/validator/transaction-инварианты) не тронут. Eligible backlog для Mamay упал с `6211` до `5380`.

## Открытые пункты на будущее (не запланированы)

- Опциональный `reason` (lifestyle/entertainment/sport/commercial/other_irrelevant) — задел в схеме есть (`reason text nullable`), не заполняется в v1.
- Human feedback subsystem (планируется позже как источник training/eval data) — сознательно не реализован.
- Дальнейшая калибровка порогов/prototypes, если появятся новые false negative сигналы — не начинать без причины (новые данные/жалобы), не микротюнинг ради тюнинга.
- Массовый Mamay backfill на routing-eligible `5380` — не запущен, следующий шаг по решению пользователя.

## Текущий git HEAD

`77cad07` (main), working tree чистый.
