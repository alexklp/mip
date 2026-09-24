#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import psycopg

import topics_snapshot as ts
from web.c1_scope import c1_analytical_gate_sql


DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent
    / "contour_topics_c1.latest.json"
)


def scoped_output_path(
    base: Path,
    object_id: int,
) -> Path:
    """Return deterministic snapshot path for one C1 object."""
    suffix = ".latest.json"

    if base.name.endswith(suffix):
        prefix = base.name[:-len(suffix)]
        name = f"{prefix}.object_{object_id}{suffix}"
    else:
        name = (
            f"{base.stem}.object_{object_id}"
            f"{base.suffix}"
        )

    return base.with_name(name)


def fetch_object_ids() -> list[int]:
    """Return all active C1 analytical objects."""
    with psycopg.connect(ts.DB_DSN) as conn:
        rows = conn.execute(
            """
            SELECT object_id
            FROM contour_reference_objects
            WHERE monitoring_contour_id = 1
              AND active
            ORDER BY object_id
            """
        ).fetchall()

    return [
        int(row[0])
        for row in rows
    ]


SCHEMA_VERSION = "contour-topics-c1/1"

KYIV = ZoneInfo("Europe/Kyiv")
COMPARE_LIMIT = 50
C1_EVIDENCE_LIMIT = 200


def build_daily_comparison(
    rows: list[dict],
    content_markers: dict,
    marker_rows: list[dict],
    *,
    as_of,
    days: int,
) -> dict:
    """Daily publication series for visible C1 phrase markers."""
    last_day = as_of.astimezone(KYIV).date()
    first_day = last_day - timedelta(days=days - 1)

    day_values = [
        first_day + timedelta(days=index)
        for index in range(days)
    ]
    day_index = {
        day: index
        for index, day in enumerate(day_values)
    }

    marker_ids = {
        row["marker_id"]
        for row in marker_rows[:COMPARE_LIMIT]
    }

    series = {
        marker_id: [0] * days
        for marker_id in marker_ids
    }

    missing_published_at = 0

    for row in rows:
        published_at = row.get("published_at")

        if published_at is None:
            missing_published_at += 1
            continue

        day = published_at.astimezone(KYIV).date()
        index = day_index.get(day)

        if index is None:
            continue

        mids = (
            content_markers
            .get(row["content_id"], {})
            .get("phrases", set())
        )

        for marker_id in mids & marker_ids:
            series[marker_id][index] += 1

    return {
        "unit": "publications",
        "time_basis": "published_at",
        "timezone": "Europe/Kyiv",
        "days": [
            day.isoformat()
            for day in day_values
        ],
        "series": series,
        "missing_published_at": missing_published_at,
    }


def fetch_rows(
    days: int,
    *,
    object_id: int | None = None,
) -> tuple[object, object, list[dict]]:
    sql = ts.SQL

    marker = """
ORDER BY
"""

    object_filter = (
        "        AND a.object_id = %s\n"
        if object_id is not None
        else ""
    )

    object_filter = (
        "        AND a.object_id = %s\n"
        if object_id is not None
        else ""
    )

    c1_filter = f"""
  AND EXISTS (
      SELECT 1
      FROM content_contour_assignments a
      WHERE a.content_id = io.content_id
        AND a.monitoring_contour_id = 1
        AND a.object_id IS NOT NULL
        AND a.evidence_type = 'exact_reference'
{object_filter}        {c1_analytical_gate_sql("a")}
  )
"""

    if sql.count(marker) != 1:
        raise RuntimeError(
            "topics SQL ORDER BY marker is not unique"
        )

    sql = sql.replace(
        marker,
        c1_filter + marker,
        1,
    )

    with psycopg.connect(ts.DB_DSN) as conn:
        conn.execute(
            "SET TRANSACTION ISOLATION LEVEL "
            "REPEATABLE READ, READ ONLY"
        )

        observed_now = conn.execute(
            "SELECT now()"
        ).fetchone()[0]

        as_of = observed_now.replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        start_at = as_of - timedelta(days=days)

        params = (
            as_of,
            start_at,
            as_of,
            as_of,
        )

        if object_id is not None:
            params = (*params, object_id)

        raw = conn.execute(
            sql,
            params,
        ).fetchall()

    rows = []

    for row in raw:
        (
            occurrence_id,
            content_id,
            source_id,
            source_name,
            source_type,
            source_url_or_handle,
            source_group,
            external_ref,
            published_at,
            collected_at,
            observed_at,
            text_content,
            routing_version,
        ) = row

        rows.append(
            {
                "occurrence_id": occurrence_id,
                "content_id": content_id,
                "source_id": source_id,
                "source_name": source_name,
                "source_type": source_type,
                "source_url_or_handle": (
                    source_url_or_handle
                ),
                "source_group": source_group,
                "external_ref": external_ref,
                "published_at": published_at,
                "collected_at": collected_at,
                "observed_at": observed_at,
                "text_content": text_content or "",
                "routing_version": routing_version,
            }
        )

    return as_of, start_at, rows


