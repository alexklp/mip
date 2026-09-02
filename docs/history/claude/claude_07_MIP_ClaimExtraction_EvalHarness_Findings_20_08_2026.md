
# МІП — Claim-extraction eval harness: результати live-прогону на MamayLM (DRAFT)

**Стан на:** 20.08.2026
**Статус документа:** DRAFT findings, не фінальна архітектура. Semantic review нижче — попереднє прочитання, не завершений людський вердикт.
**Контекст:** продовження `06_MIP_Embeddings_ClaimExtraction_Handoff_19.08.2026.md`. Vertical slice: `~/mip/experiments/claim_extraction/` — `fixture_kupiansk_v1.json`, `gold_kupiansk_v1.json`, `schema_claim_extraction_v1.json`, `prompt_claim_extractor_v1.txt`, `prompt_claim_extractor_v2.txt`, `validator.py`, `run_eval.py`, `test_validator.py`, `test_run_eval.py`, `README.md`, `report_kupiansk_v1_run2.json` (historical, v1 prompt), `report_kupiansk_v1_run3.json` (frozen-contract run, v1 prompt), `report_kupiansk_v1_run4.json` (frozen run, v2 prompt).

**Важливо:** одну агрегатну метрику ("X/8 valid") тут навмисно НЕ використовуємо — вона змішує три різні рівні (транспорт / детермінований adapter / семантика), кожен зі своєю природою помилок.

## 1. Живий сервер

`http://127.0.0.1:8080/v1/chat/completions`, `MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf`, PID 30775 (піднятий 19.08 через `nohup ... &`, переживає обриви SSH). Робочий launch — варіант з порту 8080 (`-ngl all -c 65536 -fa on`); варіант з `03_MIP_Server_State` (порт 8000, `--device Vulkan1`) конфліктує з уже зайнятою VRAM.

## 2. Rівень A — RAW Mamay output (без жодних трансформацій)

Виміряно на `report_kupiansk_v1_run2.json`, 8 evidence, 120 claims сумарно. **Provenance-застереження:** цей прогон отриманий ДО фінального уточнення контракту 20.08 — на момент прогону промпт ще явно просив модель саму рахувати `evidence_start`/`evidence_end` (зараз — ні, див. розділ 3 та README). Це історичний evidence, не прогон на фінальному frozen prompt/schema.

| метрика | результат |
|---|---|
| HTTP completed | 8/8 |
| `finish_reason == "stop"` | 8/8 |
| Markdown-огорожа (```` ```json ... ``` ````) присутня | 8/8 |
| `json.loads(raw_response)` успішний (без адаптера) | **0/8** — 100% фейл через огорожу |
| `evidence_span` дослівно присутній у input | 119/120 claims (99.2%) |
| model-provided `evidence_start`/`evidence_end` правильні | **0/120 (0%)** |

Raw response ніколи не змінюється і не переписується — зберігається в `report_kupiansk_v1_run2.json` як є.

## 3. Rівень B — Deterministic adapter (`run_eval.py`)

Дозволені трансформи, всі явно задокументовані в коді, нічого не ховається:

1. `strip_markdown_fence` — знімає огорожу ЛИШЕ якщо вона покриває весь `raw_response` цілком.
2. `json.loads` нормалізованого тексту.
3. `resolve_offsets` — `evidence_start`/`evidence_end` НЕ довіряються моделі, обчислюються кодом через `evidence_text.find(evidence_span)`.
   - `span.count() == 0` → `not_found`, offset не підміняється, claim провалюється чесно.
   - `span.count() > 1` → `ambiguous`, offset **НЕ підміняється** (з 20.08 — раніше мовчки бралось перше входження, це виправлено), claim провалюється чесно.
   - `span.count() == 1` → `resolved`, offset підставляється.
4. `validator.py` — після внесених 20.08 contract fixes (рядок `"null"` тепер відхиляється) лишається строгим і не виконує silent repair: валідує вже нормалізований (після adapter) текст, невалідне падає з чіткою причиною, а не «лагодиться» мовчки.

