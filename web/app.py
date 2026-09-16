"""FastAPI-застосунок Web MVP для МІП.

GET /health     -- перевірка життєздатності сервісу.
GET /           -- огляд інформаційного простору (статистична головна).
GET /materials  -- список публікацій (item_occurrences) з живої БД.
"""

import csv
import datetime
import io
import sys
from pathlib import Path
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.db import read_connection
from web.materials import (
    Period,
    RoutingDecision,
    SourceGroup,
    SourceType,
    OptionalRoutingDecision,
    OptionalSourceGroup,
    OptionalSourceIdUUID,
    OptionalSourceType,
    fetch_active_sources,
    fetch_materials,
)
from web.overview import fetch_overview_stats
from web.contours import (
    fetch_contours,
    fetch_c1_summary,
    fetch_c1_daily_trend,
    fetch_c1_active_objects,
    fetch_c1_changes,
)
from web.objects import (
    fetch_c1_evidence,
    fetch_object_evidence,
    fetch_objects_overview,
)
from web.sources import fetch_sources_overview
from web.theses import EXPORT_PER_CONTOUR_LIMIT, fetch_theses_by_contour
from web.timefmt import (
    fmt_kyiv,
    fmt_kyiv_clock,
    fmt_kyiv_clock_zoned,
    fmt_kyiv_zoned,
    kyiv_time_label,
    utc_offset_label,
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

DB_UNAVAILABLE_MESSAGE = "База даних тимчасово недоступна."

PERIOD_LABELS = [
    (Period.HOURS_24, "24 години"),
    (Period.HOURS_72, "72 години"),
    (Period.DAYS_7, "7 днів"),
    (Period.ALL, "Увесь час"),
]

app = FastAPI(title="mip-web")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _static_version(filename: str) -> str:
    """Версія static-файлу для cache busting за поточним mtime."""
    try:
        return format((STATIC_DIR / filename).stat().st_mtime_ns, "x")
    except OSError:
        return "0"


templates.env.globals["static_version"] = _static_version

templates.env.globals.update(
    fmt_kyiv=fmt_kyiv,
    fmt_kyiv_clock=fmt_kyiv_clock,
    fmt_kyiv_zoned=fmt_kyiv_zoned,
    fmt_kyiv_clock_zoned=fmt_kyiv_clock_zoned,
    kyiv_time_label=kyiv_time_label,
    utc_offset_label=utc_offset_label,
)


@app.get("/health")
def health() -> dict[str, str]:
    """Перевірка життєздатності сервісу."""
    return {"status": "ok", "service": "mip-web"}


def _pct(count: int, denominator: int) -> int | None:
    """Ціле число відсотків або None, якщо знаменник відсутній (немає даних)."""
    return round(100 * count / denominator) if denominator else None


@app.get("/", response_class=HTMLResponse)
def root_page(request: Request) -> HTMLResponse:
    """Головна точка входу -- тематичний простір."""
    return topics_page(request)


@app.get("/overview", response_class=HTMLResponse)
def index(request: Request, period: Period = Period.HOURS_24) -> HTMLResponse:
    """Огляд інформаційного простору -- статистична головна сторінка."""
    try:
        with read_connection() as conn:
            stats = fetch_overview_stats(conn, period=period)
    except psycopg.Error as exc:
        print(f"/: db error: {exc}", file=sys.stderr)
        raise HTTPException(status_code=503, detail=DB_UNAVAILABLE_MESSAGE) from exc

    unique = stats["unique_materials_count"]
    publications = stats["publications_count"]

    claims_stat = {
        "count": stats["claims_count"],
        "pct": _pct(stats["claims_count"], unique),
        "tip": (
            "Частка унікальних матеріалів, з яких виділено хоча б одне "
            "перевірюване твердження -- як із фрагментів, так і напряму "
            "з тексту публікації (два незалежні режими вилучення, не "
            "вимагає попереднього поділу на фрагменти)."
        ),
    }

    decision_segments = [
        {
            "label": "Для аналізу",
            "count": stats["decision_analyze"],
            "pct": _pct(stats["decision_analyze"], unique),
            "css": "var(--gold)",
        },
        {
            "label": "Потребує уточнення",
            "count": stats["decision_maybe"],
            "pct": _pct(stats["decision_maybe"], unique),
            "css": "var(--teal-mid)",
        },
        {
            "label": "Не відібрано",
            "count": stats["decision_skip"],
            "pct": _pct(stats["decision_skip"], unique),
            "css": "var(--steel-tint)",
        },
        {
            "label": "Очікує рішення",
            "count": stats["decision_pending"],
            "pct": _pct(stats["decision_pending"], unique),
            "css": "var(--stripe-pattern)",
        },
    ]

    space_segments = [
        {
            "label": "Український простір",
            "count": stats["space_ua"],
            "pct": _pct(stats["space_ua"], publications),
            "css": "var(--space-ua)",
        },
        {
            "label": "Російський простір",
            "count": stats["space_ru"],
            "pct": _pct(stats["space_ru"], publications),
            "css": "var(--space-ru)",
        },
        {
            "label": "Інші джерела",
            "count": stats["space_other"],
            "pct": _pct(stats["space_other"], publications),
            "css": "var(--space-other)",
        },
        {
            "label": "Не визначено",
            "count": stats["space_unknown"],
            "pct": _pct(stats["space_unknown"], publications),
            "css": "var(--stripe-pattern)",
        },
    ]

    pending = {
        "no_decision": stats["decision_pending"],
        "no_claims": unique - stats["claims_count"],
    }

    context = {
        "active_page": "overview",
        "current_period": period,
        "periods": PERIOD_LABELS,
        "has_data": unique > 0,
        "stats": stats,
        "claims_stat": claims_stat,
        "decision_segments": decision_segments,
        "space_segments": space_segments,
        "pending": pending,
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/materials", response_class=HTMLResponse)
def materials(
    request: Request,
    period: Period = Period.HOURS_24,
    source_group: OptionalSourceGroup = None,
    source_type: OptionalSourceType = None,
    source_id: OptionalSourceIdUUID = None,
    decision: OptionalRoutingDecision = None,
) -> HTMLResponse:
    """Сторінка списку публікацій (item_occurrences) з живої БД за фільтрами."""
    try:
        with read_connection() as conn:
            rows = fetch_materials(
                conn,
                period=period,
                source_group=source_group,
                source_type=source_type,
                source_id=source_id,
                decision=decision,
            )
            sources = fetch_active_sources(conn)
    except psycopg.Error as exc:
        print(f"/materials: db error: {exc}", file=sys.stderr)
        raise HTTPException(status_code=503, detail=DB_UNAVAILABLE_MESSAGE) from exc

    filters = {
        "period": period.value,
        "source_group": source_group.value if source_group else None,
        "source_type": source_type.value if source_type else None,
        "source_id": str(source_id) if source_id else None,
        "decision": decision.value if decision else None,
    }

    return templates.TemplateResponse(
        request,
        "materials.html",
        {
            "active_page": "materials",
            "materials": rows,
            "sources": sources,
            "filters": filters,
        },
    )


CONTOUR_SHORT_NAMES = {
    "dshv_objects": "Об'єкти ДШВ",
    "world_context": "Світ",
    "national_context": "Держава",
    "enemy_media": "Противник",
}


@app.get("/contours", response_class=HTMLResponse)
def contours_page(
    request: Request,
    contour: str = "dshv_objects",
    object_id: int | None = None,
    day: datetime.date | None = None,
) -> HTMLResponse:
    """Аналітичний простір чотирьох контурів моніторингу."""
    try:
        with read_connection() as conn:
            contour_rows = fetch_contours(conn)

            contour_by_code = {
                row["code"]: row
                for row in contour_rows
            }

            if contour not in contour_by_code:
                raise HTTPException(
                    status_code=404,
                    detail="Контур моніторингу не знайдено.",
                )

            tabs = [
                {
                    **row,
                    "short_name": CONTOUR_SHORT_NAMES.get(
                        row["code"],
                        row["name"],
                    ),
                }
                for row in contour_rows
            ]

            context = {
                "active_page": "contours",
                "contours": tabs,
                "current_contour": contour_by_code[contour],
                "current_code": contour,
                "page_updated_at": datetime.datetime.now(
                    datetime.timezone.utc
                ),
            }

            if contour == "dshv_objects":
                summary = fetch_c1_summary(conn)
                trend = fetch_c1_daily_trend(
                    conn,
                    object_id=object_id,
                )
                active_objects = fetch_c1_active_objects(
                    conn,
                    day=day,
                )
                changes = fetch_c1_changes(conn)
                context["changes"] = changes
                all_objects = fetch_objects_overview(conn)

                trend_days = {
                    row["day"]
                    for row in trend
                }

                if day is not None and day not in trend_days:
                    raise HTTPException(
                        status_code=404,
                        detail="День поза межами поточного 7-денного вікна.",
                    )

                trend_max = max(
                    (int(row["materials"]) for row in trend),
                    default=0,
                ) or 1

                trend_view = [
                    {
                        **row,
                        "height_pct": round(
                            100 * int(row["materials"]) / trend_max
                        ),
                    }
                    for row in trend
                ]

                rank_max = max(
                    (
                        int(row["materials"])
                        for row in active_objects
                    ),
                    default=0,
                ) or 1

                active_view = [
                    {
                        **row,
                        "bar_pct": round(
                            100 * int(row["materials"]) / rank_max
                        ),
                    }
                    for row in active_objects
                ]

                selected_id = object_id

                selected = (
                    next(
                        (
                            row
                            for row in all_objects
                            if row["object_id"] == selected_id
                        ),
                        None,
                    )
                    if selected_id is not None
                    else None
                )

                if selected_id is not None and selected is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Об'єкт моніторингу не знайдено.",
                    )

                evidence = (
                    fetch_object_evidence(
                        conn,
                        object_id=selected["object_id"],
                        day=day,
                    )
                    if selected
                    else fetch_c1_evidence(
                        conn,
                        day=day,
                    )
                )

                quiet_objects = [
                    row
                    for row in all_objects
                    if int(row["contents_24h"]) == 0
                ]

                if selected:
                    space_values = [
                        ("UA", int(selected["ua_24h"])),
                        ("RU", int(selected["ru_24h"])),
                    ]
                    channel_values = [
                        ("Telegram", int(selected["tg_24h"])),
                        ("RSS", int(selected["rss_24h"])),
                    ]

                    space_max = max(
                        (value for _, value in space_values),
                        default=0,
                    ) or 1
                    channel_max = max(
                        (value for _, value in channel_values),
                        default=0,
                    ) or 1

                    space_breakdown = [
                        {
                            "label": label,
                            "value": value,
                            "bar_pct": round(
                                100 * value / space_max
                            ),
                        }
                        for label, value in space_values
                    ]

                    channel_breakdown = [
                        {
                            "label": label,
                            "value": value,
                            "bar_pct": round(
                                100 * value / channel_max
                            ),
                        }
                        for label, value in channel_values
                    ]
                else:
                    space_breakdown = []
                    channel_breakdown = []

                context.update(
                    {
                        "summary": summary,
                        "trend": trend_view,
                        "active_objects": active_view,
                        "all_objects": all_objects,
                        "quiet_objects": quiet_objects,
                        "selected": selected,
                        "selected_day": day,
                        "evidence": evidence,
                        "space_breakdown": space_breakdown,
                        "channel_breakdown": channel_breakdown,
                    }
                )

    except HTTPException:
        raise
    except psycopg.Error as exc:
        print(
            f"/contours: db error: {exc}",
            file=sys.stderr,
        )
        raise HTTPException(
            status_code=503,
            detail=DB_UNAVAILABLE_MESSAGE,
        ) from exc

    return templates.TemplateResponse(
        request,
        "contours.html",
        context,
    )


@app.get("/objects", response_class=HTMLResponse)
def objects_page(
    request: Request,
    object_id: int | None = None,
) -> HTMLResponse:
    """Моніторинг канонічних об'єктів C1 та evidence по вибраному об'єкту."""
    try:
        with read_connection() as conn:
            objects = fetch_objects_overview(conn)

            selected = None
            evidence = []

            if objects:
                if object_id is None:
                    selected = objects[0]
                else:
                    selected = next(
                        (
                            row
                            for row in objects
                            if row["object_id"] == object_id
                        ),
                        None,
                    )

                    if selected is None:
                        raise HTTPException(
                            status_code=404,
                            detail="Об'єкт моніторингу не знайдено.",
                        )

                evidence = fetch_object_evidence(
                    conn,
                    object_id=selected["object_id"],
                )
    except HTTPException:
        raise
    except psycopg.Error as exc:
        print(f"/objects: db error: {exc}", file=sys.stderr)
        raise HTTPException(
            status_code=503,
            detail=DB_UNAVAILABLE_MESSAGE,
        ) from exc

    return templates.TemplateResponse(
        request,
        "objects.html",
        {
            "active_page": "objects",
            "objects": objects,
            "selected": selected,
            "evidence": evidence,
        },
    )


@app.get("/sources", response_class=HTMLResponse)
def sources_page(
    request: Request,
    period: Period = Period.ALL,
    source_group: OptionalSourceGroup = None,
    source_type: OptionalSourceType = None,
    source_name: str | None = None,
) -> HTMLResponse:
    """Сторінка охоплення джерел моніторингу."""
    try:
        with read_connection() as conn:
            all_rows = fetch_sources_overview(
                conn,
                period=period,
            )
    except psycopg.Error as exc:
        print(
            f"/sources: db error: {exc}",
            file=sys.stderr,
        )
        raise HTTPException(
            status_code=503,
            detail=DB_UNAVAILABLE_MESSAGE,
        ) from exc

    type_order = {
        "rss": 0,
        "telegram": 1,
    }
    group_order = {
        "ua_space": 0,
        "ru_space": 1,
        "other": 2,
    }

    source_types = sorted(
        {
            row["source_type"]
            for row in all_rows
            if row["source_type"]
        },
        key=lambda value: (
            type_order.get(value, 99),
            value,
        ),
    )

    source_groups = sorted(
        {
            row["source_group"]
            for row in all_rows
            if row["source_group"]
        },
        key=lambda value: (
            group_order.get(value, 99),
            value,
        ),
    )

    rows_by_type_group = [
        row
        for row in all_rows
        if (
            source_type is None
            or row["source_type"] == source_type.value
        )
        and (
            source_group is None
            or row["source_group"] == source_group.value
        )
    ]

    source_names = sorted(
        {
            row["name"]
            for row in rows_by_type_group
            if row["name"]
        },
        key=str.casefold,
    )

    rows = [
        row
        for row in rows_by_type_group
        if (
            not source_name
            or row["name"] == source_name
        )
    ]

    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "active_page": "sources",
            "current_period": period,
            "periods": PERIOD_LABELS,
            "sources": rows,
            "source_types": source_types,
            "source_groups": source_groups,
            "source_names": source_names,
            "filters": {
                "source_group": (
                    source_group.value
                    if source_group
                    else None
                ),
                "source_type": (
                    source_type.value
                    if source_type
                    else None
                ),
                "source_name": source_name,
            },
        },
    )


