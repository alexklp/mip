"""Обмежений пошук публікацій для окремої тестової сторінки."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


PAGE_SIZE = 25
MAX_PAGE = 20
PERIOD_HOURS = {"24h": 24, "72h": 72, "7d": 168}


def _like_pattern(query: str) -> str:
    """Пошук буквального фрагмента, включно із символами % та _."""
    escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def _safe_link(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not any(ord(char) < 33 for char in value)
            and "\\" not in value
        ):
            return value
    except ValueError:
        pass
    return None


def _excerpt(text: str | None, query: str) -> tuple[str, str, str]:
    normalized = " ".join((text or "").split())
    match = re.search(re.escape(query), normalized, flags=re.IGNORECASE)
    if match is None:
        return normalized[:220], "", ""
    start = max(0, match.start() - 85)
    end = min(len(normalized), match.end() + 135)
    before = ("…" if start else "") + normalized[start:match.start()]
    after = normalized[match.end():end] + ("…" if end < len(normalized) else "")
    return before, normalized[match.start():match.end()], after


def fetch_search_pilot(
    conn,
    *,
    query: str,
    period: str,
    page: int,
    source_group: str | None,
) -> tuple[list[dict], int | None]:
    """Одна публікація на рядок; загальна кількість з того самого запиту."""
    if period not in PERIOD_HOURS or not 1 <= page <= MAX_PAGE:
        raise ValueError("Некоректний період або номер сторінки")
    if not 3 <= len(query) <= 80:
        raise ValueError("Довжина запиту має бути від 3 до 80 символів")

    pattern = _like_pattern(query)
    conditions = [
        "COALESCE(io.published_at, io.collected_at) >= now() - (%s * interval '1 hour')",
        "(COALESCE(ci.title, '') ILIKE %s ESCAPE '!' OR COALESCE(ci.text_content, '') ILIKE %s ESCAPE '!')",
    ]
    params: list[object] = [PERIOD_HOURS[period], pattern, pattern]
    if source_group is not None:
        conditions.append("s.source_group = %s")
        params.append(source_group)

    sql = f"""
        SELECT io.occurrence_id::text AS occurrence_id,
               io.content_id::text AS content_id,
               COALESCE(io.published_at, io.collected_at) AS occurred_at,
               s.name AS source_name, s.source_group,
               ci.title, ci.text_content,
               ci.title ILIKE %s ESCAPE '!' AS in_title,
               count(*) OVER () AS total_count,
               io.external_ref
        FROM item_occurrences io
        JOIN content_items ci USING (content_id)
        JOIN sources s USING (source_id)
        WHERE {' AND '.join(conditions)}
        ORDER BY COALESCE(io.published_at, io.collected_at) DESC,
                 io.occurrence_id DESC
        LIMIT %s OFFSET %s
    """
    with conn.cursor() as cursor:
        cursor.execute(
            sql,
            [pattern, *params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        fetched = cursor.fetchall()

    total = int(fetched[0]["total_count"]) if fetched else (0 if page == 1 else None)
    rows = []
    for original in fetched:
        row = dict(original)
        row.pop("total_count")
        title = (row.pop("title") or "").strip()
        body = row.pop("text_content") or ""
        row["display_title"] = title or " ".join(body.split())[:100]
        row["excerpt_before"], row["excerpt_match"], row["excerpt_after"] = (
            _excerpt(body if re.search(re.escape(query), body, re.IGNORECASE) else title, query)
        )
        row["safe_link"] = _safe_link(row.pop("external_ref"))
        rows.append(row)
    return rows, total
