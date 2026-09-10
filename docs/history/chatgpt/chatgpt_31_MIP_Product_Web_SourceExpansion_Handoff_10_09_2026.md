# МІП — Product/Web/Source Expansion Handoff — checkpoint 10.09.2026

## Статус і призначення

Цей checkpoint фіксує стан МІП після робіт 05–10.09.2026 і призначений також як handoff для Claude/іншого інженерного агента, щоб не відновлювати контекст з нуля.

Він **не замінює** актуальний код, DDL, runtime або БД. Пріоритет при розбіжностях:

1. фактичний live DB/runtime і нові вимірювання;
2. поточний код/DDL/tests в активній гілці;
3. цей checkpoint та інші нові engineering artifacts;
4. старі historical notes і припущення.

Не відновлювати current state лише з цього документа. Перед практичною дією перевірити `git status`, поточний HEAD, live DB/runtime і конкретний scope користувача.

**Code baseline перед documentation-only checkpoint:** `2c28d9f1c7620b42aafde93a325e359ac4ca3d00` (`fix(web): add source links and order signal chronology`).

Активна робоча гілка на момент сесії: `wip/signals-v1`.

## 1. Головна зміна оптики продукту

06.09.2026 зафіксований аналітичний продуктовий контракт: МІП проєктується не від таблиць/claims/pipeline state, а від робочих питань аналітика ЗСУ в інформаційно-психологічному протиборстві.

Пріоритетні питання:

1. що нового з'явилося про наші об'єкти за 12/24 години;
2. чи є розголошення / OPSEC-ризик;
3. які ворожі наративи йдуть зараз і що нового вкинули;
4. чи є сплеск/аномалія навколо об'єкта/теми;
5. хто вкинув першим і як швидко підхопили;
6. тональність — лише якщо семантика показника визначена достатньо чітко;
7. що треба ескалувати і кому.

Кожен основний блок UI повинен дозволяти рішення/дію; system-health метрики не повинні маскуватися під аналітику інфопростору.

Практичне правило для дизайну:

`orient -> narrow -> inspect -> verify -> act`.

Першоджерело/evidence має бути доступне одним переходом; candidate/гіпотеза не повинна виглядати як підтверджений факт.

Дослідження конкурентних OSINT/social-listening/counter-disinfo інтерфейсів використовується лише як каталог патернів. Воно **не є roadmap** і не виправдовує Query Builder, command center, network graph, sentiment чи інший модуль без поточної потреби.

## 2. Репозиторій і runtime-контекст

Репозиторій: `alexklp/mip`.

Основні робочі каталоги на Linux host:

- `$HOME/mip` — основний repo/runtime;
- `$HOME/mip-signals-codex` — активний web/signals/topics worktree.

Web preview на момент останніх перевірок запускався з worktree через існуючий Python environment, localhost only, порт 8010. У команді uvicorn присутній `--log-level warning`.

Важливий runtime failure mode: надто строгий `pgrep`-pattern одного разу не побачив живий uvicorn через додатковий аргумент `--log-level warning`, після чого друга спроба старту отримала bind failure. Надалі process restart перевіряти по фактичній команді/порту, а не по крихкому exact-string match. Не запускати другий server process до точного підтвердження стану першого.

`web/static/app.css` використовує cache-busting через `static_version('app.css')` на основі mtime.

### Локальний незакомічений стан

Після `2c28d9f` у web worktree залишився локальний, візуально перевірений patch порівняння тем. Зафіксовані modified files:

- `web/static/app.css`;
- `web/templates/topics.html`;
- `web/topics.py`.

Ці зміни **не видно в GitHub code baseline `2c28d9f`**. Runtime check для них пройшов (`TOPICS_COMPARE_RUNTIME=PASS`), користувач візуально підтвердив, що результат виглядає добре. Повний фінальний regression + commit/push після цього patch ще не зафіксовані.