Метрики нижче — на `report_kupiansk_v1_run2.json` (промпт ще просив offset від моделі, historical):

| метрика (сумарно 120 claims / 8 evidence) | результат |
|---|---|
| `fence_stripped` | 8/8 evidence |
| `normalized_json_valid` (парситься після зняття огорожі) | 8/8 evidence |
| `offsets_derived` (span unique, offset підставлено) | 119/120 claims |
| `span_unique` (рівно 1 входження) | 119/120 claims |
| `not_found` (span відсутній взагалі) | 1/120 claims (E1/C3) |
| `ambiguous` (span >1 входження) | 0/120 claims |
| `normalized_contract_valid` (validator.py PASS) — по evidence | **7/8** (E1 FAIL — саме через C3) |
| `normalized_contract_valid` — по claims | 119/120 |

E1/C3 — єдиний реальний provalений claim: модель, розбиваючи одне складносурядне речення на два claims (логістика + обстріли), для другого claim зібрала span, якого дослівно немає в тексті (випустила середню частину речення). Це не проблема offset-лічби, а рідкісний glitch копіювання. **Retry НЕ проводився навмисно** — це чесний зафіксований FAIL evidence-lineage, не cherry-picking.

### 3.1. Frozen-contract reproducibility run (v1 prompt) — `report_kupiansk_v1_run3.json`

Прогін на вже зафіксованому (після 20.08 правок) промпті/schema/harness: модель більше НЕ просить рахувати `evidence_start`/`evidence_end` — вони тільки з adapter. Мета — не "покращити результат", а перевірити відтворюваність поточного контракту end-to-end. **Без retry окремих evidence, без правок промпту після перегляду проміжних результатів** — результат фіксується як є.

| метрика (сумарно 147 claims / 8 evidence) | результат |
|---|---|
| `resolved` | 132/147 |
| `not_found` | 15/147 |
| `ambiguous` | 0/147 |
| `normalized_contract_valid` — по evidence | **5/8** (E1, E4, E6, E7, E8 OK; E2, E3, E5 FAIL) |

По evidence: E1 3/3 resolved, E2 15/27 (12 not_found), E3 4/5 (1 not_found), E4 12/12, E5 51/53 (2 not_found), E6 2/2, E7 19/19, E8 26/26.

Усі три FAIL (E2/E3/E5) — виключно `not_found`: adapter коректно відмовляється підставляти offset для span, якого дослівно немає в evidence_text, і чесно валить claim, замість мовчки що-небудь вигадувати. Жодного `ambiguous`-випадку немає.

**Спостереження (дельта проти run2):** `not_found`-rate помітно вищий — 15/147 (10.2%) проти 1/120 (0.8%). Claim count на evidence теж вищий там, де є пряме порівняння: E2 14→27, E4 9→12, E5 47→53, E8 21→26. Гіпотеза (не перевірялась): без задачі рахувати offset модель почала дрібніше декомпозувати claims і, як побічний ефект, частіше перефразовувати замість дослівного цитування.

`report_kupiansk_v1_run3.json` — окремий artifact, `report_kupiansk_v1_run2.json` не перезаписувався.

### 3.2. Prompt v2 frozen run — `report_kupiansk_v1_run4.json`

`prompt_claim_extractor_v2.txt` = v1 + два нові правила (JSON schema/поля не змінені):
1. **Atomicity застосовується до `claim_text`, НЕ до `evidence_span`** — evidence_span завжди точна неперервна підрядка, не реконструюється й не склеюється з розрізнених частин речення; якщо короткий span не покриває atomic claim повністю — брати ширший точний span, аж до цілого речення.
2. **Qualifier inheritance** — при decomposition не втрачати attribution/epistemic modality/явний час, які стосуються дочірнього claim.

Прогін frozen: без retry окремих evidence, без правок промпту після перегляду результату.

