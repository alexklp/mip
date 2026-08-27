#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from nlp_prepare import DB_DSN


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"
TOPICS_FILE = OUTPUT_DIR / "demo_topics.json"
OUTPUT_FILE = OUTPUT_DIR / "demo_examples.json"

TOP_UNIGRAM_TERMS = 12
TOP_EMERGING_TERMS = 8
EXAMPLES_PER_TERM = 5


def many(conn, sql: str, params=None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def selected_terms(topics: dict) -> dict[str, dict[str, list[dict]]]:
    result = {}

    for group, block in topics["groups"].items():
        result[group] = {
            "top_unigrams": block["top_unigrams"][:TOP_UNIGRAM_TERMS],
            "emerging": block["bigram_shift"]["emerging"][:TOP_EMERGING_TERMS],
        }

    return result


def collect_candidate_ids(selection: dict) -> set[str]:
    ids = set()

    for group_block in selection.values():
        for row in group_block["top_unigrams"]:
            ids.update(row.get("content_ids", []))

        for row in group_block["emerging"]:
            ids.update(row.get("new_content_ids", []))

    return ids


def build_metadata(conn, content_ids: set[str]) -> dict[str, dict]:
    if not content_ids:
        return {}

    rows = many(
        conn,
        """
        SELECT
            ci.content_id::text,
            ci.title,
            left(
                regexp_replace(ci.text_content, '\\s+', ' ', 'g'),
                520
            ) AS text_preview,
            ci.first_seen_at,
            array_agg(DISTINCT s.name ORDER BY s.name)
                FILTER (WHERE s.name IS NOT NULL) AS source_names,
            array_agg(DISTINCT s.source_type ORDER BY s.source_type)
                FILTER (WHERE s.source_type IS NOT NULL) AS source_types,
            array_agg(DISTINCT s.source_group ORDER BY s.source_group)
                FILTER (WHERE s.source_group IS NOT NULL) AS source_groups,
            min(io.published_at) AS published_at
        FROM content_items ci
        LEFT JOIN item_occurrences io
          ON io.content_id = ci.content_id
        LEFT JOIN sources s
          ON s.source_id = io.source_id
        WHERE ci.content_id = ANY(%s::uuid[])
        GROUP BY
            ci.content_id,
            ci.title,
            ci.text_content,
            ci.first_seen_at
        """,
        (list(sorted(content_ids)),),
    )

    return {
        row["content_id"]: row
        for row in rows
    }


def choose_examples(
    ids: list[str],
    metadata: dict[str, dict],
    limit: int,
) -> list[dict]:
    candidates = [
        metadata[cid]
        for cid in ids
        if cid in metadata
    ]

    candidates.sort(
        key=lambda row: (
            str(row["first_seen_at"]),
            row["content_id"],
        )
    )

    selected = []
    used_sources = set()

    # Pass 1: maximize source diversity.
    for row in candidates:
        sources = tuple(row.get("source_names") or [])
        primary_source = sources[0] if sources else "unknown"

        if primary_source in used_sources:
            continue

        selected.append(row)
        used_sources.add(primary_source)

        if len(selected) >= limit:
            return selected

    # Pass 2: fill remaining slots deterministically.
    selected_ids = {
        row["content_id"]
        for row in selected
    }

    for row in candidates:
        if row["content_id"] in selected_ids:
            continue

        selected.append(row)

        if len(selected) >= limit:
            break

    return selected


def compact(row: dict) -> dict:
    return {
        "content_id": row["content_id"],
        "title": row["title"],
        "text_preview": row["text_preview"],
        "first_seen_at": row["first_seen_at"],
        "published_at": row["published_at"],
        "source_names": row["source_names"] or [],
        "source_types": row["source_types"] or [],
        "source_groups": row["source_groups"] or [],
    }


def main() -> int:
    topics = json.loads(
        TOPICS_FILE.read_text(encoding="utf-8")
    )

    selection = selected_terms(topics)
    candidate_ids = collect_candidate_ids(selection)

    print(f"candidate_content_ids={len(candidate_ids)}")

    with psycopg.connect(DB_DSN) as conn:
        metadata = build_metadata(
            conn,
            candidate_ids,
        )

    print(f"metadata_rows={len(metadata)}")

    output = {
        "meta": {
            "examples_per_term": EXAMPLES_PER_TERM,
            "selection": (
                "deterministic; source-diverse first, "
                "then first_seen_at/content_id"
            ),
        },
        "groups": {},
    }

    total_examples = 0

    for group, group_block in selection.items():
        output["groups"][group] = {
            "top_unigrams": {},
            "emerging": {},
        }

        for row in group_block["top_unigrams"]:
            term = row["term"]

            examples = choose_examples(
                row.get("content_ids", []),
                metadata,
                EXAMPLES_PER_TERM,
            )

            output["groups"][group]["top_unigrams"][term] = {
                "documents": row["documents"],
                "doc_pct": row["doc_pct"],
                "examples": [
                    compact(example)
                    for example in examples
                ],
            }

            total_examples += len(examples)

        for row in group_block["emerging"]:
            term = row["term"]

            examples = choose_examples(
                row.get("new_content_ids", []),
                metadata,
                EXAMPLES_PER_TERM,
            )

            output["groups"][group]["emerging"][term] = {
                "old_documents": row["old_documents"],
                "new_documents": row["new_documents"],
                "old_pct": row["old_pct"],
                "new_pct": row["new_pct"],
                "delta_pct": row["delta_pct"],
                "score": row["score"],
                "examples": [
                    compact(example)
                    for example in examples
                ],
            }

            total_examples += len(examples)

    OUTPUT_FILE.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
            default=str,
        ) + "\n",
        encoding="utf-8",
    )

    print(f"saved={OUTPUT_FILE}")
    print(f"bytes={OUTPUT_FILE.stat().st_size}")
    print(f"example_slots={total_examples}")

    for group in ("ua_space", "ru_space"):
        print(f"\n===== {group} SAMPLE =====")

        top_terms = list(
            output["groups"][group]["top_unigrams"].items()
        )[:3]

        for term, block in top_terms:
            print(
                f"{term}: "
                f"docs={block['documents']} "
                f"examples={len(block['examples'])}"
            )

            for example in block["examples"][:2]:
                sources = ", ".join(example["source_names"])
                preview = example["text_preview"][:100]

                print(
                    f"  - {sources} | {preview}"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
