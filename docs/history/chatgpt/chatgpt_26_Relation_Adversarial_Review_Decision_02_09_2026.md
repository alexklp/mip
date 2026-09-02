# Relation stage — adversarial review decision (02.09.2026)

## Контекст

Після серії read-only калібрувань relation candidate / relation judgment отримано independent adversarial review від Claude. Review не змінював код і був спрямований на перевірку того, чи не переускладнюється relation stage через pairwise structured prompts та independent claim signatures.

## Що прийнято з review

1. **Claim similarity ≠ referent/event identity.** Це вже було підтверджено високими cosine для generic/template claims з різних подій.
2. **Порог `claim_cosine >= 0.80` не має виміряного recall.** Нижче 0.80 систематична human-labeled перевірка ще не проводилась, тому поріг не можна вважати production-safe.
3. **Full-content cosine — корисний measured signal, але його semantic utility ще не статистично відкалібрована.** Розподіл на 4,115 pairs виміряно, але true/false quality оцінювалась лише на малій ручній sample.
4. **Pairwise structured LLM не усунув self-certification.** Mamay може сам призначити `specificity`, `alignment` і тим самим обґрунтувати хибний `confirmed`.
5. **Independent signature v1/v2 поки не довів semantic hypothesis.** Обидва прогони дали лише 5/10 valid signatures; v2 мав 1/5 comparable pair. Підхід стає схожим на передчасну OpenIE/NER/entity-linking підсистему всередині relation stage.
6. **Наступний high-value experiment має бути human gold set, а не ще один prompt/signature contract.** Це дозволить виміряти retrieval recall і реальну utility дешевих сигналів без Mamay inference.

## Важливі корекції до review

Review місцями формулює висновки занадто жорстко:

- Ідентифікуючі токени не завжди відсутні в `claim_text`: у corpus є claims з `Київ`, `Денис Лямін`, `Анімаккорд` тощо. Правильніше: **для значної частини generic atomic claims identity-сигнал слабкий або винесений у title/context**, тому claim embedding часто кодує proposition frame краще, ніж event identity.
- `location-conflict` корисний як **strong negative signal**, але не як universal deterministic positive identity gate. Контекст може містити кілька локацій, джерело/ціль/наслідки, тому naive string intersection не можна автоматично робити production rule без gold-set measurement.
- Deterministic code може добре **відкидати** частину очевидних false pairs (hub/generic/conflicting context), але позитивне підтвердження referent identity зазвичай складніше. Не слід вважати, що simple code повністю замінить semantic judgment до вимірювання.

## Поточне рішення

### STOP

- Не продовжувати prompt tuning на тих самих 5 regression pairs.
- Не продовжувати signature v3 / ontology expansion.
- Не заморожувати claim/content thresholds, degree cutoffs або time window.
- Не запускати production relation worker до окремого reliability hardening (attempt semantics + DB lifecycle + event consumer compatibility).

### NEXT

Побудувати read-only deterministic **gold-set sampler** без Mamay.

Цільова sample повинна покривати:

- 2D strata `claim_cosine × content_cosine`;
- hub / non-hub;
- окрему retrieval-recall страту **нижче claim cosine 0.80**, особливо `0.60–0.80` при підвищеному content cosine;
- fixed seed і stable pair ids;
- достатній контекст для human labeling: claim A/B, title, source/context, claim score, content score, degree/hub indicators.

Human labels мінімально:

- `relation_label`: same_fact / same_event / contradiction / related / unrelated;
- `same_referent`: yes / no / uncertain;
- `obvious_context_conflict`: yes / no / uncertain.

Після цього виміряти окремо:

- recall поточного `claim>=0.80` retrieval;
- precision/utility content cosine;
- generic/hub suppression;
- чи існує безпечна deterministic reject-zone;
- яку частину pairs реально треба віддавати Mamay.

## Operational state

Тимчасова пауза live Mamay завершена; cron для claims/contours відновлено. Relation semantics work повертається в read-only / offline calibration режим до появи gold set.