| метрика (сумарно 122 claims / 8 evidence) | результат |
|---|---|
| `resolved` | 110/122 |
| `not_found` | 12/122 |
| `ambiguous` | 0/122 |
| `normalized_contract_valid` — по evidence | **6/8** (E1, E3, E4, E6, E7, E8 OK; E2, E5 FAIL) |

По evidence: E1 3/3, E2 10/19 (9 not_found), E3 5/5, E4 11/11, E5 40/43 (3 not_found), E6 2/2, E7 19/19, E8 20/20.

Проти run3: claim count впав (E2 27→19, E4 12→11, E5 53→43, E8 26→20) — узгоджується з "widen span up to full sentence" з правила 1. E3 закрилась повністю (not_found 1→0). E2 і E5 лишаються FAIL, обидва виключно через `not_found` (не ambiguous).

**Regression guard-перевірка (semantic, на claim_text/attribution_text/epistemic_status з нормалізованого raw_response, звірено з fixture-текстом де потрібно):**

| guard | результат |
|---|---|
| E2: `об изменениях в ЛБС не сообщается` → `not_reported` | **PASS** |
| E3: attribution Трегубову і `наразі` не втрачаються | **PASS** — перевірено проти джерела: перше речення evidence має явну тире-атрибуцію "-Трегубов" (C1/C2 тримають attr+time), друге речення атрибуції в тексті не має взагалі, тому C3-C5 коректно йдуть без attr/time — це не regression, а правильне застосування "лише explicit-атрибуція" |
| E4: `наразі неефективно` / `поки що неефективно` вилучається | **PASS** — закритий давній semantic_coverage miss (документований у run2 semantic review, розділ 4) |
| E5: `по даним з місць` не перетворюється на asserted-факт | **PASS** за раніше встановленим критерієм (hedge збережено в attribution_text; сам ярлик epistemic_status лишається "asserted", не "reported" — нюанс, не блокує) |
| E5: рух до Соболівки і вздовж західного берега — окремі claims | **FAIL** — досі один злитий claim (C24 в run4, аналог C26 в run2/run3), той самий atomicity-баг, правила v2 його не закрили |
| E8: "дві наступальні дії ... у бік Новоосинового та Курилівки" не дробити штучно | **PASS** — лишився одним claim, як і мало бути |

5/6 guards закрито, 1 (E5 merge) стабільно не закривається між run2→run3→run4.

`report_kupiansk_v1_run4.json` — окремий artifact, run2/run3 не перезаписані.

### 3.3. Root-cause аналіз: 12/12 `not_found` в run4 (E2 + E5)

Посимвольна звірка кожного `not_found` claim'а (`difflib.SequenceMatcher.find_longest_match`) проти оригінального fixture-тексту показала ДВА окремих механізми, не один:

**Тип 1 — trailing-punct-only, 8/12** (E2: C2, C7, C9, C13, C15; E5: C3, C7, C11): span збігається з оригіналом дослівно, крім ОСТАННЬОГО символу — модель ставить крапку в кінці claim'а там, де в оригіналі стоїть кома чи сполучник (речення продовжується). Приклад C2: джерело `"...Белогорье, а також..."`, span моделі `"...Белогорье."`. Побічний ефект правильного по суті decomposition одного compound-речення на кілька atomic claims — модель "закриває" кожен фрагмент крапкою для граматичної завершеності claim_text і копіює цю сфабриковану крапку в evidence_span.

**Тип 2 — reconstructed/spliced span, 4/12** (E2: C3, C10, C14, C16): span НЕ є неперервною підрядкою джерела взагалі — це префікс одного фрагмента списку, приклеєний до суфіксу НЕсуміжного фрагмента, із вирізаною серединою. Приклад C3: джерело `"...продвигаются севернее населённого пункта Белогорье, а также юго-западнее Новоданиловки."`, span моделі `"На правом фланге... продвигаются юго-западнее Новоданиловки."` — префікс речення приклеєний напряму до останнього пункту переліку, пропускаючи середній пункт. Аналогічно C10, C14, C16 — усі є списками 2-3 однорідних пунктів через кому/"а також", і для НЕ-першого пункту модель зшиває спільний префікс речення з потрібним хвостом замість того, щоб взяти реальний ширший неперервний фрагмент (як і мало б бути за правилом 1).

