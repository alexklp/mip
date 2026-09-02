# МІП — технический handoff для Claude после embeddings vertical slice

**Дата среза:** 19.08.2026

**Репозиторий:** `~/mip`

**База:** `mip_dev`

**Текущая ветка:** `main`

**Текущий HEAD:** `fd34469 feat(embeddings): add BGE-M3 vector pipeline`

## 0. Зачем этот документ

Это самодостаточное продолжение контекста с момента последней кодовой задачи Claude. Он фиксирует:

- что уже реализовано и проверено фактически;
- какие эксперименты проведены после последнего коммита;
- какие выводы считаются подтверждением исходной архитектуры, а не сменой курса;
- следующий ограниченный coding task для Claude;
- что сейчас намеренно не реализуем.

Правило работы: сначала сверить фактическое состояние репозитория и БД. Если оно расходится с этим handoff, фактический код, миграции и результаты команд имеют приоритет; расхождение нужно явно назвать, а не молча «исправлять» архитектуру.

### Важный рабочий протокол

Claude работает только в обычном чате и **не имеет прямого доступа** к серверу, shell, репозиторию, БД или файлам пользователя. Claude:

- выдаёт один безопасный микрошаг или один небольшой блок команд;
- пользователь сам выполняет команды на `srv01` и возвращает полный вывод;
- Claude анализирует только реально полученный вывод;
- следующий шаг выдаётся после проверки предыдущего;
- файлы создаются переданными Claude командами, которые пользователь запускает вручную;
- Claude не должен писать «я проверил», «я запустил», «я создал» или «tests passed», пока пользователь не прислал соответствующий результат;
- потенциально необратимые действия, миграции и commit выполняются только после отдельного явного согласования.

---

## 1. Дальняя цель МІП и неизменный курс

МІП — не просто сборщик новостей и не чат над vector store. Цель — локальная, проверяемая человеком система мониторинга открытого информационного пространства, которая:

1. собирает материалы из разнородных источников;
2. хранит сырой вход, канонический контент и факты публикации раздельно;
3. выполняет детерминированную нормализацию и дедупликацию;
4. строит embeddings и semantic retrieval;
5. извлекает из evidence атомарные утверждения;
6. нормализует сущности, географию, время, действия и модальность;
7. связывает утверждения, события, источники и распространение в граф;
8. строит кластеры сюжетов, сигналы, RAG и далее GraphRAG;
9. формирует объяснимую аналитику с trace до конкретных evidence;
10. оставляет критические выводы под human review;
11. использует Hermes/оркестратор для ограниченных многошаговых циклов, инструментов и skills;
12. допускает разные модели на разных этапах, если это подтверждено eval-метриками.

Исходный архитектурный замысел изначально предполагал модульный многоэтапный pipeline. Последний эксперимент **не открыл новую архитектуру**, а эмпирически подтвердил, что границы между этапами нужны уже сейчас:

`evidence → claim extraction → validation → normalization → candidate linking → relation judgment → event building → synthesis → signals/RAG/GraphRAG`

Одна LLM не должна одним вызовом одновременно извлекать claims, нормализовать их, строить связи и писать итоговую аналитику. Конкретная модель не является архитектурным инвариантом. Mamay, Hermes, Qwen или иная модель могут занимать разные узлы после сравнительного eval.

### Архитектурные инварианты

- внешний контент — недоверенный вход;
- LLM/agent не является security boundary;
- детерминированное выполняется кодом/SQL, LLM используется для семантического и неоднозначного;
- канонический content отделён от occurrence;
- важный AI-результат хранит model/prompt/config/input provenance;
- важный вывод имеет evidence lineage и остаётся проверяемым человеком;
- agent loop имеет жёсткие лимиты шагов, времени и разрешённых tools;
- инфраструктурная сложность добавляется после измеренной необходимости;
- HLD — адаптивная baseline; проверенный DDL/LLD/pilot ближе к реализации и сильнее старого implementation-detail.

---

## 2. Git baseline и уже выполненные этапы

Последние коммиты:

