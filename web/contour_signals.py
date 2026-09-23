"""Read-only adapter for C1 30-day Signals snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from web.signals import (
    MAX_SNAPSHOT_BYTES,
    ROOT,
    safe_link,
)


EXPECTED_SCHEMA = "contour-signals-c1-30d/1"
EXPECTED_ALGORITHM = "c1-story-temporal-supported/1"

CORE_DISTANCE = 0.18
MERGE_DISTANCE = 0.24
MERGE_MIN_CROSS_LINKS = 2
MAX_STORY_SPAN_HOURS = 72

DEFAULT_C1_SNAPSHOT = (
    ROOT
    / "reporting"
    / "signals_c1_30d.latest.json"
)


def c1_signals_snapshot_path(
    object_id: int | None = None,
) -> Path:
    if object_id is None:
        return DEFAULT_C1_SNAPSHOT

    return DEFAULT_C1_SNAPSHOT.with_name(
        f"signals_c1_30d.object_{object_id}.latest.json"
    )


def _aware_datetime(
    value: Any,
    *,
    field: str,
) -> datetime:
    if not isinstance(value, str):
        raise ValueError(
            f"{field}: timestamp must be string"
        )

    parsed = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        raise ValueError(
            f"{field}: timezone required"
        )

    return parsed.astimezone(timezone.utc)


def _validate_snapshot(
    snapshot: Any,
    *,
    object_id: int | None,
) -> tuple[datetime, datetime]:
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot root must be object")

    if snapshot.get("schema_version") != EXPECTED_SCHEMA:
        raise ValueError("unsupported schema")

    if (
        snapshot.get("algorithm_version")
        != EXPECTED_ALGORITHM
    ):
        raise ValueError("unsupported algorithm")

    contour = snapshot.get("contour")

    if not isinstance(contour, dict):
        raise ValueError("invalid contour")

    if (
        contour.get("monitoring_contour_id") != 1
        or contour.get("object_id") != object_id
    ):
        raise ValueError("scope mismatch")

    window = snapshot.get("window")
    config = snapshot.get("config")
    summary = snapshot.get("summary")
    candidates = snapshot.get("candidates")

    if not isinstance(window, dict):
        raise ValueError("invalid window")

    if not isinstance(config, dict):
        raise ValueError("invalid config")

    if not isinstance(summary, dict):
        raise ValueError("invalid summary")

    if not isinstance(candidates, list):
        raise ValueError("invalid candidates")

    if (
        window.get("days") != 30
        or window.get("active_hours") != 24
        or window.get("membership_field")
            != "collected_at"
    ):
        raise ValueError("window contract mismatch")

    if (
        config.get("core_distance")
            != CORE_DISTANCE
        or config.get("merge_distance")
            != MERGE_DISTANCE
        or config.get("merge_min_cross_links")
            != MERGE_MIN_CROSS_LINKS
        or config.get("max_story_span_hours")
            != MAX_STORY_SPAN_HOURS
        or config.get("distance_method")
            != "exact_pgvector_cosine"
    ):
        raise ValueError("algorithm config mismatch")

    as_of = _aware_datetime(
        snapshot.get("as_of"),
        field="as_of",
    )

    generated_at = _aware_datetime(
        snapshot.get("generated_at"),
        field="generated_at",
    )

    active_count = 0

    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(
                f"candidate[{index}] invalid"
            )

        chronology = candidate.get("chronology")
        source_groups = candidate.get(
            "source_groups"
        )

        if not isinstance(chronology, list):
            raise ValueError(
                f"candidate[{index}] chronology invalid"
            )

        if (
            not isinstance(source_groups, list)
            or not source_groups
            or not set(source_groups)
                <= {"ua_space", "ru_space"}
        ):
            raise ValueError(
                f"candidate[{index}] source groups invalid"
            )

        source_count = candidate.get(
            "source_count"
        )
        occurrence_count = candidate.get(
            "occurrence_count"
        )
        content_count = candidate.get(
            "content_count"
        )
        active_24h = candidate.get(
            "active_24h"
        )
        span_hours = candidate.get(
            "span_hours"
        )

        if (
            type(source_count) is not int
            or source_count < 2
        ):
            raise ValueError(
                f"candidate[{index}] source_count invalid"
            )

        if (
            type(occurrence_count) is not int
            or occurrence_count < 2
            or occurrence_count
                != len(chronology)
        ):
            raise ValueError(
                f"candidate[{index}] occurrence_count invalid"
            )

        if (
            type(content_count) is not int
            or content_count < 1
        ):
            raise ValueError(
                f"candidate[{index}] content_count invalid"
            )

        if type(active_24h) is not bool:
            raise ValueError(
                f"candidate[{index}] active_24h invalid"
            )

        if (
            not isinstance(
                span_hours,
                (int, float),
            )
            or isinstance(span_hours, bool)
            or span_hours < 0
            or span_hours
                > MAX_STORY_SPAN_HOURS + 0.01
        ):
            raise ValueError(
                f"candidate[{index}] span invalid"
            )

        expected_cross = (
            len(set(source_groups)) > 1
        )

        if (
            candidate.get("cross_space")
            is not expected_cross
        ):
            raise ValueError(
                f"candidate[{index}] cross_space invalid"
            )

        for field in (
            "first_published_at",
            "last_published_at",
            "last_observed",
        ):
            _aware_datetime(
                candidate.get(field),
                field=f"candidate[{index}].{field}",
            )

        if active_24h:
            active_count += 1

        for row_index, row in enumerate(
            chronology
        ):
            if not isinstance(row, dict):
                raise ValueError(
                    f"candidate[{index}]."
                    f"chronology[{row_index}] invalid"
                )

            if (
                row.get("source_group")
                not in {
                    "ua_space",
                    "ru_space",
                }
            ):
                raise ValueError(
                    "invalid chronology source group"
                )

            collected_at = _aware_datetime(
                row.get("collected_at"),
                field="collected_at",
            )

            published_at = row.get(
                "published_at"
            )

            if published_at is not None:
                _aware_datetime(
                    published_at,
                    field="published_at",
                )

            if collected_at > as_of:
                raise ValueError(
                    "chronology after as_of"
                )

            external_ref = (
                row.get("external_ref")
                or ""
            )

            if not isinstance(
                external_ref,
                str,
            ):
                raise ValueError(
                    "external_ref invalid"
                )

            row["safe_link"] = safe_link(
                external_ref
            )

    if (
        summary.get("signals_30d")
        != len(candidates)
    ):
        raise ValueError(
            "summary signal count mismatch"
        )

    if (
        summary.get("active_24h")
        != active_count
    ):
        raise ValueError(
            "summary active count mismatch"
        )

    return as_of, generated_at


def load_c1_contour_signals(
    *,
    object_id: int | None = None,
    path: Path | None = None,
    stale_seconds: int = 7200,
    now: datetime | None = None,
) -> dict[str, Any]:
    result = {
        "state": "invalid",
        "snapshot": None,
    }

    try:
        if (
            type(stale_seconds) is not int
            or not 1
                <= stale_seconds
                <= 604800
        ):
            return result

        now = (
            now
            or datetime.now(timezone.utc)
        )

        if now.tzinfo is None:
            return result

        now = now.astimezone(timezone.utc)

        snapshot_path = (
            Path(path)
            if path is not None
            else c1_signals_snapshot_path(
                object_id
            )
        )

        if not (
            snapshot_path
            .resolve()
            .is_relative_to(ROOT)
        ):
            return result

        payload = snapshot_path.read_bytes()

        if len(payload) > MAX_SNAPSHOT_BYTES:
            return result

        snapshot = json.loads(payload)

        (
            as_of,
            generated_at,
        ) = _validate_snapshot(
            snapshot,
            object_id=object_id,
        )

        if (
            as_of > now
            or generated_at > now
        ):
            return result

        stale = (
            now
            - min(
                as_of,
                generated_at,
            )
            > timedelta(
                seconds=stale_seconds
            )
        )

        state = (
            "stale"
            if stale
            else (
                "ready"
                if snapshot["candidates"]
                else "empty"
            )
        )

        return {
            "state": state,
            "snapshot": snapshot,
        }

    except FileNotFoundError:
        return {
            "state": "missing",
            "snapshot": None,
        }

    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        json.JSONDecodeError,
    ):
        return result
