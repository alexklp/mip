# LLM Content Contour Classification Pilot v1

Технічний pilot класифікації `content_items` за стратегічними
моніторинговими контурами МІП за допомогою MamayLM.

## Scope

Класифікація multi-label за чотирма контурами:

1. `dshv_objects` — Об'єкти ДШВ ЗСУ
2. `world_context` — Світовий контекст
3. `national_context` — Загальнодержавний контекст
4. `enemy_media` — Моніторинг ворожих медіа

Facet-класифікація виконується лише в межах позитивного contour.

## Model / runtime

- MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M
- llama.cpp OpenAI-compatible `/v1/chat/completions`
- deterministic evaluation sampling:
  - `seed=42`
  - `top_k=1`
  - `samplers=["top_k"]`

Можлива незначна текстова nondeterminism у `reason`, але в перевірених
контрольних кейсах це не змінювало scoring-рішення.

## C4 provenance gate

`enemy_media` має окрему deterministic eligibility-умову.

Матеріал може отримати C4 лише якщо хоча б один його `item_occurrence`
походить із джерела, для якого:

~~sql
sources.contour_id = 4
~~

Відсутність такого provenance примусово дає effective `C4=no`.

Наявність такого provenance НЕ означає автоматичний `C4=yes`:
зміст усе одно класифікує LLM.

Raw model output зберігається окремо від effective result після gate.

## Evaluation set

- 16 fixture items
- 13 scoreable contour cases
- 3 `review_required` cases
- 8 facet-scoreable cases

## Final result

Effective production-like scoring:

- JSON/schema validity: **16/16**
- contour exact: **12/13 (92.3%)**
- facet exact: **8/8 (100%)**

Єдиний відомий scoreable contour miss:

- `P05` — false negative C4 для дуже короткого enemy-source headline:
  `В России высказались о нападении на страны НАТО`

Цей кейс не використано як підставу для подальшого prompt overfitting.

## Artifacts

- `fixture_v1.json` — pilot fixture
- `gold_v1.json` — фінальний human-reviewed gold
- `prompt_v1.txt` — baseline prompt
- `run_eval.py` — deterministic evaluation runner з C4 provenance gate
- `run_2026-09-01_part2.json` — результати P05-P16 фінального split-run

Фінальні метрики отримані об'єднанням P01-P04 із першого запуску та
P05-P16 із другого без повторного inference уже успішно перевірених кейсів.