```text
fd34469 (HEAD -> main) feat(embeddings): add BGE-M3 vector pipeline
d8fbd38 feat(ingestion): add RSS and Telegram web-preview collectors
f9b131d chore: establish pre-collector baseline
```

### Ingestion vertical slice

Рабочая модель:

`source fetch → raw_items → normalization → content_items → item_occurrences`

- `raw_items` хранит сырой payload и технический hash;
- `content_items` хранит канонический нормализованный текст и hard-dedup hash;
- `item_occurrences` хранит факт появления content в конкретном source, внешний reference и время;
- уникальность occurrence обеспечивает идемпотентность повторного запуска;
- один канонический content может иметь несколько occurrences;
- collector одного источника не должен останавливать остальные.

Текущий охват pilot:

- 14 RSS-источников;
- 72 Telegram web-preview источника;
- 86 источников суммарно;
- Telegram web-preview имеет известное ограничение: часть каналов отдаёт landing page без постов; для них позже нужен MTProto, но это не текущая задача;
- automated/unit tests для collectors пока отсутствуют; верификация была ручной на реальных данных и повторных запусках.

### Embeddings vertical slice — последняя задача Claude

Claude создал:

- `sql/006_add_embeddings.sql`;
- `collectors/embed_worker.py`.

Коммит:

```text
fd34469 feat(embeddings): add BGE-M3 vector pipeline
```

Ключевые решения:

- `embedding_models` — реестр версий embedding-моделей;
- детерминированный `embedding_model_id = 1` для текущей BGE-M3;
- `embeddings.embedding` имеет тип `vector` без фиксированной размерности;
- PK: `(content_id, embedding_model_id)`;
- partial expression HNSW для текущей модели;
- eligibility: только `content_items`, имеющие хотя бы один `item_occurrence`;
- worker работает batch по 100, CPU, fail-fast, commit per batch;
- повторный запуск идемпотентно пропускает готовые строки;
- worker сверяет hardcoded model config с записью БД и не обновляет её молча.

Текущая запись модели:

| Поле | Значение |
|---|---|
| model | `BAAI/bge-m3` |
| revision | `5617a9f61b028005a4858fdac845db406aefb181` |
| framework | `sentence-transformers` |
| framework version | `5.7.0` |
| dimension | `1024` |
| metric | `cosine` |
| normalize | `true` |
| input | `content_items.text_content` |

Индекс:

```sql
CREATE INDEX idx_embeddings_hnsw_bge_m3
ON public.embeddings
USING hnsw (((embedding)::vector(1024)) vector_cosine_ops)
WHERE embedding_model_id = 1;
```

Фактическая верификация полного прогона:

```text
eligible            3087
embedded            3087
remaining              0
total_embeddings    3087
missing_eligible       0
embedded_orphans       0
wrong_dimensions       0
```

Offline/idempotency check:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 time python collectors/embed_worker.py
```

Результат:

```text
model config verified
local_files_only=True, CPU
0 embeddings written this run, backlog exhausted
```

Известная runtime-деталь: при первом запуске `local_files_only=True` не предотвратил HTTP metadata-запросы Hugging Face. Полностью offline-поведение обеспечили переменные `HF_HUB_OFFLINE=1` и `TRANSFORMERS_OFFLINE=1`. Это нужно учесть позже в service/runtime config, но не менять сейчас embedding pipeline без отдельной задачи.

---

## 3. Инцидент во время embeddings run

SSH/ZeroTier-связь с сервером исчезла во время полного прогона. Сервер не перезагружался и embedding job не положил машину.

Kernel/network timeline:

```text
2026-08-19T09:49:28Z r8125 enp131s0 link down
2026-08-19T09:49:48Z r8125 enp131s0 link up
2026-08-19T09:50:06Z r8125 enp131s0 link down
2026-08-19T09:57:04Z r8125 enp131s0 link up
```

Это Ethernet carrier flap на `r8125/enp131s0`, а не reboot, OOM или падение PostgreSQL. Worker продолжил работу и завершил backlog. После завершения процесс отсутствовал, что нормально для одноразового batch worker.

---

## 4. Что подтвердил semantic retrieval

BGE-M3 + pgvector корректно возвращают мультиязычно связанные UA/RU/EN материалы. Поиск по темам Купянска, Покровска, линии фронта и т. п. даёт содержательно релевантные результаты.

При этом whole-document embedding решает **topic retrieval**, но не гарантирует:

- один и тот же конкретный event;
- атомарные claims;
- корректную временную совместимость;
- различение утверждения, оценки, отрицания и «не сообщается»;
- причинные связи;
- contradiction/support judgment.

Это ожидаемая граница embeddings, не дефект BGE-M3.

---

## 5. Локальный LLM baseline

Установленные модели:

```text
models/hermes-4-14b-fp8/                         ~16G
models/mamaylm-gemma3-27b-v2-q4km/              ~16G
models/qwen3.5-9b/                               ~19G
```

Текущий доказанно рабочий analytical server — MamayLM через официальный `llama.cpp` Vulkan build:

```text
binary:
/home/ovasyliev/mip/tools/llama.cpp-vulkan/llama-b9642/llama-server

