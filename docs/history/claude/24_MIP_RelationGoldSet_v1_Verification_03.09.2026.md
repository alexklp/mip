# МІП — Relation Gold Set v1: верифікація claude/23

**Дата:** 03.09.2026
**Мета:** claude/23 сам чесно позначив себе як READY-з-застереженням — код
ганяли проти заглушок (`tests/stubs/`), а не проти реального репо, і окремо
пообіцяв секцію UNVERIFIED «на сервері». Ця сесія має доступ до локальних
файлів пакету (`experiments/claim_relations/gold_set/`) і до GitHub-синку
проєкту (`alexklp/mip`, RAG) — тобто можна закрити частину UNVERIFIED
не дожидаючись сервера. Нижче — що саме перевірено, як, і що лишається.

**Важливо:** GitHits (публічний code-search MCP) на `alexklp/mip` не працює —
репо приватне, `NOT_FOUND`. Усе звірення коду нижче йде через `project_search`
(RAG-індекс синку проєкту) — той самий канал, яким користувався claude/23.
Це не read повного файлу з гарантованою побайтовою звіркою, а top-k
семантичний retrieval; для коротких self-contained фрагментів (сигнатура,
DDL, одна функція) цього достатньо, щоб зловити розбіжність, якщо вона є.

---

## 1. Що підтверджено виконанням (не просто прочитанням)

Запущено локально, наживо, без правок коду:

```
python3 -m py_compile gold_set_common.py relation_gold_set_analyze.py \
    relation_gold_set_sampler.py tests/test_gold_set.py tests/test_labeler_ui.py \
    tests/stubs/psycopg.py tests/stubs/pgvector/psycopg.py
# -> чисто, 0 помилок

python3 tests/test_gold_set.py
# -> Ran 63 tests in 0.281s — OK (число і статус збігаються з §9 claude/23)

python3 tests/test_labeler_ui.py
# -> Ran 11 tests in 10.883s — OK, реальний headless Chromium (Playwright
#    вже стоїть у цій сесії), НЕ мок DOM
```

Це закриває реальний ризик: claude/23 міг **описати** тести, які насправді
не проганялись під час його ж сесії (типова галюцинація «зробив X» без
виконання X). Тут — зроблено ще раз, з нуля, у новій сесії, і результат
той самий: 63/63 + 11/11 зелені.

## 2. Що звірено проти реального коду репо (через RAG, не заглушки)

| Твердження claude/23 | Перевірка | Результат |
|---|---|---|
| `sql/014_add_shared_referent_gate.sql` — DB CHECK, `same_fact/same_event/contradiction` заборонені без `shared_referent_status='confirmed'` | прочитано міграцію повністю через RAG | ЗБІГАЄТЬСЯ дослівно, включно з тим, що історичні рядки (status IS NULL) звільнені від gate |
| `ALLOWED_RELATIONS` у gold set дзеркалить production gate | звірено з `sql/014` і з `relation_judgment_worker.py`/`sql/015` (production prompt v3) | ЗБІГАЄТЬСЯ: `referent=no/uncertain -> лише related/unrelated`; `referent=yes -> +same_fact/same_event/contradiction/related`. Gold set додатково забороняє `unrelated` при `referent=yes` — це СТРОГІШЕ за production DB CHECK (production там unrelated технічно не блокує), але узгоджується з production prompt v3 (`sql/015`), де `unrelated` для confirmed-референта практично недосяжний. Не баг, свідоме звуження контракту анотації. |
| `candidate_pair_profile.build_corpus` повертає `skipped_no_group`/`skipped_no_occurrence`, `vectors`, `groups`, `first_seen`, `claim_ids` | прочитано функцію повністю | сигнатура і ключі словника ЗБІГАЮТЬСЯ з тим, що імпортує сампler |
| `embeddings.embedding_model_id = 1` — content embeddings (BGE-M3) | звірено з `sql/006_add_embeddings.sql` | ЗБІГАЄТЬСЯ: `embedding_model_id=1` захардкожено як BGE-M3, той самий id і в `candidate_pair_profile.EMBEDDING_MODEL_ID` |
| `candidate_pair_dual_profile.fetch_content_vector_by_claim` — read-only helper, повертає `{claim_id: vector}` | прочитано функцію | ЗБІГАЄТЬСЯ. **Важлива деталь, якої claude/23 не проговорив явно:** у тому ж модулі є `build_content_matrix`, який **падає RuntimeError**, якщо бодай один claim без content embedding. Сампler цю функцію НЕ викликає — він бере лише `fetch_content_vector_by_claim` (толерантний dict) і сам фільтрує відсутні (`keep_idx`, `missing_content` -> `skipped_no_content_embedding`). Це правильна межа реюзу, але вона неочевидна з тексту claude/23; варто було б явно написати «ми НЕ використовуємо `build_content_matrix`, бо він падає замість фільтрувати» |
| `relation_judgment_worker.fetch_claims_meta` — те саме provenance (`claim_text/evidence_span/title/context_snippet/contour_set/first_seen/display_source`), що бачить Mamay | прочитано функцію повністю + перевірено виклик у самплері (`judge.fetch_claims_meta(conn, claim_ids_needed)`, рядок 498) | ЗБІГАЄТЬСЯ дослівно, включно з форматом `display_source` |
| `validate_relation_judgment` у `relation_judgment_worker.py` — окрема валідована стадія, а не текстова інструкція | прочитано docstring і сигнатуру | ЗБІГАЄТЬСЯ з описом v3/pilot contract 2 в claude/23 |

