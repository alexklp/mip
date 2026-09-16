"""Read-only data access for the monitored C1 objects screen."""

from __future__ import annotations

from web.c1_scope import c1_analytical_gate_sql


_C1_ANALYTICAL_GATE_SQL = c1_analytical_gate_sql("a")
_C1_ANALYTICAL_GATE_OTHER_SQL = c1_analytical_gate_sql("other")


def fetch_objects_overview(conn) -> list[dict]:
    """Return all active C1 objects with recent observed activity.

    Object assignment is content-level. Publication/source evidence lives in
    item_occurrences, so counts distinguish materials from acquisition sources.
    Objects without observed matches are intentionally retained.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH object_content AS (
                SELECT DISTINCT
                    object_id,
                    content_id
                FROM content_contour_assignments a
                WHERE a.monitoring_contour_id = 1
                  AND a.object_id IS NOT NULL
                  AND a.evidence_type = 'exact_reference'
{_C1_ANALYTICAL_GATE_SQL}
            ),
            activity AS (
                SELECT
                    oc.object_id,
                    oc.content_id,
                    io.source_id,
                    s.source_group,
                    s.source_type,
                    COALESCE(io.published_at, io.collected_at) AS observed_at
                FROM object_content oc
                JOIN item_occurrences io
                  ON io.content_id = oc.content_id
                JOIN sources s
                  ON s.source_id = io.source_id
            )
            SELECT
                o.object_id,
                o.canonical_name,

                COUNT(DISTINCT a.content_id) FILTER (
                    WHERE a.observed_at >= now() - interval '24 hours'
                ) AS contents_24h,

                COUNT(DISTINCT a.content_id) FILTER (
                    WHERE a.observed_at >= now() - interval '7 days'
                ) AS contents_7d,

                COUNT(DISTINCT a.content_id) FILTER (
                    WHERE a.observed_at >= now() - interval '30 days'
                ) AS contents_30d,

                COUNT(DISTINCT a.source_id) FILTER (
                    WHERE a.observed_at >= now() - interval '24 hours'
                ) AS sources_24h,

                COUNT(DISTINCT a.content_id) FILTER (
                    WHERE a.observed_at >= now() - interval '24 hours'
                      AND a.source_group = 'ua_space'
                ) AS ua_24h,

                COUNT(DISTINCT a.content_id) FILTER (
                    WHERE a.observed_at >= now() - interval '24 hours'
                      AND a.source_group = 'ru_space'
                ) AS ru_24h,

                COUNT(DISTINCT a.content_id) FILTER (
                    WHERE a.observed_at >= now() - interval '24 hours'
                      AND a.source_type = 'telegram'
                ) AS tg_24h,

                COUNT(DISTINCT a.content_id) FILTER (
                    WHERE a.observed_at >= now() - interval '24 hours'
                      AND a.source_type = 'rss'
                ) AS rss_24h,

                MAX(a.observed_at) AS last_seen

            FROM contour_reference_objects o
            LEFT JOIN activity a
              ON a.object_id = o.object_id
            WHERE o.monitoring_contour_id = 1
              AND o.active
            GROUP BY o.object_id, o.canonical_name
            ORDER BY
                contents_24h DESC,
                last_seen DESC NULLS LAST,
                o.canonical_name
            """
        )
        return cur.fetchall()


OBJECT_EVIDENCE_LIMIT = 50


