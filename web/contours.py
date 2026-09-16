"""Read-only data access for the monitoring contours workspace."""

from __future__ import annotations


from web.c1_scope import c1_analytical_gate_sql


_C1_ANALYTICAL_GATE_SQL = c1_analytical_gate_sql("a")


def fetch_contours(conn) -> list[dict]:
    """Return active monitoring contours in configured order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                monitoring_contour_id,
                code,
                name,
                description
            FROM monitoring_contours
            WHERE active
            ORDER BY monitoring_contour_id
            """
        )
        return cur.fetchall()


def fetch_c1_summary(conn) -> dict:
    """Top-level C1 activity metrics for rolling windows."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH object_content AS (
                SELECT DISTINCT
                    object_id,
                    content_id
                FROM content_contour_assignments a
                WHERE monitoring_contour_id = 1
                  AND object_id IS NOT NULL
                  AND a.evidence_type = 'exact_reference'
{_C1_ANALYTICAL_GATE_SQL}
            ),
            activity AS (
                SELECT
                    oc.object_id,
                    oc.content_id,
                    io.occurrence_id,
                    io.source_id,
                    COALESCE(io.published_at, io.collected_at) AS observed_at
                FROM object_content oc
                JOIN item_occurrences io
                  ON io.content_id = oc.content_id
            )
            SELECT
                (
                    SELECT count(*)
                    FROM contour_reference_objects
                    WHERE monitoring_contour_id = 1
                      AND active
                ) AS objects_total,

                count(DISTINCT object_id) FILTER (
                    WHERE observed_at >= now() - interval '24 hours'
                ) AS active_objects_24h,

                count(DISTINCT object_id) FILTER (
                    WHERE observed_at >= now() - interval '7 days'
                ) AS active_objects_7d,

                count(DISTINCT content_id) FILTER (
                    WHERE observed_at >= now() - interval '24 hours'
                ) AS materials_24h,

                count(DISTINCT content_id) FILTER (
                    WHERE observed_at >= now() - interval '7 days'
                ) AS materials_7d,

                count(DISTINCT source_id) FILTER (
                    WHERE observed_at >= now() - interval '24 hours'
                ) AS sources_24h,

                max(observed_at) AS last_seen
            FROM activity
            """
        )
        return cur.fetchone()


def fetch_c1_daily_trend(
    conn,
    *,
    days: int = 30,
    object_id: int | None = None,
) -> list[dict]:
    """C1 trend by Kyiv calendar day, optionally for one object."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH object_content AS (
                SELECT DISTINCT content_id
                FROM content_contour_assignments a
                WHERE monitoring_contour_id = 1
                  AND object_id IS NOT NULL
                  AND (%s::bigint IS NULL OR object_id = %s)
                  AND a.evidence_type = 'exact_reference'
{_C1_ANALYTICAL_GATE_SQL}
            ),
            activity AS (
                SELECT
                    oc.content_id,
                    io.source_id,
                    COALESCE(
                        io.published_at,
                        io.collected_at
                    ) AS observed_at
                FROM object_content oc
                JOIN item_occurrences io
                  ON io.content_id = oc.content_id
            ),
            days AS (
                SELECT generate_series(
                    (now() AT TIME ZONE 'Europe/Kyiv')::date - (%s - 1),
                    (now() AT TIME ZONE 'Europe/Kyiv')::date,
                    interval '1 day'
                )::date AS day
            )
            SELECT
                d.day,
                count(DISTINCT a.content_id) AS materials,
                count(DISTINCT a.source_id) AS sources
            FROM days d
            LEFT JOIN activity a
              ON (
                    a.observed_at AT TIME ZONE 'Europe/Kyiv'
                 )::date = d.day
            GROUP BY d.day
            ORDER BY d.day
            """,
            (object_id, object_id, days),
        )
        return cur.fetchall()



def fetch_c1_active_objects(
    conn,
    *,
    days: int = 30,
    day=None,
) -> list[dict]:
    """Rank C1 objects for rolling window or one Kyiv calendar day."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH object_content AS (
                SELECT DISTINCT
                    object_id,
                    content_id
                FROM content_contour_assignments a
                WHERE monitoring_contour_id = 1
                  AND object_id IS NOT NULL
                  AND a.evidence_type = 'exact_reference'
{_C1_ANALYTICAL_GATE_SQL}
            ),
            activity AS (
                SELECT
                    oc.object_id,
                    oc.content_id,
                    io.source_id,
                    COALESCE(
                        io.published_at,
                        io.collected_at
                    ) AS observed_at
                FROM object_content oc
                JOIN item_occurrences io
                  ON io.content_id = oc.content_id
            )
            SELECT
                o.object_id,
                o.canonical_name,
                count(DISTINCT a.content_id) AS materials,
                count(DISTINCT a.source_id) AS sources,
                max(a.observed_at) AS last_seen
            FROM contour_reference_objects o
            JOIN activity a
              ON a.object_id = o.object_id
            WHERE o.monitoring_contour_id = 1
              AND o.active
              AND (
                    (
                        %s::date IS NULL
                        AND a.observed_at
                            >= now() - (%s * interval '1 day')
                    )
                    OR (
                        %s::date IS NOT NULL
                        AND (
                            a.observed_at AT TIME ZONE 'Europe/Kyiv'
                        )::date = %s::date
                    )
              )
            GROUP BY
                o.object_id,
                o.canonical_name
            ORDER BY
                materials DESC,
                last_seen DESC,
                o.canonical_name
            """,
            (day, days, day, day),
        )
        return cur.fetchall()


