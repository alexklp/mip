#!/usr/bin/env python3
"""Експериментальний 30-денний Signals builder для C1 ДШВ.

Контракт:
- 30 днів exact discovery;
- strict complete-link core <= 0.18;
- max story span 72 год вже на strict-core етапі;
- supported merge між cores <= 0.24;
- merge потребує >= 2 cross-content links;
- 24 години — лише ознака актуальності, не межа життя сигналу;
- БД читається тільки READ ONLY.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

import psycopg
from psycopg.rows import dict_row

from reporting.signals_postgres import WATERMARK_SQL
from web.c1_scope import c1_analytical_gate_sql


ROOT = Path(__file__).resolve().parents[1]

DEFAULT_OUTPUT = (
    ROOT
    / "reporting"
    / "signals_c1_30d.latest.json"
)

SCHEMA_VERSION = "contour-signals-c1-30d/1"
ALGORITHM_VERSION = "c1-story-temporal-supported/1"

SOURCE_GROUPS = ("ru_space", "ua_space")

WINDOW_DAYS = 30
ACTIVE_HOURS = 24

CORE_DISTANCE = 0.18
MERGE_DISTANCE = 0.24
MERGE_MIN_CROSS_LINKS = 2
MERGE_MIN_MEMBER_COVERAGE = 0.60
MAX_STORY_SPAN = timedelta(hours=72)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"

    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".c1-signals-30d-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)

    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fetch_data(
    *,
    database: str,
    object_id: int | None,
) -> tuple[
    datetime,
    datetime,
    list[dict],
    list[dict],
    list[dict],
]:
    gate = c1_analytical_gate_sql("a")

    object_filter = (
        "AND a.object_id = %(object_id)s"
        if object_id is not None
        else ""
    )

    with psycopg.connect(
        dbname=database,
        row_factory=dict_row,
        connect_timeout=5,
        options=(
            "-c default_transaction_read_only=on "
            "-c statement_timeout=30000"
        ),
    ) as conn:
        conn.execute(
            "SET TRANSACTION ISOLATION LEVEL "
            "REPEATABLE READ, READ ONLY"
        )

        watermarks = conn.execute(
            WATERMARK_SQL,
            {"groups": list(SOURCE_GROUPS)},
        ).fetchall()

        watermark_map = {
            row["source_group"]:
            row["watermark"].astimezone(timezone.utc)
            for row in watermarks
        }

        if set(watermark_map) != set(SOURCE_GROUPS):
            raise RuntimeError(
                "Немає watermark для всіх source groups"
            )

        as_of = min(watermark_map.values())
        start = as_of - timedelta(days=WINDOW_DAYS)

        params = {
            "start": start,
            "as_of": as_of,
            "groups": list(SOURCE_GROUPS),
            "object_id": object_id,
        }

        contents = conn.execute(
            f"""
            SELECT
                ci.content_id,
                ci.content_hash,
                left(ci.title, 240) AS title,
                min(
                    COALESCE(
                        io.published_at,
                        io.collected_at
                    )
                ) AS first_seen,
                max(
                    COALESCE(
                        io.published_at,
                        io.collected_at
                    )
                ) AS last_seen,
                max(io.collected_at) AS last_collected,
                count(DISTINCT io.source_id) AS source_count
            FROM content_items ci
            JOIN embeddings e
              ON e.content_id = ci.content_id
             AND e.embedding_model_id = 1
             AND vector_dims(e.embedding) = 1024
            JOIN item_occurrences io
              ON io.content_id = ci.content_id
            JOIN sources s
              ON s.source_id = io.source_id
            WHERE io.collected_at >= %(start)s
              AND io.collected_at < %(as_of)s
              AND s.source_group = ANY(%(groups)s)
              AND (
                    SELECT r.decision
                    FROM content_routing_decisions r
                    WHERE r.content_id = ci.content_id
                    ORDER BY
                        r.routing_version DESC,
                        r.created_at DESC,
                        r.routing_id DESC
                    LIMIT 1
                  ) IN ('analyze', 'maybe')
              AND EXISTS (
                    SELECT 1
                    FROM content_contour_assignments a
                    WHERE a.content_id = ci.content_id
                      AND a.monitoring_contour_id = 1
                      AND a.object_id IS NOT NULL
                      AND a.evidence_type = 'exact_reference'
                      {object_filter}
                      {gate}
                  )
            GROUP BY
                ci.content_id,
                ci.content_hash,
                ci.title
            ORDER BY
                last_collected DESC,
                source_count DESC,
                ci.content_hash,
                ci.content_id
            """,
            params,
        ).fetchall()

        ids = [
            str(row["content_id"])
            for row in contents
        ]

        if not ids:
            return as_of, start, [], [], []

        pairs = conn.execute(
            """
            WITH selected AS MATERIALIZED (
                SELECT
                    content_id,
                    embedding::vector(1024) AS embedding
                FROM embeddings
                WHERE embedding_model_id = 1
                  AND content_id = ANY(%(ids)s::uuid[])
            )
            SELECT
                a.content_id AS left_id,
                b.content_id AS right_id,
                a.embedding <=> b.embedding AS distance
            FROM selected a
            JOIN selected b
              ON a.content_id < b.content_id
            WHERE
                a.embedding <=> b.embedding
                <= %(merge_distance)s
            ORDER BY
                distance,
                left_id,
                right_id
            """,
            {
                "ids": ids,
                "merge_distance": MERGE_DISTANCE,
            },
        ).fetchall()

        occurrences = conn.execute(
            """
            SELECT
                io.occurrence_id,
                io.content_id,
                io.source_id,
                io.collected_at,
                io.published_at,
                left(io.external_ref, 2048) AS external_ref,
                left(s.name, 240) AS source_name,
                s.source_type,
                s.source_group,
                left(ci.title, 240) AS title,
                left(ci.text_content, 600) AS text,
                char_length(coalesce(ci.text_content, '')) > 600
                    AS text_truncated
            FROM item_occurrences io
            JOIN sources s USING (source_id)
            JOIN content_items ci USING (content_id)
            WHERE io.content_id = ANY(%(ids)s::uuid[])
              AND io.collected_at >= %(start)s
              AND io.collected_at < %(as_of)s
              AND s.source_group = ANY(%(groups)s)
            ORDER BY
                io.collected_at,
                io.occurrence_id
            """,
            {
                "ids": ids,
                "start": start,
                "as_of": as_of,
                "groups": list(SOURCE_GROUPS),
            },
        ).fetchall()

    return (
        as_of,
        start,
        contents,
        pairs,
        occurrences,
    )


def build_snapshot(
    *,
    as_of: datetime,
    start: datetime,
    contents: list[dict],
    pair_rows: list[dict],
    occurrences: list[dict],
    object_id: int | None,
) -> dict:
    meta = {
        str(row["content_id"]): row
        for row in contents
    }

    ids = list(meta)

    distances = {
        tuple(sorted((
            str(row["left_id"]),
            str(row["right_id"]),
        ))): float(row["distance"])
        for row in pair_rows
    }

    def distance(left: str, right: str) -> float | None:
        if left == right:
            return 0.0

        return distances.get(
            tuple(sorted((left, right)))
        )

    def span_of(group: list[str]) -> timedelta:
        return (
            max(meta[cid]["last_seen"] for cid in group)
            - min(meta[cid]["first_seen"] for cid in group)
        )

    adjacency: dict[str, set[str]] = defaultdict(set)

    for (left, right), value in distances.items():
        if value <= CORE_DISTANCE:
            adjacency[left].add(right)
            adjacency[right].add(left)

    # 1. Strict complete-link cores + temporal cap.
    groups: list[list[str]] = []
    membership: dict[str, int] = {}

    for cid in ids:
        possible = sorted({
            membership[other]
            for other in adjacency.get(cid, ())
            if other in membership
        })

        target_index = None

        for index in possible:
            group = groups[index]

            semantic_ok = all(
                distance(cid, other) is not None
                and distance(cid, other) <= CORE_DISTANCE
                for other in group
            )

            temporal_ok = (
                span_of(group + [cid])
                <= MAX_STORY_SPAN
            )

            if semantic_ok and temporal_ok:
                target_index = index
                break

        if target_index is None:
            membership[cid] = len(groups)
            groups.append([cid])
        else:
            groups[target_index].append(cid)
            membership[cid] = target_index

    strict_group_count = len(groups)

    # 2. Supported merge між strict cores.
    group_of = {
        cid: index
        for index, group in enumerate(groups)
        for cid in group
    }

    support: dict[tuple[int, int], dict] = {}

    for (left, right), value in distances.items():
        if value > MERGE_DISTANCE:
            continue

        left_group = group_of[left]
        right_group = group_of[right]

        if left_group == right_group:
            continue

        if left_group < right_group:
            edge = (left_group, right_group)
            edge_left_member = left
            edge_right_member = right
        else:
            edge = (right_group, left_group)
            edge_left_member = right
            edge_right_member = left

        row = support.setdefault(
            edge,
            {
                "links": 0,
                "min_distance": value,
                "left_members": set(),
                "right_members": set(),
            },
        )

        row["links"] += 1
        row["min_distance"] = min(
            row["min_distance"],
            value,
        )
        row["left_members"].add(edge_left_member)
        row["right_members"].add(edge_right_member)

    edges = sorted(
        (
            (
                left,
                right,
                row["links"],
                row["min_distance"],
            )
            for (left, right), row in support.items()
            if row["links"] >= MERGE_MIN_CROSS_LINKS
            and (
                len(row["left_members"]) / len(groups[left])
                >= MERGE_MIN_MEMBER_COVERAGE
            )
            and (
                len(row["right_members"]) / len(groups[right])
                >= MERGE_MIN_MEMBER_COVERAGE
            )
        ),
        key=lambda row: (
            -row[2],
            row[3],
            row[0],
            row[1],
        ),
    )

    supported_edges = {
        (left, right)
        for left, right, _links, _minimum in edges
    }

    parent = list(range(len(groups)))

    members = {
        index: list(group)
        for index, group in enumerate(groups)
    }

    group_indices = {
        index: {index}
        for index in range(len(groups))
    }

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]

        return index

    accepted_merges = 0

    for left, right, _links, _minimum in edges:
        left_root = find(left)
        right_root = find(right)

        if left_root == right_root:
            continue

        left_component = group_indices[left_root]
        right_component = group_indices[right_root]

        complete_support = all(
            tuple(sorted((left_core, right_core)))
            in supported_edges
            for left_core in left_component
            for right_core in right_component
        )

        if not complete_support:
            continue

        combined = (
            members[left_root]
            + members[right_root]
        )

        if span_of(combined) > MAX_STORY_SPAN:
            continue

        combined_group_indices = (
            left_component | right_component
        )

        if left_root > right_root:
            left_root, right_root = (
                right_root,
                left_root,
            )

        parent[right_root] = left_root
        members[left_root] = combined
        members[right_root] = []
        group_indices[left_root] = combined_group_indices
        group_indices.pop(right_root, None)

        accepted_merges += 1

    final_groups = [
        members[index]
        for index in range(len(groups))
        if find(index) == index
        and members[index]
    ]

    occurrence_map: dict[str, list[dict]] = defaultdict(list)

    for row in occurrences:
        occurrence_map[
            str(row["content_id"])
        ].append(row)

    active_start = as_of - timedelta(
        hours=ACTIVE_HOURS
    )

    candidates = []

    for group in final_groups:
        rows = [
            row
            for cid in group
            for row in occurrence_map[cid]
        ]

        source_ids = {
            str(row["source_id"])
            for row in rows
        }

        if len(source_ids) < 2:
            continue

        observed_times = [
            row["published_at"]
            or row["collected_at"]
            for row in rows
        ]

        active_24h = any(
            row["collected_at"] >= active_start
            for row in rows
        )

        source_groups = sorted({
            row["source_group"]
            for row in rows
        })

        representative_id = max(
            group,
            key=lambda cid: (
                int(meta[cid]["source_count"]),
                meta[cid]["last_collected"],
                cid,
            ),
        )

        chronology = sorted(
            rows,
            key=lambda row: (
                row["published_at"]
                or row["collected_at"],
                row["collected_at"],
                str(row["occurrence_id"]),
            ),
        )

        search_text = " ".join(
            dict.fromkeys(
                (
                    meta[cid]["title"] or ""
                ).strip()
                for cid in group
                if (
                    meta[cid]["title"] or ""
                ).strip()
            )
        )[:4000]

        candidates.append({
            "candidate_id": representative_id,
            "representative_content_id": representative_id,
            "representative_title": (
                meta[representative_id]["title"]
                or ""
            )[:240],
            "search_text": search_text,
            "content_ids": group,
            "content_count": len(group),
            "occurrence_count": len(rows),
            "source_count": len(source_ids),
            "source_groups": source_groups,
            "cross_space": len(source_groups) > 1,
            "active_24h": active_24h,
            "first_published_at": iso(
                min(observed_times)
            ),
            "last_published_at": iso(
                max(observed_times)
            ),
            "last_observed": iso(
                max(
                    row["collected_at"]
                    for row in rows
                )
            ),
            "span_hours": round(
                span_of(group).total_seconds()
                / 3600,
                2,
            ),
            "chronology": [
                {
                    "occurrence_id": str(
                        row["occurrence_id"]
                    ),
                    "content_id": str(
                        row["content_id"]
                    ),
                    "source_id": str(
                        row["source_id"]
                    ),
                    "source_name": row["source_name"],
                    "source_type": row["source_type"],
                    "source_group": row["source_group"],
                    "title": row["title"] or "",
                    "text": row["text"] or "",
                    "text_truncated": bool(
                        row["text_truncated"]
                    ),
                    "external_ref": row["external_ref"],
                    "published_at": iso(
                        row["published_at"]
                    ),
                    "collected_at": iso(
                        row["collected_at"]
                    ),
                }
                for row in chronology
            ],
        })

    candidates.sort(
        key=lambda row: (
            not row["active_24h"],
            -row["source_count"],
            -row["occurrence_count"],
            row["last_observed"],
            row["candidate_id"],
        )
    )

    candidates.sort(
        key=lambda row: (
            not row["active_24h"],
            -row["source_count"],
            -row["occurrence_count"],
            -datetime.fromisoformat(
                row["last_observed"]
            ).timestamp(),
            row["candidate_id"],
        )
    )

    all_sources = {
        str(row["source_id"])
        for row in occurrences
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "generated_at": iso(
            datetime.now(timezone.utc)
        ),
        "as_of": iso(as_of),
        "contour": {
            "monitoring_contour_id": 1,
            "code": "dshv_objects",
            "object_id": object_id,
        },
        "window": {
            "days": WINDOW_DAYS,
            "start": iso(start),
            "end": iso(as_of),
            "active_hours": ACTIVE_HOURS,
            "active_start": iso(active_start),
            "membership_field": "collected_at",
        },
        "config": {
            "core_distance": CORE_DISTANCE,
            "merge_distance": MERGE_DISTANCE,
            "merge_min_cross_links": (
                MERGE_MIN_CROSS_LINKS
            ),
            "max_story_span_hours": int(
                MAX_STORY_SPAN.total_seconds()
                // 3600
            ),
            "distance_method": (
                "exact_pgvector_cosine"
            ),
        },
        "summary": {
            "materials_30d": len(contents),
            "sources_30d": len(all_sources),
            "strict_group_count": strict_group_count,
            "accepted_supported_merges": (
                accepted_merges
            ),
            "final_group_count": len(final_groups),
            "signals_30d": len(candidates),
            "active_24h": sum(
                row["active_24h"]
                for row in candidates
            ),
            "inactive_24h": sum(
                not row["active_24h"]
                for row in candidates
            ),
        },
        "candidates": candidates,
    }



def fetch_object_ids(
    *,
    database: str,
) -> list[int]:
    """Усі активні об'єкти C1, включно з об'єктами без матеріалів."""
    with psycopg.connect(
        dbname=database,
        row_factory=dict_row,
        autocommit=True,
        connect_timeout=5,
        options=(
            "-c default_transaction_read_only=on "
            "-c statement_timeout=30000"
        ),
    ) as conn:
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
        int(row["object_id"])
        for row in rows
    ]


def object_output_path(
    object_id: int,
) -> Path:
    return (
        ROOT
        / "reporting"
        / f"signals_c1_30d.object_{object_id}.latest.json"
    )


def build_one(
    *,
    database: str,
    object_id: int | None,
    output: Path,
) -> dict:
    (
        as_of,
        start,
        contents,
        pairs,
        occurrences,
    ) = fetch_data(
        database=database,
        object_id=object_id,
    )

    snapshot = build_snapshot(
        as_of=as_of,
        start=start,
        contents=contents,
        pair_rows=pairs,
        occurrences=occurrences,
        object_id=object_id,
    )

    atomic_write_json(output, snapshot)

    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--database",
        default="mip_dev",
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
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
    )

    args = parser.parse_args()

    if (
        args.object_id is not None
        and args.object_id <= 0
    ):
        parser.error("--object-id має бути > 0")

    if (
        args.object_id is not None
        and args.all_objects
    ):
        parser.error(
            "--object-id та --all-objects "
            "не можна використовувати разом"
        )

    output = args.output.resolve()

    if not output.is_relative_to(ROOT):
        parser.error(
            "--output має бути всередині репозиторію"
        )

    global_snapshot = build_one(
        database=args.database,
        object_id=args.object_id,
        output=output,
    )

    object_count = 0

    if args.all_objects:
        object_ids = fetch_object_ids(
            database=args.database,
        )

        for object_id in object_ids:
            build_one(
                database=args.database,
                object_id=object_id,
                output=object_output_path(
                    object_id
                ),
            )

        object_count = len(object_ids)

    summary = global_snapshot["summary"]

    print(
        "C1 Signals 30d:",
        f"materials={summary['materials_30d']}",
        f"signals={summary['signals_30d']}",
        f"active24={summary['active_24h']}",
        f"merges={summary['accepted_supported_merges']}",
        f"objects={object_count}",
        f"output={output}",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
