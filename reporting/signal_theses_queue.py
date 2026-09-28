"""Eligibility/scanner для LLM-тез Signals.

Джерела:
- поточний Global Signals snapshot;
- C1 overall snapshot;
- усі C1 object-specific snapshots.

Модуль:
- не викликає Mamay;
- не змінює БД;
- не змінює snapshots;
- дедуплікує однаковий склад content_id між різними UI scopes;
- обчислює точний input_hash через signal_theses contract;
- відсікає вже готові cache entries.

Фактична черга поки implicit:
current snapshots - ready cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable

from reporting.signal_theses import (
    canonical_materials,
    make_input_hash,
    read_cache_record,
)
from web.contour_signals import (
    DEFAULT_C1_SNAPSHOT,
    load_c1_contour_signals,
)
from web.db import read_connection
from web.signals import load_signals


ROOT = Path(__file__).resolve().parents[1]
REPORTING_DIR = ROOT / "reporting"

_C1_OBJECT_RE = re.compile(
    r"^signals_c1_30d\.object_(\d+)\.latest\.json$"
)


class SignalThesesQueueError(ValueError):
    """Некоректний snapshot/queue contract."""


@dataclass(frozen=True)
class SignalReference:
    scope: str
    candidate_id: str
    content_ids: tuple[str, ...]
    rank: int


@dataclass(frozen=True)
class SignalTask:
    input_hash: str
    content_ids: tuple[str, ...]
    content_count: int
    chars: int
    references: tuple[SignalReference, ...]
    ready: bool


def canonical_content_ids(
    values: Iterable[str],
) -> tuple[str, ...]:
    ids = tuple(values)

    if not ids:
        raise SignalThesesQueueError(
            "signal content_ids порожній"
        )

    if any(
        not isinstance(content_id, str)
        or not content_id
        for content_id in ids
    ):
        raise SignalThesesQueueError(
            "signal content_ids містить некоректний ID"
        )

    if len(ids) != len(set(ids)):
        raise SignalThesesQueueError(
            "signal content_ids містить дублікати"
        )

    return tuple(sorted(ids))


def candidate_reference(
    *,
    scope: str,
    rank: int,
    candidate: dict[str, Any],
) -> SignalReference:
    candidate_id = candidate.get(
        "candidate_id"
    )

    if (
        not isinstance(scope, str)
        or not scope
        or type(rank) is not int
        or rank < 0
        or not isinstance(candidate_id, str)
        or not candidate_id
    ):
        raise SignalThesesQueueError(
            "candidate reference metadata invalid"
        )

    content_ids = candidate.get(
        "content_ids"
    )

    if not isinstance(content_ids, list):
        raise SignalThesesQueueError(
            "candidate content_ids invalid"
        )

    return SignalReference(
        scope=scope,
        candidate_id=candidate_id,
        content_ids=canonical_content_ids(
            content_ids
        ),
        rank=rank,
    )


def group_references(
    references: Iterable[SignalReference],
) -> dict[
    tuple[str, ...],
    tuple[SignalReference, ...],
]:
    grouped: dict[
        tuple[str, ...],
        list[SignalReference],
    ] = {}

    for ref in references:
        grouped.setdefault(
            ref.content_ids,
            [],
        ).append(ref)

    return {
        key: tuple(value)
        for key, value in grouped.items()
    }


def _collect_global(
) -> tuple[str, list[SignalReference]]:
    view = load_signals()
    snapshot = view.get("snapshot")

    if snapshot is None or view["state"] != "ready":
        return view["state"], []

    refs = [
        candidate_reference(
            scope="global",
            rank=rank,
            candidate=candidate,
        )
        for rank, candidate
        in enumerate(snapshot["candidates"])
    ]

    return view["state"], refs


def _c1_paths() -> list[Path]:
    paths: list[Path] = []

    if DEFAULT_C1_SNAPSHOT.exists():
        paths.append(
            DEFAULT_C1_SNAPSHOT
        )

    paths.extend(sorted(
        REPORTING_DIR.glob(
            "signals_c1_30d.object_*.latest.json"
        )
    ))

    return paths


def _collect_c1(
) -> tuple[
    list[tuple[str, str, int]],
    list[SignalReference],
]:
    states = []
    refs = []

    for path in _c1_paths():
        if path == DEFAULT_C1_SNAPSHOT:
            object_id = None
            scope = "c1:all"
        else:
            match = _C1_OBJECT_RE.match(
                path.name
            )

            if match is None:
                raise SignalThesesQueueError(
                    f"unexpected C1 snapshot: {path.name}"
                )

            object_id = int(
                match.group(1)
            )
            scope = (
                f"c1:object:{object_id}"
            )

        view = load_c1_contour_signals(
            object_id=object_id,
            path=path,
            stale_seconds=604800,
        )

        snapshot = view.get(
            "snapshot"
        )

        count = (
            len(snapshot["candidates"])
            if snapshot is not None
            else 0
        )

        states.append(
            (
                scope,
                view["state"],
                count,
            )
        )

        if snapshot is None or view["state"] != "ready":
            continue

        for rank, candidate in enumerate(
            snapshot["candidates"]
        ):
            refs.append(
                candidate_reference(
                    scope=scope,
                    rank=rank,
                    candidate=candidate,
                )
            )

    return states, refs


def collect_references(
) -> tuple[
    list[tuple[str, str, int]],
    list[SignalReference],
]:
    global_state, global_refs = (
        _collect_global()
    )

    c1_states, c1_refs = (
        _collect_c1()
    )

    states = [
        (
            "global",
            global_state,
            len(global_refs),
        ),
        *c1_states,
    ]

    return (
        states,
        [
            *global_refs,
            *c1_refs,
        ],
    )


def fetch_material_rows(
    content_ids: Iterable[str],
) -> dict[str, dict[str, str]]:
    ids = sorted(set(content_ids))

    if not ids:
        return {}

    with read_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    content_id::text
                        AS content_id,
                    title,
                    text_content
                FROM content_items
                WHERE content_id
                    = ANY(%s::uuid[])
                """,
                (ids,),
            )

            rows = cur.fetchall()

    result = {
        row["content_id"]: {
            "content_id":
                row["content_id"],
            "title":
                row["title"] or "",
            "text_content":
                row["text_content"] or "",
        }
        for row in rows
    }

    missing = sorted(
        set(ids) - set(result)
    )

    if missing:
        raise SignalThesesQueueError(
            "content_items missing rows: "
            f"{len(missing)}"
        )

    return result


