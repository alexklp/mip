#!/usr/bin/env python3
"""
collectors/tg_web_rian.py — прототип web-preview collector для Telegram (t.me/s/<channel>).

Без логіна, без MTProto, без коментарів — тільки останні ~20 постів каналу.
Перевірка контракту на одному джерелі (РИА Новости, rian_ru) перед генералізацією.
"""

import hashlib
import html
import json
import logging
import re
from datetime import datetime, timezone

import psycopg
import requests
from bs4 import BeautifulSoup

DB_DSN = "dbname=mip_dev"
SOURCE_ID = "5597d7a7-f808-443d-9cde-2ee5a86d0676"  # РИА Новости (TG)
CHANNEL_URL = "https://t.me/s/rian_ru"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
COLLECTOR_NAME = "tg_web_rian_prototype"
COLLECTOR_VERSION = "0.1.0"
MIN_TEXT_LEN = 30

TAG_RE = re.compile(r"<[^>]+>")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tg_web_rian")


def strip_html(raw):
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def content_hash_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_posts(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        data_post = msg.get("data-post")  # "rian_ru/123456"
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
    """Повертає одне з: 'new_content', 'new_occurrence', 'dup_occurrence', 'rejected'."""
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


def main():
    log.info(f"fetching {CHANNEL_URL}")
    posts = fetch_posts(CHANNEL_URL)
    log.info(f"{len(posts)} posts fetched")

    stats = {"raw": 0, "new_content": 0, "new_occurrence": 0, "dup_occurrence": 0, "rejected": 0, "errors": 0}

    with psycopg.connect(DB_DSN) as conn:
        for post in posts:
            try:
                result = process_post(conn, SOURCE_ID, post)
                conn.commit()
                stats["raw"] += 1
                stats[result] += 1
            except Exception as exc:
                conn.rollback()
                stats["errors"] += 1
                log.error(f"post error ({post.get('data_post', '?')}): {exc}")

    log.info(
        f"done: raw={stats['raw']} new_content={stats['new_content']} "
        f"new_occurrence={stats['new_occurrence']} dup_occurrence={stats['dup_occurrence']} "
        f"rejected={stats['rejected']} errors={stats['errors']}"
    )


if __name__ == "__main__":
    main()
