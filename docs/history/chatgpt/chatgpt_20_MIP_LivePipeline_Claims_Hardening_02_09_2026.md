# МІП — live pipeline + claim-extraction hardening

**Дата:** 02.09.2026  
**Статус:** checkpoint після переходу claim extraction у live-режим.  
**Гілка:** `wip/demo-report-2026-08-27`.

## 1. Що змінилося

До цього claim extraction використовувався переважно як batch/backlog worker. Нічний великий прогін показав дві практичні межі:

1. один довгоживучий PostgreSQL connection через багатогодинний LLM batch є крихким;
2. обробка всього `analyze + maybe` на одній RTX 5090 не встигає за потоком нових routing-eligible items.

Після цього claim worker був переведений на більш production-like runtime contract і включений у live pipeline тільки для пріоритетного `routing=analyze`.

## 2. Нічний incident і підтверджений root cause

Довгий claim-extraction batch завершився помилкою PostgreSQL:

- `psycopg.errors.AdminShutdown: terminating connection due to administrator command`;
- наступний `rollback()` вже отримав `psycopg.OperationalError: the connection is lost`.

Перевірка apt/unattended-upgrades показала автоматичне оновлення системних пакетів у цей самий часовий інтервал. Практичний висновок для worker design: **не тримати один PostgreSQL connection через довгі Mamay inference loops**. Незалежно від конкретного механізму service restart, worker повинен переживати втрату старого DB connection між items.

## 3. Claim worker hardening

Commit `a4c07a0` — `Harden claim extraction retries and ordering`.

Ключові зміни:

- `transport_error` став retryable;
- `valid` / `invalid` — terminal;
- `next_attempt_no = max(attempt_no) + 1`;
- існуючий DB constraint `UNIQUE(content_id, llm_model_id, prompt_id, attempt_no)` використовується без нової DDL;
- deterministic rollback-test підтвердив контракт:
  - initial eligible → attempt 1;
  - synthetic transport error → retryable;
  - retry → attempt 2;
  - synthetic invalid → terminal/excluded;
  - transaction rollback → test rows не залишились;
- додано `--order oldest|newest` з backward-compatible legacy behavior;
- persistence переведено на короткі per-item DB connections, замість одного connection на весь довгий batch.

Smoke-test після hardening:

- Mamay `/health` → OK;
- `--limit 1 --decision analyze --order newest`;
- результат: `VALID attempt=1`, 8 claims, offsets 8/8 resolved, `transport_error=0`, `error=0`.

## 4. Виміряний capacity і чому live = analyze-only

На нічному batch було виміряно приблизний Mamay claim-extraction throughput близько **81 items/hour** (~44 s/item wall clock на довгому прогоні).

За спостереженим routing intake у вікні вимірювання:

- `analyze` ≈ **26 items/hour** у середньому;
- `maybe` ≈ **143 items/hour**;
- `analyze + maybe` ≈ **169 items/hour**.

Отже:

- `analyze` окремо вкладається в поточний single-GPU capacity з запасом;
- `maybe` є головною причиною необмеженого backlog росту;
- на цьому етапі не потрібно міняти Routing v1 thresholds лише через GPU backlog;
- правильне операційне рішення: **fresh fast lane = `analyze + newest`; `maybe` — background/backlog lane тільки за наявності ресурсу**.

Цей висновок узгоджується з попередніми routing calibration artifacts у `docs/history/claude/09` та `14–16`: `analyze` у Routing v1 був відносно чистим, а prototype surgery Routing v2 не дала надійного покращення precision.

## 5. Live wrapper

Commit `3c62611` — `Add live claim extraction runner`.

`ops/run_claims_live_once.sh`:

- використовує спільний `$HOME/.local/state/mip/locks/mamay.lock`;
- якщо інший Mamay workload вже тримає lock — run штатно SKIP;
- перед inference перевіряє `http://127.0.0.1:8080/health`;
- запускає:
  - `--limit 15`
  - `--decision analyze`
  - `--order newest`;
- пише окремий runtime log.

Manual smoke batch 15 items:

- `15/15 valid`;
- `0 invalid`;
- `0 transport_error`;
- `0 error`;
- wall time ~6m16s.

## 6. Cron live pipeline

Поточний cron contract на момент checkpoint:

```text
*/15 * * * *   ingest (RSS + Telegram → normalize/dedup)
7,37 * * * *   embeddings + routing
12,27,42,57 * * * *   live claim extraction: analyze + newest
```

Перші автоматичні claim runs підтвердили, що cron працює без ручного запуску:

- 08:12 UTC: `15/15 valid`, `transport_error=0`, `error=0`;
- 08:27 UTC: `14 valid / 1 invalid`, `transport_error=0`, `error=0`.

Єдиний `invalid` — штатний validator rejection через нерозв'язаний evidence span/offset. Це вже відомий semantic/adapter failure class з `docs/history/claude/07`, а не runtime outage.

## 7. GitHub connectivity

Прямий доступ сервера до GitHub був нестабільний/заблокований на стандартних шляхах. Для repo-local Git SSH використано вже наявний scoped SOCKS/VPN contour через локальний wrapper, без глобального переналаштування мережі хоста. Після цього push робочої гілки успішно відновлено.

Не зберігати в project docs конкретні proxy credentials/адреси; operational source of truth — локальний runtime config поза git.

## 8. Архітектурний висновок на наступний етап

Поточний напрямок: **Live Analytical Spine v1**.

Не потрібно зараз:

- переносити Mamay на інший inference backend без нового measured bottleneck;
- запускати Routing v2;
- пропускати весь `maybe` через live Mamay;
- вводити Redis/RabbitMQ/Kafka/Celery;
- ставити Hermes керувати deterministic processing pipeline;
- перепроектовувати relation/event semantics, які вже мають успішні vertical slices.

Потрібно:

1. довести contour classifier до того самого operational standard, що claims;
2. запускати claims і contours через єдиний Mamay resource budget/lock;
3. далі harden relation/event Mamay workers перед live scheduling;
4. cheap deterministic/pgvector stages можна запускати між дорогими LLM stages;
5. Hermes підключати пізніше як bounded analytical agent поверх evidence graph, а не як cron/orchestration replacement.

## 9. Наступний конкретний task

`experiments/content_contours/contour_classification_worker.py` ще тримає один PostgreSQL connection через Mamay batch. Підготовлено локальний patch, який змінює тільки DB lifecycle:

- initial registry/catalog/batch read — коротке connection;
- inference loop — без відкритого DB connection;
- transport-error persistence — нове коротке connection;
- valid/invalid persistence + assignments — нове коротке connection per item.

Не змінюються:

- classifier semantics;
- C4 provenance gate;
- suppressed C1 facets;
- retry/attempt semantics;
- routing filters;
- ordering;
- assignment rules.

На момент цього checkpoint patch ще не закоммічений; наступна перевірка — один real smoke item під спільним `mamay.lock`, після чого review/commit.