Перед будь-яким reset/checkout/rebase/pull/рефакторингом обов'язково спочатку перевірити локальний diff. Не втратити ці три файли.

## 3. Web MVP: еволюція 05–10.09

Ключові commits:

- `c6ff322` — `feat(web): add live materials MVP`;
- `8156bd7` — `feat(web): add live overview and visual foundation`;
- `9472c4e` — `feat(web): add sources and contour theses views`;
- `38a81e8` — `feat(web): add live signals and topics analyst views`;
- `b2bdf36` — `fix(web): refine sources view and primary navigation`;
- `2c28d9f` — `fix(web): add source links and order signal chronology`.

Початковий Web MVP був побудований як реальний FastAPI/Jinja2 read-oriented service над PostgreSQL. Після продуктового переосмислення primary navigation звужено.

### Поточна primary navigation

Рівно:

`Теми -> Сигнали -> Джерела`

`GET /` зараз веде на Topics.

`/overview`, `/materials`, `/theses` можуть залишатися в коді як допоміжні/історично реалізовані routes, але вони прибрані з primary navigation. Не повертати їх у sidebar без нової продуктової причини.

UI: українська людино-зрозуміла мова, мінімум внутрішньої pipeline-термінології, evidence/source поруч із висновком, без decorative dashboard overload.

На checkpoint `b2bdf36` було перевірено 89 tests, `/`, `/topics`, `/signals`, `/sources` — HTTP 200; `MIP_WEB_FINAL=PASS`. На `2c28d9f` signals suite 85 tests + topics 4 tests проходили перед commit.

## 4. Topics — поточний lexical analytical layer

Основні файли:

- `reporting/topics_snapshot.py`;
- `web/topics.py`;
- `web/templates/topics.html`;
- `ops/run_topics_snapshot_once.sh`;
- `tests/test_topics.py`.

Snapshot contract:

- schema: `topics/1`;
- algorithm: `topics-lexical-rolling/1`;
- source groups: `ru_space`, `ua_space`, view `all`;
- units: `phrases` (bigrams) і `words`;
- лише latest routing decision `analyze`;
- 48h source window, current/previous по 24h;
- `as_of` округляється до початку години;
- для Topics `observed_at = COALESCE(published_at, collected_at)`;
- NLP працює по повному `content_items.text_content`; `PREVIEW_CHARS=700` стосується evidence display, а не NLP truncation;
- marker content membership унікалізується на рівні content;
- publication counts — occurrence-level;
- source counts — distinct source IDs;
- `THEME_MIN_PUBLICATIONS=2`, `THEME_LIMIT=120`;
- `CHANGE_MIN_PUBLICATIONS=3`, `CHANGE_LIMIT=40`;
- cloud limits: 52 phrases / 64 words / 42 changes;
- 24 hourly bins;
- source ranking і evidence drilldown зберігаються.

`all` означає об'єднання RU+UA source groups, але **не semantic/cross-language canonicalization**. Маркери лишаються лексичними.

Важливий перевірений приклад: `путин` і `путін`, так само `зеленский` і `зеленський`, можуть бути різними markers. Це не bug Topics. Це межа lexical-topic layer. Не «виправляти» її прихованою canonicalization всередині Topics; для цього потрібен окремий monitored-object/entity layer.

Marker ID утворюється детерміновано із `unit + normalized label key`.

Browser отримує bounded overview structures; heavy marker evidence лишається server-side і вантажиться через `/topics/marker/{marker_id}?space=...`.

Commit `2c28d9f` додав явний зовнішній source link у `Матеріали за маркером` через `external_ref`.

### Локальний Topics comparison patch — ще не в GitHub baseline

Продуктовий контракт локального patch:

- окремий блок `Порівняння динаміки тем`;
- максимум 5 одночасно вибраних markers;
- права панель `Теми для порівняння`, Top 50, scroll, checkboxes;
- default top-5 для поточного `(space, unit)`;
- один common Y-scale, 24 hourly points, Kyiv labels;
- current selected-marker detail, cloud, KPI, sources/evidence не змінюються;
- comparison state незалежний від ordinary marker selection/search;
- checkbox впливає лише на comparison chart;
- `Скинути` скидає лише comparison selection;
- Changes mode приховує comparison;
- phrase/word units не змішуються;
- source-space filter застосовується до chart;
- restrained 5-series palette, без donut/pie.

Цей patch був runtime-перевірений після коректного restart, але до наступної роботи потрібен повний regression і лише потім окреме рішення про commit/push.

## 5. Signals — готовий candidate spread/similarity vertical slice

Основні файли:

- `reporting/signals_data.py`;
- `reporting/signals_postgres.py`;
- `reporting/signals_detector.py`;
- `reporting/signals_snapshot.py`;
- `web/signals.py`;
- `web/templates/signals.html`;
- `ops/run_signals_snapshot_once.sh`;
- `docs/Signals_v1.md`;
- signals tests.

Архітектура Phase 4:

`SELECT -> observed/routing coverage -> analyze anchors round-robin -> bounded HNSW -> complete-link core -> suppression/rank/cap + separate review -> signals/4 -> GET /signals`

Критичні семантики:

- лише latest routing `decision='analyze'` входить у candidate detection;
- `maybe`, `skip`, missing — виключаються fail-closed;
- contours не є hard gate;
- previous/current для Signals: `[as_of-48h, as_of-24h)` і `[as_of-24h, as_of)` **за `collected_at`**;
- `published_at` використовується для presentation chronology, але **не замінює collected_at у detection/window membership/freshness contract**;
- common watermark — мінімум max(collected_at) вибраних source groups;
- core threshold 0.18;
- related/review threshold 0.36;
- core clustering — complete-link, без transitive A-B-C chaining через невідому/далеку пару;
- core candidate має мінімум 2 distinct `source_id`;
- same-space multi-source дозволений;
- exact republication — один `content_id` у >=2 distinct sources;
- related links не змінюють core counts/dynamics;
- усе в Signals — **candidate/unverified**, не підтверджена подія/кампанія/координація.

Scheduled wrapper на момент checkpoint використовує:

- model ID 1, BGE-M3, dim 1024;
- source groups RU+UA;
- strategy `exact_current_24h`;
- anchors 40;
- neighbours 10;
- max pairs 10000;
- rows 5000;
- core 0.18;
- related 0.36;
- ANN probe 100;
- display limit 100;
- max related 100;
- evidence 600 chars, max evidence 12;
- statement timeout 15s;
- output `reporting/signals.latest.json`.

ANN recall лишається `UNVERIFIED`; bounded HNSW не можна називати global exact recall.

Commit `2c28d9f` змінив тільки presentation chronology: evidence rows сортуються за `(published_at or collected_at, collected_at, occurrence_id)`. Detection semantics не змінювалися.

## 6. Sources UI і source identity

`/sources` показує реальне coverage джерел, що мають occurrences у вибраному періоді. Підтримані фільтри:

- `Період`;
- `Інформаційний простір`;
- `Тип джерела`;
- `Джерело`.

Options data-derived; source-name options звужуються після type/group. Таблиця має frontend sorting.

`source_group` — manual registry-level provenance інформаційного простору (`ua_space`, `ru_space`, ...). Це **не мова**, не країна, не content contour і не оцінка достовірності.

Не виводити source_group автоматично з TLD або мови тексту.

Окремий відомий modeling caveat: один видавець може мати окремий Telegram endpoint і RSS endpoint, отже зараз це різні `source_id`. Для spread/independence analytics `distinct source_id` поки означає distinct acquisition endpoints, а не гарантовано незалежних видавців/акторів. Якщо independence стане критичною метрикою, потрібен окремий `publisher/source_family` identity layer, а не прихована евристика в UI.

