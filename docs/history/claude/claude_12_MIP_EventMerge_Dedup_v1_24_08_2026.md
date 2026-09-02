# МІП — Event Merge / Dedup v1: статус, архитектура, пилотный прогон

Дата: 24.08.2026
Статус: **в проде**, миграция применена, пилотный прогон на живых данных выполнен и сверен построчно.

## 1. Контекст

Слой Event Merge/Dedup v1 стоит поверх уже работающего Event Candidate / Event Verifier v1
(см. `claude/11_MIP_EventCandidate_Builder_v1_24.08.2026.md` и связанные доки).
Задача: находить среди `accepted_seed` event_candidates пары, у которых пересекаются
`included=true` claims, прогонять их через LLM-верификатор на предмет "это один и тот же
реальный ивент или нет", и постепенно схлопывать подтверждённые пары в `canonical_events`.

## 2. Архитектурное решение (принято пользователем, с 5 явными амендментами)

Canonical↔canonical merge в v1 **не реализован**. Пары, у которых оба seed уже находятся
в РАЗНЫХ canonical_events, классифицируются как `deferred_both_canonical` и остаются для
отдельного v2 pass.

Амендменты, внесённые перед написанием кода:

1. **Никаких fake-записей `event_merges` для детерминированных skip.** Eligibility-логика
   в воркере исключает LLM-вызов для пар, где оба seed уже canonical, но НЕ пишет строку в
   `event_merges` для этого случая. SUMMARY отдельно считает `already_same_canonical` и
   `deferred_both_canonical`.
2. В `event_merges` добавлено поле `input_payload jsonb NOT NULL` — точный JSON-пакет,
   реально отправленный LLM. `side_a_ref`/`side_b_ref jsonb` оставлены как компактные
   ссылки (kind+id), но НЕ заменяют snapshot.
3. При extend canonical_event (`same_event_extend`) обновляются
   `canonical_event_summary = merged_event_summary` и `updated_at = now()`.
4. `event_merge_candidate_builder.py` пинит контракт Event Verifier v1 (модель + промпт по
   имени/версии, fail-fast при расхождении). `shared_claim_ids` — DB-invariant
   `CHECK (jsonb_array_length(shared_claim_ids) > 0)`.
5. Порядок обработки кандидатов в v1 (SQL ORDER BY и воркер):
   `jsonb_array_length(shared_claim_ids) DESC, created_at, merge_candidate_id`.

Ключевой архитектурный механизм — **live (не batch-snapshot) проверка canonical membership**:
`get_canonical_membership()` вызывается непосредственно перед обработкой каждого кандидата
внутри цикла `run()`, а не один раз в начале. Это гарантирует anti-chaining: если кандидат N
в рамках одного прогона создал/расширил canonical_event, кандидат N+1 в том же прогоне уже
увидит это изменение.

## 3. Схема (DDL, `sql/018_add_event_merge.sql`, 110 строк, md5 `8b1765dade64126921116bc4151d739a`)

Таблицы: `event_merge_prompts`, `event_merge_candidates`, `canonical_events`,
`event_merges`, `canonical_event_members`.

Ключевые констрейнты (подтверждены `\d` после применения миграции):

- `event_merge_candidates`: `CHECK (seed_a_id < seed_b_id)`,
  `CHECK (jsonb_array_length(shared_claim_ids) > 0)`, `UNIQUE (seed_a_id, seed_b_id)`.
- `event_merges`: `input_payload jsonb NOT NULL`, all-or-nothing CHECK-и на
  status/decision/rationale/summary, `UNIQUE (merge_candidate_id, llm_model_id, prompt_id, attempt_no)`.
- `canonical_events`: `founding_merge_id` — circular-FK-safe (добавлен без inline FK,
  затем `ALTER TABLE ... ADD CONSTRAINT` после создания `event_merges`).
- `canonical_event_members`: `UNIQUE (event_candidate_id)` — один seed может входить
  максимум в один canonical_event (v1-инвариант).

## 4. Доставленные файлы (все проверены byte-exact, compile OK)

| Файл | Строк | md5 |
|---|---|---|
| `sql/018_add_event_merge.sql` | 110 | `8b1765dade64126921116bc4151d739a` |
| `sql/019_seed_event_merge_registry_v1.sql` | 113 | `8dffb91bed6f3c6ae4411e33946d2f73` |
| `gen_019.py` | 47 | `b1246df1c37b7b16d09ea8e5d2257595` |
| `experiments/event_candidates/event_merge_prompt_v1.txt` | 101 | `4dd609b2daa52b8987d63c8824d93f5b` |
| `experiments/event_candidates/event_merge_candidate_builder.py` | 230 | `d6ddfd8a332ab5b77ad5d6ee9ec9c5a3` |
| `experiments/event_candidates/event_merge_worker.py` | 581 | `038e7bbe5c6177fb3f552a5592dcf367` |

`event_merge_prompt_v1.txt` — англоязычный промпт для LLM-верификатора мерджа: описывает
формат пакетов (`kind="seed"` / `kind="canonical_event"`), явно предупреждает про известный
upstream false-positive паттерн (generic/boilerplate совпадения — тот же класс проблемы, что
документирован для relation_judgment v3), задаёт три решения (`same_event` / `different_event`
/ `insufficient`) и строгий JSON-контракт из трёх полей (`decision`, `merged_event_summary`,
`rationale_text`). Заканчивается маркером `<<PAYLOAD_JSON>>` для подстановки реального пакета.

## 5. Применение миграции (24.08.2026)

