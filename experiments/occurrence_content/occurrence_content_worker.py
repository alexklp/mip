#!/usr/bin/env python3
"""
experiments/occurrence_content/occurrence_content_worker.py — v1 occurrence-level
full-text extraction worker.

Контракт — точно за claude/26_MIP_OccurrenceContent_v1_LLD (ревізія 2,
затверджено 03.09.2026):

  item_occurrences (RSS-only v1, source_type='rss')
    -> fetch article HTML за external_ref (проксі-політика й User-Agent —
       той самий MIP_RSS_PROXY_URL/.ru-евристика, що collectors/rss_worker.py,
       продубльовано навмисно, за "не ділити worker як library" рішенням)
    -> ОДИН trafilatura.bare_extraction() pass
    -> text_content і structured_content — похідні з ОДНОГО document.body
       (xmltotxt() для тексту, lxml.etree.tostring() для XML — обидва
       викликаються на тому самому LXML-дереві, без другого extraction pass)
    -> occurrence_content (append-only: кожен реальний attempt пишеться,
       success/fetch_error/extraction_error — один термінальний статус,
       next_attempt_no per extraction identity)

Не чіпає: rss_worker.py, tg_web_worker.py, content_items, embeddings, claims,
routing. Не робить: segmentation, scheduler/cron, mass backfill (--limit
обов'язковий за замовчуванням, немає режиму "усі occurrences").

text_hash — ВИКЛЮЧНО downstream-сигнал (сегментація вирішує, чи текст
змінився і чи є сенс переробляти). Він НЕ впливає на те, чи писати рядок:
кожен реально виконаний fetch+extraction завжди пише attempt, success чи
error, без виключень.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import requests
import trafilatura
from lxml import etree
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from trafilatura.xml import xmltotxt

DB_DSN = "dbname=mip_dev"

EXTRACTOR = "trafilatura"
EXTRACTOR_VERSION = pkg_version("trafilatura")
EXTRACTION_PROFILE_VERSION = "v1"

# Набір kwargs "профілю v1" — фіксований у коді, версіюється bump'ом
# EXTRACTION_PROFILE_VERSION, не registry-таблицею (claude/26 п.1).
#
# deduplicate=False — НАВМИСНО, не default бібліотеки. Перевірено емпірично
# (тестом нижче, test_single_bare_extraction_call / debug-прогін перед
# відправкою): trafilatura's deduplicate=True тримає PROCESS-GLOBAL стан
# (LRU дублікат-детектор), який переживає між ОКРЕМИМИ викликами
# bare_extraction() в одному процесі. На 4-й+ виклик з тим самим/near-identical
# текстом (навіть для РІЗНИХ occurrence, різних URL) bare_extraction() тихо
# повертає None — увесь документ трактується як "вже бачений boilerplate".
# Для батч-воркера, що обробляє багато occurrences в одному процесі, це
# небезпечно САМЕ для templated-alert кейсу з claude/26 розділу 5
# (шаблонні air-raid/землетрус-алерти) — легітимні різні occurrences могли б
# отримувати фальшивий extraction_error залежно від порядку обробки в батчі,
# непередбачувано й важко для дебагу. Ціна відмови — трохи гірше прибирання
# внутрідокументних дублікатів-абзаців; це прийнятний обмін.
EXTRACTION_PROFILE_KWARGS = dict(
    favor_precision=True,   # менше шуму (меню/related-widgets) важливіше за recall
    include_comments=False,  # коментарі читачів — не частина статті
    include_tables=True,     # таблиці даних аналітично цінні
    include_formatting=True,  # зберігаємо inline-структуру в дереві для structured_content
    include_images=False,
    include_links=False,
    deduplicate=False,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LIMIT = 20
FETCH_TIMEOUT = 20
RU_PROXY_TIMEOUT = 20
MIN_TEXT_LEN = 30  # той самий поріг, що collectors/rss_worker.py MIN_TEXT_LEN

# Той самий User-Agent, що collectors/rss_worker.py.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

REPO_ROOT = Path(__file__).resolve().parents[2]  # experiments/occurrence_content/../.. = ~/mip


def get_code_revision() -> str:
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT
    ).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


# ---------------------------------------------------------------------------
# Fetch: та сама проксі-політика, що collectors/rss_worker.py (продубльовано
# навмисно — той самий "не ділити worker як library" паттерн, що вже
# застосований до build_context_snippet у трьох relation-воркерах).
# ---------------------------------------------------------------------------

def needs_proxy(url: str) -> bool:
    hostname = urlparse(url).hostname or ""
    return hostname.endswith(".ru")


def get_proxy_url() -> str:
    proxy_url = os.environ.get("MIP_RSS_PROXY_URL")
    if not proxy_url:
        raise RuntimeError(
            "MIP_RSS_PROXY_URL не встановлено — потрібен для .ru-джерел. "
            "Той самий env var, що collectors/rss_worker.py."
        )
    return proxy_url


def fetch_article(url: str):
    """Повертає (final_url, http_status, raw_html). Кидає
    requests.exceptions.RequestException при помилці — виклик обгортає caller."""
    kwargs = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": RU_PROXY_TIMEOUT if needs_proxy(url) else FETCH_TIMEOUT,
        "allow_redirects": True,
    }
    if needs_proxy(url):
        proxy_url = get_proxy_url()
        kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}

    resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    return resp.url, resp.status_code, resp.text


# ---------------------------------------------------------------------------
# Extraction: ОДИН bare_extraction() pass. text_content і structured_content —
# похідні з того самого document.body, не з двох незалежних extract() викликів.
# ---------------------------------------------------------------------------

def extract_once(raw_html: str, url: str) -> dict | None:
    """Повертає dict(text_content, text_hash, structured_content,
    structured_content_format) або None, якщо екстракція не дала змістовного
    тексту. Один виклик trafilatura.bare_extraction() на весь результат —
    інваріант "один attempt = один документ" (claude/26 revision 2, розділ 2/6.1)."""
    document = trafilatura.bare_extraction(
        raw_html,
        url=url,
        output_format="python",
        **EXTRACTION_PROFILE_KWARGS,
    )
    if document is None or document.body is None:
        return None

    # xmltotxt(..., include_formatting=False) — той самий плоский txt-контракт,
    # що вже є в content_items.text_content. Те саме дерево, що й нижче.
    text_content = xmltotxt(document.body, False)
    if not text_content or len(text_content.strip()) < MIN_TEXT_LEN:
        return None

    # lxml.etree.tostring напряму на document.body (не trafilatura-internal
    # control_xml_output) — стабільний публічний API lxml, і свідомо БЕЗ
    # metadata-обгортки trafilatura (title/author/date) — це поза scope
    # (claude/26 розділ 2, явний non-goal).
    structured_content = etree.tostring(document.body, pretty_print=True, encoding="unicode")

    return {
        "text_content": text_content,
        "text_hash": hashlib.sha256(text_content.encode("utf-8")).hexdigest(),
        "structured_content": structured_content,
        "structured_content_format": "trafilatura_xml",
    }


# ---------------------------------------------------------------------------
# Persistence layer — усі функції беруть conn ззовні (testable/injectable,
# той самий стиль, що insert_run() у claim_extract_worker.py).
# ---------------------------------------------------------------------------

def compute_next_attempt_no(
    conn,
    occurrence_id,
    *,
    extractor: str = EXTRACTOR,
    extractor_version: str = EXTRACTOR_VERSION,
    profile_version: str = EXTRACTION_PROFILE_VERSION,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(attempt_no), 0) + 1
            FROM occurrence_content
            WHERE occurrence_id = %s
              AND extractor = %s
              AND extractor_version = %s
              AND extraction_profile_version = %s
            """,
            (occurrence_id, extractor, extractor_version, profile_version),
        )
        return cur.fetchone()[0]


