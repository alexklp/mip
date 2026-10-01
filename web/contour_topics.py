"""Read-only presentation adapter for C1 contour topics snapshot."""

from __future__ import annotations

from datetime import date
import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SNAPSHOT = (
    ROOT
    / "reporting"
    / "contour_topics_c1.latest.json"
)

EXPECTED_SCHEMA = "contour-topics-c1/1"

DAILY_CLOUD_LIMIT = 52
DAILY_CLOUD_WIDTH = 1200
DAILY_CLOUD_HEIGHT = 680


def c1_topics_snapshot_path(
    object_id: int | None = None,
) -> Path:
    if object_id is None:
        return DEFAULT_SNAPSHOT

    return DEFAULT_SNAPSHOT.with_name(
        f"contour_topics_c1.object_{object_id}.latest.json"
    )


def _lightweight_cloud_layout(
    phrases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Швидко й детерміновано компонувати денну хмару тем.

    На відміну від offline WordCloud, не виконує пошук колізій:
    рядки розміщуються за оціненою шириною тексту без суттєвої
    вартості під час HTTP-запиту.
    """
    selected = phrases[:DAILY_CLOUD_LIMIT]

    if not selected:
        return []

    maximum = max(
        int(row.get("publications", 0))
        for row in selected
    ) or 1

    x = 24.0
    y = 24.0
    row_height = 0.0
    row_index = 0
    result = []

    for rank, row in enumerate(selected, start=1):
        publications = max(
            int(row.get("publications", 0)),
            1,
        )

        ratio = publications / maximum

        font_size = round(
            17 + 35 * math.sqrt(ratio)
        )

        label = str(row.get("label", ""))

        estimated_width = min(
            520.0,
            max(
                72.0,
                len(label)
                * font_size
                * 0.52,
            ),
        )

        if (
            x + estimated_width
            > DAILY_CLOUD_WIDTH - 24
        ):
            y += row_height + 18
            row_index += 1
            x = 24.0 + (row_index % 3) * 18
            row_height = 0.0

        if (
            y + font_size
            > DAILY_CLOUD_HEIGHT - 20
        ):
            break

        result.append(
            {
                "marker_id": row["marker_id"],
                "label": label,
                "rank": rank,
                "x": round(x),
                "y": round(y),
                "font_size": font_size,
            }
        )

        x += estimated_width + 24
        row_height = max(
            row_height,
            float(font_size),
        )

    return result


def _filtered_comparison(
    comparison: dict[str, Any],
    phrases: list[dict[str, Any]],
) -> dict[str, Any]:
    marker_ids = {
        row["marker_id"]
        for row in phrases
    }

    raw_series = comparison.get(
        "series",
        {},
    )

    return {
        **comparison,
        "series": {
            marker_id: raw_series[marker_id]
            for marker_id in marker_ids
            if marker_id in raw_series
        },
    }


def _read_payload(
    path: Path,
    *,
    object_id: int | None,
) -> dict[str, Any]:
    payload = json.loads(
        path.read_text(encoding="utf-8")
    )

    if payload.get("schema_version") != EXPECTED_SCHEMA:
        raise ValueError(
            "unsupported C1 contour topics schema"
        )

    contour = payload.get("contour", {})

    if (
        contour.get("monitoring_contour_id") != 1
        or contour.get("object_id") != object_id
    ):
        raise ValueError(
            "C1 contour topics scope mismatch"
        )

    return payload


def load_c1_contour_topics(
    path: Path | None = None,
    *,
    object_id: int | None = None,
    day: date | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    path = (
        Path(path)
        if path is not None
        else c1_topics_snapshot_path(object_id)
    )

    try:
        payload = _read_payload(
            path,
            object_id=object_id,
        )
    except FileNotFoundError:
        return {
            "state": "missing",
            "items": [],
        }
    except ValueError:
        return {
            "state": "invalid",
            "items": [],
        }

    raw_comparison = (
        payload.get("comparison", {})
        .get("all", {})
        .get("phrases", {})
    )

    selected_day = (
        day.isoformat()
        if day is not None
        else None
    )

    if day is not None:
        daily = payload.get(
            "daily",
            {},
        )

        day_block = daily.get(
            selected_day
        )

        if not isinstance(
            day_block,
            dict,
        ):
            return {
                "state": "missing",
                "items": [],
                "selected_day":
                    selected_day,
            }

        phrases = list(
            day_block.get(
                "phrases",
                [],
            )
        )

        summary = dict(
            day_block.get(
                "summary",
                {},
            )
        )

        cloud = (
            _lightweight_cloud_layout(
                phrases
            )
        )

        day_filtered = True

    else:
        phrases = (
            payload.get("views", {})
            .get("all", {})
            .get("themes", {})
            .get("phrases", [])
        )

        summary = (
            payload.get("summary", {})
            .get("all", {})
            .get("current", {})
        )

        cloud = (
            payload.get("clouds", {})
            .get("all", {})
            .get("phrases", [])
        )

        day_filtered = False

    ranked_phrases = sorted(
        phrases,
        key=lambda row: (
            -int(
                row.get(
                    "materials",
                    0,
                )
            ),
            -int(
                row.get(
                    "sources",
                    0,
                )
            ),
            -int(
                row.get(
                    "publications",
                    0,
                )
            ),
            str(
                row.get(
                    "label",
                    "",
                )
            ).casefold(),
        ),
    )

    selected = ranked_phrases[:limit]

    maximum = max(
        (
            int(
                row.get(
                    "materials",
                    0,
                )
            )
            for row in selected
        ),
        default=0,
    ) or 1

    items = [
        {
            **row,
            "bar_pct": round(
                100
                * int(
                    row.get(
                        "materials",
                        0,
                    )
                )
                / maximum
            ),
        }
        for row in selected
    ]

    comparison = _filtered_comparison(
        raw_comparison,
        ranked_phrases,
    )

    return {
        "state": "ready",
        "items": items,
        "phrases": ranked_phrases,
        "summary": summary,
        "window": payload.get(
            "window",
            {},
        ),
        "generated_at":
            payload.get(
                "generated_at"
            ),
        "object_id": (
            payload.get(
                "contour",
                {}
            ).get(
                "object_id"
            )
        ),
        "comparison": comparison,
        "cloud": cloud,
        "selected_day":
            selected_day,
        "day_filtered":
            day_filtered,
    }


def load_c1_contour_topic(
    path: Path | None = None,
    *,
    marker_id: str,
    object_id: int | None = None,
    day: date | None = None,
) -> dict[str, Any]:
    """Return one C1 topic with its real publication evidence."""
    path = (
        Path(path)
        if path is not None
        else c1_topics_snapshot_path(object_id)
    )

    payload = _read_payload(
        path,
        object_id=object_id,
    )

    selected_day = (
        day.isoformat()
        if day is not None
        else None
    )

    if day is None:
        marker = (
            payload.get(
                "markers",
                {}
            ).get(
                marker_id
            )
        )
    else:
        day_block = (
            payload.get(
                "daily",
                {}
            ).get(
                selected_day
            )
        )

        if not isinstance(
            day_block,
            dict,
        ):
            raise KeyError(
                selected_day
            )

        marker = (
            day_block.get(
                "markers",
                {}
            ).get(
                marker_id
            )
        )

    if marker is None:
        raise KeyError(marker_id)

    occurrences = []

    for occurrence_id in marker.get(
        "occurrence_refs",
        [],
    ):
        row = (
            payload.get(
                "occurrences",
                {}
            ).get(
                occurrence_id
            )
        )

        if row is not None:
            occurrences.append(row)

    return {
        "marker": {
            "marker_id":
                marker["marker_id"],
            "unit":
                marker["unit"],
            "label":
                marker["label"],
        },
        "window":
            payload.get(
                "window",
                {},
            ),
        "selected_day":
            selected_day,
        "publications":
            marker["publications"],
        "materials":
            marker["materials"],
        "sources":
            marker["sources"],
        "source_ranking":
            marker.get(
                "source_ranking",
                [],
            ),
        "evidence_total":
            marker.get(
                "evidence_total",
                len(occurrences),
            ),
        "evidence_limit_reached":
            bool(
                marker.get(
                    "evidence_limit_reached",
                    False,
                )
            ),
        "occurrences":
            occurrences,
    }