## 7. Collectors: фактичний контракт

### Telegram web-preview

`collectors/tg_web_worker.py`:

- `source_type='telegram'`;
- активні URL тільки `https://t.me/s/<channel>`;
- public web-preview, без login/MTProto/comments;
- сторінка зазвичай дає приблизно останні ~20 постів;
- `.tgme_widget_message_text` -> normalized full visible post text;
- `MIN_TEXT_LEN=30`;
- `external_ref` — exact post URL;
- pipeline: `raw_items -> content_items -> item_occurrences`;
- same external_ref + changed hash -> `changed_unprocessed`;
- optional `MIP_TG_PROXY_URL`;
- retries 3;
- source delay 0.7s.

Не заявляти 100% Telegram coverage: швидкі канали можуть мати більше нових постів між polling cycles, ніж web-preview window. Немає private content, comments, media OCR/transcripts.

### RSS

`collectors/rss_worker.py`:

- `source_type='rss'`;
- з feed entry береться `title + summary/description`;
- linked article body **не завантажується**;
- `MIN_TEXT_LEN=30`;
- hard content dedup + occurrence idempotency;
- same source/external_ref зі зміненим text -> `changed_unprocessed`.

Це важливий semantic-quality caveat: для Topics/OPSEC/Narratives RSS може не містити факт, якщо він є лише в body статті, але відсутній у feed summary.

## 8. Source registry expansion 10.09.2026

Проведено live expansion source registry з великого нового списку Telegram+RSS.

Input після очищення:

- 221 rows у вихідному списку;
- 220 unique acquisition resources після internal exact-resource dedup;
- breakdown:
  - RU Telegram 55;
  - UA Telegram 56;
  - RU RSS 49;
  - UA RSS 60.

Один внутрішній duplicate був очевидним: різні назви використовували один і той самий RSS URL Російської газети; другий рядок не додавався.

Перед import у live DB було 147 source rows.

Live dedup/probe:

- 96 candidates already matched DB;
- 124 були новими і пройшли network preflight;
- 58 preflight OK і вставлені;
- 66 preflight failed і **не вставлені**;
- race/conflict 0;
- registry after import: 205.

Inserted breakdown:

- RU Telegram: 5;
- UA Telegram: 10;
- RU RSS: 17;
- UA RSS: 26.

Telegram URL були canonicalized у `https://t.me/s/<channel>` відповідно до реального collector contract.

UA candidates з вихідного файлу мали legacy `contour_id=1`, але це не переносилося: `sources.contour_id` є legacy source-level provenance, тоді як strategic monitoring contours — content-level model. Новим UA rows було задано legacy contour 3, RU — 4; existing rows не переписувалися.

### Перший повний ingestion після expansion

Перший cycle, що побачив новий registry:

- active RSS: 57;
- active Telegram web: 148;
- total registry: 205;
- start 15:30:02 UTC;
- end 15:42:56 UTC;
- wall time: 12m54s.

За timestamps:

- RSS stage приблизно 1m40s;
- Telegram stage приблизно 11m14s.

Отже поточний ingest bottleneck — Telegram sequential web-preview traversal, не RSS.

Із 58 нових sources після повного cycle:

- 50/58 вже дали occurrences (86.2%);
- усі 15/15 нових Telegram дали data;
- 35/43 нових RSS дали data.

Нові occurrences у першому проході:

- RU RSS: 1939;
- RU Telegram: 84;
- UA RSS: 1571;
- UA Telegram: 158;
- total: 3752.

8 preflight-success RSS залишилися без occurrences після першого повного cycle:

- RU: `ANNA News`, `Regnum`;
- UA: `Obozrevatel`, `Гордон`, `Думська`, `Економічна правда`, `Коментарі`, `Мілітарний`.

Не видаляти їх автоматично: endpoint preflight проходив. Спочатку дивитися exact collector log: parse/reject/redirect/content issue.

