# MIP Content Contours v2 — calibration package (25.08.2026)

Статус: **план + read-only скрипти готові, ще НЕ запущені.** `content_contour_assignments` не існує. `sources.contour_id` не змінено. Жодного запису в БД цей документ не описує.

Продовження: `claude/17_MIP_ContentContours_v2_AdversarialReview_25.08.2026.md` (adversarial review, рішення користувача від 25.08.2026 зафіксовано там і нижче).

## 0. Зафіксовані рішення (вхід для цього пакету)

- CONFIRMED / CANDIDATE / NONE — єдина state-machine, DDL поки не пишемо.
- Evidence policy різна по контурах: C1 CONFIRMED тільки через verified exact object/alias anchor, semantic — тільки CANDIDATE/diagnostic. C2/C3/C4 semantic-only поки не дає CONFIRMED, поки немає окремо валідованого deterministic evidence.
- **`hostile-source provenance` для C4 технічно = `EXISTS occurrence → source з legacy sources.contour_id=4`.** Термінологія в коді/документації: `source_group_4` / `legacy_source_contour_4`, НЕ strategic C4.
- `claim_candidate_scan.py` НЕ мігруємо зараз. Його поточний `contour_set` де-факто означає source provenance groups. Надалі: `source_group_set` = union legacy `sources.contour_id`; `content_contour_set` = confirmed strategic content assignments, коли з'являться. Це дві різні осі, не два конкуруючі визначення.
- DDL з `claude/17` не застосовуємо. Persistence проєктується пізніше під фактичні таблиці `content_items`, `monitoring_contours` (майбутній довідник, замінить нинішній `contours`) і reference registry.

## 1. Задача 1 — C1 recall / alias-gap experiment