def fetch_eligible_occurrences(
    conn,
    *,
    extractor: str = EXTRACTOR,
    extractor_version: str = EXTRACTOR_VERSION,
    profile_version: str = EXTRACTION_PROFILE_VERSION,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    source_type: str = "rss",
    limit: int = DEFAULT_LIMIT,
    occurrence_id=None,
):
    """RSS-only v1 eligibility: немає success для поточної identity, і
    кількість error-attempts для поточної identity < max_attempts.

    `occurrence_id` — опційний фільтр на одне конкретне occurrence; None
    (default) не змінює production-поведінку. Доданий для testability:
    без нього тест не міг би детерміновано перевірити eligibility однієї
    конкретної occurrence на фоні реального ~16k-рядкового production
    датасету (top-N за collected_at не гарантує влучення в вибірку)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT io.occurrence_id, io.external_ref
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            WHERE s.source_type = %s
              AND (%s::uuid IS NULL OR io.occurrence_id = %s::uuid)
              AND NOT EXISTS (
                  SELECT 1 FROM occurrence_content oc
                  WHERE oc.occurrence_id = io.occurrence_id
                    AND oc.extractor = %s
                    AND oc.extractor_version = %s
                    AND oc.extraction_profile_version = %s
                    AND oc.status = 'success'
              )
              AND (
                  SELECT COUNT(*) FROM occurrence_content oc
                  WHERE oc.occurrence_id = io.occurrence_id
                    AND oc.extractor = %s
                    AND oc.extractor_version = %s
                    AND oc.extraction_profile_version = %s
                    AND oc.status IN ('fetch_error', 'extraction_error')
              ) < %s
            ORDER BY io.collected_at DESC
            LIMIT %s
            """,
            (
                source_type,
                occurrence_id, occurrence_id,
                extractor, extractor_version, profile_version,
                extractor, extractor_version, profile_version,
                max_attempts, limit,
            ),
        )
        return cur.fetchall()


