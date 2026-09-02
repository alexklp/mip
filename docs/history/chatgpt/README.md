# ChatGPT project history

Цей каталог зберігає **інженерну пам'ять ChatGPT по проєкту МІП**: milestone/checkpoint notes, перевірені результати експериментів, негативні результати, rationale рішень, відомі failure modes та поточні переходи між етапами.

Мета — не дублювати Git history або кожну розмову, а зберігати те, що важко відновити лише з коду: **чому рішення було прийнято, що вже виміряно, які гіпотези спростовані та що не треба повторювати без нової причини**.

## Правила

1. Поточний код, DDL, тести, фактичний стан БД/runtime та новіші перевірені вимірювання мають пріоритет над historical notes.
2. Не записувати сюди credentials, токени, ключі, реальні мережеві ідентифікатори або інші чутливі дані.
3. Не створювати note для кожного дрібного commit. Новий checkpoint потрібен, коли є хоча б одна з подій:
   - завершений vertical slice або milestone;
   - перевірене архітектурне/операційне рішення;
   - важливий negative result;
   - новий performance/capacity baseline;
   - incident + підтверджений root cause/fix;
   - зміна наступного етапу робіт.
4. Старі notes не переписувати під нову реальність. Якщо стан змінився — створити новий checkpoint і явно вказати, що він supersedes попередній detail.
5. Якщо висновок перестає залежати від конкретного AI/сесії і стає стабільним проектним знанням, його можна пізніше винести в нейтральні `docs/decisions/`, `docs/research/` або `docs/operations/`.

## Співвідношення з Claude archive

- `docs/history/claude/` — історична engineering memory Claude Project.
- `docs/history/chatgpt/` — historical engineering memory ChatGPT.
- Обидва каталоги — provenance/history, а не автоматично current baseline.
- При конфлікті пріоритет мають фактично перевірений новіший код/DDL/tests/measurements.

## Індекс

| Файл | Зміст |
|---|---|
| `chatgpt_20_MIP_LivePipeline_Claims_Hardening_02_09_2026.md` | Перехід від batch/backlog processing до live `analyze + newest`, hardening claim worker, cron pipeline, виміряний Mamay capacity та наступний крок — contour live/hardening. |
| `chatgpt_21_Repo_Audit_and_Delegation_02_09_2026.md` | Repository hygiene audit, language policy, README corruption finding, AI-autograph check та практична схема делегування задач Claude. |
| `chatgpt_22_Claim_Relation_Candidate_Scaling_02_09_2026.md` | Claim embeddings 100% coverage, масштабування `candidate_version=1`, independent review global-top-N contract, blockwise profiling як reference step та вимірювання перед v2. |
