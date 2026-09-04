#!/usr/bin/env python3
"""
resolve_and_sectionize.py — pilot: для заданих occurrence_id резолвить
поточний успішний occurrence_content (identity tuple з claude/26), гонить
sectionize(), друкує sections для ручної перевірки.

Нічого не пише в БД. Mamay не викликає. Зупиняється після друку sections.
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

REPO = Path.home() / "mip"
sys.path.insert(0, str(REPO / "experiments" / "occurrence_content"))
sys.path.insert(0, str(REPO / "experiments" / "content_segmentation"))

from occurrence_content_worker import EXTRACTOR, EXTRACTOR_VERSION, EXTRACTION_PROFILE_VERSION  # noqa: E402
from sectionize import sectionize  # noqa: E402

DB_DSN = "dbname=mip_dev"

OCCURRENCE_IDS = {
    "brave1_digest": "ea29f3e7-b168-4f6c-955d-4ca67e2d970f",
    "single_topic": "a8698043-4665-4981-ba22-6c789ea62b90",
    "daily_frontline_summary": "a374a319-15ff-46cd-896a-7b6b81f8ca3e",
}


def resolve_structured_content(conn, occurrence_id: str) -> str | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT structured_content
            FROM occurrence_content
            WHERE occurrence_id = %s
              AND extractor = %s
              AND extractor_version = %s
              AND extraction_profile_version = %s
              AND status = 'success'
            ORDER BY attempt_no DESC
            LIMIT 1
            """,
            (occurrence_id, EXTRACTOR, EXTRACTOR_VERSION, EXTRACTION_PROFILE_VERSION),
        )
        row = cur.fetchone()
        return row["structured_content"] if row else None


def main() -> int:
    conn = psycopg.connect(DB_DSN)
    try:
        for label, occurrence_id in OCCURRENCE_IDS.items():
            print(f"=== {label} ({occurrence_id}) ===")
            structured_content = resolve_structured_content(conn, occurrence_id)
            if structured_content is None:
                print("  NOT FOUND: немає успішного occurrence_content для поточної identity.")
                print("  -> треба точковий прогін worker'а, потім перезапустити цей скрипт.")
                continue

            result = sectionize(structured_content)
            print(f"  document_heading={result.document_heading!r}")
            print(f"  sections={len(result.sections)}")
            for s in result.sections:
                preview = s.text[:120].replace("\n", " ")
                print(f"    [{s.section_id}] level={s.heading_level} heading={s.heading!r} "
                      f"blocks={s.block_indexes} len={len(s.text)} text[:120]={preview!r}")
            print()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
