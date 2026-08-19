#!/usr/bin/env python3
"""
collectors/rss_worker.py — універсальний RSS-collector.

Читає активні RSS-джерела з `sources` (source_type='rss', is_active=true),
обходить кожне за контрактом:
  raw_items (staging, завжди) -> normalize -> reject (якщо текст закороткий)
                                            -> перевірка на "той самий external_ref,
                                               інший текст" (changed_unprocessed)
                                            -> content_items (hard dedup)
                                            -> item_occurrences (ідемпотентно)

Режими:
  --once            один прохід по всіх джерелах і вихід
  (без аргументів)  нескінченний цикл, пауза 300с між проходами

Проксі для .ru-джерел береться з MIP_RSS_PROXY_URL (env), не хардкодиться.
"""

import argparse
import hashlib
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import psycopg
import requests

DB_DSN = "dbname=mip_dev"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
COLLECTOR_NAME = "rss_worker"
COLLECTOR_VERSION = "0.3.0"
MIN_TEXT_LEN = 30
POLL_INTERVAL_SEC = 300
RU_PROXY_TIMEOUT = 20

TAG_RE = re.compile(r"<[^>]+>")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rss_worker")


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    if not url:
        return url
    url = url.split("#", 1)[0]
    return url.rstrip("/")


def content_hash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def entry_published_at(entry):
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    return None


def needs_proxy(url):
    hostname = urlparse(url).hostname or ""
    return hostname.endswith(".ru")


def get_proxy_url():
    proxy_url = os.environ.get("MIP_RSS_PROXY_URL")
    if not proxy_url:
        raise RuntimeError(
            "MIP_RSS_PROXY_URL не встановлено — потрібен для .ru-джерел. "
            "Приклад: export MIP_RSS_PROXY_URL=socks5h://10.200.200.2:1080 "
            "(див. .env.example)"
        )
    return proxy_url


def fetch_active_sources(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id, name, url_or_handle FROM sources "
            "WHERE source_type = 'rss' AND is_active = true"
        )
        return cur.fetchall()


def process_entry(conn, source_id, entry):
    """Повертає одне з: 'new_content', 'new_occurrence', 'dup_occurrence', 'rejected', 'changed_unprocessed'."""
    with conn.cursor() as cur:
        title = getattr(entry, "title", "") or ""
        raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
        link = getattr(entry, "link", "") or ""
        guid = getattr(entry, "id", None) or link
        published_at = entry_published_at(entry)
        collected_at = datetime.now(timezone.utc)
        ext_ref = canonical_url(link)

        payload = {
            "title": title,
            "summary_raw": raw_summary,
            "link": link,
            "guid": guid,
            "published": getattr(entry, "published", None),
            "collector_name": COLLECTOR_NAME,
            "collector_version": COLLECTOR_VERSION,
        }
        raw_hash = content_hash_of(json.dumps(payload, ensure_ascii=False, sort_keys=True))

        # 1. raw_items -- завжди, без дедупу (staging)
        cur.execute(
            """
            INSERT INTO raw_items (source_id, collected_at, content_hash, payload, status)
            VALUES (%s, %s, %s, %s, 'pending')
            RETURNING raw_item_id
            """,
            (source_id, collected_at, raw_hash, json.dumps(payload, ensure_ascii=False)),
        )
        raw_item_id = cur.fetchone()[0]

        # 2. normalization + primary filter
        norm_title = strip_html(title)
        norm_text = strip_html(raw_summary)
        canonical_text = f"{norm_title}\n{norm_text}".strip()

        if len(canonical_text) < MIN_TEXT_LEN:
            cur.execute(
                "UPDATE raw_items SET status = 'rejected' WHERE raw_item_id = %s",
                (raw_item_id,),
            )
            return "rejected"

        c_hash = content_hash_of(canonical_text)

        # 2.5. той самий (source_id, external_ref) вже мав occurrence з ІНШИМ хешем?
        # Якщо так — текст змінився. Не створюємо новий content_items (він лишиться
        # сиротою, бо occurrence все одно конфліктне і не оновиться). Явна pilot-політика:
        # зберігаємо raw, позначаємо, попереджаємо. Повна історія edits — пізніше.
        cur.execute(
            """
            SELECT ci.content_hash
            FROM item_occurrences io
            JOIN content_items ci ON ci.content_id = io.content_id
            WHERE io.source_id = %s AND io.external_ref = %s
            """,
            (source_id, ext_ref),
        )
        existing = cur.fetchone()

        if existing is not None and existing[0] != c_hash:
            cur.execute(
                "UPDATE raw_items SET status = 'changed_unprocessed' WHERE raw_item_id = %s",
                (raw_item_id,),
            )
            return "changed_unprocessed"

        # 3. dedup -> content_items
        cur.execute(
            """
            INSERT INTO content_items (content_hash, title, text_content, first_seen_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            RETURNING content_id
            """,
            (c_hash, norm_title, canonical_text, collected_at),
        )
        row = cur.fetchone()
        is_new_content = row is not None
        if row:
            content_id = row[0]
        else:
            cur.execute("SELECT content_id FROM content_items WHERE content_hash = %s", (c_hash,))
            content_id = cur.fetchone()[0]

        # 4. occurrence -- ідемпотентно
        cur.execute(
            """
            INSERT INTO item_occurrences
                (content_id, raw_item_id, source_id, external_ref, published_at, collected_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, external_ref) DO NOTHING
            RETURNING occurrence_id
            """,
            (content_id, raw_item_id, source_id, ext_ref, published_at, collected_at),
        )
        is_new_occurrence = cur.fetchone() is not None

        # 5. позначаємо raw_item опрацьованим
        cur.execute(
            "UPDATE raw_items SET status = 'normalized' WHERE raw_item_id = %s",
            (raw_item_id,),
        )

        if not is_new_occurrence:
            return "dup_occurrence"
        return "new_content" if is_new_content else "new_occurrence"


