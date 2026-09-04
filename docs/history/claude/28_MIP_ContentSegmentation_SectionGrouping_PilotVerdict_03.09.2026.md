# МІП — Content Segmentation v1: sectionize() + Mamay boundary classifier — pilot verdict

**Дата:** 03.09.2026 (доповнено того самого дня — другий pilot, pairwise
adjacent-boundary, змінив рекомендацію)
**Статус:** technical pilot verdict + design decision, зафіксовано користувачем.
Prompt-tuning на сьогодні зупинено — обидва пілоти (holistic і pairwise) дали
достатньо інформації для v1-рішення.

## 0. Підсумкове рішення (найважливіше, читати першим)

> **Decision update, 03.09.2026:** Pairwise adjacent-boundary classification is the
> preferred segmentation strategy for v1. Technical pilot on three real document
> classes showed: multi-topic digest -- expected 3 segments → pairwise produced 3;
> long-form single-topic narrative -- expected 1 → pairwise produced 1, while
> holistic produced 5; daily frontline summary -- pairwise preserved the acceptable
> 3-segment structure and did not split individual operational directions.
>
> Therefore v1 pipeline is:
> `Trafilatura XML → deterministic sectionize → Mamay adjacent boundary same/new →
> deterministic contiguous segment assembly`
>
> Mamay does not generate text, offsets, or segments; it classifies only boundaries
> between adjacent sections. Holistic grouping is retained only as pilot evidence and
> is not the recommended production path. Evidence base is still small (3 real
> documents), so this is a v1 engineering decision, not a claim of universal
> segmentation accuracy.

Решта документа — методологія, повний хід пілоту (включно з holistic-версією, яку
спробували першою і відкинули) і сирі evidence-дані обох підходів.

## 1. Що тестували

Two-layer pipeline, без БД-записів, без DDL, без embeddings/claims/scheduler:

1. `sectionize()` (`experiments/content_segmentation/sectionize.py`) — детермінований
   парсинг `occurrence_content.structured_content` (`trafilatura_xml`): top-level
   candidate sections навколо `h2`, `h3+` лишається всередині поточної секції, `h1`
   (publication title) виноситься окремо в `document_heading`, НЕ бере участі у
   формуванні sections.
2. Дві альтернативні LLM-стратегії групування секцій у segments, обидві через
   `MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf`,
   `http://127.0.0.1:8080/v1/chat/completions` (підтверджений живий endpoint; `:8000`
   з `claude/03_MIP_Server_State` застарілий), `seed=42/top_k=1` детермінований
   декодинг, суворий code-owned валідатор -- модель на слово не отримує довіри:
   - **Holistic** (`mamay_group_sections.py`) — один виклик на весь документ, модель
     повертає повний список segments (`section_start`/`section_end`) одразу.
   - **Pairwise adjacent-boundary** (`mamay_adjacent_boundary.py`) — окремий виклик
     на КОЖНУ пару сусідніх sections, модель повертає ЛИШЕ один enum
     (`{"boundary":"same"|"new"}`); segments збираються з послідовності рішень
     детермінованим кодом, не моделлю.

Перевірено на трьох реальних occurrence (RSS, `extractor=trafilatura 2.2.0`,
`profile=v1`):

| Клас | occurrence_id | sections (sectionize) |
|---|---|---|
| Brave1 digest | `ea29f3e7-b168-4f6c-955d-4ca67e2d970f` | 4 |
| Single-topic лонгрід ("Списаний з фронту, вбитий у тилу...") | `a8698043-4665-4981-ba22-6c789ea62b90` | 8 |
| Добова фронтова зведення | `a374a319-15ff-46cd-896a-7b6b81f8ca3e` | 3 |

## 2. Sectionize() — PASS, без застережень

На всіх трьох реальних документах: жоден не порізаний "безглуздо" (добова зведення НЕ
розбилась по напрямках — деталізація 225 боїв лишилась одним текстовим блоком
всередині своєї секції); `h1` коректно виноситься в `document_heading`, не створює
сирітську секцію; preamble не губиться.

