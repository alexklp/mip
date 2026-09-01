#!/usr/bin/env python3
"""
collectors/tg_web_worker.py — універсальний Telegram web-preview collector.

Читає активні джерела з `sources` (source_type='telegram', url_or_handle LIKE
'https://t.me/s/%', is_active=true) — навмисно 'telegram', не 'web': це платформа
джерела, а не спосіб доступу (web-preview зараз, MTProto пізніше — різняться по
URL-патерну, не по source_type). Парсить https://t.me/s/<channel> (без логіна,
без MTProto, без коментарів — тільки останні ~20 постів каналу). Контракт
ідентичний rss_worker.py: raw_items -> normalize -> перевірка на "той самий
external_ref, інший текст" (changed_unprocessed) -> content_items (dedup) ->
item_occurrences (idempotent).

Режими:
  --once            один прохід по всіх джерелах і вихід
  (без аргументів)  нескінченний цикл, пауза 300с між проходами
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

import psycopg
import requests
from bs4 import BeautifulSoup

DB_DSN = "dbname=mip_dev"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
COLLECTOR_NAME = "tg_web_worker"
COLLECTOR_VERSION = "0.2.1"
MIN_TEXT_LEN = 30
POLL_INTERVAL_SEC = 300
BETWEEN_SOURCES_DELAY_SEC = 0.7  # ввічливість до t.me, не женемо скрейпінг

TAG_RE = re.compile(r"<[^>]+>")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tg_web_worker")


def strip_html(raw):
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def content_hash_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_active_sources(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id, name, url_or_handle FROM sources "
            "WHERE source_type = 'telegram' AND url_or_handle LIKE 'https://t.me/s/%%' "
            "AND is_active = true"
        )
        return cur.fetchall()


def get_proxy_url():
    """Optional SOCKS proxy for Telegram web-preview.

    Telegram is accessed directly by default. Set MIP_TG_PROXY_URL only
    when this deployment explicitly requires Telegram traffic through SOCKS.
    """
    return os.environ.get("MIP_TG_PROXY_URL")


def fetch_posts(url):
    proxy_url = get_proxy_url()

    request_kwargs = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": 20,
    }

    if proxy_url:
        request_kwargs["proxies"] = {
            "http": proxy_url,
            "https": proxy_url,
        }

    last_exc = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(
                url,
                **request_kwargs,
            )
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt == 3:
                raise
            log.warning(
                "fetch attempt %d/3 failed for %s: %s; retrying",
                attempt,
                url,
                exc,
            )
            time.sleep(2 * attempt)
    else:
        raise last_exc
    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        data_post = msg.get("data-post")
        text_div = msg.select_one(".tgme_widget_message_text")
        raw_text = str(text_div) if text_div else ""
        time_tag = msg.select_one(".tgme_widget_message_date time")
        published_at = None
        if time_tag and time_tag.get("datetime"):
            published_at = datetime.fromisoformat(time_tag["datetime"])
        posts.append({
            "data_post": data_post,
            "raw_html": raw_text,
            "published_at": published_at,
        })
    return posts


def process_post(conn, source_id, post):
    """Повертає одне з: 'new_content', 'new_occurrence', 'dup_occurrence', 'rejected', 'changed_unprocessed'."""
    with conn.cursor() as cur:
        data_post = post["data_post"]
        external_ref = f"https://t.me/{data_post}"
        collected_at = datetime.now(timezone.utc)

        payload = {
            "data_post": data_post,
            "raw_html": post["raw_html"],
            "published_at": post["published_at"].isoformat() if post["published_at"] else None,
            "collector_name": COLLECTOR_NAME,
            "collector_version": COLLECTOR_VERSION,
        }
        raw_hash = content_hash_of(json.dumps(payload, ensure_ascii=False, sort_keys=True))

        cur.execute(
            """
            INSERT INTO raw_items (source_id, collected_at, content_hash, payload, status)
            VALUES (%s, %s, %s, %s, 'pending')
            RETURNING raw_item_id
            """,
            (source_id, collected_at, raw_hash, json.dumps(payload, ensure_ascii=False)),
        )
        raw_item_id = cur.fetchone()[0]

        norm_text = strip_html(post["raw_html"])

        if len(norm_text) < MIN_TEXT_LEN:
            cur.execute(
                "UPDATE raw_items SET status = 'rejected' WHERE raw_item_id = %s",
                (raw_item_id,),
            )
            return "rejected"

        c_hash = content_hash_of(norm_text)

        # той самий (source_id, external_ref) вже мав occurrence з ІНШИМ хешем?
        # Пост відредаговано в Telegram — не створюємо новий content_items-сироту,
        # позначаємо raw, попереджаємо. Повна історія edits — пізніше.
        cur.execute(
            """
            SELECT ci.content_hash
            FROM item_occurrences io
            JOIN content_items ci ON ci.content_id = io.content_id
            WHERE io.source_id = %s AND io.external_ref = %s
            """,
            (source_id, external_ref),
        )
        existing = cur.fetchone()

        if existing is not None and existing[0] != c_hash:
            cur.execute(
                "UPDATE raw_items SET status = 'changed_unprocessed' WHERE raw_item_id = %s",
                (raw_item_id,),
            )
            return "changed_unprocessed"

        cur.execute(
            """
            INSERT INTO content_items (content_hash, title, text_content, first_seen_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            RETURNING content_id
            """,
            (c_hash, norm_text[:200], norm_text, collected_at),
        )
        row = cur.fetchone()
        is_new_content = row is not None
        if row:
            content_id = row[0]
        else:
            cur.execute("SELECT content_id FROM content_items WHERE content_hash = %s", (c_hash,))
            content_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO item_occurrences
                (content_id, raw_item_id, source_id, external_ref, published_at, collected_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, external_ref) DO NOTHING
            RETURNING occurrence_id
            """,
            (content_id, raw_item_id, source_id, external_ref, post["published_at"], collected_at),
        )
        is_new_occurrence = cur.fetchone() is not None

        cur.execute(
            "UPDATE raw_items SET status = 'normalized' WHERE raw_item_id = %s",
            (raw_item_id,),
        )

        if not is_new_occurrence:
            return "dup_occurrence"
        return "new_content" if is_new_content else "new_occurrence"


