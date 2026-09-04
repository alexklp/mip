# МІП — `occurrence_content` v1: LLD (схема + worker contract + resolver)

**Дата:** 03.09.2026
**Статус:** LLD, ревізія 2 після предметного review (6 зауважень, усі прийняті
й внесені нижче). Жодного DDL-файлу (`036_...sql`) і жодного worker-коду
свідомо не написано — за прямою інструкцією, стоп на review після цього
документа знову.
**Ревізія 2 — що змінено проти першої версії (з обґрунтуванням, не мовчки):**

1. Worker contract: одна extraction-спроба, не два незалежні виклики
   `extract()`. `text_content` і `structured_content` тепер похідні з
   ОДНОГО extracted document — інваріант, не збіг.
2. Resolver: вибір за явною extraction identity, яку задає споживач
   (сегментатор), не за `created_at DESC` — старий варіант ламався на
   ре-запуску старішого профілю для діагностики.
3. Знято протиріччя "append-only historia" vs "скіп INSERT при збігу
   `text_hash`". Attempt завжди пишеться; `text_hash` тепер винятково
   downstream-сигнал "чи змінився текст", не умова запису рядка.
4. `CHECK` на `success` розширено до повного успішного результату
   (`text_content`+`text_hash`+`structured_content`+`structured_content_format`+`final_url`).
   Додано `final_url` — фактичний response URL після редіректів.
5. Явний v1-контракт retry eligibility: помилки retryable до
   `max_attempts` (worker config, не DDL); успіх на поточній identity —
   термінальний.
6. Розділ 5 (templated-alert) переформульовано: головний доведений кейс —
   протилежний напрямок (однаковий canonical text у різних реальних
   інцидентах схлопується в один `content_id`), не тільки near-identical→
   різні хеші.

**Контекст:** продовження розбору ГПТ по full-text retrieval (RSS) +
occurrence-level інтеграції, звірене проти реальної DDL/коду проєкту через
RAG (`sql/001`, `sql/007`, `sql/011`, `collectors/rss_worker.py`,
`collectors/tg_web_worker.py`, `experiments/claim_extraction/*`) і проти
`claude/25_MIP_ContentSegmentation_v1_ArchitectureAnalysis`.

---

## 0. Що зафіксовано раніше і не переглядається тут

- Occurrence-level інтеграція: `occurrence_content` висить на `item_occurrences`,
  не на `content_items`.
- `attempt_no` — беремо, ретраи не перезаписують попередню спробу. Прецедент
  підтверджений живим кодом: `claim_extract_worker.py`,
  `next_attempt_no = max(attempt_no) + 1`, мотивація — саме retry після
  `transport_error` (обрив з'єднання посеред батчу, задокументовано в
  `docs/history/chatgpt/chatgpt_20_MIP_LivePipeline_Claims_Hardening_02_09_2026.md`).
  Той самий сценарій — "ретраим впалий fetch" — тут прямий, не аналогія.
- `text_hash` (не `content_hash` — назва свідомо інша, `content_hash` вже
  зайнято на `content_items` з іншою семантикою, дублювати ім'я з різним
  сенсом — граблі для майбутнього грепу). **Виправлено у ревізії 2:**
  призначення — виключно downstream-сигнал (сегментація/споживач порівнює
  `text_hash` нового successful attempt'а з уже обробленим і вирішує, чи є
  сенс переробляти), а НЕ ідемпотентність воркера. Кожен реально виконаний
  fetch+extraction завжди пише рядок, success чи error, незалежно від того,
  чи збігся `text_hash` з попереднім — append-only без винятків (розділ
  6.1). Cross-occurrence dedup full-тексту НЕ робимо — це задача
  near-dup/event-рівня (embeddings), не цієї таблиці, і сирий sha256 все одно
  не зловить templated-alert кейс (текст near-identical, не byte-identical).