def run_source(conn, source_id, name, url):
    log.info(f"[{name}] fetching {url}")
    if needs_proxy(url):
        proxy_url = get_proxy_url()
        try:
            resp = requests.get(
                url,
                proxies={"http": proxy_url, "https": proxy_url},
                headers={"User-Agent": USER_AGENT},
                timeout=RU_PROXY_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            log.error(f"[{name}] proxy fetch failed: {exc}")
            return
        if resp.status_code != 200:
            log.error(f"[{name}] HTTP status {resp.status_code} via proxy — skipping")
            return
        feed = feedparser.parse(resp.content)
    else:
        feed = feedparser.parse(url, agent=USER_AGENT)
        status = feed.get("status")
        if status is not None and status != 200:
            log.error(f"[{name}] HTTP status {status} — likely dead/moved feed, skipping")
            return

    if feed.bozo:
        log.warning(f"[{name}] feed parse issue: {feed.bozo_exception}")
    entries = feed.entries
    log.info(f"[{name}] {len(entries)} entries fetched")

    stats = {
        "raw": 0, "new_content": 0, "new_occurrence": 0, "dup_occurrence": 0,
        "rejected": 0, "changed_unprocessed": 0, "errors": 0,
    }

    for entry in entries:
        try:
            result = process_entry(conn, source_id, entry)
            conn.commit()
            stats["raw"] += 1
            stats[result] += 1
            if result == "changed_unprocessed":
                log.warning(
                    f"[{name}] content changed for existing external_ref "
                    f"(raw kept, not processed): {getattr(entry, 'link', '?')}"
                )
        except Exception as exc:
            conn.rollback()
            stats["errors"] += 1
            log.error(f"[{name}] entry error ({getattr(entry, 'link', '?')}): {exc}")

    log.info(
        f"[{name}] done: raw={stats['raw']} new_content={stats['new_content']} "
        f"new_occurrence={stats['new_occurrence']} dup_occurrence={stats['dup_occurrence']} "
        f"rejected={stats['rejected']} changed_unprocessed={stats['changed_unprocessed']} "
        f"errors={stats['errors']}"
    )


def run_once():
    with psycopg.connect(DB_DSN) as conn:
        sources = fetch_active_sources(conn)
        log.info(f"active RSS sources: {len(sources)}")
        for source_id, name, url in sources:
            try:
                run_source(conn, source_id, name, url)
            except Exception as exc:
                conn.rollback()
                log.error(f"[{name}] SOURCE-LEVEL error, skipping: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Universal RSS collector for MIP")
    parser.add_argument("--once", action="store_true", help="single pass over all sources, then exit")
    args = parser.parse_args()

    if args.once:
        run_once()
        return

    log.info(f"starting infinite loop, interval={POLL_INTERVAL_SEC}s")
    while True:
        try:
            run_once()
        except Exception as exc:
            log.error(f"top-level run_once() failed: {exc}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
