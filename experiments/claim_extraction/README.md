# claim-extraction eval — vertical slice

Vertical slice для перевірки claim-extraction контракту (`schema_claim_extraction_v1.json`) на живій моделі МІП (поточно — MamayLM-Gemma-3-27B через llama.cpp). Мета: перевірити, чи модель здатна стабільно повертати атомарні, перевірювані claims у заданому JSON-контракті, і виміряти де саме контракт ламається.

## Склад файлів

- `schema_claim_extraction_v1.json` — JSON Schema фінального (нормалізованого) claim-запису.
- `prompt_claim_extractor_v1.txt` — промпт-шаблон для моделі (`<<EVIDENCE_ID>>`/`<<EVIDENCE_TEXT>>` підставляються через `str.replace`, не `.format`).
- `fixture_kupiansk_v1.json` — 8 evidence (E1-E8), купянська наративна нитка + супутні багатотемні зведення.
- `gold_kupiansk_v1.json` — мінімальні semantic-очікування по кожному evidence для людського side-by-side review (regression baseline, не об'єктивна істина).
- `validator.py` — суворий transport-level валідатор контракту. Нічого не «лагодить» мовчки.
- `run_eval.py` — харнесс: fixture → prompt → live model → deterministic adapter → validator → JSON-звіт.
- `test_validator.py`, `test_run_eval.py` — unit tests, без звернення до LLM-сервера.
- `report_kupiansk_v1_run1.json`, `report_kupiansk_v1_run2.json` — історичні raw-звіти реальних прогонів (evidence експерименту, не переписуються — див. Provenance нижче).

## Архітектура: три рівні
cat > ~/mip/experiments/claim_extraction/README.md << 'EOF'
# claim-extraction eval — vertical slice

Vertical slice для перевірки claim-extraction контракту (`schema_claim_extraction_v1.json`) на живій моделі МІП (поточно — MamayLM-Gemma-3-27B через llama.cpp). Мета: перевірити, чи модель здатна стабільно повертати атомарні, перевірювані claims у заданому JSON-контракті, і виміряти де саме контракт ламається.

## Склад файлів

- `schema_claim_extraction_v1.json` — JSON Schema фінального (нормалізованого) claim-запису.
- `prompt_claim_extractor_v1.txt` — промпт-шаблон для моделі (`<<EVIDENCE_ID>>`/`<<EVIDENCE_TEXT>>` підставляються через `str.replace`, не `.format`).
- `fixture_kupiansk_v1.json` — 8 evidence (E1-E8), купянська наративна нитка + супутні багатотемні зведення.
- `gold_kupiansk_v1.json` — мінімальні semantic-очікування по кожному evidence для людського side-by-side review (regression baseline, не об'єктивна істина).
- `validator.py` — суворий transport-level валідатор контракту. Нічого не «лагодить» мовчки.
- `run_eval.py` — харнесс: fixture → prompt → live model → deterministic adapter → validator → JSON-звіт.
- `test_validator.py`, `test_run_eval.py` — unit tests, без звернення до LLM-сервера.
- `report_kupiansk_v1_run1.json`, `report_kupiansk_v1_run2.json` — історичні raw-звіти реальних прогонів (evidence експерименту, не переписуються — див. Provenance нижче).

## Архітектура: три рівні

```
raw model output  →  deterministic adapter  →  validator  →  (окремо) semantic review
```

1. **Raw model output** — те, що модель реально повернула (`message.content` з `/v1/chat/completions`), без жодних змін. Завжди зберігається як є в полі `raw_response` звіту.
2. **Deterministic adapter** (`run_eval.py`) — тільки явно задокументовані детерміновані трансформи:
   - `strip_markdown_fence` — знімає ```` ```json ... ``` ```` ЛИШЕ якщо огорожа покриває весь `raw_response` цілком. Якщо є текст до/після — не чіпає, це вже інша проблема.
   - `resolve_offsets` — `evidence_start`/`evidence_end` НЕ довіряються моделі (емпірично 0/120 правильних на реальному прогоні), обчислюються кодом через `evidence_text.find(evidence_span)`. LLM відповідає лише за `evidence_span` (дослівний текст); Python — за позицію.
     - span відсутній у тексті → `not_found`, offset не підміняється, claim провалюється чесно.
     - span зустрічається >1 раз → `ambiguous`, offset **НЕ** підміняється мовчки першим входженням — claim провалюється чесно (unresolved), НЕ repair.
     - span унікальний → `resolved`, offset підставляється.
3. **`validator.py`** — суворий transport-level контроль вже нормалізованого (після adapter) тексту: JSON-структура, обов'язкові/заборонені поля, `epistemic_status` enum, рядок `"null"` замість JSON `null`, дублікати `claim_local_id`, точна відповідність `evidence_text[start:end] == evidence_span`. Не виконує silent repair — невалідний рядок падає з чіткою причиною, а не «лагодиться».
4. **Semantic review проти `gold_kupiansk_v1.json`** — окремий крок, НЕ частина transport-валідатора. Виконується людиною (за потреби — асистовано) по side-by-side звіту; результат — не PASS/FAIL одним числом, а окремі осі (semantic_coverage / atomicity / epistemic_modality / attribution / claim_time / evidence_lineage).

## Запуск

### Unit tests (без LLM-сервера)

```bash
cd ~/mip/experiments/claim_extraction
python3 -m unittest test_validator test_run_eval -v
```

### Dry-run (без звернення до моделі — тільки перевірка fixture/prompt)

```bash
python3 run_eval.py --dry-run
```

### Live eval (потребує живого llama-server, за замовчуванням `http://127.0.0.1:8080`)

```bash
python3 run_eval.py --out report_<name>.json
```

Опції: `--endpoint`, `--model`, `--timeout` (default 600s), `--max-tokens` (default 8192), `--fixture`, `--prompt`.

## Output artifacts

`run_eval.py` пише один JSON-звіт (`--out`, за замовчуванням `report_<epoch>.json`) з полями:

- `summary` — агрегати: `n_total`, `n_valid`, `n_invalid`, `pass_rate`, `n_fence_stripped`.
- `results[]` — по кожному evidence: `raw_response` (незмінний), `fence_stripped`, `offset_stats` (`resolved`/`not_found`/`ambiguous`), `finish_reason`, `completion_tokens`, `latency_sec`, `valid`, `claim_count`, `errors[]`, або `transport_error` якщо HTTP/timeout впав (обробка решти evidence не зупиняється).

`pass_rate` — це PASS ЛИШЕ transport/adapter контракту, не semantic coverage. Semantic review — окремий документ/крок, тут не рахується.

## Provenance: `report_kupiansk_v1_run2.json`

Отриманий ДО фінального уточнення контракту 20.08 — на момент цього прогону промпт ще явно просив модель саму рахувати `evidence_start`/`evidence_end`. Це історичний live evidence і основа для findings-документа, а НЕ прогон на фінальному frozen prompt/schema — offset-поля в звіті пересчитані заднім числом adapter'ом (`resolve_offsets`) при повторній офлайн-обробці сирих відповідей, а не отримані з "чистого" промпту, який зараз більше не просить модель про offset. Для end-to-end reproducibility поточного (frozen) контракту виконано окремий прогін — `report_kupiansk_v1_run3.json` (див. нижче).

## Provenance: `report_kupiansk_v1_run3.json`
Прогін на вже зафіксованому (після 20.08) промпті/schema/harness: модель НЕ просить рахувати `evidence_start`/`evidence_end`, offset тільки з adapter. Без retry окремих evidence і без правок промпту після перегляду проміжних результатів — результат зафіксовано як є.

147 claims / 8 evidence сумарно: `resolved` 132/147, `not_found` 15/147, `ambiguous` 0/147. `normalized_contract_valid` по evidence — 5/8 (E1, E4, E6, E7, E8 OK; E2, E3, E5 FAIL). Усі три FAIL — виключно `not_found` (span, якого дослівно немає в тексті), жодного `ambiguous`. Це коректна робота adapter/validator (unresolved span → чесний FAIL), не регресія коду.

Спостереження без перевіреної причини: `not_found`-rate вищий, ніж на run2 (15/147 = 10.2% проти 1/120 = 0.8%), і claim count на evidence теж вищий там, де є пряме порівняння (E2 14→27, E4 9→12, E5 47→53, E8 21→26). Гіпотеза — без задачі рахувати offset модель дрібніше декомпозує claims і частіше перефразовує замість дослівного цитування; не перевірялось, не блокує.

`report_kupiansk_v1_run3.json` — окремий artifact, `report_kupiansk_v1_run2.json` не перезаписаний.

## Відомі обмеження поточного сетапу (MamayLM + llama.cpp, спостереження на конкретній конфігурації — НЕ узагальнення на всі LLM/сервери)

- Без явного `max_tokens` у запиті сервер повертав порожній `content` — причина не встановлена напевно, обхід: `max_tokens` завжди явний.
- Модель систематично (8/8 на реальному прогоні) обгортає JSON у markdown-огорожу попри пряму заборону в промпті.
- `response_format: {"type": "json_object"}` у протестованому запиті на цьому білді llama-server + кастомному Hermes-tool-aware jinja-шаблоні не змінив результат (побайтово ідентична відповідь із цим параметром і без нього). Це спостереження на конкретній конфігурації, не загальний висновок про механізм `response_format`.
- Character offsets, порахованi моделлю — 0/120 правильних на реальному прогоні; `evidence_span` при цьому дослівно точний у 119/120 (99.2%).