def insert_attempt(
    conn,
    *,
    occurrence_id,
    fetch_url: str,
    status: str,
    attempt_no: int,
    code_revision: str,
    final_url: str | None = None,
    http_status: int | None = None,
    raw_html: str | None = None,
    text_content: str | None = None,
    text_hash: str | None = None,
    structured_content: str | None = None,
    structured_content_format: str | None = None,
    errors: list | None = None,
    latency_ms: int | None = None,
    extractor: str = EXTRACTOR,
    extractor_version: str = EXTRACTOR_VERSION,
    profile_version: str = EXTRACTION_PROFILE_VERSION,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO occurrence_content
                (occurrence_id, extractor, extractor_version, extraction_profile_version, attempt_no,
                 status, fetch_url, final_url, http_status, raw_html,
                 text_content, text_hash, structured_content, structured_content_format,
                 errors, code_revision, latency_ms)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s)
            RETURNING occurrence_content_id
            """,
            (
                occurrence_id, extractor, extractor_version, profile_version, attempt_no,
                status, fetch_url, final_url, http_status, raw_html,
                text_content, text_hash, structured_content, structured_content_format,
                Jsonb(errors) if errors else None, code_revision, latency_ms,
            ),
        )
        return cur.fetchone()[0]


def resolve_current_occurrence_content(
    conn,
    occurrence_id,
    *,
    extractor: str = EXTRACTOR,
    extractor_version: str = EXTRACTOR_VERSION,
    profile_version: str = EXTRACTION_PROFILE_VERSION,
):
    """Explicit resolver за extraction identity (claude/26 revision 2, п.6.2):
    identity приходить від споживача, НЕ вгадується з created_at/version-рядків."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT *
            FROM occurrence_content
            WHERE occurrence_id = %s
              AND extractor = %s
              AND extractor_version = %s
              AND extraction_profile_version = %s
              AND status = 'success'
            ORDER BY attempt_no DESC
            LIMIT 1
            """,
            (occurrence_id, extractor, extractor_version, profile_version),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Orchestration: per-item короткі DB-з'єднання (той самий hardened pattern,
# що claim_extract_worker.py після incident 02.09.2026 — не тримати одне
# з'єднання через мережевий fetch).
# ---------------------------------------------------------------------------

def process_occurrence(
    occurrence_id,
    external_ref: str,
    code_revision: str,
    *,
    extractor: str = EXTRACTOR,
    extractor_version: str = EXTRACTOR_VERSION,
    profile_version: str = EXTRACTION_PROFILE_VERSION,
) -> str:
    """Повертає 'success' | 'fetch_error' | 'extraction_error'.

    Identity (`extractor`/`extractor_version`/`profile_version`) — явні
    keyword-параметри з дефолтами на реальні module-level константи, НЕ
    непряме читання глобалів на кожен виклик: mock.patch.object() на
    EXTRACTOR_VERSION/EXTRACTION_PROFILE_VERSION інакше НЕ подіяв би --
    Python зв'язує значення default-параметра під час визначення функції,
    а не на кожен виклик, тож без явної передачі тести тихо писали б у
    реальну identity замість тестового маркера. Знайдено й виправлено
    прогоном тестів проти реальної Postgres перед відправкою."""
    conn = psycopg.connect(DB_DSN)
    try:
        attempt_no = compute_next_attempt_no(
            conn, occurrence_id,
            extractor=extractor, extractor_version=extractor_version, profile_version=profile_version,
        )

        try:
            final_url, http_status, raw_html = fetch_article(external_ref)
        except requests.exceptions.RequestException as exc:
            insert_attempt(
                conn,
                occurrence_id=occurrence_id,
                fetch_url=external_ref,
                status="fetch_error",
                attempt_no=attempt_no,
                code_revision=code_revision,
                errors=[f"{type(exc).__name__}: {exc}"],
                extractor=extractor, extractor_version=extractor_version, profile_version=profile_version,
            )
            conn.commit()
            return "fetch_error"

        result = extract_once(raw_html, final_url)
        if result is None:
            insert_attempt(
                conn,
                occurrence_id=occurrence_id,
                fetch_url=external_ref,
                status="extraction_error",
                attempt_no=attempt_no,
                code_revision=code_revision,
                final_url=final_url,
                http_status=http_status,
                raw_html=raw_html,
                errors=["empty or failed extraction"],
                extractor=extractor, extractor_version=extractor_version, profile_version=profile_version,
            )
            conn.commit()
            return "extraction_error"

        insert_attempt(
            conn,
            occurrence_id=occurrence_id,
            fetch_url=external_ref,
            status="success",
            attempt_no=attempt_no,
            code_revision=code_revision,
            final_url=final_url,
            http_status=http_status,
            raw_html=raw_html,
            extractor=extractor, extractor_version=extractor_version, profile_version=profile_version,
            **result,
        )
        conn.commit()
        return "success"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="максимум occurrences за прогін (обов'язковий стеля, немає режиму 'всі')")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--dry-run", action="store_true", help="показати eligible occurrences, нічого не писати в БД і не ходити в мережу")
    parser.add_argument("--occurrence-id", type=str, default=None, help="точковий прогін одного occurrence (testability-фільтр, вже підтримується fetch_eligible_occurrences)")
    args = parser.parse_args()

    code_revision = get_code_revision()

    conn = psycopg.connect(DB_DSN)
    try:
        eligible = fetch_eligible_occurrences(conn, max_attempts=args.max_attempts, limit=args.limit, occurrence_id=args.occurrence_id)
    finally:
        conn.close()

    print(
        f"[occurrence_content_worker] eligible={len(eligible)} "
        f"extractor_version={EXTRACTOR_VERSION} profile={EXTRACTION_PROFILE_VERSION} "
        f"code_revision={code_revision}"
    )

    if args.dry_run:
        for occurrence_id, external_ref in eligible:
            print(f"  DRY-RUN {occurrence_id} {external_ref}")
        return 0

    stats = {"success": 0, "fetch_error": 0, "extraction_error": 0, "unexpected_error": 0}
    for occurrence_id, external_ref in eligible:
        try:
            result = process_occurrence(occurrence_id, external_ref, code_revision)
        except Exception as exc:
            print(f"[{occurrence_id}] UNEXPECTED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            stats["unexpected_error"] += 1
            continue
        stats[result] += 1
        print(f"[{occurrence_id}] {result} ({external_ref})")

    print(f"[occurrence_content_worker] done: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