@app.get("/theses", response_class=HTMLResponse)
def theses_page(request: Request) -> HTMLResponse:
    """Сторінка кураторських тез за контурами моніторингу."""
    try:
        with read_connection() as conn:
            contours = fetch_theses_by_contour(conn)
    except psycopg.Error as exc:
        print(f"/theses: db error: {exc}", file=sys.stderr)
        raise HTTPException(status_code=503, detail=DB_UNAVAILABLE_MESSAGE) from exc

    return templates.TemplateResponse(
        request,
        "theses.html",
        {
            "active_page": "theses",
            "contours": contours,
        },
    )


@app.get("/theses/export.csv")
def theses_export_csv() -> Response:
    """Експорт кураторської вибірки тез у CSV (для Excel)."""
    try:
        with read_connection() as conn:
            contours = fetch_theses_by_contour(conn, per_contour_limit=EXPORT_PER_CONTOUR_LIMIT)
    except psycopg.Error as exc:
        print(f"/theses/export.csv: db error: {exc}", file=sys.stderr)
        raise HTTPException(status_code=503, detail=DB_UNAVAILABLE_MESSAGE) from exc

    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Контур", "Статус", "Джерело", "Дата (UTC)", "Твердження", "Фрагмент", "Тег"])
    for contour in contours:
        for item in contour["theses"]:
            writer.writerow([
                contour["name"],
                "підтверджено" if item["assignment_status"] == "confirmed" else "кандидат",
                item["source_name"],
                item["published_at"].strftime("%Y-%m-%d %H:%M") if item["published_at"] else "",
                item["claim_text"],
                item["evidence_span"] or "",
                item["facet_code"] or "",
            ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=mip_thesis_export.csv"},
    )



@app.get("/topics/marker/{marker_id}")
def topics_marker(
    request: Request,
    marker_id: str,
    space: str = "all",
) -> dict:
    """Деталь одного тематичного маркера зі snapshot."""
    from web.topics import (
        DEFAULT_SNAPSHOT,
        load_topic_marker,
    )

    try:
        return load_topic_marker(
            getattr(
                request.app.state,
                "topics_snapshot_path",
                DEFAULT_SNAPSHOT,
            ),
            marker_id=marker_id,
            view=space,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Тематичний маркер не знайдено.",
        ) from exc


@app.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request) -> HTMLResponse:
    """Лише snapshot: генерація та підключення до БД поза HTTP-запитом."""
    from web.signals import DEFAULT_SNAPSHOT, load_signals

    view = load_signals(
        getattr(request.app.state, 'signals_snapshot_path', DEFAULT_SNAPSHOT),
        stale_seconds=getattr(request.app.state, 'signals_stale_seconds', 7200),
    )
    return templates.TemplateResponse(request, "signals.html", {"active_page": "signals", **view})



@app.get("/topics", response_class=HTMLResponse)
def topics_page(request: Request) -> HTMLResponse:
    """Тематичний простір з попередньо сформованого live snapshot."""
    from web.topics import DEFAULT_SNAPSHOT, load_topics

    view = load_topics(
        getattr(
            request.app.state,
            "topics_snapshot_path",
            DEFAULT_SNAPSHOT,
        ),
        stale_seconds=getattr(
            request.app.state,
            "topics_stale_seconds",
            7200,
        ),
    )

    return templates.TemplateResponse(
        request,
        "topics.html",
        {
            "active_page": "topics",
            **view,
        },
    )