model:
/home/ovasyliev/mip/models/mamaylm-gemma3-27b-v2-q4km/MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf

chat template:
/home/ovasyliev/mip/tools/chat-templates/mamaylm-gemma3-tools.jinja
```

Команда запуска (как приведена в этом handoff — **порт 8080**, см. расхождение с `03_MIP_Server_State` ниже):

```bash
cd ~/mip
nohup tools/llama.cpp-vulkan/llama-b9642/llama-server \
  -m models/mamaylm-gemma3-27b-v2-q4km/MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf \
  -ngl all \
  -c 65536 \
  -fa on \
  --host 127.0.0.1 \
  --port 8080 \
  --jinja \
  --chat-template-file tools/chat-templates/mamaylm-gemma3-tools.jinja \
  > /tmp/mamay-server.log 2>&1 &
```

API:

```text
http://127.0.0.1:8080/v1/chat/completions
model id: MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf
n_ctx: 65536
n_ctx_train: 131072
n_params: 27009346304
```

> **Расхождение с `03_MIP_Server_State_18.08.2026.md`:** тот документ фиксирует рабочий launch на `--port 8000`, `--device Vulkan1`, `--gpu-layers all`, `--parallel 1` (без `-fa on` явно). Этот handoff даёт `--port 8080`, `-ngl all`, `-fa on`, без явного `--device`. Не установлено, что это реальный конфликт (могли быть два разных запуска/тестовых инстанса) — **нужно проверить фактический live endpoint** на инспекции перед live eval (см. п. 10 ниже).

Ранее на этом же стенде подтверждено:

- full GPU offload на RTX 5090;
- 65,536 context;
- exact needle answer на prompt около 57,975 tokens;
- около 17.5–18 output tok/s;
- рабочий OpenAI-compatible tool-call roundtrip через custom Jinja.

Hermes успешно использовался как агентная модель/runtime и остаётся кандидатом для orchestration/tool use. Он не заменяет этапный analytical pipeline. Qwen сейчас только установлен; факт его пригодности для текущей задачи не установлен.

---

## 6. Купянский eval fixture

Для первого cross-contour semantic experiment вручную выбраны восемь evidence. Они относятся к одной широкой теме, но не обязаны описывать один и тот же момент или событие.

| ID | Время UTC | Контур | Источник | Суть evidence |
|---|---|---:|---|---|
| E1 | 2026-08-17 21:17:23 | 3 | ТСН | оценка ситуации как близкой к катастрофической; трудная логистика и постоянные удары на левом берегу Оскола |
| E2 | 2026-08-18 05:08:01 | 4 | WarGonzo | сообщается, что изменений в ЛБС нет |
| E3 | 2026-08-18 09:29:00 | 3 | Цензор.НЕТ | поиск северного маршрута, давление к Раевке, цель входа в Купянск, пока безуспешно |
| E4 | 2026-08-18 10:50:00 | 3 | Еспресо | тот же тезис Трегубова через Укринформ: Раевка/северный вход, пока неэффективно |
| E5 | 2026-08-18 16:33:58 | 4 | Олег Царев | утверждение о контроле большей части города; движение к Соболевке и вдоль западного берега Оскола |
| E6 | 2026-08-18 21:31:01 | 4 | Colonelcassad | утверждение о занятии восточной части Ковшаровки |
| E7 | 2026-08-19 05:08:01 | 4 | WarGonzo | практически тот же тезис о восточной части Ковшаровки |
| E8 | 2026-08-19 05:44:00 | 3 | Еспресо | две наступательные операции к Новоосиновому и Куриловке |

Важно: `source`, `contour` и `reported_at` — provenance, а не автоматическое основание считать claim истинным или ложным.

---

## 7. Два Mamay-эксперимента и фактические результаты

### Эксперимент v1: всё в одном вызове

Один prompt поручал модели:

- извлечь claims;
- определить время;
- найти duplicates/support/tension/contradiction;
- написать итоговую assessment;
- вернуть JSON.

`max_tokens=1800` оказался недостаточным: ответ обрезался. До обрезания модель правильно сохранила evidence attribution и увидела пары E3/E4 и E6/E7, но выдала ложные contradictions с высокой уверенностью.

### Эксперимент v2: усиленный контракт

Добавлены:

- `max_tokens=4000`;
- поля actor/action/location/result/reported_at/claim_time;
- расширенный enum relations;
- explicit contradiction guard;
- требование atomic decomposition;
- JSON-only/response_format.

Первичный ответ был обёрнут в Markdown fence и считался `JSON: INVALID`. После defensive strip fence получен `CLEAN JSON: VALID`.

### QA результата v2

| Проверка | Результат |
|---|---|
| Полный ответ без truncation | PASS |
| JSON после strip fence | PASS |
| Evidence ID у claims | PASS |
| E6/E7 как duplicates | PASS |
| Atomic decomposition | FAIL: ровно один claim на каждое evidence |
| JSON null | FAIL: модель пишет строку `"null"` |
| Сохранение epistemic modality | FAIL: «не сообщается об изменениях» превращено в «изменений нет» |
| Полнота E1 | FAIL: потеряна отдельная оценка «близка к катастрофической» |
| Полнота E3/E4 | FAIL: route/pressure/goal/result не разделены |
| Полнота E5 | FAIL: потеряны claim о контроле города и движение вдоль западного берега |
| Временная логика | FAIL: E2 против более поздних E6/E7 помечено `contradicts` |
| Независимость evidence | FAIL: E3/E4 помечены `supports`, хотя вероятен общий первоисточник/перепечатка |
| No invented links | FAIL: E3/E8 связаны через «может быть частью северного наступления», чего текст не устанавливает |
| Итоговая assessment | FAIL: наследует ошибочные relations |

Главный вывод: transport можно сделать надёжным валидатором, а Mamay не отклоняется как модель. Но монолитная задача смешивает независимые типы рассуждения, поэтому ошибка раннего этапа заражает весь последующий результат.

Это подтверждает исходный проектный принцип: один этап — один контракт — отдельная валидация; модели можно выбирать по каждому этапу.

---

## 8. Зафиксированные решения — не пересматривать в следующей задаче

1. Не строить сейчас полный event graph.
2. Не писать сейчас relation judge, final assessment, signal generation, RAG или GraphRAG.
3. Не сохранять текущий монолитный JSON Mamay как аналитическую истину.
4. Не «лечить» все ошибки ещё более длинным монолитным prompt.
5. Не менять embedding migration/worker в рамках следующего task.
6. Не выбирать одну LLM на все этапы заранее.
7. Не считать source/contour truth score.
8. Не смешивать `reported_at` occurrence с временем, которое утверждается в тексте claim.
9. Не превращать «изменений не сообщается» в «изменений нет».
10. Не строить relation между claims только потому, что они семантически близки.
11. Не использовать LLM для JSON repair, offset validation, hash, enum checking и других детерминированных проверок.
12. Не вводить production queue/service/systemd в этом task: сначала измеримый eval vertical slice.

---

## 9. Следующий шаг: reproducible claim-extraction eval vertical slice

Следующая задача — изолировать только первый семантический skill:

`one evidence → zero or more atomic claims → deterministic validation → eval report`

На этом этапе **нет** normalization между публикациями, relation judgment, event building или summary.

Почему сначала eval harness, а не DDL для claims:

- контракт claims ещё эмпирически не подтверждён;
- преждевременный persistence закрепит ошибки схемой БД;
- единый harness позволит сравнить Mamay/Hermes/Qwen или будущие модели на одном fixture;
- после стабилизации contract можно спроектировать `llm_models`, `prompt_versions`, `inference_runs`, `claims` и evidence lineage без гадания.

### Минимальный output contract v1

Один входной evidence содержит:

```json
{
  "evidence_id": "E1",
  "content_id": "dd8a4134...",
  "source": "ТСН",
  "contour_id": 3,
  "reported_at": "2026-08-17T21:17:23Z",
  "text": "..."
}
```

LLM возвращает только:

```json
{
  "schema_version": "claim-extraction/1",
  "evidence_id": "E1",
  "claims": [
    {
      "claim_local_id": "C1",
      "claim_text": "...",
      "evidence_span": "точная непрерывная подстрока input text",
      "evidence_start": 0,
      "evidence_end": 42,
      "epistemic_status": "asserted|reported|alleged|estimated|opinion|uncertain|denied|not_reported",
      "claim_time_text": null,
      "attribution_text": null
    }
  ]
}
```

Замечания:

- `evidence_start/end` — Python Unicode character indexes, `[start:end]`;
- nullable fields содержат JSON `null`, не строку `"null"`;
- `reported_at`, source и contour не копируются в claim как семантические поля: они уже принадлежат evidence provenance;
- `claim_time_text` содержит только явно присутствующую в тексте временную формулировку;
- `attribution_text` содержит только текстовую атрибуцию внутри evidence, а не имя source metadata;
- `claim_text` сохраняет смысл и модальность исходника без внешнего знания;
- каждый claim должен быть независимо проверяемым утверждением;
- assessment/evaluation и фактическое утверждение разделяются;
- один evidence может дать `0..N` claims;
- relation fields в этой схеме запрещены.

### Детерминированные проверки

Validator обязан отклонять ответ, если:

- есть Markdown fence или текст вне JSON;
- JSON не парсится;
- schema version/evidence ID не совпадают;
- есть неизвестные поля;
- nullable field содержит `"null"`;
- enum неизвестен;
- `claim_local_id` не уникален внутри evidence;
- span пустой или не является точной подстрокой;
- offsets не совпадают с `text[start:end]`;
- claim ссылается на другой evidence;
- модель добавила relations, event, summary, confidence или source truth score.

Raw response и validation errors сохраняются в eval artifact, но не в PostgreSQL.

### Gold expectations для Купянского fixture

Это regression fixture текущего контракта, а не утверждение объективной истины. Минимально ожидается сохранение следующих независимых смысловых единиц:

- **E1:** отдельная оценка «ситуация близка к катастрофической»; сложная логистика; постоянные российские удары; временная формулировка «місяцями» там, где она относится к claim.
- **E2:** именно «об изменениях не сообщается» с `not_reported`, а не «изменений нет».
- **E3:** поиск новых путей; направление/Раевка; цель создать возможность входа с севера; текущий результат «безуспешно» — разделить на атомарные claims настолько, насколько они независимо проверяемы.
- **E4:** давление к Раевке; цель входа с севера; оценка текущей неэффективности; сохранить атрибуцию Трегубову из текста.
- **E5:** утверждение о контроле большей части города; движение к Соболевке; движение вдоль западного берега Оскола; сохранить оговорку «по данным с мест» как epistemic/attribution cue.
- **E6:** занятие восточной части Ковшаровки.
- **E7:** занятие/занимание восточной части Ковшаровки.
- **E8:** две наступательные операции к Новоосиновому и Куриловке; не придумывать их связь с Раевкой.

Semantic coverage на первой итерации оценивается человеком по side-by-side report. Не использовать вторую LLM как «автоматическую истину» для оценки первой.

---

## 10. Конкретное coding task для Claude

### Режим выполнения

1. Claude выдаёт пользователю первый read-only микрошаг.
2. Пользователь выполняет его на `srv01` и возвращает полный вывод.
3. Claude проверяет вывод и только после PASS выдаёт следующий микрошаг.
4. После завершения inspection Claude передаёт код/команды для создания **одного логического блока или файла за раз**.
5. Пользователь выполняет команды и возвращает результат проверки.
6. Если обнаружено архитектурное расхождение или dirty changes пересекаются с task — Claude останавливается и сообщает точный blocker; чужие изменения не переписываются.
7. Не коммитить до review пользователя/GPT.

### Read-only inspection

Claude должен выдавать inspection последовательно, а не изображать его выполненным. Рекомендуемый порядок:

1. `cd ~/mip && git status --short && git log --oneline --decorate -5`
2. после проверки вывода — ограниченный список существующей структуры проекта;
3. после проверки — версии Python и уже установленного test tooling;
4. после проверки — состояние локального Mamay endpoint (**включая проверку фактического порта — см. расхождение 8080 vs 8000 в п. 5**);
5. только затем — первый файл vertical slice.

Проверить существующий стиль Python, конфигурацию DSN, наличие test tooling и `.gitignore`. Не выводить secrets и содержимое `.env`.

### Требуемые файлы

Имена можно минимально адаптировать к реально существующей структуре, но границы задачи не расширять.

```text
experiments/claim_extraction/
├── README.md
├── fixture_kupiansk_v1.json
├── gold_kupiansk_v1.json
├── schema_claim_extraction_v1.json
├── prompt_claim_extractor_v1.txt
└── run_eval.py