- `extractor`/`extractor_version` — plain-колонки, не registry-таблиця.
  Registry-паттерн проєкту (`llm_models`, `claim_extraction_prompts`) існує
  там, де байтовий дрейф тексту — ризик коректності (промпт). У trafilatura
  немає "тексту промпту", є встановлена версія пакету — plain text полю
  достатньо.

---

## 1. Extraction identity: package version ≠ profile version

Одна лише `extractor_version` (версія пакету trafilatura) не описує identity
результату повністю. Ми викликаємо `extract()`/`bare_extraction()` з власним
набором kwargs (`favor_precision`, `include_comments`, `include_tables`,
`include_formatting`, `target_language`, ...) — це наш "профіль" екстракції,
і зміна профілю змінює результат так само, як зміна версії пакету, але
незалежно від неї. Змішувати їх в одне поле — тихо втратити інформацію: "чому
текст став іншим — оновили пакет чи змінили профіль" стане непитанням, яке
неможливо відповісти пост-фактум.

**Рішення: дві окремі plain-колонки, обидві NOT NULL, обидві частина identity
attempt'а:**

- `extractor_version` — версія встановленого пакету (`importlib.metadata.version("trafilatura")`,
  наприклад `"2.2.0"`).
- `extraction_profile_version` — наша власна версія набору kwargs, звичайний
  bump-по-потребі текстовий тег (`"v1"`, `"v2"`), не registry-таблиця — та
  сама логіка, що вже застосована до `extractor_version` вище: значення
  профілю фіксоване в коді, не потребує байтової звірки з окремим артефактом
  на диску.

`UNIQUE`-ключ і `next_attempt_no` рахуються по повному тюплу
`(occurrence_id, extractor, extractor_version, extraction_profile_version)` —
апгрейд пакету або зміна профілю природно починає свій незалежний
attempt-рахунок з 1, стара версія лишається окремим рядком, не
перезаписаною.

---

## 2. `structured_content`: реальні можливості Trafilatura, не вигадка

