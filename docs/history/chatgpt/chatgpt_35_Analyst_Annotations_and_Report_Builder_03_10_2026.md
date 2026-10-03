# МІП — Analyst Annotations і Report Builder

Дата checkpoint: 03.10.2026.

## Статус

Завершено і перевірено vertical slice операціоналізації Signals:

1. Постійні analyst annotations для Signal і матеріалів.
2. Корекція належності матеріалу до Signal.
3. Розподіл Signal/матеріалів по контурах моніторингу.
4. Browser-local чернетка аналітичного звіту.
5. Серверний resolve чернетки по актуальних даних БД.
6. Редагований DOCX export із summary, тезами, прикладом публікації та активними посиланнями.
7. Stable identity Signal у звіті через exact `content_ids`.
8. Розділення автоматичного summary та ручної правки аналітика.
9. Очищення службових metadata DOCX.

Базовий HEAD до інтеграції цього slice:

`d9b1854`

Checkpoint описує перевірений робочий стан незакоміченого vertical slice перед інтеграцією.

## Analyst annotations

DDL:

`sql/043_add_analyst_annotations_v1.sql`

Основні модулі:

- `web/analyst_annotations.py`;
- `web/annotation_store.py`;
- `web/signal_annotation_view.py`.

Реалізовано:

- canonical fingerprint Signal по exact membership;
- persistent Signal context;
- explicit analyst contour annotation для Signal;
- direct contour annotation для окремого material;
- explicit include/exclude material у межах Signal;
- effective contour resolution із пріоритетом людської корекції;
- clear/reset explicit annotation;
- транзакційний write path.

Signal context не переписується мовчки при конфлікті membership.

Матеріал через Signals API можна редагувати тільки в межах фактичного Signal context, а не довільним `content_id`.

## UI аналітика

У Signals додано робочі дії:

- додавання/видалення Signal у контур;
- додавання/видалення material у контур;
- включення/виключення material зі складу Signal;
- окремий collapsed-блок виключених матеріалів;
- `До звіту` для Signal і material.

Persistent analyst annotations і локальна корзина звіту залишаються різними сутностями.

## Report Builder

Додано:

- `/report`;
- browser-local draft `mip-report-draft-v1`;
- schema `mip-report-draft/1`;
- чотири базові розділи;
- ручний розподіл елементів по розділах;
- переміщення, reorder та видалення;
- `Фактичний виклад`;
- `Оцінка аналітика`;
- DOCX export через `/report/export.docx`.

Draft зберігає references і presentation state, а сервер перед export дочитує актуальні дані.

## Stable Signal identity

Початковий варіант зберігав тільки `candidate_id`.

Це виявилось некоректним контрактом: `candidate_id` залежить від поточного snapshot і може змінитися після refresh.

Поточний новий Signal report item зберігає:

- exact `content_ids`;
- `source_groups`;
- `representative_content_id`;
- `candidate_id` лише як provenance/hint.

Для нового durable item сервер не залежить від поточного Signal snapshot для відновлення membership.

Публікації дочитуються напряму з БД по exact `content_ids`.

Legacy draft recovery залишається bounded і strict:

1. exact старий `candidate_id`;
2. старий ID як унікальний content-anchor нового candidate;
3. exact title/summary match тільки якщо він однозначний.

Fuzzy recovery не використовується.

Legacy recovery перевірено також live на старій чернетці після зміни snapshot.

## Summary provenance

Виявлено окремий контрактний дефект: автоматичний Signal summary і ручне редагування аналітика спочатку зберігались в одному `factual_summary`.

Це дозволяло старому presentation snapshot мати пріоритет над актуальним Mamay cache.

Поточний контракт:

- `factual_summary` — presentation snapshot;
- `factual_summary_override = null` — аналітик не редагував поле;
- `factual_summary_override = "..."` — explicit ручна редакція;
- `factual_summary_override = ""` — аналітик свідомо прибрав блок `Коротко`.

Для durable Signal export:

- override відсутній → використовується актуальний Mamay summary по exact membership;
- override містить текст → використовується текст аналітика;
- override порожній → `Коротко` не рендериться.

Для legacy draft стару поведінку збережено окремо.

Live E2E перевірено на трьох Signal одночасно:

