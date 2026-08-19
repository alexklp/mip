#!/usr/bin/env python3
"""
Мінімальний RSS collector для ТСН — перша ітерація ingestion pilot.
Джерело: sources.url_or_handle = FEED_URL (має бути зареєстроване заздалегідь).

Контракт:
  RSS entry -> raw_items (raw, без дедуп, staging)
            -> normalization (strip html)
            -> content_items (hard dedup за content_hash нормалізованого тексту)
            -> item_occurrences (ідемпотентно за UNIQUE(source_id, external_ref))
"""

import hashlib
import html
import json
import re
import sys
from datetime import datetime, timezone

import feedparser
import psycopg

FEED_URL = "https://tsn.ua/rss/full.rss"
DB_DSN = "dbname=mip_dev"  # peer auth під поточним OS-юзером, без паролів у коді
COLLECTOR_NAME = "rss_collector"
COLLECTOR_VERSION = "0.1.0"

TAG_RE = re.compile(r"<[^>]+>")


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


def main():
    print(f"[collector] fetching {FEED_URL}")
    feed = feedparser.parse(FEED_URL)
    if feed.bozo:
        print(f"[collector] WARNING: feed parse issue: {feed.bozo_exception}", file=sys.stderr)
    entries = feed.entries
    print(f"[collector] {len(entries)} entries fetched")

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT source_id FROM sources WHERE url_or_handle = %s", (FEED_URL,))
            row = cur.fetchone()
            if row is None:
                print(f"[collector] ERROR: source not registered for {FEED_URL}", file=sys.stderr)
                sys.exit(1)
            source_id = row[0]
        conn.commit()

        inserted_raw = new_content = new_occurrence = skipped_dup = errors = 0

        for entry in entries:
            try:
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

                    # 1. raw_items -- завжди інсертимо, без дедуп (staging)
                    cur.execute(
                        """
                        INSERT INTO raw_items (source_id, collected_at, content_hash, payload, status)
                        VALUES (%s, %s, %s, %s, 'pending')
                        RETURNING raw_item_id
                        """,
                        (source_id, collected_at, raw_hash, json.dumps(payload, ensure_ascii=False)),
                    )
                    raw_item_id = cur.fetchone()[0]
                    inserted_raw += 1

                    # 2. normalization
                    norm_title = strip_html(title)
                    norm_text = strip_html(raw_summary)
                    canonical_text = f"{norm_title}\n{norm_text}".strip()
                    c_hash = content_hash_of(canonical_text)

                    # 3. dedup за content_hash -> content_items
                    cur.execute("SELECT content_id FROM content_items WHERE content_hash = %s", (c_hash,))
                    row = cur.fetchone()
                    if row:
                        content_id = row[0]
                    else:
                        cur.execute(
                            """
                            INSERT INTO content_items (content_hash, title, text_content, first_seen_at)
                            VALUES (%s, %s, %s, %s)
                            RETURNING content_id
                            """,
                            (c_hash, norm_title, canonical_text, collected_at),
                        )
                        content_id = cur.fetchone()[0]
                        new_content += 1

                    # 4. occurrence -- ідемпотентно за (source_id, external_ref)
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
                    if cur.fetchone():
                        new_occurrence += 1
                    else:
                        skipped_dup += 1

                conn.commit()
            except Exception as exc:
                conn.rollback()
                errors += 1
                print(f"[collector] ERROR on entry {getattr(entry, 'link', '?')}: {exc}", file=sys.stderr)

        print(
            f"[collector] done: raw={inserted_raw} new_content={new_content} "
            f"new_occurrence={new_occurrence} dup_occurrence={skipped_dup} errors={errors}"
        )


if __name__ == "__main__":
    main()