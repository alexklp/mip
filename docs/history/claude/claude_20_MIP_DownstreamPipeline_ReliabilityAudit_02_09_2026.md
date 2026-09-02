# МІП — Reliability-аудит downstream analytical pipeline (relation → event → merge)

**Репозиторий:** `alexklp/mip`
**Ветка:** `wip/demo-report-2026-08-27`
**Заявленный scope:** состояние на commit `6a03d39`
**Дата аудита:** 02.09.2026
**Тип:** ограниченный reliability-аудит (connection lifetime, retry/attempt semantics, transaction boundaries, restart/idempotency). Архитектура, промпты, semantic contracts — не пересматривались и менять не предлагается.

---

## 0. Методология и её ограничения (прочитать первым)

Прямого доступа к репозиторию по этой сессии не было: `git clone`/`gh`/`api.github.com` для `alexklp/mip` в этом окружении не проксируются (получен явный отказ прокси: *"GitHub access to this repository is not enabled for this session"*), публичный код-индекс (GitHits) репозиторий не видит, т.к. он приватный. Единственный рабочий канал — RAG-поиск (`project_search`) по уже засинканному в проект GitHub-источнику (в проекте засинканы обе ветки: `main` и `wip/demo-report-2026-08-27`, без path-исключений).

Я реконструировал релевантные участки `relation_judgment_worker.py`, `event_verifier_worker.py`, `event_merge_worker.py` и DDL (`sql/011`, `sql/014`, `sql/016`, `sql/018`) через ~15 целевых запросов к RAG-индексу и перепроверил совпадение фрагментов между независимыми запросами (перекрытие context'ов). Все три аудируемых worker'а и все проверенные DDL-файлы вернулись из индекса в **одной** консистентной версии (ни одного случая, где два независимых запроса дали бы разный текст для одного и того же участка кода).

**SHA-пин подтверждён.** Изначально в этом разделе была оговорка о том, что я не могу гарантировать, что RAG-индекс отражает состояние ровно на `6a03d39`, а не более новый/старый коммит ветки. Автор подтвердил напрямую: дата в имени ветки (`wip/demo-report-2026-08-27`) — это дата **старта** ветки, а `6a03d39` — это её текущий **HEAD** (последний коммит). Поскольку project-sync отслеживает HEAD ветки, а `6a03d39` и есть HEAD, RAG-индексированное содержимое соответствует ровно этому коммиту. Эта неопределённость закрыта, находки ниже (в первую очередь F1) относятся к `6a03d39` без оговорок.

**Остаётся один отдельный, более узкий момент — не про SHA-пин, а про полноту индексации одного конкретного файла.** Для `claim_extract_worker.py` (один из двух заявленных "hardened" reference-файлов, сам по себе не аудируемый, а используемый только для сравнения) индекс отдал **две расходящиеся версии** (разные `doc_uuid`, разный код): старую, где `ATTEMPT_NO` — захардкоженная константа и `transport_error` НЕ retryable, и новую, где `attempt_no` вычисляется динамически (`COALESCE(MAX(r.attempt_no),0)+1`) и `transport_error` явно retryable. Раз ветка стабильна на `6a03d39`, это, скорее всего, не смешение веток, а недочищенный старый чанк в RAG-индексе (индекс мог не полностью инвалидировать предыдущую версию файла после коммита, который её заменил). Судя по докстрингу новой версии ("transport_error залишається retryable з наступним attempt_no") и по тому, что именно это поведение соответствует описанию "hardened" в ТЗ, я использовал **новую** версию как эталон для сравнения в F1/F2. Это не меняет ни одной находки по трём аудируемым worker'ам (они найдены самосогласованно, в единственной версии каждый) — влияет только на то, дословно ли точна цитата эталонного паттерна. Если хочется закрыть и это, тот же grep ниже, точечно по одному файлу, снимает вопрос:

```bash
git show 6a03d39:experiments/claim_extraction/claim_extract_worker.py | grep -n "ATTEMPT_NO\|attempt_no" | head -20
```

Ниже — сам аудит.

---

## 1. Находки

### F1 — Захардкоженный `ATTEMPT_NO = 1` делает `transport_error` неretryable во всех трёх worker'ах

**Severity:** important
**Status:** confirmed issue (для `6a03d39` — SHA-пин подтверждён, см. §0)
**Файлы:** `experiments/claim_relations/relation_judgment_worker.py`, `experiments/event_candidates/event_verifier_worker.py`, `experiments/event_candidates/event_merge_worker.py` — везде идентичный паттерн.

**Доказательство.** Во всех трёх файлах:

```python
LLM_MODEL_ID = 1
PROMPT_ID = 1  # (или 3 для relation_judgment_worker.py)
ATTEMPT_NO = 1
```

`fetch_batch` / `fetch_pending_candidates` определяет eligibility так (пример из `relation_judgment_worker.py`, идентично в остальных двух с заменой имён таблиц):

```sql
SELECT cp.candidate_pair_id, cp.claim_id_a, cp.claim_id_b, cp.score
FROM candidate_pairs cp
WHERE NOT EXISTS (
    SELECT 1 FROM relation_judgments rj
    WHERE rj.candidate_pair_id = cp.candidate_pair_id
      AND rj.llm_model_id = %s AND rj.prompt_id = %s AND rj.attempt_no = %s
)
ORDER BY cp.score DESC, cp.candidate_pair_id
LIMIT %s
```

с параметрами `(LLM_MODEL_ID, PROMPT_ID, ATTEMPT_NO, limit)` — то есть `NOT EXISTS` бьёт по **любой** строке с `attempt_no = ATTEMPT_NO(=1)`, независимо от `status`. А `insert_judgment`/`insert_verification`/`insert_merge` при записи `transport_error` тоже используют тот же захардкоженный `ATTEMPT_NO`:

```python
cur.execute(
    "INSERT INTO relation_judgments (..., attempt_no, status, ...) VALUES (..., %s, %s, ...)",
    (..., ATTEMPT_NO, status, ...),
)
```

DDL это допускает и не мешает (`sql/011`: `attempt_no smallint NOT NULL DEFAULT 1 CHECK (attempt_no > 0)`, `UNIQUE (candidate_pair_id, llm_model_id, prompt_id, attempt_no)` — то же самое в `sql/016` для `event_verifications` и `sql/018` для `event_merges`).

**Реальный failure mode.** Один сетевой/таймаут-сбой при обращении к Mamay (до 600s на попытку, `TIMEOUT = 600.0` во всех трёх) во время инференса → в БД пишется строка `status='transport_error', attempt_no=1`. На следующем запуске worker'а (хоть через минуту, хоть через месяц) этот же `candidate_pair_id`/`event_candidate_id`/`merge_candidate_id` **навсегда** выпадает из `fetch_batch`, потому что строка с `attempt_no=1` уже существует — а `ATTEMPT_NO` в коде это не volatile runtime-значение, это статическая константа модуля, поднять её нечем: в CLI (`argparse`) нет флага для attempt_no ни в одном из трёх worker'ов. Единственный выход — вручную удалить строку из БД или руками бампнуть константу в исходнике и передеплоить. Никакого сигнала/алерта об этом в SUMMARY не предусмотрено — `transport_error` в счётчике неотличим от "потом само переретраится".

Учитывая, что дорогая LLM-инференция (до 600s на пару/кандидата) — единственный производитель этих данных, а входной поток (`candidate_pairs`, `event_candidates`, `event_merge_candidates`) не бесконечен, каждая такая "залипшая" запись — это одна навсегда потерянная для пайплайна пара/кандидат без какого-либо fallback.

**Сравнение с эталоном.** `claim_extract_worker.py` (hardened-версия, см. оговорку в §0) и `contour_classification_worker.py` решают это иначе: `fetch_batch` вычисляет `next_attempt_no` через `CROSS JOIN LATERAL (SELECT COALESCE(MAX(r.attempt_no), 0) + 1 AS next_attempt_no, COALESCE(BOOL_OR(r.status IN ('valid','invalid')), false) AS has_terminal FROM ... )`, и фильтрует `WHERE NOT history.has_terminal` — то есть **transport_error не входит в terminal-состояния**, элемент остаётся eligible, а при повторной попытке `insert_run(..., attempt_no=attempt_no, ...)` пишет **следующий** attempt_no (не константу), что не конфликтует с `UNIQUE(..., attempt_no)`.

**Минимальный фикс без изменения семантики.** Ровно тот же паттерн, что уже есть в hardened-референсах, дословно переносится:
1. В каждом из трёх worker'ов заменить `WHERE NOT EXISTS (... attempt_no = %s)` на `CROSS JOIN LATERAL (SELECT COALESCE(MAX(attempt_no),0)+1 AS next_attempt_no, COALESCE(BOOL_OR(status IN ('valid','invalid')), false) AS has_terminal FROM <runs_table> WHERE <fk> = <id> AND llm_model_id=%s AND prompt_id=%s) h ... WHERE NOT h.has_terminal`, и брать `next_attempt_no` вместо константы при INSERT.
2. `ATTEMPT_NO = 1` как модульная константа убирается там, где она использовалась для WHERE/INSERT; `attempt_no` начинает приходить из результата `fetch_batch`/`fetch_pending_candidates` вместе с остальными полями строки, как уже сделано в `claim_extract_worker.py` (`for content_id, evidence_text, attempt_no in batch:`).
3. `status IN ('valid','invalid')` как terminal-набор, `transport_error` — не terminal. Ничего в определениях `relation_label`/`shared_referent_status`/`event_decision`/`decision` не трогается — это чисто SQL-уровень eligibility, семантика ответов LLM не меняется.

---

### F2 — Нет short-lived-соединения для persist-шага; одно долгоживущее соединение держится через весь batch, включая все инференс-вызовы; `conn.rollback()` не защищён от мёртвого соединения

**Severity:** important
**Status:** confirmed issue (структурное отличие от эталона видно во всех трёх файлах одинаково)
**Файлы:** те же три worker'а — `def run(...)`, `def process_pair`/`process_candidate`.

**Доказательство.** Во всех трёх `run()` выглядит так (пример `relation_judgment_worker.py`, структура идентична у остальных двух):

```python
def run(limit: int) -> int:
    code_revision = get_code_revision()
    summary = {"valid": 0, "invalid": 0, "transport_error": 0, "error": 0}
    with psycopg.connect(DB_DSN) as conn:
        prompt_text = fetch_and_verify_registry(conn)
        batch = fetch_batch(conn, limit)
        ...
        for pair in batch:
            status = process_pair(conn, pair, meta, prompt_text, code_revision)
            summary[status] = summary.get(status, 0) + 1
        ...
```

Один и тот же `conn`, открытый один раз в начале `run()`, передаётся в `process_pair`/`process_candidate` для **каждого** элемента batch'а и используется как для чтения, так и для INSERT+commit каждого элемента. Между элементами соединение не закрывается и не переоткрывается. Внутри `process_pair` персист выглядит так:

```python
try:
    insert_judgment(conn, candidate_pair_id, status=status, ...)
    conn.commit()
except Exception as db_err:
    conn.rollback()
    print(f"[{pair_id_str}] DB ERROR while persisting: {db_err}", file=sys.stderr)
    return "error"
```

`conn.rollback()` вызван "голым", без собственного `try/except`.

**Сравнение с эталоном.** И `claim_extract_worker.py` (hardened), и `contour_classification_worker.py` для **persist-шага каждого элемента** открывают **отдельное** короткоживущее соединение и гарантированно закрывают его в `finally`, никогда не вызывая `rollback()` на нём явно (закрытие незакоммиченного соединения само откатывает работу):

```python
db_conn = None
try:
    db_conn = psycopg.connect(DB_DSN)
    run_id = insert_run(db_conn, ...)
    if status == "valid":
        insert_claims(db_conn, run_id, parsed["claims"])
    db_conn.commit()
except Exception as db_err:
    print(f"[{content_id}] DB ERROR while persisting: {type(db_err).__name__}: {db_err}", file=sys.stderr)
    return "error"
finally:
    if db_conn is not None:
        try:
            db_conn.close()
        except Exception:
            pass
```

Комментарий в `persist_single_run.py` формулирует это явно как принцип: *"Не тримаємо DB-транзакцію відкритою, поки Mamay думає"*.

**Уточнение по факту (после повторной проверки по коду, а не по памяти): в исходной формулировке этого раздела была неточность.** Держится не только объект соединения — в части случаев держится и сама транзакция, потому что psycopg по умолчанию работает не в autocommit, и **любой** `SELECT` на этом `conn` открывает implicit-транзакцию, которая живёт до explicit `commit()`/`rollback()`. Это не одинаково для всех трёх файлов — по факту три разных картины:

- **`event_merge_worker.py` — транзакция открыта во время `call_model()` на КАЖДОМ item, без исключений.** `get_canonical_membership(conn, seed_a_id)` + `get_canonical_membership(conn, seed_b_id)` — это два `SELECT`, которые по докстрингу вызываются *"НАЖИВО безпосередньо перед обробкою кожного кандидата"*, то есть прямо в `process_candidate()` перед `call_model()`, на каждой итерации `for merge_candidate_id, ... in batch:`. Плюс следом `build_side_payload_seed`/`build_side_payload_canonical` — тоже `SELECT`ы. Раз предыдущий item уже сделал `conn.commit()`, эти reads на каждой новой итерации открывают новую implicit-транзакцию заново — и она висит открытой все 600s инференции. Здесь версия из GPT-ревью точна на 100%: держится не просто "мёртвый груз соединения", а реально открытая read-транзакция на каждый inference-вызов.
- **`relation_judgment_worker.py` и `event_verifier_worker.py` — только на ПЕРВОМ item батча.** В обоих `batch`, `meta`/`seed_members` вычитываются ОДИН раз до цикла (`meta = fetch_claims_meta(conn, claim_ids)` вне `for pair in batch:`), а `process_pair`/`process_candidate` не делают ни одного `SELECT` перед `call_model()` — только Python-сборка payload'а из уже вычитанных данных. Значит implicit-транзакция от начальных `fetch_batch`/`fetch_claims_meta` висит открытой ровно до первого `insert_judgment(...); conn.commit()` — то есть транзакция реально открыта во время инференции **только для первого item батча**; для item'ов 2..N `conn` во время `call_model()` физически простаивает (idle), но НЕ находится в открытой транзакции — предыдущий commit её закрыл, а новый `execute()` ещё не начался.

Это уточнение не меняет ни severity, ни fix: сам failure mode ниже (мёртвый socket валит и `commit()`, и `rollback()` одинаково) не зависит от того, была ли в момент обрыва открыта транзакция или соединение просто простаивало — рвётся TCP, а не conkретно транзакция. Но для `event_merge_worker.py` картина объективно хуже: помимо риска обрыва, открытая read-only транзакция на каждый item (до 600s, потенциально Х раз подряд на весь batch) держит xmin horizon и мешает autovacuum на всей БД, а не только создаёт риск краха при рестарте PG — отдельный побочный эффект, который тоже закрывается тем же самым фиксом (short-lived connection на persist-шаг + перенос reads на него же).

**Реальный failure mode.** Если PostgreSQL перезапускается (или роняется TCP-сессия — рестарт pgbouncer/сети) между обработкой элементов N и N+1 batch'а: на элементе N+1 `insert_judgment`/`conn.commit()` падают с `OperationalError` на мёртвом соединении → попадаем в `except`, вызываем `conn.rollback()` — а он **тоже** падает на том же мёртвом соединении, потому что это не новая ошибка первого рода, а тот же самый сокет. Это исключение из `rollback()` **не перехвачено** (внутри `except`-блока нет своего `try`), оно пробрасывается из `process_pair` наружу, через `for pair in batch:` в `run()`, и завершает процесс необработанным traceback'ом. Итог: все элементы batch'а после точки сбоя не обрабатываются вообще (ни попытки, ни `transport_error`-строки для них), `SUMMARY` не печатается, скрипт падает с ненулевым кодом выхода без явного сообщения "PG недоступен, доделай остальное вручную". Уже закоммиченные до этого момента элементы **не теряются** — они честно persisted до сбоя.

Это именно тот сценарий, который ТЗ прямо просило проверить ("переживёт ли worker restart PostgreSQL между items") — ответ: нет, не переживёт грациозно (упадёт с traceback вместо per-item `"error"` статуса), хотя данные не портятся.

**Минимальный фикс без изменения семантики — с поправкой по итогам повторной сверки (важно для реализации, не только "перенести INSERT на короткий conn").** Формулировка "основной `conn` оставить для reads, persist — на короткий conn" достаточна для `relation_judgment_worker.py`/`event_verifier_worker.py`, но **недостаточна для `event_merge_worker.py`** — если `get_canonical_membership`/`build_side_payload_seed`/`build_side_payload_canonical` останутся на долгоживущем `conn`, implicit-транзакция всё равно будет висеть на каждом item во время `call_model()` (см. §F2 выше), просто по другой причине (reads, а не persist). Оставлять их на основном `conn` — не фикс, а видимость фикса. Правильная реализация различается по файлам:

- **`relation_judgment_worker.py` / `event_verifier_worker.py`.** Тут проще: `batch`/`meta`/`seed_members` и так вычитываются одним блоком ДО цикла. Read-соединение можно закрыть сразу после этого вычитывания — **до первого вызова `call_model()`** — а каждый result дальше писать отдельным короткоживущим write-соединением (`db_conn = psycopg.connect(...)`, `insert_judgment`/`insert_verification(+members)`, `db_conn.commit()`, `finally: db_conn.close()`), по образцу `claim_extract_worker.py`. Итоговая схема: один короткий read в начале run() → N инференс-вызовов без единого открытого DB-соединения между ними → N коротких write-соединений, по одному на результат.
- **`event_merge_worker.py`.** Тут read нельзя вычитать один раз в начале run(), потому что `canonical_event`-состояние может измениться прямо внутри текущего прогона (item N создаёт/расширяет `canonical_event`, и это должно быть видно item'у N+1 — на это прямо указывает докстринг `get_canonical_membership`: *"поточний run міг щойно розширити/створити canonical_event на попередньому кроці"*). Значит read здесь нужен заново на каждый item, но — коротким соединением, не основным долгоживущим: **короткий read-conn → открыть, сделать `get_canonical_membership`×2 + `build_side_payload_seed`/`canonical`, собрать payload-снэпшот в Python → `close()` read-conn → `call_model()` без единого открытого DB-соединения → короткий write-conn → atomic persist (`insert_merge` + `create_canonical_event`/`extend_canonical_event`) → `commit()` → `close()`**. Это чуть больше соединений на item (два коротких вместо одного), но каждое живёт секунды, а не 600s, и в момент инференции к БД вообще нет активного соединения — ни транзакции, ни голого коннекта. Implementation detail, семантику `already_same_canonical`/`deferred_both_canonical`/decision-логику не меняет.

Итог по всем трём: во время `call_model()` не должно быть открытого DB-соединения вообще (ни read, ни write, ни просто "висящего" `conn`) — единственное отличие между файлами в том, где именно проходит граница "read перед батчем целиком" vs "read перед каждым item".

---

### F3 — Нет защиты от гонки при одновременном запуске двух инстансов одного worker'а

**Severity:** minor
**Status:** plausible risk (только при отклонении от документированной модели эксплуатации — один инстанс за раз; ни один из docstring'ов не описывает параллельный запуск, `run_eval.py`-инфраструктура явно помечена "без queue/broker, без parallel inference")
**Файлы:** те же три worker'а.

**Доказательство.** `fetch_batch`/`fetch_pending_candidates` делает `SELECT ... WHERE NOT EXISTS (...) ORDER BY ... LIMIT %s` без `FOR UPDATE SKIP LOCKED` и без какого-либо app-level lock. Если запустить два процесса одного и того же worker'а одновременно (например, случайно пересекшийся cron + ручной запуск), оба могут выбрать в свой batch один и тот же `candidate_pair_id`/`event_candidate_id`/`merge_candidate_id`, оба прогонят на нём дорогую (до 600s) LLM-инференцию, и при INSERT один из двух гарантированно словит `UniqueViolation` на `UNIQUE (..., llm_model_id, prompt_id, attempt_no)`.

**Реальный failure mode.** Не порча данных — DDL-констрейнт (defense in depth) корректно отбивает дубль. "Проигравший" процесс попадает в тот же `except Exception as db_err: conn.rollback(); return "error"`, что и обычная DB-ошибка — то есть по логам это неотличимо от F2 (в `error`-счётчике). Реальная цена — впустую потраченные ~600s инференции на Mamay на "проигравшую" сторону, не более того.

**Минимальный фикс.** Если параллельный запуск двух инстансов действительно не входит в модель эксплуатации (как следует из докстрингов) — можно **оставить как есть**: UNIQUE-констрейнт уже страхует от порчи данных, а стоимость — только впустую потраченный инференс-время при случайном двойном запуске, что редкий и самоочевидный (по дублирующимся логам) сценарий. Если хочется формально закрыть — `SELECT ... FOR UPDATE SKIP LOCKED` в `fetch_batch`/`fetch_pending_candidates` решает без изменения семантики отбора кандидатов, но это уже не обязательный минимальный патч, а опциональный hardening.

---

### F4 — `event_candidate_builder.py` жёстко фильтрует по `attempt_no=1`: фикс F1 без этой находки молча теряет результаты успешных retry

**Severity:** important сейчас, **эскалируется в blocker в момент деплоя фикса F1**, если деплоить F1 отдельно от этой находки.
**Status:** confirmed issue (найдено при повторной проверке, подтверждено прямой цитатой кода).
**Файл:** `experiments/event_candidates/event_candidate_builder.py`, функция `fetch_qualifying_edges`. Не входил в исходный основной scope (был помечен как context-only), но напрямую зависит от контракта, который меняет F1 — поэтому проверен отдельно.

**Доказательство.**

```python
RELATION_LLM_MODEL_ID = 1
RELATION_PROMPT_ID = 3
RELATION_ATTEMPT_NO = 1
```

```python
def fetch_qualifying_edges(conn) -> list[tuple]:
    ...
    cur.execute(
        """
        SELECT cp.claim_id_a, cp.claim_id_b, cp.score, rj.relation_label,
               rj.judgment_id, cp.candidate_pair_id
        FROM relation_judgments rj
        JOIN candidate_pairs cp ON cp.candidate_pair_id = rj.candidate_pair_id
        WHERE rj.llm_model_id = %s AND rj.prompt_id = %s AND rj.attempt_no = %s
          AND rj.status = 'valid'
          AND rj.relation_label = ANY(%s)
          AND rj.shared_referent_status = 'confirmed'
        """,
        (RELATION_LLM_MODEL_ID, RELATION_PROMPT_ID, RELATION_ATTEMPT_NO, list(QUALIFYING_LABELS)),
    )
```

Докстринг модуля прямо документирует это как часть contract'а: *"qualifying edge = relation_judgments.status='valid' AND relation_label IN (...) під зафіксованим relation-judgment контрактом (llm_model_id=1, prompt_id=3, attempt_no=1 -- поточний v3, referent gate)"* — то есть `attempt_no=1` здесь трактуется как часть identity модели/промпта, наравне с `llm_model_id`/`prompt_id`. Это неверно: `llm_model_id`/`prompt_id` — это версия модели и промпта (меняются редко, осознанно), а `attempt_no` — счётчик попыток одного и того же item (меняется в рамках нормальной работы retry-механизма). Смешение этих двух категорий в одну "контрактную" константу — это тот же самый концептуальный баг, что и в F1, только на стороне потребителя данных.

**Реальный failure mode.** Если применить фикс F1 (сделать `attempt_no` в `relation_judgment_worker.py` динамическим, `transport_error` — retryable) БЕЗ этой находки: пара, у которой попытка 1 упала с `transport_error`, а попытка 2 (уже с новым `attempt_no=2`) успешно получила `status='valid'` — для `event_candidate_builder.py` останется **невидимой навсегда**, потому что `fetch_qualifying_edges` жёстко ищет `attempt_no = 1`, а такой строки для этой пары больше никогда не будет (attempt 1 — `transport_error`, attempt 2 — `valid`, ни один вариант не удовлетворяет `attempt_no=1 AND status='valid'` одновременно). Никакой ошибки, никакого предупреждения — просто пара молча не попадает в качестве qualifying edge ни в один seed. Это хуже исходного бага F1: сейчас застрявшая `transport_error`-строка хотя бы видна в БД и в SUMMARY worker'а; после "исправления" F1 в одиночку результат успешного retry просто исчезает из пайплайна без следа.

**Сравнение с соседним скриптом (важно — это НЕ архитектурная проблема, паттерн правильного решения уже есть в репо).** `event_merge_candidate_builder.py` эту же категорию проблемы решает верно — его `fetch_accepted_seed_members()` вообще не фильтрует по `attempt_no`, только `status='valid' AND event_decision='accepted_seed' AND prompt_id=%s`, а затем в Python явно проверяет, не нашлось ли **больше одного** `attempt_no` со `status='valid'` для одного `event_candidate_id`, и если да — падает с explicit `RuntimeError` вместо того, чтобы молча выбрать один из них:

```python
multi_attempt = {cid: attempts for cid, attempts in seed_attempts.items() if len(attempts) > 1}
if multi_attempt:
    raise RuntimeError(
        f"event_candidate(s) with more than one valid accepted_seed verification attempt "
        f"found -- v1 builder does not know which to prefer, resolve manually: {multi_attempt}"
    )
```

Это уже написанный в репозитории правильный паттерн под тот же класс проблемы — фиксить `event_candidate_builder.py` можно копированием этого же подхода, а не изобретением нового.

**Минимальный фикс без изменения семантики.** В `fetch_qualifying_edges()` убрать `rj.attempt_no = %s`/`RELATION_ATTEMPT_NO` из `WHERE`/параметров — оставить только `llm_model_id`, `prompt_id`, `status='valid'`, `relation_label`, `shared_referent_status='confirmed'` (ровно то, что уже определяет "qualifying edge" по смыслу, без привязки к номеру попытки). Опционально — по образцу `event_merge_candidate_builder.py` — добавить явную проверку "не найдено ли больше одного `valid` attempt на одну `candidate_pair_id`" и `raise` вместо молчаливого выбора (при корректной работе F1's `valid`/`invalid`-terminal логики такого случая быть не должно, но проверка ничего не стоит и уже есть готовый пример в репо). Комментарий-докстринг про "attempt_no=1 як частина контракту" стоит поправить отдельно (это просто неточная формулировка, не код) — `attempt_no` не идентичность модели/промпта, а retry-счётчик.

**Важно для порядка деплоя:** этот патч и патч F1 для `relation_judgment_worker.py` — **не два независимых патча, а один атомарный релиз**. Деплоить F1 без F4 — регрессия хуже исходного бага (см. failure mode выше). F1 без F4 деплоить нельзя.

---

## 2. Проверено и подтверждено как корректное (без изменений)

Явно, как и просили — не выдумываю проблем там, где их нет:

- **Per-item транзакционные границы корректны.** Каждый элемент batch'а коммитится отдельно (`conn.commit()` внутри `process_pair`/`process_candidate` для каждого item), одна плохая пара/кандидат не валит остальной batch — питоновский `try/except` вокруг обработки одного элемента возвращает статус, а не пробрасывает исключение наверх (кроме сценария F2). Это тот же паттерн, что в `claim_extract_worker.py`/`contour_classification_worker.py`, и он реализован верно.
- **Дорогая инференс-работа не теряется из-за ошибки другого item.** Раз коммит per-item, а не per-batch (в отличие, например, от `embed_worker.py`/`routing_worker.py`, которые намеренно коммитят весь batch разом и падают целиком при любой ошибке) — успешно обработанный и закоммиченный элемент N остаётся в БД независимо от того, что случится с элементом N+1. Не проблема.
- **Multi-statement запись одного item атомарна, partial persistence не обнаружен.** В `event_verifier_worker.py` `insert_verification()` + `insert_verification_members()` выполняются на одном соединении **до** единственного `conn.commit()` — либо обе части пишутся, либо ни одна. То же самое в `event_merge_worker.py`: `insert_merge()` + `create_canonical_event()`/`extend_canonical_event()` (которая сама пишет и `canonical_events`/`UPDATE`, и `canonical_event_members`) — всё до одного `commit()`. Если процесс упадёт между этими вызовами — откатится всё целиком (implicit rollback при незакоммиченном соединении/явный `except`). Partial persistence не подтверждён.
- **`valid`/`invalid` корректно terminal.** Eligibility-запрос исключает элемент из будущих batch'ей, как только для него появилась строка с `attempt_no=ATTEMPT_NO`, независимо от `status` — для `valid`/`invalid` это ровно ожидаемое поведение (результат получен, пересчитывать нечего). Проблема (F1) — только в том, что `transport_error` ошибочно объединён с ними в одну "уже обработано" категорию, хотя по смыслу не должен быть terminal.
- **Idempotency при перезапуске самого worker-процесса (не PostgreSQL) — корректна.** Если убить/перезапустить сам скрипт между элементами batch'а (не PG, а сам python-процесс — OOM, Ctrl-C, деплой), при повторном запуске `fetch_batch` заново вычислит eligibility с нуля: уже закоммиченные элементы корректно исключатся, элемент, находившийся "в полёте" на момент убийства (для него ещё нет строки в БД, раз коммит не успел пройти), просто переберётся заново — без дублей, без порчи. Это именно то поведение, которое просили проверить в п.4 ("повторный запуск worker") — работает верно.
- **Semantic contracts не затронуты ни одной из находок.** Ни F1, ни F2, ни F3 не требуют трогать `relation_label`/`shared_referent_status`/referent gate, `event_decision`/`event_verification_members`-инвариант ("accepted_seed валиден только если anchor.included=true И ≥1 neighbor.included=true"), `event_merges.decision`/canonical-event merge-правила (single-hop, no connected-components, `already_same_canonical`/`deferred_both_canonical` skip-логика), тексты промптов или правила построения кандидатов (`event_candidate_builder.py` TOP_K=5/seed_version, `event_merge_candidate_builder.py` overlap-правило). Все предложенные минимальные патчи — это SQL eligibility-условие (`WHERE`) и то, какое соединение делает INSERT; ни один DB CHECK/UNIQUE и ни один validator (`validate_relation_judgment`, `validate_event_verification`, `validate_merge_decision`) не меняется.

---

## 3. Итог

**Что действительно надо исправить до live pipeline:**
1. **F1 + F4 вместе, одним релизом** (hardcoded `ATTEMPT_NO` → transport_error не retryable, ПЛЮС `event_candidate_builder.py`, который жёстко читает `attempt_no=1` и без F4 молча потеряет результаты retry, добавленного фиксом F1). Разносить их по разным деплоям нельзя — деплой одного F1 создаёт более тихую и более вредную версию исходной проблемы (см. F4 failure mode).
2. F2 (нет short-lived соединения для persist, незащищённый `rollback()`, а для `event_merge_worker.py` — ещё и implicit-транзакция реально открыта на каждый item во время всей инференции, не только riск обрыва, но и лишняя нагрузка на autovacuum) — без этого один PG-рестарт/сетевой сбой посреди batch'а роняет весь оставшийся прогон с traceback вместо graceful per-item `"error"`.

**Что можно оставить как есть:**
- F3 (race между конкурентными инстансами) — DDL уже страхует от порчи данных, цена — просто впустую потраченный инференс при случайном двойном запуске, которого документированная модель эксплуатации не предполагает. Если параллельный запуск воркеров всё же случается на практике — держать это через существующий operational-инвариант (внешний lock на весь Mamay-workload, если он уже используется для других воркеров) проще и безопаснее, чем `FOR UPDATE SKIP LOCKED`: чтобы row-lock реально резервировал item на время инференции, пришлось бы держать транзакцию открытой все 600s — то есть вернуть F2 через чёрный ход. `SKIP LOCKED` стоит рассматривать только если понадобится **несколько параллельных LLM-воркеров** как осознанная модель эксплуатации — тогда это отдельная небольшая задача, не экстренный патч.
- Всё из раздела 2 — транзакционные границы, atomicity multi-statement записи, per-item error isolation, idempotency при рестарте самого worker'а — уже корректны, трогать не нужно.

**Рекомендованный порядок минимальных патчей:**
1. Сначала F2 (connection lifetime) — он меняет только "какое соединение и когда" делает reads/INSERT, ортогонален F1/F4, и его проще всего проверить локально (перезапустить `postgresql` посреди `--limit 5` прогона на любом из трёх worker'ов и убедиться, что скрипт теперь не падает с traceback, а корректно останавливается/логирует по элементам).
2. Затем F1+F4 **одним патчем/релизом**: сначала `event_candidate_builder.py` (F4 — убрать фильтр по `attempt_no`), затем в том же релизе — `relation_judgment_worker.py` (F1), затем `event_verifier_worker.py` и `event_merge_worker.py` (у которых F1 без даунстрим-потребителя с hardcoded attempt_no, поэтому для них дополнительного F4-аналога не требуется — см. §1). После каждого файла — прогнать на реальном `transport_error`-кейсе (можно смоделировать, временно уронив Mamay endpoint) и убедиться, что при повторном запуске элемент **выбирается заново** с `attempt_no=2`, а результат retry **виден** в `event_candidate_builder.py`, а не теряется.
3. F3 — по желанию, не блокирует; если делать — через существующий `mamay.lock`-инвариант, а не `SKIP LOCKED`.

Ни один патч не требует трогать DDL (все нужные `CHECK`/`UNIQUE` уже на месте и совместимы с динамическим `attempt_no`), промпты, или relation_label/shared_referent_status/event_decision/merge decision логику.