```
psql -d mip_dev -f sql/018_add_event_merge.sql
CREATE TABLE / CREATE TABLE / CREATE TABLE / CREATE TABLE / ALTER TABLE / CREATE TABLE
```

Все 6 DDL-операций прошли без ошибок, порядок совпал с ожидаемым (4×CREATE TABLE,
ALTER TABLE для circular FK, ещё CREATE TABLE). Структура сверена через `\d` по всем
четырём новым таблицам — все CHECK/FK/UNIQUE констрейнты на месте.

```
psql -d mip_dev -f sql/019_seed_event_merge_registry_v1.sql
INSERT 0 1
```

Реестр промпта: `prompt_id=1, prompt_name=event_merge, prompt_version=v1,
schema_version=event-merge/1, length(prompt_text)=5193` (расхождение с `wc -c`=5215 байт —
11 символов em-dash (—) в UTF-8 по 3 байта каждый, не порча данных).

## 6. Пилотный прогон на живых данных

### 6.1 Candidate builder

```
python3 experiments/event_candidates/event_merge_candidate_builder.py
accepted_seed event_candidates with >=1 included claim: 8
overlapping seed pairs found: 5
SUMMARY: code_revision=5f55fb4+dirty accepted_seeds=8 overlapping_pairs=5 inserted=5 already_existed=0
```

Проверено: во всех 5 строках `seed_a_id < seed_b_id`, `n_shared >= 1`, порядок ровно
`shared_claim_ids DESC, created_at, merge_candidate_id`.

### 6.2 Merge worker — прогон 1 (`--limit 2`)

```
[526eaf82] VALID decision=same_event (seed:4b510228 vs seed:52bb7620) 18.2s
[be53b380] VALID decision=same_event (seed:31080e48 vs canonical_event:112734e4) 17.9s
SUMMARY: total=2 same_event_new=1 same_event_extend=1
```

**Anti-chaining подтверждён живьём**: второй кандидат (`be53b380`, seed_a=31080e48,
seed_b=52bb7620) обработался как seed vs `canonical_event:112734e4`, а НЕ как seed vs seed —
потому что seed `52bb7620` уже вошёл в canonical_event, созданный первым кандидатом
парой секунд раньше, в ТОМ ЖЕ прогоне. Live-проверка membership (не batch-snapshot)
сработала как спроектировано.

Проверено в БД: `canonical_events.founding_merge_id` = merge_id первого мерджа;
`updated_at` (08:20:56) > `created_at` (08:20:38) — обновилось после extend;
`canonical_event_summary` — свежий summary ИЗ ВТОРОГО (extend) мерджа, не из founding —
амендмент №3 подтверждён; `canonical_event_members` — 3 строки (2 founding + 1 extended),
`added_via_merge_id` корректен на каждой.

### 6.3 Merge worker — прогон 2 (`--limit 3`, оставшиеся кандидаты)

```
[f38ba586] VALID decision=same_event (seed:1374fa7c vs seed:47a14dae) 19.1s
[f950a4a7] VALID decision=same_event (seed:7cf99e30 vs seed:b427102a) 16.0s
[40300b08] SKIP already_same_canonical (112734e4)
SUMMARY: total=3 same_event_new=2 already_same_canonical=1
```

`40300b08` (seed_a=31080e48, seed_b=4b510228 — оба уже в canonical_event 112734e4) дал
`already_same_canonical`. Проверено: `SELECT COUNT(*) FROM event_merges WHERE
merge_candidate_id='40300b08...'` → `0` — амендмент №1 подтверждён, fake-строка не создана.

Итог по всему batch из 5 кандидатов: 3× `same_event_new` + 1× `same_event_extend` +
1× `already_same_canonical` = 5. В БД: 3 отдельных `canonical_events`
(112734e4 с 3 членами, 9c9a59a8 с 2, c6883957 с 2 — итого 7 member-строк, арифметика бьётся).

### 6.4 Известное поведение: skip-кандидаты не персистятся

Повторный запуск (`--limit 10`) показал `batch size: 1` вместо ожидаемого пустого backlog —
`40300b08` снова попал в выборку и пересчитался в `already_same_canonical` заново.

Это **не баг, а прямое следствие амендмента №1**: раз для `already_same_canonical`/
`deferred_both_canonical` не пишется строка в `event_merges`, `fetch_pending_candidates`
не может отфильтровать такие пары по наличию записи — они будут пересчитываться (без
LLM-вызова, но с лишним SELECT+JOIN) на КАЖДОМ будущем запуске воркера, пока не появится
LLM-подтверждённый мердж или явный v2-механизм (skip-лог / materialized-фильтр).

На текущих объёмах (1 такой кандидат) — не проблема, чинить не нужно. Стоит держать в
уме при масштабировании: если после массового прогона candidate builder на большом
корпусе объём skip-кандидатов вырастет, каждый прогон воркера будет заново пересчитывать
весь их набор — по деньгам бесплатно (нет LLM-вызовов), по времени/нагрузке на БД может
стать заметным.

## 7. Что дальше (не сделано, явно отложено)

- Canonical↔canonical merge (v2) — пары, где оба seed уже canonical, но в РАЗНЫХ
  canonical_events (`deferred_both_canonical`). В пилотном прогоне таких кандидатов не
  было (не проверено на реальных данных).
- Возможный v2-механизм для не-персистящихся skip-кандидатов (см. 6.4), если объём
  вырастет настолько, что пересчёт на каждом прогоне станет заметен по времени.
