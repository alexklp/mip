"""Read-only adapter ready cache для Signals theses.

HTTP-шар:
- не звертається до БД;
- не викликає Mamay;
- не створює pending jobs;
- використовує лише validated ready cache.

Однаковий exact-набір content_id може використовувати
один результат у Global, C1 overall та object-specific views.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from reporting.signal_theses import (
    DEFAULT_STATE_ROOT,
    SignalThesesContractError,
    read_cache_record,
)


def _membership_key(
    content_ids: Any,
) -> tuple[str, ...] | None:
    if (
        not isinstance(content_ids, list)
        or not content_ids
        or any(
            not isinstance(content_id, str)
            or not content_id
            for content_id in content_ids
        )
        or len(content_ids)
            != len(set(content_ids))
    ):
        return None

    return tuple(
        sorted(content_ids)
    )


def load_ready_theses_index(
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[
    tuple[str, ...],
    dict[str, Any],
]:
    """Прочитати всі валідні cache entries поточної версії."""

    cache_dir = (
        Path(state_root)
        / "cache"
    )

    try:
        paths = sorted(
            cache_dir.glob("*.json")
        )
    except OSError:
        return {}

    index: dict[
        tuple[str, ...],
        dict[str, Any],
    ] = {}

    for path in paths:
        input_hash = path.stem

        try:
            record = read_cache_record(
                input_hash,
                state_root=state_root,
            )
        except (
            OSError,
            SignalThesesContractError,
        ):
            # Один пошкоджений/застарілий cache не повинен
            # ламати всю сторінку Signals.
            continue

        if record is None:
            continue

        key = tuple(
            sorted(
                record["content_ids"]
            )
        )

        previous = index.get(
            key
        )

        if (
            previous is None
            or record["generated_at"]
                > previous["generated_at"]
        ):
            index[key] = record

    return index


def attach_ready_theses(
    candidates: Iterable[dict[str, Any]],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> None:
    """Додати presentation-only ai_theses до snapshot candidates."""

    index = load_ready_theses_index(
        state_root=state_root,
    )

    for candidate in candidates:
        candidate["ai_theses"] = None

        key = _membership_key(
            candidate.get(
                "content_ids"
            )
        )

        if key is None:
            continue

        record = index.get(key)

        if record is None:
            continue

        candidate["ai_theses"] = {
            "summary":
                record["summary"],
            "theses":
                record["theses"],
            "material_count":
                record["material_count"],
            "generated_at":
                record["generated_at"],
        }
