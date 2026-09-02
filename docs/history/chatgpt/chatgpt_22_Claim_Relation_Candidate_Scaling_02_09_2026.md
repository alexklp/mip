# МІП — Claim relation candidate scaling checkpoint (02.09.2026)

## Контекст

Після переведення ingestion/routing/claim extraction/contour classification у live-режим наступним deterministic етапом став `claim embeddings -> candidate_pairs`.

Цей checkpoint фіксує виміряний стан corpus, масштабування `candidate_version=1` та зміну постановки задачі після незалежного review Claude/Fable. Це historical engineering note; поточний код/DDL/вимірювання мають пріоритет.

## Фактично виміряний стан

Claim embeddings повністю догнані:

- `claims_total = 29912`
- `claim_embeddings_total = 29912`
- `claim_embeddings_missing = 0`
- фінальний catch-up: `5218` claims за `6m35s` wall time.

Поточний source-provenance `contour_set` для claims з embeddings:

- `{4}`: `20962`
- `{3}`: `8800`
- `{2}`: `150`

Отже current v1 cross-group space має приблизно `188.9M` eligible cross-contour pairs.

Новіший strategic layer `content_contour_assignments` покриває лише невелику частину цього corpus:

- `{2}`: 481 claims
- `{1}`: 50
- `{3}`: 41
- `{4}`: 19
- `{3,4}`: 1

Разом приблизно 592 claims (~2%). Тому strategic contour assignments зараз не можуть замінити source-provenance axis у чинному v1 candidate contract.

## Поточний `candidate_version=1`

`candidate_pair_generator.py` зараз:

1. читає весь corpus `claim_embeddings`;
2. будує повну `N x N` float32 cosine matrix;
3. Python-циклом перебирає всі `i < j`;
4. відкидає same-run та пари з перетином source-provenance contour sets;
5. materialize-ить усі eligible tuples у Python list;
6. глобально сортує весь list за score;
7. бере runtime `--top-n` і append-only пише в `candidate_pairs`.

На pilot corpus це було прийнятно. На ~30k claims головна проблема вже не сама ~3.6 GB similarity matrix, а materialization/sort близько 189M Python tuples і ~447M Python pair iterations.

У БД залишаються 30 historical `candidate_version=1` pairs:

- min score `0.86768925`
- max score `0.99677855`
- avg score `0.90778049`

Вони корисні як historical sanity/reference data, але не є ground truth.

## Важливий semantic висновок

Початкова ідея була просто масштабувати exact global top-N через blockwise NumPy.

Незалежний review Claude/Fable правильно підсвітив більш фундаментальну проблему: `candidate_version=1` походить з read-only exploration (`claim_candidate_scan.py`) і як live relation candidate queue має слабкий coverage contract.

Global top-N оптимізує глобальну щільність similarity, а downstream `event_candidate_builder` потребує per-anchor околиці. Append-only global top-N у зростаючому corpus також структурно дає `inserted=0`, поки нова пара не потрапить у глобальний top-N; нові claims без дуже високого score можуть ніколи не отримати relation edge.

Тому v1 треба розглядати як pilot/reference contract, а не автоматично як production live candidate strategy.

## Що підтверджено, а що лишається гіпотезою

Підтверджено:

- full-matrix + full Python materialization більше не є практичним execution path;
- blockwise exact top-N математично може зберегти v1 selection без ANN, якщо блоки є disjoint partition і використовується єдиний deterministic total order;
- current v1 не має явного deterministic tie-break (`fetch_claims` без `ORDER BY`, sort лише за score);
- ties реальні у historical top pairs;
- `candidate_pairs.score` має DB CHECK `[-1, 1]`, тоді як float32 dot для майже/повністю однакових normalized vectors може через numerical noise трохи вийти за межу; перед persistence потрібен bounded cosine (`clip`) як numerical hardening;
- source-provenance axis і strategic `content_contour_assignments` — різні осі; sparse strategic layer зараз не підміняє v1.

Ще НЕ виміряно/не слід вважати фактом:

- що near-duplicate pairs системно домінують на всіх потрібних score ranges;
- що contradictions лежать у конкретному cosine band;
- правильний time window для relation candidates;
- relation-judgment latency/throughput на реальних live pairs;
- конкретні `k`/`tau` для майбутнього per-claim candidate generation;
- коли HNSW стане необхідним.

## Рішення на поточний етап

1. **Не запускати старий full-materialization generator на ~30k corpus.**
2. `candidate_version=1` не переводити на `content_contour_assignments` — це була б одночасна semantic migration і scaling change при ~2% coverage.
3. Зробити exact blockwise implementation v1 як:
   - execution hardening;
   - regression/reference implementation;
   - read-only/controlled profiling tool для параметрів наступного contract.
4. У blockwise pass зібрати без materialization всіх pairs:
   - score histogram;
   - per-claim degree для кількох candidate thresholds;
   - `|delta t|` distribution для high-similarity pairs;
   - near-duplicate band size + manual sample;
   - empty-source-group count;
   - deterministic tie handling.
5. Після вимірювань визначити `candidate_version=2` як окремий contract. Найімовірніша форма для перевірки — per-new-claim bounded neighborhood (threshold + top-k + temporal/novelty policy), але параметри не фіксувати до профілювання.
6. HNSW не вводити лише тому, що index уже існує. Для поточного масштабу exact blockwise NumPy треба спочатку виміряти.

## Додаткові застереження до review

Деякі сильні тези незалежного review слід залишити як гіпотези до вимірювання:

- твердження про типовий cosine range contradictions;
- припущення про relation latency;
- твердження, що exact kNN залишатиметься «секундами» на значно більшому corpus.

Також майбутній v2 не повинен спрощувати provenance до одного `source_id`: один canonical `content_id` може мати кілька `item_occurrences`/sources. Source diversity треба визначати через множину provenance/occurrences або залишити атрибутом candidate, а не неявним scalar filter.

Для temporal v2 `first_seen = MIN(collected_at)` теж не можна автоматично приймати як live event time: re-observed/re-emergent content може мати старий first_seen. Потрібне окреме вимірювання `first_seen` vs latest/relevant occurrence time.

## Downstream reliability dependency

До переведення relation/event Mamay workers у live залишаються confirmed reliability fixes з окремого аудиту:

- dynamic retry attempts (`transport_error` retryable);
- consumer-side removal of hardcoded `attempt_no=1` у `event_candidate_builder`;
- short-lived DB lifecycle навколо Mamay inference, особливо у event merge;
- shared operational Mamay lock замість concurrent inference.

Ці reliability fixes ортогональні blockwise profiling і не блокують deterministic candidate measurements.

## Наступний практичний крок

Controlled patch `candidate_pair_generator.py`: exact blockwise v1 + deterministic tie-break + bounded cosine + profiling counters. Спочатку dry/read-only profile на поточному corpus, потім рішення про v2 на основі вимірювань, а не припущень.