def fetch_c1_changes(
    conn,
    *,
    recent_days: int = 7,
    baseline_days: int = 30,
    min_quiet_days: int = 7,
    min_peak_active_days: int = 10,
) -> list[dict]:
    """Return recent deterministic C1 change events.

    Event types:
    - first: first observed mention in available MIP history;
    - return: mention after a sufficiently long quiet interval;
    - peak: new rolling-window maximum for an object with enough history.

    The current Kyiv calendar day is excluded because it is incomplete.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH activity AS (
                SELECT
                    a.object_id,
                    o.canonical_name,
                    (
                        COALESCE(
                            io.published_at,
                            io.collected_at
                        ) AT TIME ZONE 'Europe/Kyiv'
                    )::date AS day,
                    count(DISTINCT a.content_id) AS materials
                FROM content_contour_assignments a
                JOIN contour_reference_objects o
                  ON o.object_id = a.object_id
                 AND o.monitoring_contour_id = a.monitoring_contour_id
                JOIN item_occurrences io
                  ON io.content_id = a.content_id
                WHERE a.monitoring_contour_id = 1
                  AND a.object_id IS NOT NULL
                  AND a.evidence_type = 'exact_reference'
{_C1_ANALYTICAL_GATE_SQL}
                  AND o.active
                GROUP BY
                    a.object_id,
                    o.canonical_name,
                    day
            ),
            candidates AS (
                SELECT
                    a.object_id,
                    a.canonical_name,
                    a.day,
                    a.materials,
                    previous.previous_day,
                    first_seen.first_seen_day,
                    baseline.baseline_active_days,
                    baseline.previous_max,
                    CASE
                        WHEN previous.previous_day IS NOT NULL
                        THEN a.day - previous.previous_day - 1
                        ELSE NULL
                    END AS quiet_days
                FROM activity a
                LEFT JOIN LATERAL (
                    SELECT max(p.day) AS previous_day
                    FROM activity p
                    WHERE p.object_id = a.object_id
                      AND p.day < a.day
                ) previous ON true
                LEFT JOIN LATERAL (
                    SELECT min(f.day) AS first_seen_day
                    FROM activity f
                    WHERE f.object_id = a.object_id
                ) first_seen ON true
                LEFT JOIN LATERAL (
                    SELECT
                        count(*) AS baseline_active_days,
                        max(b.materials) AS previous_max
                    FROM activity b
                    WHERE b.object_id = a.object_id
                      AND b.day >= a.day - %s
                      AND b.day < a.day
                ) baseline ON true
                WHERE a.day >= (
                        (now() AT TIME ZONE 'Europe/Kyiv')::date - %s
                      )
                  AND a.day < (
                        now() AT TIME ZONE 'Europe/Kyiv'
                      )::date
            ),
            classified AS (
                SELECT
                    *,
                    CASE
                        WHEN previous_day IS NULL
                            THEN 'first'
                        WHEN baseline_active_days >= %s
                             AND materials > COALESCE(previous_max, 0)
                            THEN 'peak'
                        WHEN quiet_days >= %s
                            THEN 'return'
                        ELSE NULL
                    END AS event_type
                FROM candidates
            )
            , events AS (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY object_id, event_type
                        ORDER BY day DESC
                    ) AS event_rank
                FROM classified
                WHERE event_type IS NOT NULL
            )
            SELECT
                object_id,
                canonical_name,
                day,
                materials,
                previous_day,
                first_seen_day,
                baseline_active_days,
                previous_max,
                quiet_days,
                event_type
            FROM events
            WHERE event_type <> 'peak'
               OR event_rank = 1
            ORDER BY
                day DESC,
                CASE event_type
                    WHEN 'peak' THEN 1
                    WHEN 'first' THEN 2
                    WHEN 'return' THEN 3
                    ELSE 9
                END,
                canonical_name
            """,
            (
                baseline_days,
                recent_days,
                min_peak_active_days,
                min_quiet_days,
            ),
        )
        return cur.fetchall()
