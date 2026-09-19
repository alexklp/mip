"""Read-only adapter for scoped C1 Signals snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from web.signals import ROOT, load_signals


DEFAULT_C1_SNAPSHOT = (
    ROOT / "reporting" / "signals_c1.latest.json"
)


def c1_signals_snapshot_path(
    object_id: int | None = None,
) -> Path:
    if object_id is None:
        return DEFAULT_C1_SNAPSHOT

    return DEFAULT_C1_SNAPSHOT.with_name(
        f"signals_c1.object_{object_id}.latest.json"
    )


def load_c1_contour_signals(
    *,
    object_id: int | None = None,
    stale_seconds: int = 7200,
) -> dict[str, Any]:
    view = load_signals(
        c1_signals_snapshot_path(object_id),
        stale_seconds=stale_seconds,
    )

    snapshot = view.get("snapshot")

    if snapshot is None:
        return view

    selection = snapshot.get("selection") or {}

    if (
        selection.get("strategy") != "exact_current_24h"
        or selection.get("monitoring_contour_id") != 1
        or selection.get("object_id") != object_id
    ):
        return {
            "state": "invalid",
            "snapshot": None,
        }

    return view