tests/
└── test_claim_extraction_contract.py
```

Если в repo уже есть канонические `fixtures/`, `prompts/` или `tests/`, использовать их вместо параллельной структуры и объяснить выбор.

### `run_eval.py`

Обязательное поведение:

- CLI параметры: `--base-url`, `--model`, `--fixture`, `--gold`, `--output`;
- defaults допустимы для `http://127.0.0.1:8080/v1` и текущего Mamay model ID, но model/backend не hardcode как единственно допустимые;
- обрабатывает **один evidence одним LLM-вызовом**;
- `temperature=0` или минимально поддерживаемое deterministic значение;
- разумный per-evidence `max_tokens`, без 4k общего монолита;
- timeout и понятная ошибка HTTP;
- raw model response сохраняется в eval result;
- strict parse без silent repair; fence считается validation failure;
- JSON Schema validation;
- отдельная проверка offsets/span/real null/allowed keys;
- продолжает eval следующего evidence после ошибки одного, фиксируя status;
- не пишет в PostgreSQL;
- не вызывает внешнюю сеть, кроме явно заданного local OpenAI-compatible endpoint;
- не использует LLM для validation или scoring;
- output artifact включает timestamp, model ID, base URL без credentials, inference params, prompt SHA-256, schema version, fixture SHA-256, per-evidence latency, raw response, parsed response, validation errors и итоговые counts.