def build_tasks(
    references: Iterable[SignalReference],
    material_rows: dict[
        str,
        dict[str, str],
    ],
) -> list[SignalTask]:
    grouped = group_references(
        references
    )

    tasks = []

    for content_ids, refs in grouped.items():
        rows = [
            material_rows[content_id]
            for content_id in content_ids
        ]

        materials = canonical_materials(
            rows,
            expected_content_ids=list(
                content_ids
            ),
        )

        input_hash = make_input_hash(
            materials
        )

        cached = read_cache_record(
            input_hash
        )

        tasks.append(
            SignalTask(
                input_hash=input_hash,
                content_ids=tuple(
                    row["content_id"]
                    for row in materials
                ),
                content_count=len(
                    materials
                ),
                chars=sum(
                    len(row["title"])
                    + len(row["text"])
                    for row in materials
                ),
                references=refs,
                ready=(
                    cached is not None
                ),
            )
        )

    return tasks


def scan_queue(
) -> tuple[
    list[tuple[str, str, int]],
    list[SignalReference],
    list[SignalTask],
]:
    states, references = (
        collect_references()
    )

    content_ids = {
        content_id
        for ref in references
        for content_id
        in ref.content_ids
    }

    material_rows = (
        fetch_material_rows(
            content_ids
        )
    )

    tasks = build_tasks(
        references,
        material_rows,
    )

    return (
        states,
        references,
        tasks,
    )


def main() -> int:
    (
        states,
        references,
        tasks,
    ) = scan_queue()

    pending = [
        task
        for task in tasks
        if not task.ready
    ]

    ready = [
        task
        for task in tasks
        if task.ready
    ]

    print("=== SIGNAL THESIS SCOPES ===")

    for scope, state, count in states:
        print(
            f"{scope:<18} "
            f"state={state:<7} "
            f"candidates={count}"
        )

    print()
    print("=== QUEUE ===")
    print(
        "candidate references:",
        len(references),
    )
    print(
        "unique tasks:",
        len(tasks),
    )
    print(
        "dedup saved:",
        len(references) - len(tasks),
    )
    print(
        "ready:",
        len(ready),
    )
    print(
        "pending:",
        len(pending),
    )

    cross_scope = sum(
        1
        for task in tasks
        if len({
            ref.scope
            for ref in task.references
        }) > 1
    )

    print(
        "reused across scopes:",
        cross_scope,
    )

    print()
    print("=== NEXT 10 PENDING ===")

    for task in sorted(
        pending,
        key=lambda row: (
            -row.chars,
            row.input_hash,
        ),
    )[:10]:
        scopes = sorted({
            ref.scope
            for ref in task.references
        })

        print(
            task.input_hash[:8],
            f"contents={task.content_count}",
            f"chars={task.chars}",
            f"refs={len(task.references)}",
            "scopes=" + ",".join(scopes),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