Відкритий пробіл: жоден з трьох документів не містив `h3` — вкладеність `h3`
перевірена лише на синтетиці, НЕ на реальних даних.

## 3. Дві стратегії групування — хід пілоту

### 3.1. Holistic — знайдено й виправлено баг промпту, потім впёрлись у межу підходу

Перша ітерація промпту містила приклад формату відповіді з конкретними числами
(`0,2,3`) — модель буквально копіювала їх як шаблон, незалежно від реальної
кількості секцій (на 3-секційному документі це дало галюцинований `section_id=3`,
валідатор коректно впіймав). Після фіксу (прибрано числовий приклад, залишено
prose-опис схеми) — падінь більше не було, але зʼявилась стабільна проблема:
**over-segmentation довгого single-topic наративу** (8 секцій → 5 segments замість
очікуваної 1) і неточна кількість на дайджесті (2 замість ~3). Причина, за
результатами другого пілоту (3.2) -- ймовірно структурна: holistic-виклик вимагає
від моделі ОДНОРАЗОВО утримати ціле рішення про партиціонування N елементів, це
складніше за N-1 локальних бінарних рішень.

### 3.2. Pairwise adjacent-boundary — той самий день, без жодної ітерації тюнингу, влучило одразу

| Клас | очікування | Holistic (після фіксу) | Pairwise |
|---|---|---|---|
| Brave1 digest | ~3 segments | 2 (`[лід+дрони]`,`[втрати+боїв]`) | **3** — `[(0,1),(2,2),(3,3)]` |
| Single-topic лонгрід | 1 segment | 5 (over-segmentation) | **1** — `[(0,7)]` |
| Добова зведення | не різати по напрямках | 3 (по секції) | 3 (по секції), той самий розумний результат |

Latency: pairwise-виклики поштучно суттєво швидші (~3.2–4.9s проти ~11–23s у
holistic, менший prompt і мінімальний `max_tokens=64`), сумарний wall-clock на
документ — порівнянний або кращий за holistic, навіть з N-1 викликами замість 1.

## 4. Design decision (зафіксовано користувачем, 03.09.2026)

- **v1 pipeline:** `Trafilatura XML → deterministic sectionize → Mamay adjacent
  boundary same/new (pairwise) → deterministic contiguous segment assembly`.
  Дивись п.0 -- повний текст рішення.
- **Architectural principle (лишається в силі з першої версії вердикту):** пріоритет
  — precision меж сегмента, false split переважніший за false merge. Segment НЕ
  повинен бути атомарною подією.
- **Sectionize() (структурний шар) — PASS**, без змін.
- **Pairwise adjacent-boundary classifier — preferred production path для v1.**
  Mamay класифікує ЛИШЕ межі (`same`/`new`) між сусідніми sections, нічого не генерує
  і не повертає offsets/segments напряму.
- **Holistic grouping — retained only as pilot evidence, NOT the recommended
  production path.** Код (`mamay_group_sections.py`) лишається в репо як
  задокументований негативний результат, не видаляється.
- **Evidence base мала (3 реальних документи)** — це v1 engineering decision, НЕ
  твердження про універсальну точність сегментації. Розширення evidence — окрема
  майбутня робота, не сьогодні.
- **Prompt-tuning на сьогодні ЗУПИНЕНО** для обох підходів.
- **Наступний крок (НЕ сьогодні, окрема сесія):** мінімальний LLD/persistence
  segmentation v1 на базі pairwise-підходу — без нового дослідження бібліотек, без
  чергового циклу prompt-tuning.

## 5. Evidence — сирі pilot outputs

### 5.1. Holistic, до фіксу промпту (баг: literal example copying)

