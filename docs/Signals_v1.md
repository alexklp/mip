# Сигнали: Phase 4

Стан на 2026-09-08: relevance gate, баланс anchors, core/review та bounded
presentation реалізовано. 85 signals tests PASS, compile/syntax/whitespace та
`git diff --check` PASS. Один live read-only benchmark завершився RC=0.
ANN recall і семантична якість — UNVERIFIED. Новий production snapshot не
генерувався; наявний `reporting/signals.latest.json` залишається діагностичним.

## Контракт та архітектура

`SELECT → observed/routing coverage → analyze anchors round-robin → bounded HNSW → complete-link core → suppression/rank/cap + separate review → signals/4 → GET /signals`

- `signals_data.py`: типізовані матеріали, джерела, occurrences та відомі SQL-пари;
  `Content.routing_decision=None` означає відсутність рішення, не analyze.
- `signals_postgres.py`: параметризовані SELECT; перевіряє read-only, repeatable
  read, timeout, live модель, dimension та ANN settings. Немає DDL/DML/SET.
- `signals_detector.py`: analyze-only відбір, complete-link core, окремі related
  links, suppression та детермінований показ. Невідома пара не створює зв'язку.
- `signals_snapshot.py`: явна `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`
  перед SELECT, валідація schema `signals/4`, алгоритм `signals-routing-core-review/4`.
  Benchmark не викликає запис; лише окремий режим `--output` атомарно замінює JSON.
- `web/signals.py` та шаблон: лише bounded JSON із перевіркою контракту, без БД,
  моделей та recall у request path. Стани missing/invalid/stale/empty/ready.

Schema `signals/2` несумісна з новими обов'язковими metadata і відхиляється.
Це стосується й діагностичного latest.json; його не конвертовано та не замінено.
До дозволеної генерації `signals/4` UI може показувати invalid.

## Routing relevance gate

Перевірені джерела: `sql/009_add_content_routing.sql`,
`experiments/content_routing/routing_worker.py`, `routing_scan.py` та shadow v2.
Production v1 рахує raw margin cosine similarity до relevant/irrelevant prototypes:
`score >= 0.10 → analyze`, `score <= -0.07 → skip`, інакше `maybe`.
Shadow v2 використовує ці пороги провізорно й не переписує production decisions.
Signals не переобчислює routing score і не навчає модель.

Таблиця має PK routing_id, FK content/model, додатний routing_version,
CHECK decision IN ('analyze','maybe','skip'), UNIQUE(content_id,routing_version).
Цей UNIQUE також створює B-tree для доступу за content/version; новий індекс
не потрібен для коректності latest lookup. Вибір завжди:

```sql
SELECT r.decision FROM content_routing_decisions r
WHERE r.content_id = <поточний content_id>
ORDER BY r.routing_version DESC, r.created_at DESC, r.routing_id DESC
LIMIT 1
```

Лише після latest застосовується `= 'analyze'`. Не можна фільтрувати analyze
всередині latest: новий skip/maybe мусить перекривати старий analyze. Немає
фіксованої routing_version чи фільтра за моделлю перед latest. Відсутній рядок
дає NULL і fail-closed виключення. Gate є в ANCHOR_SQL, зовнішньому
NEIGHBOUR_SQL та ROWS_SQL, а також у локальному detector-контракті.
`content_contour_assignments` не використовується як gate.

`routing_coverage[previous|current][ru_space|ua_space]` містить distinct content
counts `analyze/maybe/skip/missing` до relevance gate. Сума в кожній комірці
дорівнює `coverage[window][group].content_count`; validator перевіряє ключі,
типи, невід'ємність та суми. Матеріал може рахуватися в кількох комірках,
тому їх сума не є глобальним distinct count. Missing дає incomplete та
попередження; очікувані maybe/skip не є критичною втратою evidence.

## Вікна та спільний watermark

Previous `[as_of−48h, as_of−24h)`, current `[as_of−24h, as_of)` за `collected_at`.
`published_at` зберігається окремо, може бути відсутнім чи поза вікнами.
Coverage всього спостережуваного потоку залишається знаменником dynamics,
незалежно від routing, display cap та лімітів evidence.

`--as-of common` у тій самій read-only транзакції читає max(collected_at)
кожної вибраної групи до CURRENT_TIMESTAMP і бере мінімум цих maxima. Якщо
хоча б одна група не має watermark, runner відмовляється. Верхня межа вікна
не включена. Це спільний watermark спостережень, не гарантія завершення
collectors, embeddings або routing. Для відтворюваного зрізу можна передати
явний UTC timestamp. Latest routing означає стан на момент транзакції;
історичний as_of не відновлює старі routing decisions.

## Bounded round-robin anchors

Для кожної вибраної групи SQL повертає максимум `anchors + 1` analyze-матеріалів,
упорядкованих за last_observed DESC, observed_sources DESC, content_hash.
`count(*) OVER()` дає точний available до LIMIT, після eligibility-фільтрів.
Загальний алгоритм чергує лексикографічно впорядковані назви груп, пропускає
порожні черги та дублікати content_id. Назви просторів у round-robin не зашиті.
Загальний `--anchors` не множиться на кількість груп. Для двох непорожніх
неперетинних черг і anchors=40 результат — 20/20.