**Це прямо суперечить explicit-забороні з правила 1 v2** ("ніколи не реконструюй і не склеюй span з розрізнених частин речення") — заборона є в тексті промпту, але 4/12 (33% від not_found, 3.3% від усіх 122 claims) її порушують. Правило 1 в частині "widen span up to full sentence" спрацювало для запобігання деяким випадкам (E3 повністю закрилась), але не для всіх list-подібних речень.

Висновок для можливого v3 (НЕ пишеться зараз, за прямим рішенням користувача): проблема не в самій ідеї decomposition list-речень, а в тому, що правило 1 не дає моделі явної інструкції "якщо не можеш знайти один неперервний span, що покриває тільки цей пункт списку — візьми ширший span, що покриває весь перелік цілком, а не зшивай префікс+суфікс". Trailing-punct (тип 1) — менш критична проблема, можливо навіть прийнятна для м'якшого порівняння (напр. ignore trailing punctuation при `resolve_offsets`), але це вже зміна adapter-логіки, не промпту, і теж не вирішується зараз.

## 4. Rівень C — Semantic review проти `gold_kupiansk_v1.json` (попереднє прочитання, не фінальний вердикт)

Осі: `semantic_coverage` / `atomicity` / `epistemic_modality` / `attribution` / `claim_time` / `evidence_lineage`.

**Застереження:** semantic review нижче виконаний на `report_kupiansk_v1_run2.json` (historical, v1 prompt). Для run3/run4 повний semantic review проти gold не проводився — замість цього для run4 зроблено вузьку regression guard-перевірку та root-cause аналіз not_found (розділи 3.2-3.3).

**E1 (ТСН)**
- semantic_coverage: **PASS** — усі 4 gold-пункти (катастрофічна ситуація, складна логістика, постійні удари, "місяцями") присутні.
- evidence_lineage: **FAIL** — C3 span glitch (див. розділ 3).
- atomicity / epistemic_modality / attribution / claim_time: OK (opinion/asserted коректні, атрибуція "оглядач" присутня, "місяцями" збережено).

**E2 (WarGonzo, scope: лише купянське речення)**
- epistemic_modality: **PASS** — головний regression guard: "об изменениях в ЛБС не сообщається" збережено як `not_reported`, НЕ перетворилось на `asserted`. Підтверджено повторно на run4.

**E3 (Цензор.НЕТ)**
- semantic_coverage: **PASS** — усі 5 пунктів (пошук шляхів, Раївка, мета, "наразі безуспішно").
- atomicity: **PASS, покращення** — route/pressure/goal/result розділено на 5 окремих atomic claims.
- attribution: OK (Трегубов). claim_time: OK ("наразі").

**E4 (Еспресо)**
- semantic_coverage: **FAIL на run2** — тезис "наразі неефективно" відсутній серед C1-C9. **Закрито на run4** (C5, div. 3.2) — прямий фікс v2-промптом.
- attribution: OK (Трегубов присутній по тексту, не з source-метаданих).

**E5 (Олег Царев, scope: купянський уривок)**
- semantic_coverage: **PASS** — контроль міста "за даними з місць" (C25) + рух до Соболівки + західний берег Осколу (обидва в C26).
- atomicity: **ISSUE, persist через run2→run3→run4** — рух до Соболівки/вздовж західного берега + purpose clause стабільно зливаються в один claim. v2-правила (atomicity applies to claim_text) це не виправили.
- epistemic_modality: PASS (оговорка "по даним з місць" збережена як attribution).

**E6 (Colonelcassad)**
- semantic_coverage: **PASS** — заняття східної частини Ковшарівки, дослівно.

**E7 (WarGonzo)**
- semantic_coverage: **PASS** — той самий факт (Ковшарівка), дослівно.

