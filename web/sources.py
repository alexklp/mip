"""Bounded read-запити для сторінки Джерела (Web MVP).

Показує охоплення джерел моніторингу: обсяг публікацій/матеріалів за
джерелом, перше та останнє надходження за вибраний період. Джерела без
жодного надходження за період не включаються (INNER JOIN) -- сторінка про
реальне охоплення, а не про реєстр усіх налаштованих джерел (для цього є
фільтр на /materials).
"""
from __future__ import annotations

from datetime import datetime, timezone

from web.materials import PERIOD_TO_TIMEDELTA, Period


def fetch_sources_overview(conn, *, period: Period = Period.ALL) -> list[dict]:
    """Список джерел з обсягами публікацій/матеріалів за період."""
    period_filter = ""
    params: list[object] = []
    if period is not Period.ALL:
        cutoff = datetime.now(timezone.utc) - PERIOD_TO_TIMEDELTA[period]
        period_filter = "AND COALESCE(io.published_at, io.collected_at) >= %s"
        params.append(cutoff)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                s.source_id,
                s.name,
                s.source_type,
                s.source_group,
                s.is_active,
                COUNT(*) AS publications,
                COUNT(DISTINCT io.content_id) AS materials,
                MIN(COALESCE(io.published_at, io.collected_at)) AS first_item,
                MAX(COALESCE(io.published_at, io.collected_at)) AS last_item
            FROM sources s
            JOIN item_occurrences io ON io.source_id = s.source_id
            WHERE true {period_filter}
            GROUP BY s.source_id, s.name, s.source_type, s.source_group, s.is_active
            ORDER BY materials DESC, s.name ASC
            """,
            params,
        )
        return cur.fetchall()