1. untouched auto-summary;
2. explicit manual override;
3. explicit empty override.

Усі три сценарії пройшли.

## DOCX resolve і render

Основні модулі:

- `web/report_resolve.py`;
- `web/report_docx.py`.

Signal export може містити:

- заголовок;
- кількість джерел і публікацій;
- `Коротко`;
- `Основні тези`;
- `Приклад публікації`;
- оцінку аналітика.

Якщо готових тез немає, порожній блок не створюється.

Приклад публікації вибирається з фактичних publications Signal.

Повтор заголовка на початку example text прибирається bounded normalization.

Зовнішні URL рендеряться як реальні OOXML hyperlinks.

На live DOCX для трьох Signal підтверджено:

- 3 унікальні publication URL;
- 6 external hyperlink elements:
  заголовок + `Відкрити публікацію` для кожного Signal;
- A4 layout;
- коректний page flow;
- відсутність clipping/overlap у візуальному render.

## DOCX metadata

Прибрано provenance дефолтного шаблону `python-docx`.

Поточна генерація встановлює:

- фактичні `created` / `modified`;
- порожні `author` / `lastModifiedBy`;
- порожній `comments`;
- `Application = МІП Report Builder`.

Видаляються недостовірні extended properties шаблону:

- `Template`;
- `TotalTime`;
- `Pages`;
- `Words`;
- `Characters`;
- `CharactersWithSpaces`;
- `Lines`;
- `Paragraphs`;
- `AppVersion`.

Live DOCX перевірено після перепакування OOXML:
ZIP package валідний, hyperlinks і render не пошкоджені.

Фінальний `comments=""` додано після live export і підтверджено unit test.

## Перевірки

Фінальний integration gate перед checkpoint:

- Python compile — OK;
- 63 unit/regression tests — OK;
- `git diff --check` — clean.

Тести покривають:

- annotation fingerprint/effective semantics;
- transaction commit/rollback;
- Signal context consistency;
- contour/member writes і clears;
- Signals UI contracts;
- Report Builder UI contract;
- durable membership;
- legacy identity recovery;
- summary provenance/override;
- DOCX validation;
- rich Signal rendering;
- real hyperlinks;
- metadata sanitation.

Live перевірено:

- preview web runtime;
- `/health`;
- `/report`;
- analyst annotation writes;
- Report Builder із чистої корзини;
- legacy report recovery;
- DOCX generation;
- OOXML hyperlinks;
- rendered DOCX pages.

## Відомий відкритий defect: Mamay date quality

Під час live Report Builder QA знайдено окремий data-quality defect Mamay summary.

Для Signal із публікаціями жовтня 2026 року автоматичний summary містив дату:

`2 жовтня 2023 року`

При цьому example publication у тому самому Signal мала дату 03.10.2026.

Після summary provenance fix підтверджено, що Report Builder не створює цю дату і не використовує stale draft snapshot: він коректно рендерить canonical Mamay summary для exact membership.

Отже defect належить до thesis generation / factual validation, а не до Report Builder exporter.

Не виправляти його в DOCX renderer або report resolver.

Наступна окрема задача:

- дослідити date grounding у Mamay summary/theses;
- перевірити input publications і prompt;
- додати bounded factual/date validation тільки після визначення root cause.

Один приклад не є підставою для широкого retuning prompt.

## Межі поточного рішення

Свідомо не додавали:

- server-side persistence report draft;
- окремий report database model;
- Redis/Celery;
- fuzzy Signal recovery;
- автоматичне виправлення AI facts у exporter;
- складний template engine для DOCX.

Поточний slice є мінімальним робочим vertical slice:
аналітик коригує Signal → формує локальний draft → сервер resolve-ить актуальні дані → отримує редагований DOCX.

## Supersedes / relation to previous checkpoints

Цей checkpoint не замінює `chatgpt_34_Signals_Export_and_Mamay_Theses_28_09_2026.md` як опис архітектури Mamay theses.

Він supersedes проміжний робочий стан Analyst Annotations / Report Builder, сформований після 28.09.2026, і фіксує перевірений стан цього vertical slice на 03.10.2026.

При розбіжності current code, DDL, tests і runtime мають пріоритет над цією historical note.
