"""Bounded read-запити для сторінки Materials (Web MVP).

Один рядок списку — одна публікація item_occurrences, а НЕ дедуплікований
content_items: однаковий текст з різних джерел/публікацій не повинен
схлопуватися в один рядок. Наявність full text і segments визначається
через EXISTS, без розмноження рядків і без N+1. Останнє routing-рішення
для content_id обирається детерміновано (routing_version DESC,
created_at DESC, LIMIT 1) через LATERAL join.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID
from typing import Annotated

from pydantic import BeforeValidator

MATERIALS_LIMIT = 50
PREVIEW_LENGTH = 240
TITLE_FALLBACK_LENGTH = 80


class Period(str, Enum):
    """Часове вікно вибірки публікацій."""

    HOURS_24 = "24h"
    HOURS_72 = "72h"
    DAYS_7 = "7d"
    ALL = "all"


class SourceGroup(str, Enum):
    """Інформаційний простір джерела (sources.source_group)."""

    UA_SPACE = "ua_space"
    RU_SPACE = "ru_space"
    OTHER = "other"
    UNKNOWN = "unknown"


class SourceType(str, Enum):
    """Тип джерела (sources.source_type)."""

    RSS = "rss"
    TELEGRAM = "telegram"
    WEB = "web"
    API = "api"


class RoutingDecision(str, Enum):
    """Routing-рішення для content_id, включно з відсутнім рішенням."""

    ANALYZE = "analyze"
    MAYBE = "maybe"
    SKIP = "skip"
    PENDING = "pending"


def _blank_to_none(value: object) -> object:
    """Порожній рядок з форми (незаповнений фільтр) означає 'фільтр не задано'."""
    if value == "":
        return None
    return value


OptionalSourceGroup = Annotated[SourceGroup | None, BeforeValidator(_blank_to_none)]
OptionalSourceType = Annotated[SourceType | None, BeforeValidator(_blank_to_none)]
OptionalRoutingDecision = Annotated[RoutingDecision | None, BeforeValidator(_blank_to_none)]
OptionalSourceIdUUID = Annotated[UUID | None, BeforeValidator(_blank_to_none)]


PERIOD_TO_TIMEDELTA = {
    Period.HOURS_24: timedelta(hours=24),
    Period.HOURS_72: timedelta(hours=72),
    Period.DAYS_7: timedelta(days=7),
}


def fetch_active_sources(conn) -> list[dict]:
    """Активні джерела для форми фільтрів, детерміновано відсортовані."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_id, name
            FROM sources
            WHERE is_active = true
            ORDER BY name, source_id
            """
        )
        return cur.fetchall()


def fetch_materials(
    conn,
    *,
    period: Period = Period.HOURS_24,
    source_group: SourceGroup | None = None,
    source_type: SourceType | None = None,
    source_id: UUID | None = None,
    decision: RoutingDecision | None = None,
) -> list[dict]:
    """Список публікацій (item_occurrences) з живої БД за фільтрами.

    Один рядок — одна публікація, не один дедуплікований content_items.
    Наявність full text/segments — через EXISTS, без N+1. Останнє
    routing-рішення для content_id — через LATERAL
    (routing_version DESC, created_at DESC, LIMIT 1).
    """
    conditions: list[str] = []
    params: list[object] = []

    if period is not Period.ALL:
        cutoff = datetime.now(timezone.utc) - PERIOD_TO_TIMEDELTA[period]
        conditions.append("COALESCE(io.published_at, io.collected_at) >= %s")
        params.append(cutoff)

    if source_group is not None:
        conditions.append("s.source_group = %s")
        params.append(source_group.value)

    if source_type is not None:
        conditions.append("s.source_type = %s")
        params.append(source_type.value)

    if source_id is not None:
        conditions.append("io.source_id = %s")
        params.append(source_id)

    if decision is RoutingDecision.PENDING:
        conditions.append("routing.decision IS NULL")
    elif decision is not None:
        conditions.append("routing.decision = %s")
        params.append(decision.value)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    query = f"""
        SELECT
            io.occurrence_id,
            io.content_id,
            COALESCE(io.published_at, io.collected_at) AS occurred_at,
            s.name AS source_name,
            s.source_type,
            s.source_group,
            COALESCE(
                NULLIF(btrim(ci.title), ''),
                left(btrim(ci.text_content), {TITLE_FALLBACK_LENGTH})
            ) AS display_title,
            left(btrim(ci.text_content), {PREVIEW_LENGTH}) AS preview_text,
            routing.decision AS routing_decision,
            routing.score AS routing_score,
            EXISTS (
                SELECT 1 FROM occurrence_content oc
                WHERE oc.occurrence_id = io.occurrence_id AND oc.status = 'success'
            ) AS has_full_text,
            EXISTS (
                SELECT 1
                FROM occurrence_content oc
                JOIN segmentation_runs sr
                    ON sr.occurrence_content_id = oc.occurrence_content_id
                WHERE oc.occurrence_id = io.occurrence_id AND sr.status = 'success'
            ) AS has_segments
        FROM item_occurrences io
        JOIN content_items ci ON ci.content_id = io.content_id
        JOIN sources s ON s.source_id = io.source_id
        LEFT JOIN LATERAL (
            SELECT decision, score
            FROM content_routing_decisions crd
            WHERE crd.content_id = io.content_id
            ORDER BY crd.routing_version DESC, crd.created_at DESC
            LIMIT 1
        ) routing ON true
        {where_clause}
        ORDER BY COALESCE(io.published_at, io.collected_at) DESC, io.occurrence_id DESC
        LIMIT {MATERIALS_LIMIT}
    """

    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()
