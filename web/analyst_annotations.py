"""Чистий контракт ручної аналітичної розмітки МІП."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from hashlib import sha256
from uuid import UUID


FINGERPRINT_VERSION = 1

SOURCE_SYSTEM = "system"
SOURCE_SIGNAL = "manual_signal"
SOURCE_CONTENT = "manual_content"


def canonical_signal_members(
    content_ids: Iterable[str],
) -> tuple[str, ...]:
    """Нормалізувати exact-набір content_id для identity сигналу."""
    if isinstance(content_ids, (str, bytes)):
        raise ValueError("content_ids має бути колекцією UUID")

    normalized: list[str] = []

    try:
        for value in content_ids:
            normalized.append(
                str(UUID(str(value)))
            )
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            "Некоректний content_id"
        ) from exc

    if not normalized:
        raise ValueError(
            "Сигнал повинен містити хоча б один content_id"
        )

    if len(normalized) != len(set(normalized)):
        raise ValueError(
            "content_ids сигналу не повинні дублюватися"
        )

    return tuple(sorted(normalized))


def signal_fingerprint(
    content_ids: Iterable[str],
) -> str:
    """SHA-256 канонічного exact-набору content_id, контракт v1."""
    members = canonical_signal_members(content_ids)

    payload = "\n".join(members).encode("ascii")

    return sha256(payload).hexdigest()


def validate_signal_context(
    *,
    content_ids: Iterable[str],
    representative_content_id: str,
) -> tuple[str, ...]:
    """Перевірити, що representative належить exact-набору сигналу."""
    members = canonical_signal_members(content_ids)

    try:
        representative = str(
            UUID(str(representative_content_id))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(
            "Некоректний representative_content_id"
        ) from exc

    if representative not in members:
        raise ValueError(
            "Representative не належить signal membership"
        )

    return members


def resolve_effective_contours(
    *,
    system_contour_ids: Iterable[int],
    signal_annotations: Mapping[int, bool],
    content_annotations: Mapping[int, bool],
) -> dict[int, dict[str, object]]:
    """Обчислити effective contour state для матеріалу в сигналі.

    Пріоритет:
        manual_content
        > manual_signal
        > system assignment.
    """
    system = _validate_contour_ids(
        system_contour_ids
    )
    signal = _validate_annotation_map(
        signal_annotations
    )
    content = _validate_annotation_map(
        content_annotations
    )

    contour_ids = sorted(
        system
        | set(signal)
        | set(content)
    )

    result: dict[int, dict[str, object]] = {}

    for contour_id in contour_ids:
        if contour_id in content:
            result[contour_id] = {
                "included": content[contour_id],
                "source": SOURCE_CONTENT,
            }
        elif contour_id in signal:
            result[contour_id] = {
                "included": signal[contour_id],
                "source": SOURCE_SIGNAL,
            }
        else:
            result[contour_id] = {
                "included": True,
                "source": SOURCE_SYSTEM,
            }

    return result


def _validate_contour_ids(
    values: Iterable[int],
) -> set[int]:
    result: set[int] = set()

    if isinstance(values, (str, bytes)):
        raise ValueError(
            "monitoring_contour_id має бути integer"
        )

    try:
        for value in values:
            if (
                type(value) is not int
                or value <= 0
            ):
                raise ValueError(
                    "Некоректний monitoring_contour_id"
                )
            result.add(value)
    except TypeError as exc:
        raise ValueError(
            "Некоректний список контурів"
        ) from exc

    return result


def _validate_annotation_map(
    values: Mapping[int, bool],
) -> dict[int, bool]:
    if not isinstance(values, Mapping):
        raise ValueError(
            "Annotations мають бути mapping"
        )

    result: dict[int, bool] = {}

    for contour_id, included in values.items():
        if (
            type(contour_id) is not int
            or contour_id <= 0
        ):
            raise ValueError(
                "Некоректний monitoring_contour_id"
            )

        if type(included) is not bool:
            raise ValueError(
                "included має бути boolean"
            )

        result[contour_id] = included

    return result
