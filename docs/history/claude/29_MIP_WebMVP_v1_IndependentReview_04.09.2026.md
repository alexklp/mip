# МІП — Web MVP v1: bounded independent review

**Дата:** 04.09.2026
**Тип:** незалежний review baseline `docs/Web_MVP_v1.md` (commit `6c0a7bd`, за твоїм повідомленням). Нічого не реалізовано, нічого в репо не змінено.
**Межі review (за прямим запитом):** web/pipeline boundary, вибір стеку, перший vertical slice, human-review/write path, очевидні ризики. Без redesign всієї МІП, без production-інфраструктури.

---

## 0. Короткий вердикт

Документ адекватний, не суперечить зафіксованим архітектурним інваріантам (`01_PROJECT_CONTEXT.md`) і сам по собі не потребує redesign. Дизайн-рішення (FastAPI/Jinja2/HTMX, web як тонкий read+bounded-write шар над існуючим pipeline, vertical slice замість all-screens-at-once) — правильні і відповідають правилу 8/14 проєкту (просте оборотне рішення, adopt перед custom).

Знайдено **дві реальні прогалини**, не косметичні:

1. Перший vertical slice (`Segments → Claims`) мовчки припускає, що в claims завжди є segment. Фактичний стан БД — навпаки: сегментація щойно (03–04.09) пройшла pilot і йде bounded/cron (2 occurrence/год), більшість існуючих claims — ще content-level (`segment_id IS NULL`, legacy). Це не архітектурна помилка документа, а недомовлений edge case, який зламає перший же екран "Segments", якщо його не закласти explicitly.
2. ~~"Next slice: Relations → Verification/Annotation" неявно припускає, що relations-бекенд готовий приймати верифікацію..."~~ **[ОНОВЛЕНО 04.09.2026, застаріло]** — див. п.6b. Оцінка спиралась на `claude/20–22` (STOP на production relation worker, candidate v1 оптимізує не ту величину) і вже не відповідає стану: того ж дня реалізовано і bounded-live підтверджено Relations v2 (targeted `candidate_version=2`, exact segment-provenance, judgment PASS для гілок `unrelated`/`related`). Актуальний, вужчий список того, що лишилось — у п.6b.

Решта — дрібніші, але конкретні зауваження нижче.

---

## 1. Що підтверджено

`docs/Web_MVP_v1.md` знайдено через project sync (GitHub `alexklp/mip`), дата в документі `2026-09-04`, статус `working baseline / reference design` — збігається з тим, що ти описав. Повний текст (розділи 1–9: Purpose, Core principles, Technology baseline, Functional scope, Access/identity, Web/pipeline boundary, Data source policy, Definition of MVP, Implementation direction) прочитано і звірено з іншими проєктними документами нижче. Точний git-diff/commit hash `6c0a7bd` окремо не звірявся (доступу до `git log` немає, тільки до синхронізованого вмісту) — якщо для тебе важлива саме бітова відповідність коміту, зістав вручну; зміст, який я бачу, виглядає як фінальна версія цього рішення.

---

## 2. Web / pipeline boundary — оцінка

Розділ 6 документа (web може bounded-читати і транзакційно bounded-писати human-review; НЕ реалізує collectors/workers, не робить global backlog scan з HTTP-запиту, не запускає паралельні виклики Mamay, не обходить provenance/contracts, не мутує analytical output мовчки) дослівно узгоджується з архітектурними інваріантами `01_PROJECT_CONTEXT.md` ("явне розділення collection/processing/AI/presentation", "критичні висновки залишаються перевірюваними людиною", "LLM не є security boundary"). Розбіжностей не знайдено.

Один нюанс: "explicit bounded job/worker interface" для UI-ініційованого recalculation/LLM-виклику (кінець розділу 6) — правильна вимога, але зараз такого інтерфейсу немає. Існуючі workers — це `ops/run_*.sh` bash-обгортки з `flock`-локами (`mamay.lock`, `ingest.lock`, `segment-pipeline.lock`) і cron, без HTTP/status API. Для v1 (read-only slice) це не блокер — документ сам відкладає це "later". Але коли дійде до "UI action needs recalculation", не варто винаходити чергу з нуля (rule 14) — подивитись, чи вистачить того самого lock-файлового паттерна + рядка статусу в БД, перш ніж тягнути Celery/RQ/іншу чергу.

---

## 3. Вибір стеку — оцінка

FastAPI + Jinja2 + HTMX + існуючий PostgreSQL — узгоджується з базовим стеком проєкту (Python/uv, PostgreSQL+pgvector як "поточні базові рішення" з `01_PROJECT_CONTEXT.md`) і з rule 14 (adopt, не custom: усі три — зрілі бібліотеки, а не власний фреймворк). React/Vue explicitly відкинуті без конкретної причини — правильно за rule 8/12.

