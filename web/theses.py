"""Bounded read-запити для сторінки Тези (Web MVP).

Показує кураторську вибірку виділених тверджень за моніторинговими
контурами (ТЗ, тегова структура 4 контурів). Це чорновий аналітичний
зріз, не повний авто-дамп: якість автоматичної прив'язки твердження
до контуру ще нерівномірна (класифікатор і claims-worker ще
донавчаються), тому в кожному контурі пріоритет віддається
твердженням зі status='confirmed', а якщо підтверджених замало --
контур доповнюється candidate-твердженнями і позначається на сторінці
як чернетковий.
"""
from __future__ import annotations

CONTOUR_ORDER = ["dshv_objects", "national_context", "world_context", "enemy_media"]
PER_CONTOUR_LIMIT = 50
EXPORT_PER_CONTOUR_LIMIT = 200


def fetch_theses_by_contour(conn, per_contour_limit: int = PER_CONTOUR_LIMIT) -> list[dict]:
    """Кураторська вибірка тверджень по 4 контурах моніторингу."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH base AS (
                SELECT
                    mc.code AS contour_code,
                    mc.name AS contour_name,
                    cca.status AS assignment_status,
                    cca.facet_code,
                    s.name AS source_name,
                    io.published_at,
                    c.claim_text,
                    c.evidence_span,
                    ROW_NUMBER() OVER (
                        PARTITION BY mc.code, c.claim_text
                        ORDER BY io.published_at DESC
                    ) AS dup_rank
                FROM content_contour_assignments cca
                JOIN monitoring_contours mc ON mc.monitoring_contour_id = cca.monitoring_contour_id
                JOIN claim_extraction_runs cer
                    ON cer.content_id = cca.content_id
                   AND cer.status = 'valid'
                   AND cer.claim_count > 0
                JOIN claims c ON c.run_id = cer.run_id
                JOIN item_occurrences io ON io.content_id = cca.content_id
                JOIN sources s ON s.source_id = io.source_id
            ),
            dedup AS (
                SELECT * FROM base WHERE dup_rank = 1
            ),
            ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY contour_code
                        ORDER BY (assignment_status = 'confirmed') DESC, published_at DESC
                    ) AS pick_rank,
                    COUNT(*) FILTER (WHERE assignment_status = 'confirmed')
                        OVER (PARTITION BY contour_code) AS confirmed_count
                FROM dedup
            )
            SELECT contour_code, contour_name, assignment_status, facet_code,
                   source_name, published_at, claim_text, evidence_span,
                   confirmed_count
            FROM ranked
            WHERE pick_rank <= %s
            ORDER BY contour_code, published_at DESC
            """,
            [per_contour_limit],
        )
        rows = cur.fetchall()

    by_code: dict[str, dict] = {}
    for r in rows:
        code = r["contour_code"]
        if code not in by_code:
            by_code[code] = {
                "code": code,
                "name": r["contour_name"],
                "confirmed_count": r["confirmed_count"],
                "theses": [],
            }
        by_code[code]["theses"].append(r)

    return [by_code[c] for c in CONTOUR_ORDER if c in by_code]