`selection.anchor_groups[group]`: selected — IDs, приписані ходу цієї групи;
available — усі придатні IDs групи; limit_reached — залишилися невідібрані IDs.
Один content може бути available для кількох груп, але selected рахується
один раз. Через перетин selected < available не завжди означає limit_reached.
Bounded prefix `anchors+1` достатній для встановлення факту залишку; повний
корпус IDs не надходить у Python. Запити агрегації можуть бути дорогими.

## Core, review та показ

Core threshold default 0.18 (`--core-distance`, сумісний alias `--max-distance`).
Лише відомі пари <= core threshold можуть об'єднувати матеріали. Додавання до
групи вимагає відомої допустимої відстані до КОЖНОГО її учасника. A–B–C
не зливається через міст B. Невідомі взаємні пари розділяють потенційні сюжети.

Related threshold default 0.36 (`--related-distance`, має бути >= core).
Пари `(core, related]` зберігаються окремо в `related_links`, статус unverified,
порядок distance/left ID/right ID. Default `--max-related-links 100`, ceiling 1000.
Вони не змінюють core content_count, source_count, occurrence_count або dynamics.
Review endpoint може належати подавленій чи непоказаній групі; це не робить
його кандидатом. Validator перевіряє endpoints серед selected_content_ids,
діапазон відстані, унікальність, порядок та ліміт.

Core із менше двох distinct source_id не показується. Причини suppression
взаємовиключні та рахують групи:

- singleton_single_source: один content та одна occurrence в одному джерелі;
- repeated_content_single_source: повтори одного content лише в одному джерелі;
- core_single_source: кілька core contents, але лише одне джерело.

Exact republication означає один content_id у мінімум двох distinct sources,
не повторну occurrence в тому самому source. Same-space multi-source core
дозволений; cross_space — окремий прапорець, не вимога публікації.

Ранжування: cross_space DESC, source_count DESC, content_count DESC,
last_observed DESC, candidate_id ASC. `--display-limit` default 20, ceiling 200.
Freshness береться з усіх core occurrences, не з обрізаної chronology.
`presentation` зберігає core_group_count, eligible/displayed counts, suppression,
display cap, related available/count/cap flags та bounded selected_content_ids.
Validator звіряє суми, порядок, caps, distinct-source контракт та повну chronology,
коли evidence не обрізано. Suppression та display cap не змінюють coverage.

Усі результати — candidate/unverified, не підтверджені події, наративи,
запозичення чи незалежні підтвердження. Калібрування routing і thresholds —
окрема наступна фаза.

## HNSW та обмеження recall

Збережено `WITH ann AS MATERIALIZED`, прямий cosine ORDER BY над
`embedding::vector(1024)`, scalar anchor InitPlan та bounded probe. Literal
predicate `embedding_model_id=1` відповідає існуючому partial expression
`idx_embeddings_hnsw_bge_m3` із `sql/006_add_embeddings.sql`. Read звіряє live
модель/ревізію, cosine та dimension=1024 і відхиляє інший ID/dimension.
Змінні — bind parameters, SQL-рядки користувача не інтерполюються.

Кожний production neighbour виконується з `prepare=False`, також після
багатьох викликів; psycopg не переводить його на generic plan через threshold.
Literal partial predicate додатково не залежить від параметризованого model ID.
`enable_seqscan` не змінюється. Runtime EXPLAIN автоматично не запускається.

`--ann-probe-limit` default 100, ceiling 1000, не менше neighbours+1; CLI
використовує ту саму PostgresLimits validation. Значення є у selection.limits
та selection.ann. Metadata також має ef_search, iterative_scan, prepared=false,
filter_stage=after_probe та recall_status=UNVERIFIED. Підтримано iterative_scan=off;
ef_search перевіряється в межах 1–1000, жодних SET адаптер не виконує.

ANN шукає в усій моделі ДО routing/часових/source-group фільтрів. Старі,
maybe/skip/missing та anchor можуть витрачати probe. LIMIT 100 не гарантує
100 кандидатів за ef_search=40 і не обмежує всі внутрішні відвідування графа.
Навіть ненасичений neighbour limit не доводить вичерпності. Будь-який ANN
пошук консервативно дає truncated=true; recall лишається UNVERIFIED.
Детермінований порядок гарантується для однакового набору ANN-кандидатів,
не глобальний exact top-k чи стабільний склад на межі probe при рівних відстанях.

## Перевірені результати

Після другого self-review: 85 тестів PASS за 0.401 s, без SKIP, у наявному venv.
У sandbox HTTP TestClient зависає; точний suite поза sandbox проходить без
сервера. Нових залежностей не встановлено. Перевірені latest SELECT над
синтетичними versions/ties/missing, analyze gate, routing sums, generic
round-robin, core/review boundary, suppression, same-space exact spread,
rank/caps, CLI probe/common watermark, schema та HTTP без DB/recall/network.
Compile 9 Python-файлів і Jinja syntax, whitespace та git diff --check — PASS.