```
=== brave1_digest (ea29f3e7-b168-4f6c-955d-4ca67e2d970f) ===
  sections=4
  segments=2 exclude=[]
    [0-2] -> ['Українські військові отримали можливість замовляти повністю кастомні дрони...', 'Кастомні дрони для Сил оборони на Brave1 Market', 'Росія втратила за добу 1390 вояків, 3 танки і 57 артилерійських систем']
    [3-3] -> ['20 серпня на фронті відбулося 238 бойових зіткнень']

=== single_topic (a8698043-4665-4981-ba22-6c789ea62b90) ===
  sections=8
  segments=2 exclude=[]
    [0-2] -> ['Пораненого на фронті бійця часто списують у запас...', '"Моя мрія здійснилася": Михайло Ковалів', 'Юрій Бондаренко: убитий не на фронті, а на службі в тилу']
    [3-7] -> ['Сихів: коли на бік ухилянта стає натовп', '"Треба стати чоловіком": погляд зсередини системи', 'Порушення трапляються і з боку самих ТЦК', "Три тисячі скарг за п'ять місяців", 'Замість висновку']

=== daily_frontline_summary (a374a319-15ff-46cd-896a-7b6b81f8ca3e) ===
  sections=3
  GROUPING FAILED: segments[1] діапазон поза межами [0,2] або start>end: {'section_start': 3, 'section_end': 3}
  parsed={'segments': [{'section_start': 0, 'section_end': 2}, {'section_start': 3, 'section_end': 3}], 'exclude_section_ids': []}
```

### 5.2. Holistic, після фіксу промпту

```
=== brave1_digest ===
  segments=2 exclude=[]
    [0-1] -> ['...дрони...', 'Кастомні дрони для Сил оборони на Brave1 Market']
    [2-3] -> ['Росія втратила за добу 1390 вояків, 3 танки і 57 артилерійських систем', '20 серпня на фронті відбулося 238 бойових зіткнень']

=== single_topic ===
  segments=5 exclude=[]
    [0-1], [2-3], [4-4], [5-6], [7-7]

=== daily_frontline_summary ===
  segments=3 exclude=[]
    [0-0], [1-1], [2-2]
```

### 5.3. Pairwise adjacent-boundary (фінальний, preferred підхід)

```
=== brave1_digest (ea29f3e7-b168-4f6c-955d-4ca67e2d970f) ===
  document_heading='Українські військові зможуть замовляти кастомні дрони на Brave1 Market, Росія втратила у війні ще 1390 окупантів, на фронті - 238 боїв. Підсумки доби 20 серпня'
  sections=4
  boundary 0→1 = same  (latency=3.17s)
  boundary 1→2 = new  (latency=4.15s)
  boundary 2→3 = new  (latency=4.89s)
  resulting segments = [(0, 1), (2, 2), (3, 3)]

=== single_topic (a8698043-4665-4981-ba22-6c789ea62b90) ===
  document_heading='Списаний з фронту, вбитий у тилу: чому "безпечна" служба в ТЦК виявляється не безпечнішою за окоп'
  sections=8
  boundary 0→1 = same  (latency=3.9s)
  boundary 1→2 = same  (latency=3.97s)
  boundary 2→3 = same  (latency=4.17s)
  boundary 3→4 = same  (latency=3.95s)
  boundary 4→5 = same  (latency=3.88s)
  boundary 5→6 = same  (latency=3.78s)
  boundary 6→7 = same  (latency=3.74s)
  resulting segments = [(0, 7)]

=== daily_frontline_summary (a374a319-15ff-46cd-896a-7b6b81f8ca3e) ===
  document_heading='Сили оборони України ліквідували за добу 1600 російських вояків. Повітряні сили збили 96 цілей. На фронті - 225 боїв'
  sections=3
  boundary 0→1 = new  (latency=3.68s)
  boundary 1→2 = new  (latency=4.9s)
  resulting segments = [(0, 0), (1, 1), (2, 2)]
```

## 6. Артефакти пілоту (код)

- `experiments/content_segmentation/sectionize.py` — self-tested на синтетиці, PASS.
- `experiments/content_segmentation/mamay_group_sections.py` — holistic-підхід,
  self-tested (`--self-test`, 8/8), 2 реальних прогони. **Не production path**,
  лишається як negative evidence.
- `experiments/content_segmentation/mamay_adjacent_boundary.py` — pairwise-підхід,
  self-tested (`--self-test`, 13/13), 1 реальний прогін (12 викликів). **Preferred
  production path для v1.**
- Жодних змін у БД, жодного DDL, жодного persistence.
