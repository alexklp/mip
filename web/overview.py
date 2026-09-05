"""Bounded read-запити для сторінки Огляд (Web MVP).

Показники обчислюються за унікальними матеріалами (content_items), а не за
публікаціями (item_occurrences), крім двох явних винятків:

- "Публікації" та розподіл за інформаційним простором рахуються за
  публікаціями: один content_id може мати входження одразу з кількох
  джерел/просторів у межах періоду, тому рахувати розподіл простору на
  рівні унікального матеріалу було б неоднозначно (якому простору віддати
  перевагу?). База розрахунку явно позначається в UI (tooltip).
- Рішення щодо аналізу (content_routing_decisions) прив'язане до
  content_id, тому природна база для нього -- унікальні матеріали.

Останнє рішення для content_id обирається так само детерміновано, як і в
materials.py (routing_version DESC, created_at DESC, LIMIT 1) через LATERAL
join -- жодних припущень щодо унікальності по-іншому.

"Твердження виділено" рахується через claim_extraction_runs.content_id
(status='valid' AND claim_count>0). content_id на цій таблиці зберігається
як parent provenance навіть для segment-scoped runs (041_add_segment_claim
_scope.sql), тому один EXISTS покриває і whole-content, і segment-режим
без додаткових джойнів.
"""

from __future__ import annotations

from datetime import datetime, timezone

from web.materials import PERIOD_TO_TIMEDELTA, Period


def fetch_overview_stats(conn, *, period: Period = Period.HOURS_24) -> dict:
    """Зведена статистика для сторінки Огляд за період.

    Повертає скалярні показники без рядкових даних: кількість публікацій,
    унікальних матеріалів, джерел з надходженнями, активних джерел, час
    останнього надходження, стан обробки (текст/аналіз/фрагменти/
    твердження) і два розподіли (рішення щодо аналізу; інформаційний
    простір).
    """
    period_filter = ""
    params: list[object] = []
    if period is not Period.ALL:
        cutoff = datetime.now(timezone.utc) - PERIOD_TO_TIMEDELTA[period]
        period_filter = "WHERE COALESCE(io.published_at, io.collected_at) >= %s"
        params.append(cutoff)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*) AS publications_count,
                COUNT(DISTINCT io.content_id) AS unique_materials_count,
                COUNT(DISTINCT io.source_id) AS sources_with_arrivals,
                MAX(COALESCE(io.published_at, io.collected_at)) AS latest_arrival
            FROM item_occurrences io
            {period_filter}
            """,
            params,
        )
        core = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*) AS active_sources_count FROM sources WHERE is_active = true"
        )
        active = cur.fetchone()

        cur.execute(
            f"""
            SELECT
                COUNT(DISTINCT io.content_id) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM occurrence_content oc
                        WHERE oc.occurrence_id = io.occurrence_id
                          AND oc.status = 'success'
                    )
                ) AS text_received_count,
                COUNT(DISTINCT io.content_id) FILTER (
                    WHERE EXISTS (
                        SELECT 1
                        FROM occurrence_content oc
                        JOIN segmentation_runs sr
                            ON sr.occurrence_content_id = oc.occurrence_content_id
                        WHERE oc.occurrence_id = io.occurrence_id
                          AND sr.status = 'success'
                    )
                ) AS segmented_count,
                COUNT(DISTINCT io.content_id) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM claim_extraction_runs cer
                        WHERE cer.content_id = io.content_id
                          AND cer.status = 'valid'
                          AND cer.claim_count > 0
                    )
                ) AS claims_count,
                COUNT(DISTINCT io.content_id) FILTER (
                    WHERE COALESCE(routing.decision, 'pending') = 'analyze'
                ) AS decision_analyze,
                COUNT(DISTINCT io.content_id) FILTER (
                    WHERE COALESCE(routing.decision, 'pending') = 'maybe'
                ) AS decision_maybe,
                COUNT(DISTINCT io.content_id) FILTER (
                    WHERE COALESCE(routing.decision, 'pending') = 'skip'
                ) AS decision_skip,
                COUNT(DISTINCT io.content_id) FILTER (
                    WHERE COALESCE(routing.decision, 'pending') = 'pending'
                ) AS decision_pending
            FROM item_occurrences io
            LEFT JOIN LATERAL (
                SELECT decision
                FROM content_routing_decisions crd
                WHERE crd.content_id = io.content_id
                ORDER BY crd.routing_version DESC, crd.created_at DESC
                LIMIT 1
            ) routing ON true
            {period_filter}
            """,
            params,
        )
        funnel = cur.fetchone()

        cur.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE s.source_group = 'ua_space') AS space_ua,
                COUNT(*) FILTER (WHERE s.source_group = 'ru_space') AS space_ru,
                COUNT(*) FILTER (WHERE s.source_group = 'other') AS space_other,
                COUNT(*) FILTER (WHERE s.source_group = 'unknown') AS space_unknown
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            {period_filter}
            """,
            params,
        )
        space = cur.fetchone()

    return {
        "publications_count": core["publications_count"],
        "unique_materials_count": core["unique_materials_count"],
        "sources_with_arrivals": core["sources_with_arrivals"],
        "active_sources_count": active["active_sources_count"],
        "latest_arrival": core["latest_arrival"],
        "text_received_count": funnel["text_received_count"],
        "segmented_count": funnel["segmented_count"],
        "claims_count": funnel["claims_count"],
        "decision_analyze": funnel["decision_analyze"],
        "decision_maybe": funnel["decision_maybe"],
        "decision_skip": funnel["decision_skip"],
        "decision_pending": funnel["decision_pending"],
        "space_ua": space["space_ua"],
        "space_ru": space["space_ru"],
        "space_other": space["space_other"],
        "space_unknown": space["space_unknown"],
    }
