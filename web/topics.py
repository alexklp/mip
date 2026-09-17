from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "reporting"
    / "topics.latest.json"
)

EXPECTED_SCHEMA = "topics/1"
VALID_VIEWS = {"all", "ru_space", "ua_space"}
TOPICS_COMPARE_LIMIT = 50


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)

    if result.tzinfo is None:
        raise ValueError(
            "timestamp must be timezone-aware"
        )

    return result


@lru_cache(maxsize=4)
def _load_snapshot_cached(
    path_value: str,
    mtime_ns: int,
) -> dict[str, Any]:
    del mtime_ns

    path = Path(path_value)

    snapshot = json.loads(
        path.read_text(encoding="utf-8")
    )

    if (
        snapshot.get("schema_version")
        != EXPECTED_SCHEMA
    ):
        raise ValueError(
            "unsupported topics schema"
        )

    for key in (
        "generated_at",
        "as_of",
        "summary",
        "views",
        "windows",
        "markers",
        "occurrences",
    ):
        if key not in snapshot:
            raise ValueError(
                f"missing {key}"
            )

    _timestamp(
        snapshot["generated_at"]
    )

    return snapshot


def _read_snapshot(
    path: Path,
) -> dict[str, Any]:
    path = Path(path)

    stat = path.stat()

    return _load_snapshot_cached(
        str(path.resolve()),
        stat.st_mtime_ns,
    )



def _comparison_payload(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Lean hourly series for the Topics comparison chart."""
    result: dict[str, Any] = {}

    for view in (
        "all",
        "ru_space",
        "ua_space",
    ):
        result[view] = {}

        for unit in (
            "phrases",
            "words",
        ):
            series = {}

            for row in snapshot[
                "views"
            ][view]["themes"][unit][
                :TOPICS_COMPARE_LIMIT
            ]:
                marker_id = row["marker_id"]

                marker = snapshot[
                    "markers"
                ].get(marker_id)

                if marker is None:
                    raise ValueError(
                        "comparison marker missing"
                    )

                hourly = marker[
                    "views"
                ][view]["current"]["hourly"]

                if len(hourly) != 24:
                    raise ValueError(
                        "comparison hourly series "
                        "must have 24 bins"
                    )

                series[marker_id] = list(
                    hourly
                )

            result[view][unit] = series

    return result


def load_topics(
    path: Path = DEFAULT_SNAPSHOT,
    *,
    stale_seconds: int = 7200,
) -> dict[str, Any]:
    path = Path(path)

    if not path.is_file():
        return {
            "state": "missing",
            "snapshot": None,
            "topic_data": None,
        }

    try:
        snapshot = _read_snapshot(path)

        generated_at = _timestamp(
            snapshot["generated_at"]
        )

        age = (
            datetime.now(timezone.utc)
            - generated_at.astimezone(timezone.utc)
        ).total_seconds()

        state = (
            "stale"
            if age > stale_seconds
            else "ready"
        )

        # Browser gets only overview structures.
        # Heavy evidence stays server-side.
        topic_data = {
            "summary": snapshot["summary"],
            "views": snapshot["views"],
            "windows": snapshot["windows"],
            "comparison": _comparison_payload(
                snapshot
            ),
        }

        return {
            "state": state,
            "snapshot": snapshot,
            "topic_data": topic_data,
        }

    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        return {
            "state": "invalid",
            "snapshot": None,
            "topic_data": None,
        }


def load_topic_marker(
    path: Path,
    *,
    marker_id: str,
    view: str,
) -> dict[str, Any]:
    if view not in VALID_VIEWS:
        raise ValueError(
            f"unsupported view: {view}"
        )

    snapshot = _read_snapshot(
        Path(path)
    )

    marker = snapshot[
        "markers"
    ].get(marker_id)

    if marker is None:
        raise KeyError(marker_id)

    result = {
        "marker": {
            "marker_id": marker[
                "marker_id"
            ],
            "unit": marker["unit"],
            "label": marker["label"],
        },
        "view": view,
        "windows": snapshot["windows"],
    }

    for slice_name in (
        "current",
        "previous",
    ):
        source = marker[
            "views"
        ][view][slice_name]

        occurrence_refs = source.get(
            "occurrence_refs",
            [],
        )

        occurrences = []

        for occurrence_id in occurrence_refs:
            item = snapshot[
                "occurrences"
            ].get(occurrence_id)

            if item is not None:
                occurrences.append(item)

        result[slice_name] = {
            "publications": source[
                "publications"
            ],
            "materials": source[
                "materials"
            ],
            "sources": source[
                "sources"
            ],
            "share": source[
                "share"
            ],
            "hourly": source[
                "hourly"
            ],
            "hourly_sources": source.get(
                "hourly_sources",
                [[] for _ in range(24)],
            ),
            "source_ranking": source[
                "source_ranking"
            ],
            "evidence_total": source[
                "evidence_total"
            ],
            "evidence_limit_reached": (
                bool(
                    source.get(
                        "evidence_limit_reached",
                        False,
                    )
                )
                or source[
                    "evidence_total"
                ] > len(occurrences)
            ),
            "occurrences": occurrences,
        }

        if view == "all":
            breakdown = {}

            for space_name in (
                "ru_space",
                "ua_space",
            ):
                space_row = marker[
                    "views"
                ][space_name][slice_name]

                breakdown[space_name] = {
                    "publications": space_row[
                        "publications"
                    ],
                    "materials": space_row[
                        "materials"
                    ],
                    "sources": space_row[
                        "sources"
                    ],
                    "share": space_row[
                        "share"
                    ],
                }

            result[
                slice_name
            ]["space_breakdown"] = breakdown

    return result
