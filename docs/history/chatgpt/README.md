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
| `chatgpt_23_Relation_Candidate_Profile_02_09_2026.md` | Фактичний профіль 188.9M cross-source-group пар: blockwise exact performance, cosine tail, degree/coverage, time-window distribution, generic hubs та next step — stratified semantic sampling. |
| `chatgpt_24_Relation_Judge_Grounding_Failure_02_09_2026.md` | Calibration negative result: relation_judge v3 falsely confirmed unrelated Zaporizhzhia/Ufa events; validator only proved quote provenance, not two-sided shared referent; strict calibration-only grounding retest required before scaling. |
| `chatgpt_25_Relation_Semantic_Calibration_02_09_2026.md` | Claim/content dual-score findings, repeated pairwise relation self-certification failures, independent-signature v1/v2 results, current stop condition for prompt-only tuning, and next-step constraints. |
| `chatgpt_26_Relation_Adversarial_Review_Decision_02_09_2026.md` | Independent adversarial review reconciled with measurements: stop signature/prompt expansion, preserve caveats on content cosine and location conflict, and switch next experiment to a human-labeled gold set including below-0.80 recall strata. |
| `chatgpt_27_Content_Segmentation_Persistence_Pilot_04_09_2026.md` | Content Segmentation v1 persistence vertical slice: DDL/registry, minimal writer, atomic segment persistence, live single/multi-section verification та idempotency PASS. |
