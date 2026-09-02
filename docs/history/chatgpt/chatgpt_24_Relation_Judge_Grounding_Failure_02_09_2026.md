# МІП — Relation Judge grounding failure checkpoint (02.09.2026)

## Контекст

Після dual-score profiling виконано перший read-only Mamay calibration run по 5 relation-candidate pairs: по одній парі з кожної content-cosine смуги. Відбір не використовував time signal. Production `candidate_pairs` / `relation_judgments` не змінювались.

## Фактичний результат calibration v3

- total=5
- valid=5
- invalid=0
- transport_error=0
- elapsed=91.0s
- referents: confirmed=4, not_confirmed=1
- labels: related=1, same_event=2, same_fact=2

## Критичний negative result

`relation_judge v3` все ще може хибно підтверджувати shared referent.

Найчистіший failure:

- claim A: удар по Запоріжжю, постраждав чоловік;
- claim B: атака по Уфі, постраждала одна людина;
- claim cosine ~0.864;
- content cosine ~0.518;
- Mamay: `shared_referent_status=confirmed`, `relation_label=same_fact`.

Це семантично хибно: джерела явно описують різні конкретні події та різні локації.

Додатковий ризиковий приклад:

- два повідомлення про вибухи у Києві;
- claim cosine ~0.956;
- content cosine ~0.627;
- Mamay: `confirmed/same_event`;
- надана grounding-цитата була лише з claim A, а не доказом спільної конкретної події з обох сторін.

## Root cause у поточному app validator

`validate_relation_judgment()` при `shared_referent_status=confirmed` перевіряє лише те, що хоча б одна цитата у `shared_referent_evidence` дослівно трапляється у об'єднаному groundable text claim A + claim B.

Це доводить provenance цитати, але НЕ доводить:

1. що evidence присутнє з обох сторін;
2. що quoted evidence є shared identifying anchor;
3. що модель не використала generic casualty/action phrase як нібито shared referent.

Отже current v3 relation judge не готовий до масштабного live judgment навіть після candidate filtering.

## Наступний bounded fix для calibration

Production prompt/registry/DDL поки не змінювати.

У read-only calibration harness додати:

- calibration-only instruction: `confirmed` потребує дві цитати — одну з A і одну з B;
- різні explicit location/object/person -> `not_confirmed`;
- broad same location + generic event type insufficient;
- app-side strict check: confirmed result invalid, якщо evidence не має хоча б однієї verbatim quote з A і хоча б однієї з B.

Після цього повторити ті самі 5 deterministic pairs. Лише якщо false-positive cases виправляться без втрати очевидних true pairs, розширювати calibration sample.

## Candidate-design implication

Dual-score candidate filtering залишається корисним deterministic recall/discrimination layer, але relation semantic judge є окремим reliability boundary. Не можна компенсувати слабкий shared-referent verifier простим підняттям cosine threshold: high-cosine false relations уже виміряні.