Одне практичне зауваження, суто щоб не наступити на вже задокументовані граблі (`claude/27_MIP_RuntimeEnv_Proxy_Convention`): будь-який запуск ASGI-застосунку в цьому репо — тільки через `uv run` (`uv run uvicorn ...`), інакше `ModuleNotFoundError` на перший `import fastapi`. Це вже двічі граблі проєкту на одноразових скриптах; для довгоживучого сервісу варто одразу зафіксувати systemd unit / launch-скрипт з `uv run`, а не покладатись, що хтось згадає це вручну під час першого деплою.

---

## 4. Перший vertical slice (`Overview → Materials → Material detail → Segments → Claims`) — оцінка

Сам вибір read-only slice як першого — правильний (перевіряє, що live pipeline взагалі можна інспектувати через реальний АРМ, без ризику write-side).

**Головна знахідка:** документ описує ланцюжок `Material detail → Segments → Claims` як якщо б segment був обов'язковою проміжною ланкою. Фактичний стан пайплайна (за `claude/28`, `chatgpt_27–30`, `sql/041_add_segment_claim_scope.sql`):

- Сегментація (`content_segments`, `segmentation_runs`) — pilot verdict PASS щойно, 03–04.09.2026, pairwise adjacent-boundary. Evidence base — 3 реальних документи + кілька bounded live-runs (`occurrence limit 2–3` за прогін, cron раз на годину). Це не "готова фіча", а щойно ввімкнена, з мінімальним покриттям корпусу.
- Схема claim extraction (`041_add_segment_claim_scope.sql`) explicitly підтримує ДВА режими одночасно: `segment_id IS NULL` (whole-content, увесь історичний масив claims) і `segment_id IS NOT NULL` (нові, segment-level). Документ прямо каже: "Existing historical claim runs remained whole-content runs."

Практичний наслідок для UI/data layer: більшість матеріалів у "Material detail" сьогодні НЕ матимуть жодного segment — там claims висять прямо на content/occurrence. Якщо екран "Segments" жорстко очікує `segments[] → claims[]`, він буде порожнім або зламається для переважної більшості поточних даних. Потрібно explicitly закласти в LLD/шаблон:

- claims без `segment_id` показуються як "claims матеріалу" (можливо, під псевдо-групою "весь матеріал", без окремого segment-рівня);
- claims із `segment_id` показуються під конкретним segment;
- екран не повинен мовчки ховати perspective "матеріал без сегментації" — це і є "traceability is mandatory" (принцип 6 самого документа), просто застосований до власного проміжного шару.

Це не суперечить документу — він просто не проговорив цей edge case явно. Варто дописати одним абзацом у сам baseline перед стартом реалізації Materials/Segments екранів (крок 3–5 з розділу 9), інакше перший же реальний матеріал у списку покаже "Segments: немає" і буде незрозуміло, це баг чи нормальний стан.

---

## 5. Human-review / write path — оцінка

Для v1 (read-only) це коректно відкладено, і документ сам ставить правильну передумову: shared anonymous credential недостатній для attribution, auth-механізм — implementation detail, який треба вибрати ДО початку write-slice. Логічно, блокерів на сьогодні немає.

**Питання на подумати, не блокер:** принцип 5 документа ("Human decisions are first-class data... must be written to PostgreSQL... must not live in local JSON files") прямо суперечить тому, ЯК вже практикується розмітка для relation gold set (`claude/23–24`, `experiments/claim_relations/gold_set/*`): там навмисно offline JSONL + standalone HTML labeler, стратифікована вибірка, hidden repeats для QA — методологія, яка НЕ є "просто письмо в БД швидше", а окремий вимірювальний інструмент калібрування порогів. Це, ймовірно, дві різні популяції з різними цілями (gold set = офлайн калібрування/вимір recall-precision; Web MVP annotation = live operational human-in-the-loop на реальних кандидатах), і в такому разі вони мирно співіснують. Але баланс варто зафіксувати явно одним реченням у baseline ("Relations → Verification/Annotation" слайс — це production review path, gold-set tooling лишається окремим calibration-інструментом і не замінюється), інакше хтось на наступній сесії спробує "переписати gold set під Web MVP" без причини (rule 14/rule 11 — не чіпати робоче без конкретної причини).

---

## 6. Очевидні ризики

**a. ngrok / OPSEC — уточнено користувачем.** Мій початковий тезис у цьому пункті був неточний: вхідні дані МІП — це OSINT з відкритих джерел (новинні сайти, соцмережі), уже публічні за визначенням; частина задачі системи — саме шукати вже сталися OPSEC-витоки в цьому публічному потоці, а не зберігати щось чутливе самому. Тобто на рівні сирих матеріалів (`content_items`/`item_occurrences`) показ через ngrok нічого не розсекречує — воно й так відкрите.

Залишається вужче питання, не про дані, а про **аналітичний шар**: які claims/relations/events МІП вважає значущими, який routing/contour отримує пріоритет — це вже продукт власного аналізу, і його широкий показ потенційно розкриває фокус і методологію моніторингу (що саме відстежується і наскільки уважно), а не самі публічні факти. Наскільки це чутливо і чи взагалі є тут проблема — не мені оцінювати, це не інженерне питання; варто просто явно зафіксувати в baseline, чи стосується "non-restricted" обмеження лише сирих матеріалів (тоді питань немає) чи й аналітичного виводу теж (тоді треба або scope, або усвідомлене рішення показувати як є). Інфраструктурний OPSEC (паролі/токени/мережеві ідентифікатори, rule 10) документ і так закриває в розділі 5 окремо — там зауважень немає.