**E8 (Еспресо)**
- semantic_coverage: **PASS** — обидві наступальні дії (Новоосинове + Курилівка) в одному claim, без вигаданого зв'язку з Раївкою.
- atomicity: **ВІДКРИТЕ ПИТАННЯ закрито як "не потрібно дробити"** — regression guard на run4 підтвердив: один claim лишається правильним рішенням, не змінюємо.

## 5. Відомі failure modes і як вони закриті

| проблема | статус |
|---|---|
| Відсутній `max_tokens` → порожній content | Виправлено (`max_tokens=8192` в `run_eval.py`) |
| Модель ігнорує заборону на markdown-огорожу (8/8 на run2/run3/run4) | Не виправлено на рівні моделі; закрито на рівні adapter (`strip_markdown_fence`) |
| `response_format: {"type": "json_object"}` не змінив результат на цьому білді | Спостереження на конкретній конфігурації, не узагальнюємо |
| Model-generated character offsets — 0/120 правильних (run2) | Offset обчислюється adapter-кодом. На run3/run4 (без офсетів від моделі) `not_found`-rate 15/147 і 12/122 — root-cause розібраний (розділ 3.3): 8/12 trailing-punct, 4/12 spliced/reconstructed span |
| Ambiguous span мовчки резолвився по першому входженню | Виправлено 20.08. На run3/run4 ambiguous-випадків 0 |
| `"null"` рядок замість JSON `null` не відхилявся validator'ом | Виправлено 20.08 |
| E5: рух до Соболівки/західний берег зливаються в один claim | **НЕ закрито** v2-промптом (rule 1 "atomicity applies to claim_text" мало б це виправити, але не спрацювало) — кандидат для root-cause аналізу перед v3 |
| E4: пропущений тезис "наразі неефективно" | Закрито v2-промптом (qualifier inheritance + explicit rule зробили своє) |
| E2/E5: 4/12 not_found — span зшитий з несуміжних фрагментів списку | Прямо порушує explicit-заборону правила 1 v2, попри наявність цієї заборони в тексті промпту — root-cause розібраний (3.3), фікс не застосовувався (frozen run, за домовленістю) |

## 6. Що ще НЕ зроблено / відкрито

- Semantic review в розділі 4 — це AI-асистоване попереднє прочитання run2, НЕ фінальний людський sign-off. Для run3/run4 — тільки regression guards + root-cause not_found, не повний review.
- E1/C3 залишається чесним FAIL, без retry.
- `report_kupiansk_v1_run2.json` — historical evidence, v1 prompt зі старим offset-контрактом.
- `report_kupiansk_v1_run3.json` — frozen-contract run на v1 prompt, 5/8 valid, всі FAIL через not_found.
- `report_kupiansk_v1_run4.json` — frozen run на v2 prompt (`prompt_claim_extractor_v2.txt`), 6/8 valid, 5/6 regression guards PASS, E5-merge guard FAIL. Root-cause 12/12 not_found розібраний (розділ 3.3): 8 trailing-punct-only (незначний), 4 spliced/reconstructed span (прямо порушує explicit rule 1 заборону).
- v3 промпту наразі НЕ пишемо — явне рішення користувача. Root-cause аналіз завершено (E2/E5 not_found — розділ 3.3; E5-merge — розділ 3.2/4), рішення про v3 і його зміст — за користувачем/GPT.
- `nvidia-smi --query-gpu` на цій машині не працює — поза scope.
- Unit tests — 27/27 PASS, без звернення до живого LLM (перевірено і після додавання v2 prompt).
- README.md — оновлено provenance-секціями по кожному run (run2/run3/run4) паралельно з цим документом.
- Git: `fc2b305` — baseline vertical slice (v1 prompt, run2, run3, harness, tests, README). `4706b98` — v2 prompt + frozen run4 (guard table, README/findings provenance). Root-cause аналіз (розділ 3.3) — read-only, файли на диску не змінювались, коміту не потребує (лише findings-документ).