### Tests

Unit tests не должны требовать запущенной LLM. Минимум:

1. valid output проходит;
2. string `"null"` отклоняется;
3. неизвестное поле отклоняется;
4. fence отклоняется;
5. неправильный evidence ID отклоняется;
6. span отсутствует в input text — отклоняется;
7. неверные offsets — отклоняются;
8. duplicate local claim ID — отклоняется;
9. relation/event/summary field — отклоняется;
10. HTTP failure одного evidence не уничтожает общий eval report.

Не добавлять тяжёлый framework, если достаточно стандартной библиотеки и уже имеющихся зависимостей. Если нужен новый dependency (`jsonschema`, `pytest` и т. п.), сначала проверить, установлен ли он; изменение dependency manifest должно быть явным и минимальным.

### Acceptance criteria

Coding task считается готовым к review, когда:

- `git diff --check` чист;
- unit tests проходят без LLM server;
- fixture содержит ровно E1–E8 с точным текстом и provenance из эксперимента;
- один live Mamay eval завершает все восемь evidence и создаёт один reproducible report;
- transport metrics отделены от semantic human review;
- invalid response не исправляется молча;
- не создано таблиц БД и не изменено существующих миграций;
- не реализованы relations/events/summary;
- README содержит команды запуска и ограничения;
- пользователь выполняет переданные Claude проверки и возвращает `git status`, `git diff --stat`, `git diff --check`, test output и live eval output;
- Claude сверяет фактический вывод и даёт короткую сводку live eval без приписывания себе выполнения команд;
- изменений не коммитить до совместного review.

