"""FastAPI-застосунок Web MVP для МІП.

GET /health     -- перевірка життєздатності сервісу.
GET /           -- огляд інформаційного простору (статистична головна).
GET /materials  -- список публікацій (item_occurrences) з живої БД.
"""

import sys
from pathlib import Path
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.db import read_connection
from web.materials import (
    Period,
    RoutingDecision,
    SourceGroup,
    SourceType,
    fetch_active_sources,
    fetch_materials,
)
from web.overview import fetch_overview_stats

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


@app.get("/health")
def health() -> dict[str, str]:
    """Перевірка життєздатності сервісу."""
    return {"status": "ok", "service": "mip-web"}


def _pct(count: int, denominator: int) -> int | None:
    """Ціле число відсотків або None, якщо знаменник відсутній (немає даних)."""
    return round(100 * count / denominator) if denominator else None


@app.get("/", response_class=HTMLResponse)
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

    stages = [
        {
            "label": "Текст отримано",
            "count": stats["text_received_count"],
            "pct": _pct(stats["text_received_count"], unique),
            "tip": (
                "Частка унікальних матеріалів періоду, для яких вдалося "
                "отримати повний текст статті за посиланням (окремий крок "
                "збагачення понад початковий текст із джерела)."
            ),
        },
        {
            "label": "Відібрано для аналізу",
            "count": stats["decision_analyze"],
            "pct": _pct(stats["decision_analyze"], unique),
            "tip": "Частка унікальних матеріалів періоду з останнім рішенням «Для аналізу».",
        },
        {
            "label": "Поділено на фрагменти",
            "count": stats["segmented_count"],
            "pct": _pct(stats["segmented_count"], unique),
            "tip": (
                "Частка унікальних матеріалів періоду, текст яких успішно "
                "поділено на аналітичні фрагменти."
            ),
        },
        {
            "label": "Твердження виділено",
            "count": stats["claims_count"],
            "pct": _pct(stats["claims_count"], unique),
            "tip": (
                "Частка унікальних матеріалів, з яких виділено хоча б одне "
                "перевірюване твердження -- як із фрагментів, так і напряму "
                "з тексту публікації (два незалежні режими вилучення, не "
                "вимагає попереднього поділу на фрагменти)."
            ),
        },
    ]

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
        "no_text": unique - stats["text_received_count"],
        "no_decision": stats["decision_pending"],
        "no_segments": unique - stats["segmented_count"],
        "no_claims": unique - stats["claims_count"],
    }

    context = {
        "active_page": "overview",
        "current_period": period,
        "periods": PERIOD_LABELS,
        "has_data": unique > 0,
        "stats": stats,
        "stages": stages,
        "decision_segments": decision_segments,
        "space_segments": space_segments,
        "pending": pending,
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/materials", response_class=HTMLResponse)
def materials(
    request: Request,
    period: Period = Period.HOURS_24,
    source_group: SourceGroup | None = None,
    source_type: SourceType | None = None,
    source_id: UUID | None = None,
    decision: RoutingDecision | None = None,
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