def run_source(conn, source_id, name, url):
    log.info(f"[{name}] fetching {url}")
    try:
        posts = fetch_posts(url)
    except requests.exceptions.RequestException as exc:
        log.error(f"[{name}] fetch failed: {exc}")
        return
    log.info(f"[{name}] {len(posts)} posts fetched")

    stats = {
        "raw": 0, "new_content": 0, "new_occurrence": 0, "dup_occurrence": 0,
        "rejected": 0, "changed_unprocessed": 0, "errors": 0,
    }

    for post in posts:
        try:
            result = process_post(conn, source_id, post)
            conn.commit()
            stats["raw"] += 1
            stats[result] += 1
            if result == "changed_unprocessed":
                log.warning(
                    f"[{name}] content changed for existing external_ref "
                    f"(raw kept, not processed): {post.get('data_post', '?')}"
                )
        except Exception as exc:
            conn.rollback()
            stats["errors"] += 1
            log.error(f"[{name}] post error ({post.get('data_post', '?')}): {exc}")

    log.info(
        f"[{name}] done: raw={stats['raw']} new_content={stats['new_content']} "
        f"new_occurrence={stats['new_occurrence']} dup_occurrence={stats['dup_occurrence']} "
        f"rejected={stats['rejected']} changed_unprocessed={stats['changed_unprocessed']} "
        f"errors={stats['errors']}"
    )


def run_once():
    with psycopg.connect(DB_DSN) as conn:
        sources = fetch_active_sources(conn)
        log.info(f"active web (TG) sources: {len(sources)}")
        for source_id, name, url in sources:
            try:
                run_source(conn, source_id, name, url)
            except Exception as exc:
                conn.rollback()
                log.error(f"[{name}] SOURCE-LEVEL error, skipping: {exc}")
            time.sleep(BETWEEN_SOURCES_DELAY_SEC)


def main():
    parser = argparse.ArgumentParser(description="Telegram web-preview collector for MIP")
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
