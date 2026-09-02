# Relation semantic calibration checkpoint — 02.09.2026

## Контекст

Після full-corpus relation-candidate profiling і dual-score experiment було перевірено кілька способів відрізнити textual/claim similarity від реальної shared-referent/event identity.

Цей checkpoint фіксує саме виміряні semantic calibration результати. Він не робить production contract із calibration harness і не змінює downstream DDL/worker semantics.

## Що вже підтверджено

1. Raw claim cosine не є достатнім relation gate. На high-score tail є substantial generic/template hubs; корисні same-fact/same-event пари трапляються вже біля claim cosine ~0.80, а unrelated generic claims можуть мати cosine >0.97.
2. Full-content cosine дає незалежний contextual signal. На 4,115 парах із claim cosine >=0.80 content cosine сильно розтягує хвіст; high claim-score generic pairs часто падають по content score, тоді як відомі корисні пари можуть зберігати вищий content similarity.
3. Content cosine корисний як ranking/filtering feature, але production threshold не заморожено: human-labeled sample недостатній для статистично надійного hard gate.
4. Time/first_seen не використовується як relation eligibility/ranking signal. Поточний corpus timeline змішаний (batch/backfill/manual/live), тому measured time gaps лише descriptive і не мають використовуватися для global window/weight.

## Pairwise relation judge: negative results

### Free-form v3 referent gate

Read-only Mamay calibration на 5 deterministic pairs показав critical false positive:

- Запоріжжя vs Уфа: різні міста/події, але Mamay спочатку повернув `shared_referent_status=confirmed` + `same_fact`.

Причина виявилася не transport/schema failure, а semantic self-justification: validator перевіряв, що evidence quote grounded десь у A+B, але не доводив two-sided shared referent.

### Strict two-sided grounding

Calibration-only prompt вимагав окремі verbatim quotes із A і B та забороняв generic result/action як sufficient referent proof.

Результат:

- Запоріжжя vs Уфа виправився до `not_confirmed`;
- generic cross-location pairs стали conservative;
- але два повідомлення "вибухи в Києві" все ще проходили як `confirmed/same_event` без достатньо специфічного event anchor;
- `Анимаккорд`: `студія під санкціями` vs `гендиректор студії під санкціями` спочатку помилково став `same_fact`; після додаткового rule перейшов у `same_event`.

Висновок: prompt tuning дає локальні покращення, але не усуває self-certification problem.

### Pairwise structured anchors/alignment

Наступний harness вимагав structured anchors (`anchor_type`, `specificity`, normalized value, A/B quotes) і proposition alignment (`subject/predicate/object`).

Negative result:

- для Kyiv explosions Mamay сам класифікував `Київ` як `specific_location/unique` і `вибухи` як `event_detail/specific`, після чого сам же дозволив `confirmed/same_event`;
- для Animakkord pair Mamay сам виставив subject/predicate/object як `same` і повернув `same_fact`, хоча claim propositions відрізняються (`organization under sanctions` vs `organization CEO under sanctions`).

Це показало, що pairwise structured schema сама по собі не вирішує проблему: модель може self-certify specificity/alignment.

## Independent per-claim signatures

Гіпотеза: Mamay не бачить пару. Кожний claim окремо перетворюється у grounded signature; pair comparison робить deterministic Python.

### Signature v1

На 5 pairs / 10 unique claims:

- valid signatures: 5
- failed signatures: 5
- comparable pairs: 0

Failure modes були переважно schema/grounding contract:

- `predicate.quote` reconstructed instead of verbatim;
- unsupported node/anchor kinds;
- invalid canonical keys;
- generic `person:man` намагався стати identity key.

Окремо виявлено semantic bug у v1 contract: generic unnamed person не повинен бути strong identity entity.

### Signature v2

v2 розділив:

- proposition — лише literal claim_text;
- named context anchors — із claim/title/context;
- generic person/entity може мати semantic kind, але `key=null`;
- location не є strong event identity сама по собі;
- quote для subject/predicate/object має бути verbatim із claim_text.

Фактичний run на тих самих 5 pairs / 10 claims:

- valid signatures: 5
- failed signatures: 5
- comparable pairs: 1
- elapsed ~164 s

Єдиний comparable pair — Kyiv explosions:

- обидва signatures: predicate `occur`, anchors `location:kyiv`;
- deterministic comparison правильно повернув `not_confirmed / related`;
- shared strong anchors були відсутні.

Тобто architecture direction показав корисну властивість: broad same-location + same generic predicate більше не підтверджує event identity.

Але overall signature extraction contract лишився brittle: 50% schema-validity недостатньо для практичного relation stage.

Observed v2 failures:

- Mamay повертає morphologically normalized/reconstructed quote замість exact substring (`"опинилася під санкціями"` vs actual word order; `"пострадал"` case mismatch);
- invents unsupported scope enum (`title` замість `claim|context`);
- інколи subject quote відрізняється мовою/формою від claim_text;
- інколи додає generic/non-identity anchor із null key.

## Поточний висновок

1. Основна проблема relation stage — не similarity computation, а referent/event identity under generic linguistic templates.
2. Pairwise LLM judge, навіть structured, схильний до semantic self-justification.
3. Independent signature approach conceptually може зменшувати pairwise confirmation bias, але current schema extraction reliability (5/10 valid у двох послідовних versions) занадто низька. Не робити ще один prompt-only iteration без нового design reason.
4. Не будувати зараз повноцінний OpenIE/NER/entity-linking subsystem лише заради relation stage.
5. На наступному кроці потрібен independent review (Claude packet підготовлено) і/або простіший experiment, який відділить candidate ranking від final relation semantics із мінімумом Mamay calls.
6. Production relation worker поки не запускати: окремо від semantics уже відомі downstream reliability blockers (attempt handling, DB connection lifecycle, attempt-aware event consumer).

## Операційний стан

На час calibration live Mamay claims/contours cron lanes були тимчасово призупинені, щоб не конкурувати за shared `mamay.lock`. Ingestion/basic processing залишались активними. Після завершення calibration live Mamay lanes треба відновити.

## Що не повторювати без нової причини

- не продовжувати безкінечний pairwise prompt tuning на тих самих 5 regression pairs;
- не заморожувати content-score threshold із малої sample;
- не вводити hard time window із поточного mixed-history corpus;
- не будувати HNSW/graph DB/orchestration framework для цієї задачі;
- не трактувати location або generic predicate як event identity;
- не вважати LLM-generated `specificity`/`alignment` достатнім deterministic proof без незалежної перевірки.