One-off import reports збережені поза repo:

- `$HOME/.local/state/mip/source_import_20260910/inserted.tsv`;
- `$HOME/.local/state/mip/source_import_20260910/failed.tsv`.

Сам registry expansion **ще не оформлений як reproducible SQL seed у repo**. Якщо потрібно довготривало зафіксувати source registry, не реконструювати його вручну з цього note: спочатку read-only dump фактичної `sources` table, потім створити idempotent seed за current DB і окремо перевірити diff.

## 9. Ingestion scheduling/capacity після expansion

Поточний ingest wrapper виконує RSS, потім Telegram послідовно і захищений `flock`.

Cron ingestion: кожні 15 хвилин.

До expansion старі cycles з 147 sources зазвичай займали близько 9–12 хв, інколи довше. Перший повний cycle з 205 sources зайняв 12m54s — залишився малий запас до 15-хвилинного cadence.

Якщо попередній cycle ще працює, наступний cron правильно робить `SKIP: previous ingestion cycle is still running`. Це очікуваний safety behavior, не failure.

Потрібно виміряти нічну серію cycles до зміни cadence. Не оптимізувати cron лише за одним виміром.

Окремий capacity caveat: RSS worker пише `raw_item` на кожен отриманий feed entry навіть якщо occurrence вже існує. Після збільшення кількості feeds, деякі з яких повертають сотні entries, `raw_items` може рости значно швидше. Виміряти приріст перед retention/optimization.

### RU RSS proxy routing — open issue

Поточний `rss_worker.needs_proxy(url)` використовує TLD-евристику:

`hostname.endswith('.ru')`

Тобто `ru_space` sources на `.com`, `.tv`, `.su`, `.news`, `.info` можуть іти direct. Під час preflight частина таких RU sources дала connect timeout direct, тоді як `.ru` errors явно проходили через SOCKS stack.

Це не виправлено. Логічний candidate change: proxy routing для RSS за registry provenance (`source_group == 'ru_space'`) замість TLD. Але перед зміною перевірити фактичний runtime/config і targeted endpoints; не вносити масовий rewrite.

## 10. Поточний cron landscape

На момент checkpoint підтверджено розклад:

- ingestion: `*/15 * * * *`;
- basic processing: `7,37 * * * *`;
- claims live: `12,27,42,57 * * * *`;
- contours live: `4,34 * * * *`;
- segment pipeline: `17 * * * *`;
- signals snapshot: `10,40 * * * *`;
- topics snapshot: `22 * * * *`.

Mamay workloads залишаються serialized через lock; не запускати паралельні LLM workloads без нового обґрунтування.

## 11. Segment/claim path — важливий inherited baseline

ChatGPT notes `27–30` від 04.09 фіксують:

- occurrence full-text persistence;
- content segmentation v1;
- segment embeddings;
- segment routing;
- segment-aware claim extraction;
- bounded live segment orchestrator;
- strict evidence grounding;
- `max_claim_chars=4000` як reversible operational safety guard;
- segment pipeline cron раз на годину.

Legacy whole-content claims і segment-level claims співіснують. Не будувати UI/data logic, яка припускає, що `segment_id` обов'язковий для кожного claim.

Відомий negative result: fuzzy evidence grounding не вводити лише для «порятунку» майже-дослівних span. Exact evidence provenance залишається інваріантом strict validator.

## 12. Sentiment / «тональність» — PAUSED negative result

Було виконано невеликий bakeoff generic sentiment models на реальному RU/UA corpus. Ніякої DB/schema/UI integration не було; outputs лишилися локальними.

Eval set:

- 98 rows total;
- RU 48;
- UA 50;
- майже всі source/name pairs різні;
- 98 unique contents.

Моделі:

1. `cardiffnlp/twitter-xlm-roberta-base-sentiment` — CPU після GPU OOM через зайняту VRAM; 98 rows приблизно за 31.2s; labels: 46 negative / 51 neutral / 1 positive.
2. `cointegrated/rubert-tiny-sentiment-balanced` — RU 48 only; приблизно 0.72s; 16 negative / 31 neutral / 1 positive.

Cardiff confidence <0.60:

- UA: 21/50;
- RU: 27/48.

RU Cardiff/RuBERT agreement: 30/48 = 62.5%.

Ключовий результат не в accuracy, а в **невірно сформульованій ontology** для поточного корпусу. Для військових/новинних текстів треба розділяти:

1. expressed emotional/evaluative tone тексту;
2. stance/attitude toward an explicit target;
3. narrative/frame positioning.

Наприклад фактичний опис ракетного удару може бути stylistically neutral, хоча event valence/stance для конкретної сторони очевидно інший. Generic sentiment classifier легко змішує ці рівні.

Спроба human-gold на hard cases була зупинена, бо користувач не міг послідовно розмітити `negative/neutral/positive` без додаткового target/context contract. Це трактовано як product-semantic negative result, не як failure annotator/model pipeline.

**Рішення користувача 10.09:** тональність поставити на паузу. Якщо stakeholder сильно хоче цей показник — спочатку він має пояснити, що саме під ним розуміє.

Не продовжувати model-zoo, annotation або UI інтеграцію sentiment без нового explicit semantic contract.

Local experiment outputs:

- `$HOME/.local/state/mip/sentiment/eval_v1.csv`;
- `$HOME/.local/state/mip/sentiment/bakeoff_v1.csv`.

## 13. Новий продуктовий insight: lexical topics != monitored objects

Перевірка Putin/Zelensky показала правильну, але важливу межу Topics:

- lexical markers можуть дробити одну реальну сутність за мовою/формою;
- top-N може приховати низькочастотний, але аналітично важливий monitored object;
- search по visible/top rows не дорівнює entity lookup по всьому corpus.

Звідси рекомендований, але **ще не затверджений користувачем як наступний task**, напрям:

1. спочатку завершити/зафіксувати локальний Topics comparison milestone;
2. read-only feasibility audit canonical monitored objects / C1 aliases на live DB;
3. мінімальний `Наші об'єкти` v1 з canonical object + aliases + current-vs-previous dynamics + sources + evidence;
4. на цій основі OPSEC candidate detector;
5. OPSEC candidates інтегрувати у вже існуючий Signals analyst workflow замість нового окремого «монстра»;
6. semantic narratives/LLM layer — після object/OPSEC foundation.

Причина: monitored-object identity одночасно допомагає питанням «що нового про наші об'єкти», spikes, OPSEC і майбутнім narrative relations.

Не будувати universal NER/knowledge graph наперед. Почати з approved monitoring dictionary/aliases і фактичної користі.

## 14. OPSEC — поки product target, не готова feature

OPSEC є одним із найвищих пріоритетів продуктового контракту, але production detector/UI ще не реалізовано.

Мінімальна задумана семантика candidate:

`monitored object/unit + location + temporal binding + sensitive context (наприклад наслідки удару / переміщення / персонал)`

Особлива увага до UA-space sources.

Не називати machine candidate «витоком» без human verification. Формулювання повинно бути на рівні `можлива ознака розголошення` / `потребує перевірки`.

Перед реалізацією перевірити coverage actual C1 objects/aliases і фактичні candidate volumes. RSS summary-only limitation може суттєво впливати на recall.

## 15. Що зараз вважається завершеним/замороженим без нового симптома

- primary navigation `Теми -> Сигнали -> Джерела`;
- `/sources` filters + sorting;
- Signals Phase 4 current candidate workflow;
- source links у Topics detail;
- Signals chronology presentation fix;
- базова Topics lexical semantics;
- sentiment experiment як negative result — не продовжувати без нового requirement.