def fetch_object_evidence(
    conn,
    *,
    object_id: int,
    days: int = 30,
    day=None,
) -> list[dict]:
    """Latest publication evidence for one active C1 object."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                io.occurrence_id,
                io.content_id,
                COALESCE(io.published_at, io.collected_at) AS observed_at,
                s.source_id,
                s.name AS source_name,
                s.source_group,
                s.source_type,
                io.external_ref,
                COALESCE(
                    NULLIF(btrim(ci.title), ''),
                    left(btrim(ci.text_content), 120)
                ) AS display_title,
                left(btrim(ci.text_content), 320) AS preview_text,
                e.reference_text AS matched_alias,
                EXISTS (
                    SELECT 1
                    FROM content_contour_assignments other
                    WHERE other.content_id = a.content_id
                      AND other.monitoring_contour_id = 1
                      AND other.object_id IS NOT NULL
                      AND other.object_id <> a.object_id
                      AND other.evidence_type = 'exact_reference'
{_C1_ANALYTICAL_GATE_OTHER_SQL}
                ) AS multi_object
            FROM content_contour_assignments a
            JOIN contour_reference_objects o
              ON o.object_id = a.object_id
             AND o.monitoring_contour_id = a.monitoring_contour_id
            JOIN item_occurrences io
              ON io.content_id = a.content_id
            JOIN content_items ci
              ON ci.content_id = a.content_id
            JOIN sources s
              ON s.source_id = io.source_id
            LEFT JOIN contour_reference_entries e
              ON e.reference_id = a.reference_id
            WHERE a.monitoring_contour_id = 1
              AND a.object_id = %s
              AND a.evidence_type = 'exact_reference'
{_C1_ANALYTICAL_GATE_SQL}
              AND o.active
              AND (
                    (
                        %s::date IS NULL
                        AND COALESCE(
                            io.published_at,
                            io.collected_at
                        ) >= now() - (%s * interval '1 day')
                    )
                    OR (
                        %s::date IS NOT NULL
                        AND (
                            COALESCE(
                                io.published_at,
                                io.collected_at
                            ) AT TIME ZONE 'Europe/Kyiv'
                        )::date = %s::date
                    )
              )
            ORDER BY
                COALESCE(io.published_at, io.collected_at) DESC,
                io.occurrence_id DESC
            LIMIT %s
            """,
            (object_id, day, days, day, day, OBJECT_EVIDENCE_LIMIT),
        )
        return cur.fetchall()


C1_EVIDENCE_LIMIT = 12


def fetch_c1_evidence(
    conn,
    *,
    days: int = 30,
    day=None,
) -> list[dict]:
    """Latest publication-level evidence for the analytical C1 population."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                io.occurrence_id,
                io.content_id,
                COALESCE(io.published_at, io.collected_at) AS observed_at,
                s.source_id,
                s.name AS source_name,
                s.source_group,
                s.source_type,
                io.external_ref,
                COALESCE(
                    NULLIF(btrim(ci.title), ''),
                    left(btrim(ci.text_content), 120)
                ) AS display_title,
                left(btrim(ci.text_content), 320) AS preview_text,
                string_agg(
                    DISTINCT e.reference_text,
                    ' · '
                    ORDER BY e.reference_text
                ) FILTER (
                    WHERE e.reference_text IS NOT NULL
                ) AS matched_alias,
                count(DISTINCT a.object_id) > 1 AS multi_object
            FROM content_contour_assignments a
            JOIN contour_reference_objects o
              ON o.object_id = a.object_id
             AND o.monitoring_contour_id = a.monitoring_contour_id
            JOIN item_occurrences io
              ON io.content_id = a.content_id
            JOIN content_items ci
              ON ci.content_id = a.content_id
            JOIN sources s
              ON s.source_id = io.source_id
            LEFT JOIN contour_reference_entries e
              ON e.reference_id = a.reference_id
            WHERE a.monitoring_contour_id = 1
              AND a.object_id IS NOT NULL
              AND a.evidence_type = 'exact_reference'
{_C1_ANALYTICAL_GATE_SQL}
              AND o.active
              AND (
                    (
                        %s::date IS NULL
                        AND COALESCE(
                            io.published_at,
                            io.collected_at
                        ) >= now() - (%s * interval '1 day')
                    )
                    OR (
                        %s::date IS NOT NULL
                        AND (
                            COALESCE(
                                io.published_at,
                                io.collected_at
                            ) AT TIME ZONE 'Europe/Kyiv'
                        )::date = %s::date
                    )
              )
            GROUP BY
                io.occurrence_id,
                io.content_id,
                COALESCE(io.published_at, io.collected_at),
                s.source_id,
                s.name,
                s.source_group,
                s.source_type,
                io.external_ref,
                ci.title,
                ci.text_content
            ORDER BY
                COALESCE(io.published_at, io.collected_at) DESC,
                io.occurrence_id DESC
            LIMIT %s
            """,
            (day, days, day, day, C1_EVIDENCE_LIMIT),
        )
        return cur.fetchall()