Self-review виправив зовнішнє SQL-ім'я window у routing coverage, додав
перевірку review endpoints, presentation sums, сортування та distinct-source
exact republication. Усунено повторне discovery імпортованого тестового класу.
Зміни пройшли повторний повний suite до єдиного live benchmark.

Live benchmark 2026-09-08: явна REPEATABLE READ READ ONLY транзакція, common
watermark **2026-09-08T11:15:04.004453+00:00**, 40/10/400/5000,
core=0.18, related=0.36, probe=100, display=20, review cap=100, timeout=15 s.

| Вікно | Простір | analyze | maybe | skip | missing | Усього content |
|---|---|---:|---:|---:|---:|---:|
| current | ru_space | 457 | 3213 | 417 | 0 | 4087 |
| current | ua_space | 112 | 699 | 217 | 22 | 1050 |
| previous | ru_space | 314 | 2626 | 315 | 0 | 3255 |
| previous | ua_space | 92 | 521 | 178 | 0 | 791 |

На відміну від попереднього наданого 100% coverage, на новому watermark
22 current/ua_space content мають missing routing. Вони fail-closed виключені;
це не приховано як analyze чи нульовий missing.

| Група anchors | Selected | Available (48h distinct) | Limit reached |
|---|---:|---:|---|
| ru_space | 20 | 770 | true |
| ua_space | 20 | 204 | true |

Результат: 130 selected contents, 134 occurrences, 249 inspected pairs,
156 унікальних пар <=0.36. Із 122 core groups показано **7 core candidates**;
115 подавлено: **111 singleton_single_source, 2 repeated_content_single_source,
2 core_single_source**. Display cap не досягнуто. **100 related links із 144**,
related_limit_reached=true. critical_incomplete=false, **benchmark RC=0**.

| SELECT | Викликів | Разом ms | Min–max ms |
|---|---:|---:|---:|
| dimension | 1 | 182.206 | 182.206 |
| coverage | 1 | 14.084 | 14.084 |
| routing coverage | 1 | 17.132 | 17.132 |
| anchors | 2 | 296.265 | 146.418–149.847 |
| neighbours | 40 | **72.742** | **0.893–3.381** |
| evidence rows | 1 | 3.647 | 3.647 |

ANN settings: ef_search=40, iterative_scan=off. У Phase 4 новий EXPLAIN не
запускався: benchmark виконує тільки SELECT. Shape захищено regression tests;
query_plan_status лишається UNVERIFIED для цього запуску. Історичний live
EXPLAIN Phase 3 (до routing gate) підтвердив Index Scan idx_embeddings_hnsw_bge_m3
без Seq Scan embeddings у neighbour: 0.980 ms, shared hit/read 259/359, 10 rows.
Тоді anchor мав Seq Scan embeddings: 176.180 ms, hit/read 280293/31646.
Не переносимо один старий план на всі нові вікна; швидкі timings не є доказом
плану. Поточні anchor/dimension SELECT залишаються основною витратою часу.

Snapshot до/після benchmark має незмінний SHA256:
`f29c618c617655786fd6c8b78c286eb544e71c47db6fe472382bc7157bde2757`.
Його шлях додано в .gitignore. DB writes, індекси, міграції, model calls,
collectors/workers/scheduler, новий сервер та commit/push/merge не виконувалися.

## Перевірка і наступна дозволена оператором генерація

З кореня `/home/ovasyliev/mip-signals-codex`:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/ovasyliev/mip/.venv/bin/python -m unittest discover -s tests -p 'test_signals*.py' -v
git diff --check
```

Нижче одна команда для генерації нового snapshot **лише після окремого дозволу
оператора**; у Phase 4 її не виконували. Вона читає live БД в явній read-only
транзакції, потім атомарно замінює лише локальний JSON. PGPASSFILE=/dev/null
виключає читання password file (libpq може надрукувати попередження про тип файла).
Для benchmark замість `--output ...` використовується `--benchmark`.

```bash
PYTHONDONTWRITEBYTECODE=1 PGPASSFILE=/dev/null PGHOST=/var/run/postgresql \
/home/ovasyliev/mip/.venv/bin/python -m reporting.signals_snapshot \
  --as-of common --database mip_dev \
  --model-id 1 --model BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181 \
  --dimension 1024 --source-groups ru_space ua_space \
  --anchors 40 --neighbours 10 --pairs 400 --rows 5000 \
  --core-distance 0.18 --related-distance 0.36 --ann-probe-limit 100 \
  --display-limit 20 --max-related-links 100 \
  --evidence-chars 600 --max-evidence 12 --statement-timeout-ms 15000 \
  --output reporting/signals.latest.json
```

Critical evidence loss (row cap, відсутні очікувані evidence, analyze-потік без
придатного evidence), timeout або помилка контракту повертають RC=1 і не
замінюють попередній файл. Missing routing сам по собі дає incomplete, не RC=1.
Новий generation time не робить старий watermark свіжим; loader stale default
7200 s. os.replace дає атомарну видимість; fsync директорії не виконується.
Розклад генерації не активовано. HTTP перечитує JSON, але не оновлює його.