def fetch_identity_words() -> set[str]:
    """Lexical identity vocabulary of active C1 object aliases."""
    with psycopg.connect(ts.DB_DSN) as conn:
        aliases = conn.execute(
            """
            SELECT DISTINCT reference_text
            FROM contour_reference_entries
            WHERE monitoring_contour_id = 1
              AND object_id IS NOT NULL
              AND entry_type = 'alias'
              AND active
            """
        ).fetchall()

    stopwords = ts.load_stopwords()
    rules = ts.load_rules()

    identity_words: set[str] = set()

    for (reference_text,) in aliases:
        cleaned = ts.clean_text(
            reference_text or "",
            [],
            rules,
        )

        if not cleaned.strip():
            continue

        lang = ts.detect_language(cleaned)

        tokens, _surfaces = (
            ts.analysis_tokens_with_surfaces(
                cleaned,
                lang,
                stopwords,
            )
        )

        words = ts.document_terms(
            tokens,
            ngram=1,
            stopwords=stopwords,
        )

        words, _phrases = (
            ts.canonicalize_marker_families(
                words,
                set(),
            )
        )

        identity_words.update(words)

    return identity_words


def is_identity_marker(
    meta: dict,
    identity_words: set[str],
) -> bool:
    """Suppress lexical markers that only describe C1 identity."""
    terms = meta["normalized_terms"]

    if meta["unit"] == "words":
        return any(
            term in identity_words
            for term in terms
        )

    for term in terms:
        parts = term.split()

        if (
            parts
            and all(
                part in identity_words
                for part in parts
            )
        ):
            return True

    return False


def build_phrase_details(
    rows: list[dict],
    content_markers: dict,
    marker_meta: dict,
    marker_rows: list[dict],
) -> tuple[dict, set[str]]:
    """Evidence contract for the visible C1 phrase markers."""
    selected_ids = {
        row["marker_id"]
        for row in marker_rows[:COMPARE_LIMIT]
    }

    details = {}
    referenced_occurrences: set[str] = set()

    for marker_id in selected_ids:
        meta = marker_meta.get(marker_id)

        if meta is None:
            continue

        selected = [
            row
            for row in rows
            if marker_id
            in content_markers
            .get(row["content_id"], {})
            .get("phrases", set())
        ]

        selected.sort(
            key=lambda row: (
                row["published_at"]
                or row["observed_at"],
                row["occurrence_id"],
            ),
            reverse=True,
        )

        occurrence_refs = [
            row["occurrence_id"]
            for row
            in selected[:C1_EVIDENCE_LIMIT]
        ]

        referenced_occurrences.update(
            occurrence_refs
        )

        details[marker_id] = {
            "marker_id": marker_id,
            "unit": "phrases",
            "label": meta["label"],
            "publications": len(selected),
            "materials": len({
                row["content_id"]
                for row in selected
            }),
            "sources": len({
                row["source_id"]
                for row in selected
            }),
            "source_ranking": (
                ts.source_rank(selected)
            ),
            "evidence_total": len(selected),
            "evidence_limit_reached": (
                len(selected)
                > C1_EVIDENCE_LIMIT
            ),
            "occurrence_refs": occurrence_refs,
        }

    return (
        details,
        referenced_occurrences,
    )