### Ожидаемая финальная выдача Claude

1. список файлов и краткое назначение;
2. решения по contract/validation;
3. команды проверки;
4. команды, которыми пользователь получает test output;
5. после получения вывода пользователя — фактический live eval summary;
6. известные проблемы Mamay по каждому evidence;
7. `git diff --stat` и `git status --short`;
8. никакого «следом заодно добавил relations/DB/service».

---

## 11. Что делает пользователь после ответа Claude

1. Не коммитить сразу.
2. Передать GPT:
   - diff/stat;
   - schema и prompt;
   - test output;
   - live eval report или его полный JSON;
   - комментарии Claude о расхождениях.
3. Совместно проверить:
   - не потеряны ли claims;
   - сохранена ли epistemic modality;
   - не смешаны ли reported time и claim time;
   - нет ли invented facts;
   - достаточно ли contract для будущей нормализации.
4. После PASS — коммитить eval vertical slice.
5. Только после этого проектировать следующий persistence slice:
   - model registry;
   - prompt versions;
   - inference runs;
   - claims и evidence lineage.

---

## 12. Ближайшие стадии после claim extraction

Порядок ориентировочный и уточняется измерениями, но направление фиксировано:

1. **Claim extraction eval** — текущий следующий шаг.
2. **Claim extraction persistence** — DDL/provenance/idempotent worker после стабилизации contract.
3. **Claim normalization skill** — entity/action/location/time/modality canonicalization отдельно от extraction.
4. **Candidate linker** — embeddings + entity overlap + geography + time window; только shortlist.
5. **Relation judge** — одна пара или малый набор claims, explicit temporal/epistemic gates.
6. **Critic/validator** — отбрасывает неподтверждённые связи и ложные contradictions.
7. **Event builder** — собирает версии события из валидированных claims, не стирая противоречия и provenance.
8. **Graph layer** — content/occurrence/claim/entity/event/source/model/prompt/run как узлы и доказанные связи как рёбра.
9. **Signals + human review** — полезность, severity, подтверждение/отклонение, feedback.
10. **RAG/GraphRAG** — retrieval по содержанию и структуре с evidence trace.
11. **Hermes orchestration/skills** — bounded loops, retries, разрешённые tools, state и audit.
12. **Multi-model routing** — выбор модели по measured quality/latency/cost каждого skill.
13. **Operational pilot** — реальные аналитики, FP/FN, latency, throughput, observability.
14. **Production hardening** — security, access, backup/restore, deployment/rollback, runbooks.

