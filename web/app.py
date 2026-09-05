"""FastAPI-застосунок Web MVP для МІП.

GET /health     -- перевірка життєздатності сервісу.
GET /           -- головна сторінка через Jinja2.
GET /materials  -- список публікацій (item_occurrences) з живої БД.
"""

import sys
from pathlib import Path
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
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

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="mip-web")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/health")
def health() -> dict[str, str]:
    """Перевірка життєздатності сервісу."""
    return {"status": "ok", "service": "mip-web"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Головна сторінка Web MVP."""
    return templates.TemplateResponse(request, "index.html", {})


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
        raise HTTPException(
            status_code=503, detail="Базу даних тимчасово недоступна."
        ) from exc

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
        {"materials": rows, "sources": sources, "filters": filters},
    )
