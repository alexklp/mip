"""Read-only presentation adapter for C1 contour topics snapshot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SNAPSHOT = (
    ROOT
    / "reporting"
    / "contour_topics_c1.latest.json"
)

EXPECTED_SCHEMA = "contour-topics-c1/1"


def load_c1_contour_topics(
    path: Path = DEFAULT_SNAPSHOT,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {
            "state": "missing",
            "items": [],
        }
    except Exception:
        return {
            "state": "invalid",
            "items": [],
        }

    if payload.get("schema_version") != EXPECTED_SCHEMA:
        return {
            "state": "invalid",
            "items": [],
        }

    contour = payload.get("contour", {})

    if contour.get("monitoring_contour_id") != 1:
        return {
            "state": "invalid",
            "items": [],
        }

    phrases = (
        payload.get("views", {})
        .get("all", {})
        .get("themes", {})
        .get("phrases", [])
    )

    comparison = (
        payload.get("comparison", {})
        .get("all", {})
        .get("phrases", {})
    )

    comparison_series = comparison.get(
        "series",
        {},
    )

    comparable_phrases = [
        row
        for row in phrases
        if row.get("marker_id") in comparison_series
    ]

    ranked_phrases = sorted(
        comparable_phrases,
        key=lambda row: (
            -int(row.get("materials", 0)),
            -int(row.get("sources", 0)),
            -int(row.get("publications", 0)),
            str(row.get("label", "")).casefold(),
        ),
    )

    selected = ranked_phrases[:limit]

    maximum = max(
        (
            int(row.get("materials", 0))
            for row in selected
        ),
        default=0,
    ) or 1

    items = [
        {
            **row,
            "bar_pct": round(
                100
                * int(row.get("materials", 0))
                / maximum
            ),
        }
        for row in selected
    ]

    summary = (
        payload.get("summary", {})
        .get("all", {})
        .get("current", {})
    )

    return {
        "state": "ready",
        "items": items,
        "phrases": ranked_phrases,
        "summary": summary,
        "window": payload.get("window", {}),
        "generated_at": payload.get("generated_at"),
        "comparison": comparison,
        "cloud": (
            payload.get("clouds", {})
            .get("all", {})
            .get("phrases", [])
        ),
    }


def load_c1_contour_topic(
    path: Path = DEFAULT_SNAPSHOT,
    *,
    marker_id: str,
) -> dict[str, Any]:
    """Return one C1 topic with its real publication evidence."""
    path = Path(path)

    payload = json.loads(
        path.read_text(encoding="utf-8")
    )

    if (
        payload.get("schema_version")
        != EXPECTED_SCHEMA
    ):
        raise ValueError(
            "unsupported C1 contour topics schema"
        )

    marker = (
        payload.get("markers", {})
        .get(marker_id)
    )

    if marker is None:
        raise KeyError(marker_id)

    occurrences = []

    for occurrence_id in marker.get(
        "occurrence_refs",
        [],
    ):
        row = (
            payload.get("occurrences", {})
            .get(occurrence_id)
        )

        if row is not None:
            occurrences.append(row)

    return {
        "marker": {
            "marker_id": marker["marker_id"],
            "unit": marker["unit"],
            "label": marker["label"],
        },
        "window": payload.get("window", {}),
        "publications": marker["publications"],
        "materials": marker["materials"],
        "sources": marker["sources"],
        "source_ranking": marker.get(
            "source_ranking",
            [],
        ),
        "evidence_total": marker.get(
            "evidence_total",
            len(occurrences),
        ),
        "evidence_limit_reached": bool(
            marker.get(
                "evidence_limit_reached",
                False,
            )
        ),
        "occurrences": occurrences,
    }
