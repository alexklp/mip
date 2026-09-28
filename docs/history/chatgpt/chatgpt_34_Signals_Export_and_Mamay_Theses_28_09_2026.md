# МІП — Signals: XLSX export і Mamay theses

Дата checkpoint: 28.09.2026.

## Статус

Завершено і перевірено вертикальний зріз аналітичного представлення сигналів:

1. XLSX export для Global Signals і C1 Signals.
2. Короткий AI-опис та основні тези сигналу через MamayLM.
3. Фонове bounded-формування тез із кешуванням та дедуплікацією.
4. Захист черги від блокування окремою невалідною відповіддю моделі.
5. Уніфіковане відображення тез у Global Signals та C1.
6. Виправлення focus-артефактів у Signals/C1 і пошуку Topics.

## XLSX export

Додано `web/signal_export.py`.

Підтримуються:

- C1: `/contours/signal/{candidate_id}/export.xlsx`;
- Global: `/signals/{candidate_id}/export.xlsx`.

Workbook містить:

- `Зведення`;
- `Поширення`;
- `Публікації`.

Для Global export використовується exact-history до 7 днів без зміни detector/snapshot.

Історичне розширення виконується тільки за exact `content_id`.
Найраніший знайдений occurrence не називається першоджерелом; у продукті використовується формулювання на кшталт «Найраніша знайдена exact-публікація».

## Mamay theses

Основні компоненти:

- `reporting/signal_theses.py`;
- `reporting/signal_theses_queue.py`;
- `reporting/signal_theses_worker.py`;
- `web/signal_theses.py`;
- `ops/run_signal_theses_once.sh`.

Модель:

`MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf`.

Модель отримує всі унікальні матеріали, що входять у конкретний сигнал, а не тільки UI evidence sample.

Контракт результату:

- короткий `summary`;
- 2–4 основні тези;
- для кожної тези 1–3 representative evidence material IDs.

Відповідь Mamay проходить строгий JSON/contract validator.
Сирий model output не є аналітичним висновком без валідації.

## Cache і дедуплікація

Cache schema:

`signal-theses-cache/1`.

Input hash включає:

- версію контракту;
- prompt name/version;
- модель;
- точний canonical набір `{content_id, title, text}`.

Scope, candidate ID та UI title до hash не входять.

Це дозволяє повторно використовувати один результат для однакового exact-набору матеріалів у різних C1 views.

На контрольному зрізі:

- candidate references: 295;
- unique tasks: 243;
- dedup saved: 52;
- reused across scopes: 40.

Snapshot не містить повних AI-текстів.
Web request не запускає Mamay і не створює нову thesis task — UI лише читає готовий cache.

## Background worker

Один запуск wrapper обробляє максимум одну pending-задачу.

Використовується спільний non-blocking `mamay.lock`.
Паралельний inference з claims/contours/segment не запускається.

Mamay timeout для thesis worker:

150 секунд.

Cron на runtime:

`9,24,39,54 * * * * .../ops/run_signal_theses_once.sh`

Theses workload є низькопріоритетним відносно основних Mamay pipeline jobs.

## Failure isolation

Реалізовано persistent failure state:

`signal-theses-failure/1`.

Cooldown:

- перша помилка — 1 година;
- друга — 6 годин;
- наступні — 24 години.

Це запобігає starvation: одна детерміновано невалідна відповідь Mamay не блокує всю pending-чергу.

Механізм підтверджено на реальному task:

- task стабільно порушував evidence contract;
- worker записав failure state і cooldown;
- наступний cron-run вибрав інший input hash;
- наступна задача успішно пройшла `VALIDATION: OK`.

На момент checkpoint:

- ready: 4;
- pending: 239;
- один task знаходився у failure cooldown.

## UI

Старий блок «Формулювання у просторах» замінено у Global Signals і C1 на:

- `Коротко`;
- `Основні тези`.

Поки cache відсутній, UI показує нейтральний fallback без запуску inference з HTTP request.

Також прибрано небажаний focus-ring навколо всього signal tabpanel.
У Topics search усунено подвійний focus-артефакт, при цьому keyboard focus для реальних інтерактивних елементів збережено.

## Перевірки

Перед checkpoint підтверджено:

- 19 target unit tests — OK;
- Python compile — OK;
- Jinja template syntax — OK;
- wrapper `bash -n` — OK;
- `git diff --check` — clean;
- `/health` — OK;
- live Mamay generation — OK;
- automatic cron generation — OK;
- live failure cooldown і перехід до наступної задачі — OK;
- візуальна перевірка Global Signals, C1 Signals і Topics search — OK.

Спостережена latency успішних невеликих Mamay tasks у цьому slice:
приблизно 13–23 секунди.

## Межі рішення

Свідомо не додавали:

- Redis/Celery або окремий broker;
- PostgreSQL-таблицю черги;
- AI-тексти безпосередньо у signal snapshot;
- автоматичне виправлення/обрізання невалідного evidence від моделі.

Поточне рішення залишається простим, локальним, перевірюваним і достатнім для поточного масштабу.

## Наступні перевірки

Після накопичення більшої кількості готових thesis cache entries слід окремо оцінити:

- якість summary/theses на різних типах сигналів;
- частоту contract failures;
- характер повторюваних помилок Mamay;
- фактичний steady-state темп заповнення черги.

Prompt або normalization policy змінювати варто лише на основі накопиченої вибірки, а не одного failure case.