**Скрипт:** `experiments/content_contours/c1_recall_gap_scan.py` (додається файлом).
**Конфіг:** `experiments/content_contours/c1_object_aliases.json` (шаблон додається — заповнити РЕАЛЬНИМИ канонічними назвами/алиасами об'єктів ДШВ, якими вже користуєшся; сам скрипт нічого не вигадує і нічого не зберігає).

Метод — 3 незалежні candidate-generation сигнали, жоден не є assignment:

1. **semantic** — cosine similarity content-вектора (BGE-M3, embedding_model_id=1) до канонічних назв об'єктів. М'який поріг 0.35 (retrieval, не production).
2. **lexical** — генеричні (НЕ object-specific) доктринальні терміни роду військ ("десантно-штурмов", "аеромобільн", "повітряно-десантн" тощо) — публічна термінологія, не прив'язана до конкретної частини, тому безпечна для хардкоду в скрипті.
3. **trigram (pg_trgm)** — `similarity()` між текстом і кожним alias, ловить морфологічні варіанти/скорочення/одруківки повз exact ILIKE. Скрипт сам перевіряє, чи розширення встановлене; якщо ні — сигнал пропускається, лишаються два інших.

EXCLUDE — усе, що вже проходить exact alias-match (= поточний C1 CONFIRMED pool), інакше вибірка тавтологічно підтвердить те, що вже підтверджено.

**Припущення, яке потрібно звірити:** скрипт рахує exact-match сам через ILIKE title+text_content по кожному alias. Якщо твоє калібрування, що дало 8 items, рахувало exact-match інакше (інший запит/логіка) — EXCLUDE-множина тут може не збігтися 1:1 з тими 8. Якщо так — скажи, яким запитом рахував, перепишу `exact_alias_content_ids()`.

**Вихід скрипта:** до 80 кандидатів (відсортовані за кількістю спрацьованих сигналів, потім за semantic score), кожен — `content_id`, title, які сигнали спрацювали (semantic score/facet, trgm score/alias, lexical cues), preview тексту. Плюс підсумкові лічильники (exact pool vs candidate pool).

**Результати:** ще не отримані — потрібно прогнати на сервері і прислати вивід (як у Routing v2 ablation), розділ 4 нижче буде дописано.

## 2. Задача 2 — C4 provenance ablation

**Скрипт:** `experiments/content_contours/c4_provenance_ablation.py` (додається файлом).
**Конфіг:** `experiments/content_contours/c4_facets.json` (шаблон додається — заповнити реальними facet-прототипами C4, якими вже рахував median/p90/max у попередньому калібруванні; включно з `ukrainian_strikes_rf`, оскільки ти вже назвав цей facet_id).

`source_group_4` = `EXISTS occurrence → source з sources.contour_id=4` (рішення §0).

Семантичний сигнал — max cosine similarity до facet-прототипів, той самий zero-shot патерн, що `routing_scan.py`. **Якщо твій актуальний C4-скрипт рахує семантику інакше** (інша агрегація/пороги/facets) — цей скрипт дасть інші числа, ніж твої попередні median/p90/max; тоді заміни `compute_semantic_scores()` на свою реальну логіку, решта (стратифікація, вивід) лишається без змін.

Порівняння (A/B/C), стратифікована вибірка замість top-20:

- **A. provenance-only** — просто розмір `source_group_4` (baseline).
- **B. provenance + semantic**, стратифіковано навколо ілюстративної робочої межі (median 0.398 ± 0.05): high / band / low — по 25 у вибірку кожен stratum.
- **C. high semantic БЕЗ provenance** — потенційно C4-релевантний контент поза `source_group_4`, той самий поріг, вибірка 25.

Directionality (`ukrainian_strikes_rf` та подібні) свідомо НЕ виправляється тут — facet виводиться як є, для фіксації як окремого known failure mode, згідно рішення від 25.08.2026 не чіпати prototypes зараз.

**Результати:** ще не отримані — потрібен прогін і вивід у чат, розділ 4 нижче буде дописано після цього.

## 3. Задача 3 — rename plan для `claim_candidate_scan.py`

Точний scope (нічого в коді ще не змінено, тільки план):

| Було | Стане |
|---|---|
| docstring: "шукає найсильніші **cross-contour** candidate pairs" | "шукає найсильніші **cross-source-group** candidate pairs" |
| docstring: "**Cross-contour контракт** (узгоджено явно, варіант 1 — консервативний)" | "**Cross-source-group контракт** (узгоджено явно, варіант 1 — консервативний)" |
| docstring: "**contour_set(claim)** = множина усіх distinct contour_id серед occurrences його content_id" | "**source_group_set(claim)** = множина усіх distinct legacy `sources.contour_id` серед occurrences його content_id" |
| docstring: "пара (A, B) вважається **cross-contour**, ТІЛЬКИ якщо **contour_set(A)** і **contour_set(B)** повністю НЕ перетинаються" | "пара (A, B) вважається **cross-source-group**, ТІЛЬКИ якщо **source_group_set(A)** і **source_group_set(B)** повністю НЕ перетинаються" |
| `build_claim_meta()`: словник-ключ `"contour_set": contour_ids` | `"source_group_set": contour_ids` (сама змінна `contour_ids`/логіка `{c for c, *_ in occs if c is not None}` — БЕЗ змін, це і так вже `sources.contour_id`) |
| `run()`: `if meta_i["contour_set"] & meta_j["contour_set"]: continue  # є спільний contour — не cross-contour у варіанті 1` | `if meta_i["source_group_set"] & meta_j["source_group_set"]: continue  # є спільний source_group — не cross-source-group у варіанті 1` |
| `print(f"cross-contour candidate pairs (all, before top-N cut): ...")` | `print(f"cross-source-group candidate pairs (all, before top-N cut): ...")` |
| `print(f"\n--- top {TOP_N} cross-contour pairs by cosine similarity ---\n")` | `print(f"\n--- top {TOP_N} cross-source-group pairs by cosine similarity ---\n")` |
| вивід рядків: `print(f"       contour_set={sorted(a['contour_set'])} ...")` (×2, для A і B) | `print(f"       source_group_set={sorted(a['source_group_set'])} ...")` |

Не в scope цієї зміни (НЕ чіпаємо):

- SQL-колонка `sources.contour_id` — лишається як є, це реальна колонка легасі-схеми, її сама назва тут не обговорюється.
- `fetch_occurrences()` — SQL-запит і назва параметра `contour_id` у tuple лишаються (це прямий read з реальної колонки).
- WARNING-повідомлення "жоден occurrence без contour_id, пропускаю" — про null у реальній колонці, не про перейменований derived-концепт.

**Відомий drift-ризик поза scope цього завдання** (не робимо зараз, тільки фіксую): `experiments/claim_relations/candidate_pair_generator.py` і `sql/011_add_claim_relations.sql` використовують ту саму термінологію `contour_set`/"cross-contour" у docstring/коментарях, посилаючись саме на контракт `claim_candidate_scan.py`. Якщо перейменувати тільки `claim_candidate_scan.py` — ці два файли розійдуться термінологічно з ним (той самий концепт, різні назви). Не чіпаю без окремого рішення — просто щоб не спливло як несподіванка пізніше.

## 4. Результати експериментів

**Поки порожньо.** C1 alias-gaps, C4 ablation numbers, підтверджено/спростовано — після того, як прогониш обидва скрипти на сервері і пришлеш повний вивід (той самий формат, що для Routing v2 ablation). Я не вигадую цифри наперед — це прямо суперечило б задачі ("не превращать отсутствие измерений в новый baseline").

## 5. Файли пакету

- `experiments/content_contours/c1_recall_gap_scan.py`
- `experiments/content_contours/c1_object_aliases.json` (шаблон, заповнити реальними даними)
- `experiments/content_contours/c4_provenance_ablation.py`
- `experiments/content_contours/c4_facets.json` (шаблон, заповнити реальними facet-прототипами)

Усі чотири — окремими файлами в цьому повідомленні, поклади в `experiments/content_contours/` свого репо.