def build_snapshot(
    *,
    days: int,
    object_id: int | None = None,
) -> dict:
    fetch_started = time.perf_counter()

    as_of, start_at, rows = fetch_rows(
        days,
        object_id=object_id,
    )

    print(
        f"C1 timing: object_id={object_id} "
        f"fetch={time.perf_counter() - fetch_started:.1f}s "
        f"rows={len(rows)}",
        flush=True,
    )

    nlp_started = time.perf_counter()

    (
        terms_by_content,
        phrase_labels,
        language_counts,
        cache_status,
    ) = ts.process_contents(rows)

    print(
        f"C1 timing: object_id={object_id} "
        f"nlp={time.perf_counter() - nlp_started:.1f}s",
        flush=True,
    )

    total = cache_status["total"]

    coverage_pct = (
        100.0 * cache_status["hits"] / total
        if total
        else 0.0
    )

    if total and coverage_pct < ts.TOPICS_MIN_NLP_COVERAGE_PCT:
        raise RuntimeError(
            "C1 Topics NLP cache coverage too low: "
            f"{coverage_pct:.2f}% "
            f"< {ts.TOPICS_MIN_NLP_COVERAGE_PCT:.2f}%"
        )

    (
        _term_to_marker,
        content_markers,
        marker_meta,
    ) = ts.canonicalize_markers(
        terms_by_content,
        phrase_labels,
    )

    identity_words = fetch_identity_words()

    visible_marker_meta = {
        mid: meta
        for mid, meta in marker_meta.items()
        if not is_identity_marker(
            meta,
            identity_words,
        )
    }

    # Уся 30-денна вибірка навмисно є одним
    # аналітичним зрізом "current".
    stats = ts.aggregate_marker_stats(
        rows,
        content_markers,
        current_start=start_at,
    )

    summary = {}

    for view in ts.VIEWS:
        view_rows = [
            row
            for row in rows
            if (
                view == "all"
                or row["source_group"] == view
            )
        ]

        summary[view] = {
            "previous": {
                "publications": 0,
                "materials": 0,
                "sources": 0,
            },
            "current": {
                "publications": len(view_rows),
                "materials": len({
                    row["content_id"]
                    for row in view_rows
                }),
                "sources": len({
                    row["source_id"]
                    for row in view_rows
                }),
            },
        }

    views = {}

    for view in ts.VIEWS:
        views[view] = {
            "themes": {
                unit: ts.theme_rows(
                    view=view,
                    unit=unit,
                    marker_meta=visible_marker_meta,
                    stats=stats,
                    summary=summary,
                )
                for unit in ts.UNITS
            }
        }

    all_phrases = views["all"]["themes"]["phrases"]

    cloud_phrases = ts.cloud_layout(
        all_phrases[:ts.CLOUD_PHRASES],
        mode="themes",
        unit="phrases",
    )

    comparison = build_daily_comparison(
        rows,
        content_markers,
        all_phrases,
        as_of=as_of,
        days=days,
    )

    (
        marker_details,
        referenced_occurrence_ids,
    ) = build_phrase_details(
        rows,
        content_markers,
        visible_marker_meta,
        all_phrases,
    )

    occurrences = {
        row["occurrence_id"]:
            ts.occurrence_payload(row)
        for row in rows
        if row["occurrence_id"]
        in referenced_occurrence_ids
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ts.ALGORITHM_VERSION,
        "generated_at": ts.iso(as_of),
        "as_of": ts.iso(as_of),
        "contour": {
            "monitoring_contour_id": 1,
            "code": "dshv_objects",
            "object_id": object_id,
        },
        "window": {
            "days": days,
            "start": ts.iso(start_at),
            "end": ts.iso(as_of),
            "membership": (
                "COALESCE("
                "item_occurrences.published_at,"
                "item_occurrences.collected_at)"
            ),
        },
        "summary": summary,
        "nlp_coverage": {
            "input_materials": total,
            "analyzed_materials": cache_status["hits"],
            "missing_materials": cache_status["misses"],
            "coverage_pct": round(
                coverage_pct,
                2,
            ),
            "languages": dict(language_counts),
        },
        "comparison": {
            "all": {
                "phrases": comparison,
            },
        },
        "markers": marker_details,
        "occurrences": occurrences,
        "clouds": {
            "all": {
                "phrases": cloud_phrases,
            },
        },
        "presentation": {
            "theme_selection": (
                "c1_identity_vocabulary_v1"
            ),
            "comparison_limit": COMPARE_LIMIT,
            "comparison_time_basis": "published_at",
            "phrases_primary": True,
            "identity_words": len(
                identity_words
            ),
            "raw_markers": len(
                marker_meta
            ),
            "visible_markers": len(
                visible_marker_meta
            ),
        },
        "views": views,
    }


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--days",
        type=int,
        default=30,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--object-id",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--all-objects",
        action="store_true",
    )

    args = parser.parse_args()

    if args.all_objects and args.object_id is not None:
        parser.error(
            "--all-objects and --object-id are mutually exclusive"
        )

    base_output = args.output or DEFAULT_OUTPUT

    if args.all_objects:
        jobs = [(None, base_output)]
        jobs.extend(
            (
                object_id,
                scoped_output_path(
                    base_output,
                    object_id,
                ),
            )
            for object_id in fetch_object_ids()
        )
    else:
        output = (
            base_output
            if args.object_id is None
            else (
                args.output
                or scoped_output_path(
                    DEFAULT_OUTPUT,
                    args.object_id,
                )
            )
        )
        jobs = [(args.object_id, output)]

    for object_id, output in jobs:
        build_started = time.perf_counter()

        snapshot = build_snapshot(
            days=args.days,
            object_id=object_id,
        )

        build_elapsed = time.perf_counter() - build_started

        write_started = time.perf_counter()

        ts.atomic_write_json(
            output,
            snapshot,
        )

        write_elapsed = time.perf_counter() - write_started

        current = snapshot["summary"]["all"]["current"]

        print(
            "C1 topics:",
            f"object_id={object_id}",
            f"days={args.days}",
            f"publications={current['publications']}",
            f"materials={current['materials']}",
            f"sources={current['sources']}",
            f"build={build_elapsed:.1f}s",
            f"write={write_elapsed:.1f}s",
            f"output={output}",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())