Перевірено проти офіційної документації ([Python usage](https://trafilatura.readthedocs.io/en/latest/usage-python.html),
[Quickstart](https://trafilatura.readthedocs.io/en/latest/quickstart.html)):

| Формат Trafilatura | Що зберігає |
|---|---|
| `txt` (default) | плоский текст, без структури, без metadata |
| `markdown` | плоский текст + структурні елементи (заголовки, списки, форматування) |
| `xml` | структура (`<p>`, заголовки, списки, за бажанням таблиці/лінки/картинки) у явному дереві |
| `xmltei` | те саме, TEI-стандарт, важче за потреби |
| `json` | **важливо:** прапорці форматування "не мають ефекту на JSON-вивід" — це bundle тексту+метадані, НЕ структурний формат |

Отже `jsonb` як тип колонки з мого попереднього чернетки — помилка, засновувана
на назві "structured_content", а не на реальній поведінці бібліотеки: формат
`json` у Trafilatura якраз НЕ несе структуру, яка потрібна сегментації
(межі абзаців/списків — та сама проблема, яку `claude/25` п.2.3 діагностував
на рівні collector'а: `strip_html` схлопує всі whitespace, знищуючи межі
абзаців ще до будь-якого аналізу; ціль `structured_content` — не повторити
цю помилку на рівні екстрактора).

**Рішення:** `structured_content` — тип `text`, вміст — вивід Trafilatura у
форматі `xml` як є (reuse, не custom-парсинг). Окрема колонка
`structured_content_format text CHECK (... IN ('trafilatura_xml'))` —
self-describing на майбутнє, якщо колись переключимось на `markdown` (напр.
якщо сегментація виявиться LLM-based і markdown зручніший як вхід моделі —
`claude/25` п.3.4 явно лишає це відкритим питанням, не вирішує тут).
`text_content` лишається плоским `txt`-виводом — той самий контракт, що вже
є в `content_items.text_content` (character offsets, evidence-span logic не
винаходяться заново).

**Важливий інваріант, доданий у ревізії 2:** `text_content` і
`structured_content` МУСЯТЬ бути похідними з одного й того самого extracted
document, не з двох незалежних викликів `extract()`. Перевірено — офіційний
API це дозволяє напряму: `bare_extraction()` повертає `Document` з тілом як
LXML-деревом (`.body`), а `trafilatura.xml.xmltotxt(xmloutput, include_formatting)`
конвертує це саме дерево в plain text
([Core functions](https://trafilatura.readthedocs.io/en/latest/corefunctions.html)).
Деталі — розділ 6.1. Це не питання швидкості: якщо `text_content` і
`structured_content` походять із двох окремих extraction-проходів,
майбутні segment boundaries (структурні, з `structured_content`) і
character offsets claims/evidence (плоскі, з `text_content`) можуть
розійтися між собою для того самого attempt'а — а інваріант "один attempt
= один документ" саме це мав гарантувати.

Metadata, яку сама Trafilatura вміє витягувати (title/author/date/hostname
через `with_metadata=True`) — **свідомо поза scope цього LLD.** Це окрема
можливість, не запитана, додавання зараз — scope creep. Якщо знадобиться,
окремі колонки пізніше, не зараз.

---

## 3. Attempt status: один термінальний статус, не pending/success/failed×2

Порівняв з precedent: `claim_extraction_runs.status IN ('valid', 'invalid',
'transport_error')`, `relation_judgments.status` — той самий паттерн. В
обох випадках **pending не зберігається як рядок узагалі** — worker-selection
запити (`NOT EXISTS ... WHERE r.content_id = ... AND r.attempt_no = ...`)
працюють на "рядка нема = ще не робили", не на "рядок є зі статусом
pending". Рядок з'являється лише коли attempt завершився.

**Рішення:** `status text NOT NULL CHECK (status IN ('success', 'fetch_error',
'extraction_error'))`. Один рядок = одна повна спроба пайплайну "fetch HTML →
extract text" для одного occurrence; статус каже, на якому кроці воно
зупинилось, без окремих pending-полів і без recovery-стану, який довелось би
десь тримати. `fetch_error` — не отримали HTML взагалі (extraction не
запускався). `extraction_error` — HTML отримали, trafilatura впала/повернула
порожнечу.

**Виправлено у ревізії 2 — CHECK на one полі був заслабким.**
`CHECK ((status = 'success') = (text_content IS NOT NULL))` пропускав
неможливий стан "success із текстом, але без XML" — сегментація за задумом
спирається саме на `structured_content`, тож success мусить гарантувати
ПОВНИЙ успішний бандл разом, не лише один стовпець:

```sql
CHECK (
    (status = 'success')
    = (
        text_content IS NOT NULL
        AND text_hash IS NOT NULL
        AND structured_content IS NOT NULL
        AND structured_content_format IS NOT NULL
        AND final_url IS NOT NULL
    )
)
```

Той самий all-or-nothing принцип, що вже є в `relation_judgments`
(`CHECK ((status = 'valid') = (relation_label IS NOT NULL))`), лише
поширений на весь бандл полів, а не на одне. Деталі й повний DDL — розділ 7.

---

## 4. Що НЕ змінюється (явно, за прямою вимогою)

- `content_items`, `content_hash`, поточний `rss_worker.py`/`tg_web_worker.py`
  — без змін. Дедуплікація на вході лишається такою, як є.
  `occurrence_content` — новий, повністю адитивний шар, що висить на
  `item_occurrences`, нічого не переозначає під ним.
- Старі `embeddings` (FK на `content_id`), `claim_extraction_runs`
  (`UNIQUE(content_id, ...)`), `claims.evidence_start/end` — без змін, без
  міграції, без перепрогону. Вони й далі оперують плоским
  `content_items.text_content`, як сьогодні. `claude/25` п.3.2 залишав
  відкритим питання "мігрувати FK embeddings/claims на segment-рівень чи ні"
  — це питання ЦЬОГО LLD не торкається взагалі; `occurrence_content` не є
  тим "segment"-шаром, лише передумовою для нього (повний текст, з якого
  колись сегментація нарізатиме фрагменти).
- Ніщо в existing pipeline не читає `occurrence_content` за замовчуванням —
  споживання виключно через явний resolver (розділ 6), опційно, коли
  з'явиться перший споживач (сегментація).

---

## 5. `content_id` — lexical dedup identity, НЕ publication/event identity

**Переформульовано у ревізії 2** — перша версія цього розділу вела з менш
небезпечного напрямку. `content_hash` (і, відповідно, `content_id`)
обчислюється як `sha256(canonical_text)` у collector'і
(`rss_worker.py`/`tg_web_worker.py`, підтверджено кодом) — це чистий
байтовий/лексичний ключ дедупу на вході, нічого більше. Є ДВА окремі
напрямки, де ця межа ламається, і головний — не той, що я описав першим:

**Головний, доведений кейс: однаковий canonical text у РІЗНИХ реальних
інцидентах схлопується в ОДИН `content_id`.** Шаблонні алерти
(землетрус/повітряна тривога/обмеження в аеропорту) генеруються джерелом за
фіксованим текстовим шаблоном; якщо конкретний інцидент не змінює
canonical-текст (та сама структура фрази, змінна частина — за межами того,
що потрапляє в хеш, або відсутня), `sha256(canonical_text)` дає той самий
хеш для різних дат/URL — і, відповідно, для різних реальних подій.
Наслідок: `item_occurrences` з різними `published_at`/`external_ref`, що
описують РІЗНІ інциденти, опиняються прив'язаними до ОДНОГО `content_id`.
Це і є найсильніший аргумент, чому `content_id` не можна трактувати як
publication/event identity — не гіпотетичний edge case, а підтверджений
напрямок помилки.

**Другий, окремий кейс (те, що я описав першим): near-identical текст
(дата/число відрізняється) дає РІЗНИЙ `content_id`**, хоча на
аналітичному рівні це може бути той самий клас матеріалу. Протилежний
напрямок тієї самої проблеми — хеш надто грубий в обидва боки одразу:
байтова різниця в одному символі розділяє те, що аналітично одне й те
саме; байтовий збіг шаблону об'єднує те, що аналітично різне.

`occurrence_content`, прив'язаний до `occurrence_id`, успадковує цю межу
свідомо і не намагається її вирішити в жоден бік. Він не робить
template-collision dedup, не розділяє схлопнуті інциденти і не встановлює
event identity. Це питання майбутнього segment/event-шару (`claude/25`, ще
не спроєктований), не цієї таблиці. Якщо колись знадобиться розрізняти
"той самий шаблон, різні інциденти" чи об'єднувати "різні хеші, той самий
клас матеріалу" — це near-dup/event-рівень на embeddings або окрема
евристика, не `content_hash`/`text_hash`.

---

## 6. Worker contract і resolver

### 6.1. Запис attempt'а (worker-side, псевдокод контракту, не реалізація)

**Виправлено у ревізії 2 — один extraction pass, не два.** Попередня версія
двічі викликала `extract()` (`output_format='txt'` і окремо `'xml'`) — це
два незалежні проходи парсера, які теоретично можуть дати документи, що
розійшлись (навіть якщо на практиці малоймовірно). API дозволяє коректний
варіант напряму: `bare_extraction()` один раз повертає `Document` з тілом
як LXML-деревом, `xmltotxt()` перетворює те саме дерево в plain text —
`text_content` і `structured_content` гарантовано походять з одного
документа.

```
identity = (extractor='trafilatura', extractor_version, extraction_profile_version)

eligibility (worker query, не DDL):
    occurrence_id обирається, якщо
      НЕМАЄ рядка status='success' для (occurrence_id, identity)
      AND кількість рядків status IN ('fetch_error','extraction_error')
          для (occurrence_id, identity) < max_attempts  -- worker config, напр. 3

next_attempt_no = (
    SELECT COALESCE(MAX(attempt_no), 0) + 1
    FROM occurrence_content
    WHERE occurrence_id = %s AND (extractor, extractor_version, extraction_profile_version) = identity
)

response = fetch(item_occurrences.external_ref)   -- слідує редіректам
  -> fetch failed:   INSERT status='fetch_error', final_url=NULL, text_content=NULL, ...
  -> fetch success:
       final_url = response.url                    -- фактичний URL після редіректів
       raw_html  = response.data

       document = bare_extraction(raw_html, url=final_url, ...profile kwargs...)
       -- ОДИН extraction pass: document.body — LXML-дерево

       -> document / document.body порожні: INSERT status='extraction_error', raw_html, final_url збережені
       -> успіх:
            structured_content = serialize(document.body)              -- xml, те саме дерево
            text_content       = xmltotxt(document.body, include_formatting=False)  -- те саме дерево
            text_hash          = sha256(text_content)
            INSERT status='success', final_url, raw_html, text_content, text_hash,
                   structured_content, structured_content_format='trafilatura_xml', ...
```

**Знято протиріччя append-only vs skip-on-match (виправлено у ревізії 2).**
Перша версія одночасно стверджувала "attempt-історія ніколи не
перезаписується" і "воркер може скіпнути INSERT, якщо `text_hash`
збігається" — це суперечність: якщо fetch+extraction реально відбулись,
attempt відбувся і мусить лишитись у історії, збігся текст чи ні. Рішення:

- **Кожен реально виконаний fetch+extraction завжди пише рядок**, success
  чи error. Без винятків, без "тихого" skip.
- `text_hash` НЕ впливає на те, чи писати рядок. Його роль — виключно
  downstream: сегментація/споживач порівнює `text_hash` нового successful
  attempt'а з тим, що вже обробив, і вирішує, чи є сенс перезапускати
  сегментацію (текст не змінився → нічого робити не треба), не воркер
  екстракції.
- **Звичайний (scheduled) worker взагалі не переробляє success на тій
  самій identity** — eligibility-запит вище явно виключає occurrence з
  уже існуючим `status='success'` для поточної identity. Повторний
  successful прогін тієї самої identity відбувається лише за явним
  запитом (force/re-extraction для дебагу — операторська дія, не
  routine-поведінка) або природно — коли `extractor_version`/
  `extraction_profile_version` реально змінились (нова identity, свій
  attempt-рахунок з 1).

Один INSERT на attempt, без UPDATE в місці — той самий all-or-nothing
транзакційний контракт, що `claim_extraction_runs`/`relation_judgments`.

**Відкрите питання, не вирішую тут (recommended default, не факт, і не
блокує LLD):** зберігати `raw_html` завжди при вдалому fetch (навіть якщо
extraction впала) — корисно для дебагу `extraction_error` і для повторної
екстракції новим профілем без повторного мережевого fetch. Ціна — розмір
рядка на кожен attempt; retention policy для `raw_html` вирішується перед
worker-реалізацією, не тут.

### 6.2. Resolver для downstream (сегментація)

**Виправлено у ревізії 2 — попередній варіант (`ORDER BY created_at DESC`)
був некоректний.** Сценарій, що його ламає: сьогодні поточна identity —
`trafilatura 2.3 / profile-v2`; завтра хтось для діагностики повторно
запускає СТАРУ `2.2 / profile-v1` на тому самому occurrence. Старий запуск
запишеться пізніше за часом — і `created_at DESC` поверне його як "поточний",
хоча насправді це діагностичний прогін застарілою identity.

Правильний resolver не вгадує "поточну" identity з таблиці — він отримує
її явно від споживача (сегментатора), який знає свою власну поточну
конфігурацію (`extractor_version`/`extraction_profile_version`, з якими він
розрахований на роботу):

```sql
SELECT *
FROM occurrence_content
WHERE occurrence_id = %s
  AND extractor = %s
  AND extractor_version = %s
  AND extraction_profile_version = %s
  AND status = 'success'
ORDER BY attempt_no DESC
LIMIT 1
```

Порівняння version-рядків (лексикографічне `"2.10.0" > "2.9.0"` дає
невірний результат) взагалі не потрібне — identity не обчислюється з даних,
вона приходить ззовні як параметр виклику. Registry-таблиця для цього поки
не потрібна (сам ти це підтвердив) — сегментатор просто тримає свою
поточну identity у власному конфізі, так само як `extractor_version`/
`extraction_profile_version` — plain-значення, не FK.

Це один helper (за аналогією з `fetch_claims_meta`/provenance-функціями
relation-worker'а), не бібліотека — узгоджено з "не ділити worker як
library" рішенням проєкту.

---

## 7. Чорновий DDL (ілюстрація рішень вище, НЕ `036_...sql`)

**Зміни у ревізії 2:** додано `final_url` (фактичний response URL після
редіректів — Trafilatura сама рекомендує передавати його в
`bare_extraction(..., url=response.url)` для provenance й точнішого
визначення дати:
[Python usage](https://trafilatura.readthedocs.io/en/latest/usage-python.html)).
`CHECK` на `success` розширено з одного поля (`text_content`) до повного
успішного результату — інакше можливий` success` з текстом, але без XML,
хоча сегментація за задумом спирається саме на `structured_content`.

```sql
CREATE TABLE occurrence_content (
    occurrence_content_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurrence_id              uuid NOT NULL REFERENCES item_occurrences(occurrence_id),

    extractor                  text NOT NULL DEFAULT 'trafilatura',
    extractor_version          text NOT NULL,
    extraction_profile_version text NOT NULL,
    attempt_no                 smallint NOT NULL DEFAULT 1 CHECK (attempt_no > 0),

    status                     text NOT NULL CHECK (status IN ('success', 'fetch_error', 'extraction_error')),
    fetch_url                  text NOT NULL,   -- те, що ми ЗАПИТУВАЛИ (= item_occurrences.external_ref)
    final_url                  text,            -- те, що реально ВІДПОВІЛО, після редіректів; NULL при fetch_error
    http_status                integer,
    raw_html                   text,

    text_content                text,
    text_hash                   text,
    structured_content          text,
    structured_content_format   text CHECK (structured_content_format IS NULL OR structured_content_format IN ('trafilatura_xml')),

    errors                      jsonb CHECK (errors IS NULL OR jsonb_typeof(errors) = 'array'),
    code_revision                text NOT NULL CHECK (btrim(code_revision) <> ''),
    latency_ms                    integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at                    timestamptz NOT NULL DEFAULT now(),

    CHECK (
        (status = 'success')
        = (
            text_content IS NOT NULL
            AND text_hash IS NOT NULL
            AND structured_content IS NOT NULL
            AND structured_content_format IS NOT NULL
            AND final_url IS NOT NULL
        )
    ),
    UNIQUE (occurrence_id, extractor, extractor_version, extraction_profile_version, attempt_no)
);
CREATE INDEX idx_occurrence_content_occurrence_id ON occurrence_content(occurrence_id);
```

`http_status`/`raw_html`/`final_url` навмисно НЕ входять у бінарну
success/error CHECK-групу вище — вони легітимно можуть бути присутні і на
`extraction_error` (fetch пройшов, HTML і final_url є, а екстракція впала)
— обмежувати їх було б хибним звуженням.

---

## 8. Що лишається відкритим для наступного проходу (не вирішую зараз)

Частина пунктів першої версії вже отримала напрямок у ревізії 2 — позначено
нижче явно, це НЕ мовчазне закриття питання, а фіксація рішення, ухваленого
в ході review:

1. `raw_html` — зберігати завжди при вдалому fetch чи ні (п.6.1). Модель
   лишає поле nullable в обох випадках — рішення "зберігати завжди" ПРИЙНЯТО
   як напрямок; конкретна retention policy (скільки тримати, чи стискати)
   вирішується перед worker-реалізацією, не блокує LLD.
2. `structured_content_format` — `trafilatura_xml` зараз; чи знадобиться
   `markdown`, вирішить дизайн самого segmentation-алгоритму (`claude/25`
   п.3.4, свідомо не спроєктований).
3. Worker eligibility scope — **напрямок ПРИЙНЯТО:** таблиця
   source-агностична (схема нічого не диктує), але worker v1 — RSS-only
   (`WHERE source_type='rss'` на рівні eligibility-запиту, не DDL).
   Telegram зараз не потребує full-text fetching (`claude/25` п.3.3 —
   повний текст вже в `content_items.text_content`).
4. `max_attempts` для retry eligibility (п.6.1) — конкретне число (напр. 3)
   визначається в worker design, не в DDL; сам контракт (errors retryable
   до ліміту, success термінальний на identity) зафіксовано тут.
5. Real-world розмір: скільки з ~16 451 RSS occurrences реально фетчаться
   без paywall/anti-scraping — не виміряно тут, це вимога до pilot-прогону
   на сервері, не до LLD.

---

## 9. Ревізія 2 → реалізація: два знахідки з реального прогону (03.09.2026)

Ревізія 2 схвалена, `036_add_occurrence_content.sql` + `occurrence_content_worker.py`
+ тести написані й прогнані проти реальної локальної Postgres (не лише
код-рев'ю) перед передачею на сервер. Дві речі, знайдені саме прогоном, а не
читанням коду — варто зафіксувати тут, бо вони змінюють implementation
detail, не архітектуру:

1. **`deduplicate=True` (типовий default trafilatura) — process-global
   stateful, не per-call.** Емпірично: `bare_extraction()` з
   `deduplicate=True`, викликаний ПОВТОРНО в одному Python-процесі на
   near-identical текст (навіть для різних occurrences/URL), на 4-й+ виклик
   тихо повертає `None` — LRU-детектор дублікатів трактує весь документ як
   "вже баченим boilerplate". Для батч-воркера, що обробляє багато
   occurrences в одному прогоні, це небезпечно САМЕ для templated-alert
   кейсу з розділу 5 (шаблонні air-raid/землетрус-алерти): легітимні різні
   occurrences могли б отримувати фальшивий `extraction_error` залежно від
   порядку обробки в батчі. Профіль v1 використовує `deduplicate=False`.
   Ціна — трохи гірше прибирання внутрідокументних дублікатів-абзаців;
   прийнятний обмін.
2. **Python default-параметри зв'язуються під час визначення функції, не
   на кожен виклик** — суто implementation-деталь, не архітектурна, але
   зафіксована тут, бо мало не призвела до тихого запису тестових рядків
   під РЕАЛЬНОЮ identity замість тестового маркера (спіймано прогоном
   тестів проти реальної БД, не рев'ю коду). `process_occurrence()` тепер
   приймає `extractor`/`extractor_version`/`profile_version` як явні
   keyword-параметри з дефолтами на module-level константи, а не читає
   глобали неявно.

---

**Стоп на review знято користувачем — перехід до реалізації.**
`sql/036_add_occurrence_content.sql`, `experiments/occurrence_content/occurrence_content_worker.py`
і `experiments/occurrence_content/test_occurrence_content_worker.py` написані
строго за ревізією 2 вище (включно з п.9), 24/24 тести пройшли проти реальної
Postgres. Segmentation, scheduler/cron і mass backfill — свідомо НЕ зроблені,
за прямою вимогою. Наступний крок — pilot на кількох реальних RSS occurrences
на сервері, не на всьому корпусі.
