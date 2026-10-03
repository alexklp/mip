"""Вузький persistence adapter для ручної аналітичної розмітки."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from web.analyst_annotations import (
    FINGERPRINT_VERSION,
    signal_fingerprint,
    validate_signal_context,
)
from web.db import (
    DB_DSN,
    WEB_STATEMENT_TIMEOUT_MS,
)


@contextmanager
def annotation_write_connection():
    """Окрема коротка транзакція лише для analyst annotations."""
    conn = psycopg.connect(
        DB_DSN,
        row_factory=dict_row,
        options=(
            f"-c statement_timeout="
            f"{WEB_STATEMENT_TIMEOUT_MS}"
        ),
    )

    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_signal_context(
    conn,
    *,
    content_ids,
    representative_content_id,
) -> str:
    """Знайти або створити immutable exact-контекст сигналу."""
    members = validate_signal_context(
        content_ids=content_ids,
        representative_content_id=representative_content_id,
    )
    representative = _uuid_text(
        representative_content_id,
        field="representative_content_id",
    )
    fingerprint = signal_fingerprint(members)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analyst_signal_contexts (
                fingerprint_version,
                signal_fingerprint,
                representative_content_id
            )
            VALUES (%s, %s, %s::uuid)
            ON CONFLICT (
                fingerprint_version,
                signal_fingerprint
            )
            DO NOTHING
            RETURNING signal_context_id::text
                AS signal_context_id
            """,
            (
                FINGERPRINT_VERSION,
                fingerprint,
                representative,
            ),
        )

        row = cur.fetchone()
        created = row is not None

        if created:
            context_id = _uuid_text(
                row["signal_context_id"],
                field="signal_context_id",
            )

            cur.executemany(
                """
                INSERT INTO analyst_signal_context_members (
                    signal_context_id,
                    content_id
                )
                VALUES (%s::uuid, %s::uuid)
                """,
                [
                    (
                        context_id,
                        content_id,
                    )
                    for content_id in members
                ],
            )
        else:
            cur.execute(
                """
                SELECT signal_context_id::text
                    AS signal_context_id
                FROM analyst_signal_contexts
                WHERE fingerprint_version = %s
                  AND signal_fingerprint = %s
                """,
                (
                    FINGERPRINT_VERSION,
                    fingerprint,
                ),
            )

            existing = cur.fetchone()

            if existing is None:
                raise RuntimeError(
                    "Signal context conflict without stored row"
                )

            context_id = _uuid_text(
                existing["signal_context_id"],
                field="signal_context_id",
            )

        cur.execute(
            """
            SELECT content_id::text AS content_id
            FROM analyst_signal_context_members
            WHERE signal_context_id = %s::uuid
            ORDER BY content_id
            """,
            (context_id,),
        )

        stored_members = tuple(
            sorted(
                _uuid_text(
                    row["content_id"],
                    field="content_id",
                )
                for row in cur.fetchall()
            )
        )

    if stored_members != members:
        raise ValueError(
            "Stored signal context membership mismatch"
        )

    return context_id


def set_signal_contour_annotation(
    conn,
    *,
    signal_context_id,
    monitoring_contour_id,
    included,
) -> None:
    """Записати explicit human-рішення signal → contour."""
    context_id = _uuid_text(
        signal_context_id,
        field="signal_context_id",
    )
    contour_id = _contour_id(
        monitoring_contour_id
    )
    value = _included(included)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analyst_signal_contour_annotations (
                signal_context_id,
                monitoring_contour_id,
                included
            )
            VALUES (%s::uuid, %s, %s)
            ON CONFLICT (
                signal_context_id,
                monitoring_contour_id
            )
            DO UPDATE SET
                included = EXCLUDED.included,
                updated_at = now()
            """,
            (
                context_id,
                contour_id,
                value,
            ),
        )


def set_content_contour_annotation(
    conn,
    *,
    content_id,
    monitoring_contour_id,
    included,
) -> None:
    """Записати explicit human-рішення material → contour."""
    normalized_content_id = _uuid_text(
        content_id,
        field="content_id",
    )
    contour_id = _contour_id(
        monitoring_contour_id
    )
    value = _included(included)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analyst_content_contour_annotations (
                content_id,
                monitoring_contour_id,
                included
            )
            VALUES (%s::uuid, %s, %s)
            ON CONFLICT (
                content_id,
                monitoring_contour_id
            )
            DO UPDATE SET
                included = EXCLUDED.included,
                updated_at = now()
            """,
            (
                normalized_content_id,
                contour_id,
                value,
            ),
        )


def set_signal_membership_annotation(
    conn,
    *,
    signal_context_id,
    content_id,
    included,
) -> None:
    """Записати human correction membership матеріалу в сигналі."""
    context_id = _uuid_text(
        signal_context_id,
        field="signal_context_id",
    )
    normalized_content_id = _uuid_text(
        content_id,
        field="content_id",
    )
    value = _included(included)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analyst_signal_membership_annotations (
                signal_context_id,
                content_id,
                included
            )
            VALUES (%s::uuid, %s::uuid, %s)
            ON CONFLICT (
                signal_context_id,
                content_id
            )
            DO UPDATE SET
                included = EXCLUDED.included,
                updated_at = now()
            """,
            (
                context_id,
                normalized_content_id,
                value,
            ),
        )


def _uuid_text(
    value,
    *,
    field: str,
) -> str:
    try:
        return str(UUID(str(value)))
    except (
        TypeError,
        ValueError,
        AttributeError,
    ) as exc:
        raise ValueError(
            f"Некоректний {field}"
        ) from exc


def _contour_id(value) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(
            "Некоректний monitoring_contour_id"
        )

    return value


def _included(value) -> bool:
    if type(value) is not bool:
        raise ValueError(
            "included має бути boolean"
        )

    return value


def clear_signal_contour_annotation(
    conn,
    *,
    signal_context_id,
    monitoring_contour_id,
) -> None:
    """Прибрати explicit human-рішення signal → contour."""
    context_id = _uuid_text(
        signal_context_id,
        field="signal_context_id",
    )
    contour_id = _contour_id(
        monitoring_contour_id
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM analyst_signal_contour_annotations
            WHERE signal_context_id = %s::uuid
              AND monitoring_contour_id = %s
            """,
            (
                context_id,
                contour_id,
            ),
        )


def clear_content_contour_annotation(
    conn,
    *,
    content_id,
    monitoring_contour_id,
) -> None:
    """Прибрати explicit human-рішення material → contour."""
    normalized_content_id = _uuid_text(
        content_id,
        field="content_id",
    )
    contour_id = _contour_id(
        monitoring_contour_id
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM analyst_content_contour_annotations
            WHERE content_id = %s::uuid
              AND monitoring_contour_id = %s
            """,
            (
                normalized_content_id,
                contour_id,
            ),
        )


def require_active_contour(
    conn,
    monitoring_contour_id,
) -> int:
    """Перевірити, що contour існує та активний."""
    contour_id = _contour_id(
        monitoring_contour_id
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT monitoring_contour_id
            FROM monitoring_contours
            WHERE monitoring_contour_id = %s
              AND active
            """,
            (contour_id,),
        )

        if cur.fetchone() is None:
            raise ValueError(
                "Контур не знайдено або він неактивний"
            )

    return contour_id