Главная защита от отклонения курса: каждый следующий модуль должен улучшать путь от сырого evidence к проверяемому аналитическому результату и оставлять trace назад. Если новая функция не усиливает этот путь или не закрывает измеренный bottleneck, она не должна вытеснять текущий vertical slice.

---

## Приложение: расхождения, найденные при сверке с проектными доками (Claude, 19.08.2026)

- **Порт Mamay endpoint:** `03_MIP_Server_State_18.08.2026.md` фиксирует рабочий launch на `--port 8000` (+ `--device Vulkan1`, `--parallel 1`, без явного `-fa on`). Этот handoff (разделы 5 и 10) даёт `--port 8080` (+ `-fa on`, без явного `--device`). Не факт, не путать: могли быть два разных запуска/окна, либо конфиг реально сменился между 18.08 и 19.08. **Нужно подтвердить фактический live endpoint на шаге read-only inspection перед live eval**, не гадать.
- В остальном (git baseline, ingestion vertical slice, статус RSS/TG-web collectors, архитектурные инварианты, зафиксированные решения) handoff непротиворечиво продолжает `05_MIP_Collectors_Pilot_Status_18.08.2026.md` (последнее обновление — утро 19.08, TG web-preview) и `01_PROJECT_CONTEXT.md`. Конфликтов не найдено.
