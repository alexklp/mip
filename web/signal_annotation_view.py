"""Read-only проєкція analyst annotations для одного поточного Signal."""

from __future__ import annotations

from web.analyst_annotations import (
    FINGERPRINT_VERSION,
    resolve_effective_contours,
    signal_fingerprint,
)


def fetch_signal_annotation_view(
    conn,
    candidate: dict,
) -> dict:
    """Повернути persisted та effective contour state для Signal."""
    content_ids = list(candidate["content_ids"])

    if not content_ids:
        raise ValueError(
            "Signal не містить content_id"
        )

    fingerprint = signal_fingerprint(content_ids)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                monitoring_contour_id,
                code,
                name
            FROM monitoring_contours
            WHERE active
            ORDER BY monitoring_contour_id
            """
        )
        contours = [
            {
                "monitoring_contour_id":
                    int(row["monitoring_contour_id"]),
                "code": row["code"],
                "name": row["name"],
            }
            for row in cur.fetchall()
        ]

        active_ids = {
            row["monitoring_contour_id"]
            for row in contours
        }

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
        context_row = cur.fetchone()

        context_id = (
            context_row["signal_context_id"]
            if context_row
            else None
        )

        signal_annotations: dict[int, bool] = {}
        excluded_content_ids: set[str] = set()

        if context_id is not None:
            cur.execute(
                """
                SELECT content_id::text AS content_id
                FROM analyst_signal_context_members
                WHERE signal_context_id = %s::uuid
                ORDER BY content_id
                """,
                (context_id,),
            )

            stored_members = {
                row["content_id"]
                for row in cur.fetchall()
            }

            if stored_members != set(content_ids):
                raise ValueError(
                    "Stored signal context membership mismatch"
                )

            cur.execute(
                """
                SELECT
                    a.monitoring_contour_id,
                    a.included
                FROM analyst_signal_contour_annotations a
                JOIN monitoring_contours c
                  USING (monitoring_contour_id)
                WHERE a.signal_context_id = %s::uuid
                  AND c.active
                ORDER BY a.monitoring_contour_id
                """,
                (context_id,),
            )

            signal_annotations = {
                int(row["monitoring_contour_id"]):
                    bool(row["included"])
                for row in cur.fetchall()
            }

            cur.execute(
                """
                SELECT
                    content_id::text AS content_id,
                    included
                FROM analyst_signal_membership_annotations
                WHERE signal_context_id = %s::uuid
                ORDER BY content_id
                """,
                (context_id,),
            )

            excluded_content_ids = {
                row["content_id"]
                for row in cur.fetchall()
                if not row["included"]
            }

        cur.execute(
            """
            SELECT
                a.content_id::text AS content_id,
                a.monitoring_contour_id,
                a.included
            FROM analyst_content_contour_annotations a
            JOIN monitoring_contours c
              USING (monitoring_contour_id)
            WHERE a.content_id = ANY(%s::uuid[])
              AND c.active
            ORDER BY
                content_id,
                monitoring_contour_id
            """,
            (content_ids,),
        )

        content_annotations: dict[
            str,
            dict[int, bool],
        ] = {}

        for row in cur.fetchall():
            content_annotations.setdefault(
                row["content_id"],
                {},
            )[int(row["monitoring_contour_id"])] = (
                bool(row["included"])
            )

        cur.execute(
            """
            SELECT DISTINCT
                a.content_id::text AS content_id,
                a.monitoring_contour_id
            FROM content_contour_assignments a
            JOIN monitoring_contours c
              USING (monitoring_contour_id)
            WHERE a.content_id = ANY(%s::uuid[])
              AND c.active
            ORDER BY
                content_id,
                monitoring_contour_id
            """,
            (content_ids,),
        )

        system_contours: dict[str, set[int]] = {}

        for row in cur.fetchall():
            system_contours.setdefault(
                row["content_id"],
                set(),
            ).add(
                int(row["monitoring_contour_id"])
            )

    materials = {}

    for content_id in content_ids:
        effective = resolve_effective_contours(
            system_contour_ids=(
                system_contours.get(
                    content_id,
                    set(),
                )
            ),
            signal_annotations=signal_annotations,
            content_annotations=(
                content_annotations.get(
                    content_id,
                    {},
                )
            ),
        )

        materials[content_id] = {
            "excluded_from_signal":
                content_id in excluded_content_ids,
            "contours": [
                {
                    "monitoring_contour_id":
                        contour_id,
                    "included":
                        state["included"],
                    "source":
                        state["source"],
                }
                for contour_id, state
                in sorted(effective.items())
                if contour_id in active_ids
            ],
        }

    return {
        "signal_context_id": context_id,
        "signal_fingerprint": fingerprint,
        "contours": contours,
        "signal_annotations": [
            {
                "monitoring_contour_id":
                    contour_id,
                "included": included,
            }
            for contour_id, included
            in sorted(signal_annotations.items())
        ],
        "excluded_content_ids":
            sorted(excluded_content_ids),
        "materials": materials,
    }
