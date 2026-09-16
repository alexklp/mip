# МІП — Topics monitored corpus, temporal correctness і NLP cache — checkpoint 16.09.2026

## Статус і призначення

Цей checkpoint фіксує перевірені зміни Topics після `chatgpt_31_MIP_Product_Web_SourceExpansion_Handoff_10_09_2026.md`, а також важливий operational defect, виявлений під час фінального повторного review.

Документ є engineering memory, а не current baseline. При розбіжностях пріоритет мають live DB/runtime, актуальний код/tests і новіші вимірювання.

**Code baseline:** `9f4997b2717e06baef1de8ccecff8da3fbe94448` — `feat(topics): stabilize monitored snapshots with cached NLP`.

Активна гілка: `wip/signals-v1`.

Цей checkpoint **supersedes Topics implementation details** з `chatgpt_31` щодо `topics-lexical-rolling/1`, analyze-only routing і повної відсутності cross-language canonicalization.

## 1. Root cause coverage defect: `maybe` не можна трактувати як нерелевантний шум

Перевірка контрольних матеріалів показала, що значна частина релевантного корпусу потрапляє у routing state `maybe`. На вибірці UA/Trump за 24h спостерігалося приблизно:

- `analyze`: 19;
- `maybe`: 201;
- missing: 9;
- `skip`: 4.

Отже, старий Topics gate `decision='analyze'` системно втрачав релевантні публікації. Семантика, прийнята для Topics:

- `skip` — hard reject / high-confidence irrelevant;
- `analyze + maybe` — monitored corpus;
- routing state не треба приховувати або інтерпретувати як підтвердження факту.

У `9f4997b` Topics читає latest routing decision зі станами `analyze` або `maybe`.

Контрольний ефект на UA-просторі після зміни:

- phrase `дональд трамп`: rank 27, 76 publications, 29 sources;
- word `трамп`: rank 64, 168 publications, 52 sources.

Для порівняння старий analyze-only snapshot давав phrase `дональд трамп` приблизно rank 108, 7 publications, 7 sources. Це підтверджує, що проблема була саме coverage/routing-contract, а не лише presentation ranking.

## 2. Temporal correctness: snapshot не повинен знати майбутнє

Під час діагностики було знайдено дві temporal leakage проблеми старого snapshot:

1. occurrence міг потрапити в snapshot `as_of=T`, якщо `published_at<T`, навіть коли `collected_at>=T`;
2. latest routing decision обирався без `created_at < as_of`, тому старий snapshot міг ретроактивно змінюватися після нового routing run.

Поточний контракт у `reporting/topics_snapshot.py`:

- latest routing: `created_at < as_of`;
- observed window: `COALESCE(published_at, collected_at) >= start_at` і `< as_of`;
- додатковий ingestion cutoff: `io.collected_at < as_of`.

Перевірений invariant для тестового `as_of=2026-09-15 07:00:00+00:00`:

- rows: 28,389;
- max `collected_at`: `2026-09-15 06:58:07.892918+00:00`;
- rows with `collected_at >= as_of`: 0;
- result: `TEMPORAL_INVARIANT=OK`.

Це важливий truth/provenance invariant: publication time може описувати час матеріалу, але snapshot membership не може включати дані, яких система ще не зібрала на момент `as_of`.

## 3. Cache-only NLP contract

Повний synchronous NLP для monitored corpus виявився занадто дорогим для hourly snapshot. На старому контурі один run міг тривати десятки хвилин; окремий cron-run 16.09 був зупинений після ~1h21m, коли старий `topics_snapshot.py` все ще обробляв NLP і тримав `topics-snapshot.lock`.

Поточна модель розділяє responsibilities:

- `reporting/build_topics_nlp_cache.py` — preprocessing і persistent NLP cache;
- `reporting/topics_snapshot.py` — cache-only analytical snapshot, без fallback NLP на cache miss;
- cache path: `~/.local/state/mip/topics_nlp_cache_v1.jsonl`;
- cache version: `topics-nlp-cache/1`;
- fingerprint: cache version + full content text + sorted union of source names for `content_id`;
- cached payload: language, tokens, surfaces;
- cache miss не блокує один документ: він пропускається, а coverage явно рахується.

Причина включати union source names у fingerprint: `clean_text()` залежить від source names; ключ лише `(content_id, source_group)` давав би неправильний preprocessing contract.

Warmup measurements до checkpoint були приблизно 9–11 docs/s на 4 workers. Це performance observation, не SLA.

## 4. Coverage contract і latest accepted snapshot

`topics-lexical-monitored-cache/2` публікує `nlp_coverage` і має minimum publication coverage 95%.

Останній успішний snapshot, перевірений 16.09 для `as_of=2026-09-16T08:00:00+00:00`:

- input publications: 37,579;
- analyzed publications: 36,272;
- publication coverage: 96.522%;
- input materials: 34,323;
- analyzed materials: 33,124;
- missing materials: 1,199;
- material coverage: 96.507%;
- `incomplete=true`;
- `critical_incomplete=false`;
- DB read: 0.254s;
- cache/term processing: 4.948s;
- aggregation: 35.795s;
- total snapshot time: 40.998s.

`incomplete=true` тут означає відому неповноту cache, а не runtime failure. Це відповідає truth contract: coverage показується разом із denominator, а не маскується великими totals.

Aggregation (~36s) тепер є більшим bottleneck, ніж NLP read (~5s), але для hourly cadence цей baseline поки прийнятний. Оптимізацію aggregation не робити без нового symptom/вимірювання.

## 5. Limited cross-language entity canonicalization

У Topics додано вузьку, explicit canonicalization лише для двох entity markers:

- `путін` / `путин`, `володимир путін` / `владимир путин` -> `володимир путін`;
- `зеленський` / `зеленский`, `володимир зеленський` / `владимир зеленский` -> `володимир зеленський`.

Це **не** загальний entity-resolution layer і не дозвіл приховано canonicalize довільні теми. Механізм hard-coded і bounded; якщо список почне рости, його треба винести в окремий явний contract/data artifact.

Також Topics input очищає від відомого RSS/CMS footer boilerplate типу `The post ... first appeared on ...`.

## 6. Operational incident: старий hourly run тримав lock

Cron для Topics запускається щогодини на `:22`, wrapper використовує `flock` на `topics-snapshot.lock`.

16.09 старий analyze-only/synchronous-NLP run працював понад годину та блокував наступні запуски. Він був зупинений точково через SIGTERM Python process; process tree завершився, lock звільнився, wrapper записав `rc=143`.

Висновок: `RUNNER_ELAPSED=0s` при ручному smoke не означає швидкий success — це може означати, що `flock -n` відмовив і wrapper тихо вийшов 0. При operational smoke завжди перевіряти log markers і фактичний `algorithm_version/as_of`, а не лише shell rc.

## 7. Критичний defect, знайдений повторним code review після `9f4997b`

Фінальний review виявив, що hourly cache refresh у code baseline `9f4997b` **фактично зламаний**.

`build_topics_nlp_cache.py` досі робить:

```python
sql = ts.SQL.replace(
    "WHERE r.decision = 'analyze'",
    "WHERE r.decision IN ('analyze', 'maybe')",
    1,
)

if sql == ts.SQL:
    raise RuntimeError("routing gate replacement failed")
```

Але `ts.SQL` у `topics_snapshot.py` вже містить `WHERE r.decision IN ('analyze', 'maybe')`. Тому replacement нічого не змінює і builder детерміновано падає з `routing gate replacement failed`.

Додатково wrapper зберігає `cache_rc`, але фінальний exit code бере лише зі snapshot:

- cache builder може впасти;
- snapshot може успішно відпрацювати на старому cache;
- wrapper поверне `0`.

Тому попередній operational smoke `RUNNER_RC=0`, ~47s і валідний `/2` snapshot **не доводить**, що cache refresh пройшов. Навпаки, короткий runtime узгоджується з builder failure + snapshot на вже прогрітому cache.

Це current open defect. До виправлення не вважати hourly cache updater operationally verified.

### Мінімальний fix direction

Builder повинен використовувати authoritative monitored SQL contract без stale string-rewrite. Найпростіший варіант — брати `sql = ts.SQL` напряму, після чого:

1. запустити builder smoke і підтвердити `errors=0`;
2. запустити full wrapper один раз;
3. у log перевірити `TOPICS CACHE END rc=0`;
4. перевірити fresh snapshot `/2`, coverage >=95%;
5. тільки тоді вважати cache -> snapshot hourly chain green.

Окремо варто вирішити, чи cache failure має робити wrapper non-zero. Поточний snapshot coverage gate дає fail-safe від занадто stale cache, але silent cache failure погіршує observability.

## 8. Що не повторювати

- Не повертати Topics до analyze-only без нового evidence: coverage loss уже виміряний.
- Не запускати full synchronous NLP всередині snapshot: operational stall уже спостерігався.
- Не оцінювати temporal correctness лише по `published_at`; потрібен ingestion cutoff.
- Не вважати shell rc достатнім smoke criterion для wrapper з `flock` та окремими child rc.
- Не трактувати `incomplete=true` як помилку без перегляду `nlp_coverage`.
- Не масштабувати hard-coded entity aliases у неявний entity layer.

## 9. Наступний крок

Immediate task: виправити stale SQL rewrite у `build_topics_nlp_cache.py`, перевірити реальний cache refresh і wrapper log contract, після чого зробити окремий code checkpoint.

Після цього повернутися до наступного продуктового/Signals slice. Поточні локальні зміни Signals/web/contours не змішувати з Topics fix без окремої причини і перевірки diff.