**b. Готовність relations-бекенду до slice 2 — ОНОВЛЕНО 04.09.2026.** Перша версія цього пункту спиралась на `claude/20–22` (STOP на production relation worker, candidate v1 оптимізує не ту величину, судження Mamay self-certifying) і вже застаріла. Того ж дня реалізовано і bounded-live перевірено Relations v2: `experiments/claim_relations/segment_candidate_pair_worker.py` (`candidate_version=2`, targeted `--run-id`, exact segment→occurrence→source provenance замість старого агрегованого `content_id → contour_set`, cross-run/occurrence/contour exclusion, persistence+idempotency PASS — код звірено через project sync) і `segment_relation_judgment_worker.py` (targeted selection поверх `candidate_version=2`, той самий prompt v3/shared-referent gate, що й старий worker, без global backlog scan). Живі прогони (за твоїм звітом, не перевірялись мною повторно на БД) дали PASS для гілок `not_confirmed → unrelated` і `not_confirmed → related`. STOP з `claude/20–22` стосувався СТАРОГО global content-level v1, а не нинішнього targeted v2 — повторювати той теза "backend не готовий" вже некоректно.

Актуальний, вужчий список того, що дійсно відкрито:

- **production scheduling відсутній.** v2 не підключено в live cron/orchestrator — candidate generation і judgment запускаються вручну targeted-командами (`--run-id`), черга кандидатів не росте автоматично разом із segment pipeline.
- **гілка** **`confirmed`****/****`same_event`****/****`same_fact`****/****`contradiction`** **ще не спостерігалась живою саме на новому segment-scope шляху** (на старому content-level worker — так, раніше). Перш ніж малювати UI-стан для цих лейблів "з голови", варто прогнати ще один targeted batch і зловити хоч один живий `confirmed`-приклад.
- **write-path людської верифікації (accept/reject/label + actor identity) — окрема, ще не реалізована частина**, як і зафіксовано в `Web_MVP_v1.md` розділ 5 (auth-механізм — implementation detail до вибору перед цим слайсом).

Практичний висновок для Web MVP slice 2: читати вже існуючі `candidate_pairs`/`relation_judgments` (v2) як bounded/test-дані можна вже зараз — просто це буде невеликий, вручну згенерований набір (десятки candidate pairs, одиниці живих judgment), а не зростаючий live-потік. Це не блокер для старту UI-роботи над slice 2, тільки уточнення масштабу даних, які там буде видно спочатку.

**c. Mamay — суворо серійний ресурс.** `flock`-лок (`mamay.lock`), cron раз/год, bounded ліміти (2–3 occurrence/прогін), виміряні латентності claim extraction — 65–205 секунд на виклик. Принцип 7 документа це вже правильно враховує ("long LLM work not coupled to page requests"), просто нагадування на етапі реалізації: жодна майбутня кнопка "проаналізувати зараз" не повинна чекати відповідь синхронно в межах HTTP-запиту — тільки постановка в чергу + окремий статус/polling, інакше перший же тестер зловить 30–60-секундний зависаючий запит або (гірше) timeout проксі/ngrok.

---

## 7. Що НЕ перевірялося (межі цього review)

Немає доступу до живого сервера (`srv01`) в цій сесії — не перевірялося: чи вільний порт під FastAPI, чи є вже інший процес на localhost, фактичний обсяг `content_segments`/`claims` у продакшн БД на сьогодні (цифри вище — з задокументованих pilot-прогонів, не з live-запиту), і чи `6c0a7bd` — справді HEAD `main` чи іншої гілки. Якщо потрібно — це вже наступний, окремий крок (`live audit`, за зразком того, що вже робилося для `claude/25`), не частина цього bounded review.

Аналогічно для Relations v2 (п.6b): код воркерів (`segment_candidate_pair_worker.py`, `segment_relation_judgment_worker.py`) і DDL/prompt-контракти, на які вони спираються, звірено через project sync і відповідають опису. Конкретні цифри живих прогонів (кількість candidate pairs, scores, exit-коди lock contention) — з твого звіту, не перевірялись мною повторно на БД чи в логах.

---

## 8. Рекомендація

Redesign не потрібен. Можна починати реалізацію за порядком з розділу 9 baseline (FastAPI skeleton + health + Jinja layout → PostgreSQL read layer → Materials list → Material detail → Segment/claim display → лише потім write-path) за умови, що перед кроком "Segment/claim display" (крок 5) в сам baseline додається один абзац про dual-mode claims (п.4 вище), а перед стартом slice 2 явно зафіксовано, що relations-бекенд (candidate v2 + calibrated thresholds + reliability hardening) — окрема передумова, не "просто ще один екран".
