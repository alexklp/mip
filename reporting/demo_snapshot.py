#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from nlp_prepare import DB_DSN


OUTPUT_DIR = Path(__file__).resolve().parent / "output"
OUTPUT_FILE = OUTPUT_DIR / "demo_snapshot.json"

OLD_START = "2026-08-22T00:00:00+00:00"
OLD_END   = "2026-08-23T00:00:00+00:00"

NEW_START = "2026-08-26T07:00:00+00:00"
NEW_END   = "2026-08-26T08:00:00+00:00"


def one(conn, sql: str, params=None) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else {}


def many(conn, sql: str, params=None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(DB_DSN) as conn:
        sources = one(
            conn,
            """
            SELECT
                count(*) FILTER (WHERE is_active) AS active_sources,
                count(*) AS registry_sources
            FROM sources
            """,
        )

        source_breakdown = many(
            conn,
            """
            SELECT
                source_group,
                source_type,
                count(*) AS sources
            FROM sources
            WHERE is_active = true
            GROUP BY source_group, source_type
            ORDER BY source_group, source_type
            """,
        )

        content = one(
            conn,
            """
            SELECT
                count(*) AS content_items,
                min(first_seen_at) AS first_observed,
                max(first_seen_at) AS last_observed
            FROM content_items
            """,
        )

        occurrences = one(
            conn,
            """
            SELECT count(*) AS item_occurrences
            FROM item_occurrences
            """,
        )

        eligible = one(
            conn,
            """
            SELECT count(DISTINCT ci.content_id) AS eligible_content
            FROM content_items ci
            JOIN item_occurrences io
              ON io.content_id = ci.content_id
            """,
        )

        embeddings = one(
            conn,
            """
            SELECT count(DISTINCT content_id) AS embedded_content
            FROM embeddings
            """,
        )

        claim_runs = many(
            conn,
            """
            SELECT
                status,
                count(*) AS runs,
                coalesce(sum(claim_count), 0) AS reported_claims
            FROM claim_extraction_runs
            GROUP BY status
            ORDER BY status
            """,
        )

        claims = one(
            conn,
            """
            SELECT count(*) AS persisted_claims
            FROM claims
            """,
        )

        events = one(
            conn,
            """
            SELECT count(*) AS canonical_events
            FROM canonical_events
            """,
        )

        canonical_events = many(
            conn,
            """
            SELECT
                canonical_event_id::text,
                canonical_event_summary,
                created_at,
                updated_at
            FROM canonical_events
            ORDER BY updated_at DESC, canonical_event_id
            """,
        )

        event_verifications = many(
            conn,
            """
            SELECT
                event_decision,
                count(*) AS verifications
            FROM event_verifications
            WHERE status = 'valid'
            GROUP BY event_decision
            ORDER BY event_decision
            """,
        )

        event_trace_rows = many(
            conn,
            """
            WITH included_claims AS (
                SELECT DISTINCT
                    cem.canonical_event_id,
                    evm.claim_id
                FROM canonical_event_members cem
                JOIN event_verifications ev
                  ON ev.event_candidate_id = cem.event_candidate_id
                 AND ev.status = 'valid'
                 AND ev.event_decision = 'accepted_seed'
                JOIN event_verification_members evm
                  ON evm.verification_id = ev.verification_id
                 AND evm.included = true
            ),
            ranked AS (
                SELECT
                    ic.canonical_event_id::text,
                    c.claim_id::text,
                    c.claim_text,
                    c.epistemic_status,
                    c.evidence_span,
                    cer.content_id::text,
                    ci.title,
                    left(regexp_replace(ci.text_content, '\\s+', ' ', 'g'), 700)
                        AS text_preview,
                    ci.first_seen_at,
                    s.name AS source_name,
                    s.source_type,
                    s.source_group,
                    io.external_ref,
                    row_number() OVER (
                        PARTITION BY ic.canonical_event_id
                        ORDER BY ci.first_seen_at, c.claim_id
                    ) AS rn
                FROM included_claims ic
                JOIN claims c
                  ON c.claim_id = ic.claim_id
                JOIN claim_extraction_runs cer
                  ON cer.run_id = c.run_id
                JOIN content_items ci
                  ON ci.content_id = cer.content_id
                LEFT JOIN item_occurrences io
                  ON io.content_id = ci.content_id
                LEFT JOIN sources s
                  ON s.source_id = io.source_id
            )
            SELECT
                canonical_event_id,
                claim_id,
                claim_text,
                epistemic_status,
                evidence_span,
                content_id,
                title,
                text_preview,
                first_seen_at,
                source_name,
                source_type,
                source_group,
                external_ref
            FROM ranked
            WHERE rn <= 6
            ORDER BY canonical_event_id, rn
            """,
        )

        contours = many(
            conn,
            """
            SELECT
                mc.monitoring_contour_id,
                mc.code,
                mc.name,
                cca.status,
                count(*) AS assignments,
                count(DISTINCT cca.content_id) AS documents
            FROM content_contour_assignments cca
            JOIN monitoring_contours mc
              ON mc.monitoring_contour_id = cca.monitoring_contour_id
            GROUP BY
                mc.monitoring_contour_id,
                mc.code,
                mc.name,
                cca.status
            ORDER BY
                mc.monitoring_contour_id,
                cca.status
            """,
        )

        observed_slices = many(
            conn,
            """
            SELECT
                source_group,
                count(DISTINCT ci.content_id) AS documents
            FROM content_items ci
            JOIN item_occurrences io
              ON io.content_id = ci.content_id
            JOIN sources s
              ON s.source_id = io.source_id
            WHERE s.source_group IN ('ua_space', 'ru_space')
              AND ci.first_seen_at >= %s::timestamptz
              AND ci.first_seen_at <  %s::timestamptz
            GROUP BY source_group
            ORDER BY source_group
            """,
            (NEW_START, NEW_END),
        )

        previous_slices = many(
            conn,
            """
            SELECT
                source_group,
                count(DISTINCT ci.content_id) AS documents
            FROM content_items ci
            JOIN item_occurrences io
              ON io.content_id = ci.content_id
            JOIN sources s
              ON s.source_id = io.source_id
            WHERE s.source_group IN ('ua_space', 'ru_space')
              AND ci.first_seen_at >= %s::timestamptz
              AND ci.first_seen_at <  %s::timestamptz
            GROUP BY source_group
            ORDER BY source_group
            """,
            (OLD_START, OLD_END),
        )

    snapshot = {
        "meta": {
            "title": "МІП — демонстраційний аналітичний звіт",
            "snapshot_version": 1,
            "generated_at_utc": datetime.now(timezone.utc),
            "git_baseline": "8991054",
            "report_type": "interactive_demo_snapshot",
        },
        "observation_windows": {
            "previous": {
                "start": OLD_START,
                "end": OLD_END,
                "label": "22.08.2026",
                "documents": previous_slices,
            },
            "current": {
                "start": NEW_START,
                "end": NEW_END,
                "label": "26.08.2026",
                "documents": observed_slices,
            },
        },
        "kpi": {
            **sources,
            **content,
            **occurrences,
            **eligible,
            **embeddings,
            **claims,
            **events,
        },
        "sources": source_breakdown,
        "claim_runs": claim_runs,
        "event_verifications": event_verifications,
        "contours": contours,
        "canonical_events": canonical_events,
        "event_trace_rows": event_trace_rows,
    }

    OUTPUT_FILE.write_text(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            indent=2,
            default=str,
        ) + "\n",
        encoding="utf-8",
    )

    print(f"saved={OUTPUT_FILE}")
    print(f"bytes={OUTPUT_FILE.stat().st_size}")

    print("\n=== SNAPSHOT KPI ===")
    for key, value in snapshot["kpi"].items():
        print(f"{key}: {value}")

    print("\n=== CURRENT WINDOW ===")
    for row in observed_slices:
        print(f"{row['source_group']}: {row['documents']}")

    print("\n=== PREVIOUS WINDOW ===")
    for row in previous_slices:
        print(f"{row['source_group']}: {row['documents']}")

    print("\n=== EVENT TRACE ROWS ===")
    print(f"rows={len(event_trace_rows)}")
    for row in event_trace_rows:
        print(
            f"{row['canonical_event_id']} | "
            f"{row['source_name']} | "
            f"{row['claim_text']}"
        )

    print("\n=== CANONICAL EVENTS ===")
    for event in canonical_events:
        print(
            f"{event['canonical_event_id']} | "
            f"{event['canonical_event_summary']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
