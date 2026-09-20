# Signals: Global Detection + Analyst Workflow Checkpoint — 20.09.2026

## Статус

Завершено та перевірено робочий vertical slice **Signals** у гілці `wip/signals-v1`.

Цей checkpoint у частині Signals уточнює та замінює проміжний стан, описаний у `chatgpt_31_MIP_Product_Web_SourceExpansion_Handoff_10_09_2026.md`.

Ключові commits:
- `4c76d1b` — `feat(signals): scale global detection with sparse HNSW graph`
- `86a9341` — `feat(signals): complete analyst timeline and evidence workflow`

Поточний код і новіші runtime-вимірювання мають пріоритет над цією note.

## Що реалізовано

### 1. Global Signals detector

Попередній exact all-pairs підхід для поточної популяції контенту був непридатний за вартістю. Поточний production-підхід — sparse kNN через PostgreSQL + pgvector HNSW.

Основні властивості:
- поточна популяція, без persisted edge table;
- HNSW cosine;
- bounded anchors/neighbours/pairs/rows;
- source/routing/time constraints застосовуються у read path;
- кандидати ранжуються насамперед за шириною поширення між джерелами, а не за різницею у секундах свіжості;
- `recall_status=UNVERIFIED` означає, що ANN recall відносно exact ground truth не виміряний, а не те, що runtime incomplete.

### 2. Presentation snapshot

Основний Web Signals читає bounded JSON snapshot і не виконує clustering у HTTP request.

Важливі контракти:
- hard cap snapshot: 8 MiB;
- останній перевірений snapshot після UI/data-contract змін: близько **6.91 MiB**;
- `chronology` у snapshot навмисно є bounded sample;
- `representative_title` використовується як нормальний сюжетний заголовок;
- `first_published_at` / `last_published_at` рахуються по повному candidate, а не по `chronology[:12]`.

Перевірка full-range contract:
- перевірено 200/200 displayed candidates;
- у 90/200 реальний publication range був ширший за sample chronology;
- отже старий підхід через перші 12 рядків справді міг занижувати тривалість сигналу.

### 3. Publication spread

`publication_spread` — окремий аналітичний зріз за publication time у поточних 24 годинах.

Важливо не змішувати:
- `candidate.occurrence_count` — повний signal candidate, який може включати підтримку ширшого 48h analytical window;
- `publication_spread.published_count` — лише публікації, що потрапили у current 24h publication-time window.

Dot timeline будується з повного `publication_spread.hourly`, а не з bounded chronology sample.

Hourly buckets не містять точних хвилин кожної публікації. UI рівномірно розкладає точки всередині годинного bucket лише як візуалізацію щільності. Такі позиції не є точними timestamps.

### 4. Повна хронологія та evidence

Додано окремий bounded read-only endpoint:

`GET /signals/{candidate_id}/chronology`

Він:
- працює через штатний `web.db.read_connection()`;
- приймає лише candidate, присутній у поточному snapshot;
- використовує параметризований SQL;
- повертає chronology/evidence порціями;
- звіряє expected total із snapshot і при розбіжності не маскує stale/mismatched стан.

UI розділено на два незалежні сценарії:
- **Хронологія сигналу** автоматично догружає повний ланцюг лише для активного сигналу, батчами по 50;
- **Публікації та джерела** залишається detail/evidence view: bounded initial sample + ручне `Показати ще 30`.

Догрузка evidence більше не керує верхньою хронологією.

### 5. Analyst UI

Підтверджено:
- сюжетні заголовки замість масового generic `Пов’язані публікації у двох просторах`;
- повний dot spread за hourly aggregate;
- повна auto-load chronology для активного сигналу;
- незалежний detail listing;
- queue publication range по повному candidate;
- auto-refresh зменшено з 60 секунд до 10 хвилин, щоб не збивати роботу аналітика;
- chronology panel зроблено компактнішим, workbench — вищим.

## Перевірки

Останній цільовий regression run:

```
62 tests
OK
```

Набір:
- `tests/test_signals_web.py`
- `tests/test_signals_phase4.py`
- `tests/test_signals_postgres.py`

Також:
- `git diff --check` — clean перед commit;
- live chronology endpoint перевірено на реальному candidate: HTTP 200, pagination contract PASS;
- frontend отримує повні hourly aggregates;
- generic cross-space titles у live HTML: 0;
- remote `wip/signals-v1` після push вказував на `86a9341`.

## Відомі обмеження / технічний борг

1. Snapshot все ще близький до 8 MiB hard cap. Поточні ~6.91 MiB прийнятні для MVP, але запас не безмежний. Правильний майбутній напрямок — поділ summary/detail payload, а не механічне підняття cap.
2. ANN recall проти exact ground truth не калібрований; це окремий quality task, не runtime failure.
3. Реальні UX-дефекти ще можуть проявитися лише під час експлуатації з великими/нетиповими signal candidates.
4. Автоматичний reload раз на 10 хвилин — компроміс MVP. Кращий майбутній UX: показувати "Є нові дані · Оновити" без примусового reload.
5. Локальний незакомічений `web/templates/topics.html` не входить до цього Signals milestone і має розглядатися окремим slice.

## Не змінювати без нової причини

- Не повертати exact all-pairs для глобального Signals.
- Не будувати dot timeline з `chronology[:N]`.
- Не трактувати synthetic point position всередині hourly bucket як точний publication timestamp.
- Не змішувати `publication_spread` current-24h із повним candidate/48h support.
- Не роздувати основний snapshot повною chronology всіх 200 candidates.
- Не завантажувати повну chronology всіх сигналів наперед; lazy-load лише активного сигналу.
- Не робити volume єдиним пріоритетом ranking.

## Наступний крок

Перед подальшим розширенням Signals доцільний незалежний static engineering review поточного slice. Виправляти лише підтверджені findings; не відкривати новий refactor/architecture scope без конкретного дефекту або виміряного ризику.