Не рефакторити ці області «заодно».

## 16. Відомі пастки — не повторювати

1. **Не змішувати timestamps.** Topics використовує `COALESCE(published_at,collected_at)` для observed window; Signals detection — `collected_at`, а chronology може використовувати publication time.
2. **Не робити lexical entity canonicalization у Topics.** Окремий object/entity layer.
3. **Не називати HNSW bounded recall exact/global.** ANN recall у Signals не доведений.
4. **Не трактувати distinct source_id як гарантовано незалежного publisher.** TG/RSS одного outlet можуть бути різними source IDs.
5. **Не називати RSS text повним текстом статті.** Зараз це title + summary/description.
6. **Не називати Telegram coverage повним.** Web-preview bounded, ~20 постів.
7. **Не вважати cron SKIP помилкою, якщо lock зайнятий.** Це очікувана overlap protection.
8. **Не міняти cadence/parallelism без вимірювань.** Після source expansion перший full ingest 12m54s; потрібна серія нічних measurements.
9. **Не чіпати local Topics compare diff до `git status`/diff review.** Він не закомічений.
10. **Не відновлювати sentiment labeling/model search без semantic contract.**
11. **Не повертати pipeline KPIs у primary analyst navigation лише тому, що routes існують.**
12. **Не запускати другий uvicorn по крихкому `pgrep` match.** Спочатку підтвердити процес/порт.
13. **Не додавати credentials/network identifiers у repo/history.**

## 17. Мінімальний bootstrap для Claude перед новою роботою

Якщо Claude підключається завтра/пізніше, порядок такий:

1. прочитати `AGENTS.md`;
2. прочитати `docs/Web_MVP_v1.md` і `docs/Signals_v1.md`;
3. прочитати `docs/history/chatgpt/README.md`, notes `27–30` і цей `31`;
4. за потреби — Claude archive `24–29`, особливо segmentation/occurrence full-text/Web MVP review;
5. перевірити current `git status --short` і HEAD;
6. не втратити uncommitted Topics comparison files;
7. перевірити live DB/runtime лише в обсязі конкретного task;
8. не розширювати scope, не робити сусідній refactor;
9. після зміни — targeted tests + smoke, а не тестування заради кількості;
10. commit/push/merge тільки після окремого явного дозволу користувача.

Для конкретного task користувач очікує: ціль -> критерій готовності -> мінімальна перевірка -> реалізація -> diff/test result -> наступний крок.

## 18. Що перевірити на старті 11.09, якщо користувач ще не обрав feature

Найкорисніший read-only operational morning check після source expansion:

- нічні ingestion cycle durations;
- кількість `SKIP` через overlap;
- приріст raw/content/occurrence за ніч;
- чи стабільно збираються 50/58 нових sources;
- точна причина 8 zero-occurrence RSS;
- чи підтверджується Telegram як основний ingest bottleneck;
- чи треба змінювати RU RSS proxy routing;
- чи зростання `raw_items` стало значущим.

Після цього користувач вирішує, який продуктовий vertical slice почати. На момент завершення 10.09 остаточний вибір між monitored objects/OPSEC/іншим наступним блоком **ще не зроблено**.

## 19. Підсумковий стан

За 05–10.09 МІП фактично перейшов від «показати, що pipeline щось робить» до першого analyst-oriented Web MVP:

- Topics дає lexical картину тем і їх динаміки;
- Signals дає candidate spread/similarity workflow з evidence;
- Sources дає coverage джерел;
- primary navigation очищено від system-centric екранів;
- source registry розширено з 147 до 205 endpoints;
- перший новий ingest дав 3752 occurrences лише з доданих sources;
- sentiment свідомо зупинено як semantic/product mismatch;
- наступний сильний напрям — canonical monitored objects -> OPSEC, але користувач прийме рішення після ранкового operational check.

Це current handoff на кінець робочого дня 10.09.2026.