## 3. Прочитано і перевірено логічно (не лише «тест зелений»)

Повністю прочитано `gold_set_common.py` (767 рядків) рядок за рядком:

- **`assign_stratum`** — пріоритет `same_group -> near-dup -> hub -> claim/content bands` відповідає таблиці §3.2 claude/23 один в один; межі страт half-open (перевірено і тестом `test_boundaries_are_half_open`, і вручну).
- **`plan_hidden_repeats`** (та сама функція, де claude/23 сам знайшов і виправив offset-by-one) — простежено, що `_ready()` звіряється саме з **фактичною** позицією вставки оригіналу (`final_origin_pos`), не з доіндексною. Формально: оскільки кандидати на повтор беруться лише з `ordered[:cutoff]` де `cutoff = n - min_separation`, а перші `start` позицій фінальної послідовності завжди — оригінали (repeat-placement стартує не раніше `start`), то до моменту, коли всі оригінали вичерпані, кожен потенційний кандидат уже точно розміщений. `separation_short`-фолбек лишається, але за цією арифметикою фактично недосяжний — саме так, як стверджує коментар у коді.
- **`fill_allocation`** — кап застосовується ПІД ЧАС набору (не постфактум), decision-critical страти обробляються першими у фіксованому порядку. Відповідає пункту 4 списку «знайдено і виправлено» в claude/23.
- **`wilson_interval` / `max_errors_for_lower_bound`** — стандартна формула Вілсона; монотонність по `errors` (яку використовує ранній `break`) коректна, бо lower bound монотонно зростає з `p`. Числа з таблиці §2.2 (35→0, 40→0, 60→1, 80→2, 100→4) звірені тестом `test_error_budget_matches_design_report`, який фактично проганяється проти цієї ж функції — не просто задекларовані в тексті.
- **`stratum_weights` / `weighted_proportion` / `bootstrap_weighted_ci`** — Horvitz-Thompson + stratified bootstrap реалізовано коректно; exploratory frame (S9) послідовно виключається з обох.

Багів НЕ знайдено. Це друга незалежна перевірка (перша — сам claude/23 своїми тестами), і вона підтверджує той самий висновок, а не просто повторює його.

## 4. Що й досі UNVERIFIED (і чому це не закривається без сервера)

Список із §9 claude/23 залишається чинним майже повністю — тут доступу до
`~/mip`, PostgreSQL чи реального `mip_dev` немає (сесія не залінкована до
комп'ютера, БД тут узагалі немає):

| # | Що | Чому не закрито тут |
|---|---|---|
| 1 | Реальний `import experiments.claim_relations.gold_set.relation_gold_set_sampler` без заглушок | Немає локальної копії репо — імпорт `candidate_pair_profile`/`candidate_pair_dual_profile`/`relation_judgment_worker` як реальних модулів (не через RAG-читання тексту) фізично неможливий тут |
| 2 | `conn.read_only = True` приймається реальним psycopg3 проти реальної БД | Немає PostgreSQL і немає `mip_dev` |
| 3 | Реальні розміри страт (S8 може бути замалою) | Синтетичний corpus (230 claims) із тестів — не реальний корпус; реальні числа невідомі до `--dry-run` на сервері |
| 4 | Час/пам'ять на ~30k claims | Синтетичний прогін — мілісекунди; нічого не каже про масштаб |
| 5 | Повнота content-embeddings у реальних даних | Прочитано МЕХАНІЗМ підрахунку (п.2 вище) і підтверджено, що він коректний і не впаде — але саме ЧИСЛО з реальної БД тут не отримати |
| 6 | `get_code_revision()` у git-каталозі | Немає git-каталогу `~/mip` тут |

Це не «не встиг перевірити» — це структурна межа сесії: перевірка коду і
перевірка проти живих даних/сервера — дві різні речі, і друге тут
недосяжне за визначенням.

## 5. Висновок

READY-вердикт claude/23 **підтверджується**, а не просто повторюється:

- код, який claude/23 показав як «прогнав end-to-end через заглушки», справді
  проганяється, зараз, у новій сесії, з тим самим результатом (63+11 тестів);
- усі цитати реального repo-коду (SQL, сигнатури, ALLOWED_RELATIONS,
  provenance fetch), на які спирається дизайн, звірені й точні;
- знайдено одну неявну (але коректну) деталь реюзу коду, варту одного
  речення в майбутній версії документа (п.2 вище: чому сампler НЕ викликає
  `build_content_matrix`);
- нових багів або розбіжностей між текстом claude/23 і кодом не знайдено.

Секція UNVERIFIED із claude/23 лишається списком «на сервері» без змін по
суті — команди з §10 claude/23 актуальні й можна виконувати як є.
