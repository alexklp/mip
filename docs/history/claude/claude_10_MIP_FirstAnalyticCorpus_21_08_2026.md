# МІП — первый реальный аналитический корпус (21.08.2026)

## Контекст

После закрытия Content Routing v1 (`77cad07`, см. `09_MIP_ContentRouting_v1_21.08.2026.md`) remaining backlog по contour был: contour2=36, contour3=1854, contour4=3490 (все уже отфильтрованы routing на `analyze`/`maybe`).

## Патч: `--contour-id` в `claim_extract_worker.py`

Коммит `9e73219` (поверх `77cad07`), branch `main`. Минимальный патч, добавляет опциональный CLI-флаг `--contour-id N` (1..4), не меняет существующие eligibility/idempotency/routing condition/claim-extraction contract.

При заданном `--contour-id`:
- кандидаты дополнительно фильтруются по `EXISTS` на `item_occurrences` → `sources.contour_id = N` (correlated subquery, без JOIN — `content_items` не фанаутится);
- сортировка — `ORDER BY (SELECT max(io.collected_at) ... WHERE contour_id = N) DESC`, т.е. по самому свежему occurrence именно в этом контуре, не по `first_seen_at` и не по любому occurrence.

Без `--contour-id` запрос побайтово идентичен пре-патч версии.

## Три прогона (план: 2→3→4, реальные данные, не disposable eval)

| contour | limit | valid | invalid | transport_error | error | claims persisted |
|---|---|---|---|---|---|---|
| 2 | 36 | 30 | 6 | 0 | 0 | 90 |
| 3 | 100 | 77 | 23 | 0 | 0 | 454 |
| 4 | 100 | 84 | 16 | 0 | 0 | 675 |
| **итого** | **236** | **191** | **45** | **0** | **0** | **1219** |

`code_revision=9e73219` (clean, без `+dirty`) на всех трёх прогонах.

Provenance-чек на каждом прогоне: `sum(claim_count) валидных runs == count(*) в claims` для точного списка content_id этого батча (не time-window — по урокам contour-2, где 20-минутное окно недобрало 3 строки из-за длительности прогона). Все три прогона сошлись 1:1.

## Разбор invalid (45 шт.)

44 из 45 — уже задокументированный defect class (см. `07_MIP_ClaimExtraction_EvalHarness_Findings_20.08.2026.md`): модель не проставляет `evidence_start`/`evidence_end` для части claims (resolve_offsets не может разрешить оффсет — либо `not_found`, либо `ambiguous`). Ничего нового, ожидаемо.

1 разовое исключение — `d3b3df78-d0f1-4b06-a96f-af46296dba72` (contour3): невалидный JSON верхнего уровня (`Expecting property name enclosed in double quotes: line 19 column 1`), не missing-fields дефект, а сломанный вывод модели целиком. Единичный случай, на contour4 не повторился. Зафиксировано для протокола, целенаправленно не расследовалось (нет повторяемости — нет повода).

## Текущее состояние

- git HEAD: `9e73219`, working tree чистый.
- Первый реальный аналитический корпус: 191 персистнутый valid run с claims (1219 claims) на content из contour 2/3/4, отфильтрованных routing v1.
- prompt v3 / quality tuning missing-fields дефекта — по-прежнему paused, без новой причины не трогаем.

## Следующий шаг — не определён

Явного продолжения после этих трёх батчей пользователь не задавал. Дальнейшая работа (claim linking, entities/events, первый user-facing аналитический результат) требует отдельного явного запроса.
