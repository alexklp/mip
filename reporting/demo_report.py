#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import json
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"

SNAPSHOT_FILE = OUTPUT_DIR / "demo_snapshot.json"
TOPICS_FILE = OUTPUT_DIR / "demo_topics.json"
EXAMPLES_FILE = OUTPUT_DIR / "demo_examples.json"
OUTPUT_FILE = OUTPUT_DIR / "mip_demo_report.html"


EVENT_DISPLAY = {
    "112734e4-fb0b-4657-b8fa-625e0f448603":
        "Повідомлення про 10 загиблих унаслідок удару в Харківській області",
    "c6883957-8014-4a79-9971-b16175bacfa4":
        "Повідомлення про роботу екстрених служб на місцях падіння уламків у Москві",
    "9c9a59a8-8562-45f4-9d4c-b593443db0d5":
        "Повідомлення про візит Володимира Путіна на спірні Курильські острови",
}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def image_data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{raw}"


def theme_rows(group: str, topics: dict, limit: int = 10) -> str:
    rows = topics["groups"][group]["top_unigrams"][:limit]
    if not rows:
        return ""

    max_pct = max(row["doc_pct"] for row in rows) or 1.0

    parts = []

    for row in rows:
        width = 100.0 * row["doc_pct"] / max_pct

        parts.append(
            f"""
            <button class="theme-row clickable-term"
                    data-group="{esc(group)}"
                    data-kind="top_unigrams"
                    data-term="{esc(row['term'])}">
                <span class="theme-name">{esc(row['term'])}</span>
                <span class="theme-bar-wrap">
                    <span class="theme-bar" style="width:{width:.1f}%"></span>
                </span>
                <span class="theme-value">
                    {row['documents']} · {row['doc_pct']:.1f}%
                </span>
            </button>
            """
        )

    return "\n".join(parts)


def shift_rows(group: str, topics: dict, direction: str, limit: int = 8) -> str:
    rows = topics["groups"][group]["bigram_shift"][direction][:limit]
    parts = []

    for row in rows:
        clickable = direction == "emerging"
        cls = "shift-row clickable-term" if clickable else "shift-row"

        attrs = ""
        if clickable:
            attrs = (
                f'data-group="{esc(group)}" '
                f'data-kind="emerging" '
                f'data-term="{esc(row["term"])}"'
            )

        delta_cls = "up" if row["delta_pct"] >= 0 else "down"

        parts.append(
            f"""
            <div class="{cls}" {attrs}>
                <div class="shift-term">{esc(row['term'])}</div>
                <div class="shift-values">
                    <span>{row['old_pct']:.2f}%</span>
                    <span class="arrow">→</span>
                    <span>{row['new_pct']:.2f}%</span>
                    <span class="{delta_cls}">
                        {row['delta_pct']:+.2f} п.п.
                    </span>
                </div>
            </div>
            """
        )

    return "\n".join(parts)


def build_event_cards(snapshot: dict) -> str:
    traces = defaultdict(list)

    for row in snapshot["event_trace_rows"]:
        traces[row["canonical_event_id"]].append(row)

    parts = []

    for event in snapshot["canonical_events"]:
        eid = event["canonical_event_id"]
        rows = traces.get(eid, [])

        source_names = sorted({
            row["source_name"]
            for row in rows
            if row.get("source_name")
        })

        headline = EVENT_DISPLAY.get(
            eid,
            event["canonical_event_summary"],
        )

        claims = len({
            row["claim_id"]
            for row in rows
        })

        sources = len(source_names)

        parts.append(
            f"""
            <article class="event-card">
                <div class="event-kicker">CANONICAL EVENT</div>
                <h3>{esc(headline)}</h3>

                <div class="event-stats">
                    <span>{claims} claims</span>
                    <span>{sources} джерел</span>
                </div>

                <div class="source-list">
                    {esc(" · ".join(source_names))}
                </div>

                <details>
                    <summary>Технічний summary event layer</summary>
                    <p>{esc(event['canonical_event_summary'])}</p>
                </details>
            </article>
            """
        )

    return "\n".join(parts)


def build_trace(snapshot: dict) -> str:
    target = "112734e4-fb0b-4657-b8fa-625e0f448603"

    rows = [
        row
        for row in snapshot["event_trace_rows"]
        if row["canonical_event_id"] == target
    ]

    cards = []

    for row in rows:
        cards.append(
            f"""
            <div class="trace-card">
                <div class="trace-source">{esc(row['source_name'])}</div>
                <div class="trace-claim">{esc(row['claim_text'])}</div>

                <div class="evidence-label">Evidence span</div>
                <blockquote>{esc(row['evidence_span'])}</blockquote>

                <div class="trace-meta">
                    epistemic_status: {esc(row['epistemic_status'])}
                </div>
            </div>
            """
        )

    return "\n".join(cards)


def build_contours(snapshot: dict) -> str:
    parts = []

    for row in snapshot["contours"]:
        parts.append(
            f"""
            <div class="contour-card">
                <div class="contour-code">C{row['monitoring_contour_id']}</div>
                <div>
                    <h3>{esc(row['name'])}</h3>
                    <div class="contour-stats">
                        {row['documents']} документів ·
                        {row['assignments']} прив’язок ·
                        {esc(row['status']).upper()}
                    </div>
                </div>
            </div>
            """
        )

    parts.append(
        """
        <div class="contour-card muted">
            <div class="contour-code">C4</div>
            <div>
                <h3>Моніторинг ворожих медіа</h3>
                <div class="contour-stats">
                    Семантичний monitoring/calibration.
                    CONFIRMED assignment у цьому звіті не заявляється.
                </div>
            </div>
        </div>
        """
    )

    return "\n".join(parts)


def main() -> int:
    snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    topics = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
    examples = json.loads(EXAMPLES_FILE.read_text(encoding="utf-8"))

    kpi = snapshot["kpi"]

    current_docs = {
        row["source_group"]: row["documents"]
        for row in snapshot["observation_windows"]["current"]["documents"]
    }

    previous_docs = {
        row["source_group"]: row["documents"]
        for row in snapshot["observation_windows"]["previous"]["documents"]
    }

    source_type_counts = defaultdict(int)

    for row in snapshot["sources"]:
        source_type_counts[row["source_type"]] += row["sources"]

    confirmed_assignments = sum(
        row["assignments"]
        for row in snapshot["contours"]
        if row["status"] == "confirmed"
    )

    ua_cloud = image_data_uri(
        OUTPUT_DIR / "wordcloud_ua_space_demo.png"
    )
    ru_cloud = image_data_uri(
        OUTPUT_DIR / "wordcloud_ru_space_demo.png"
    )

    cloud_html = ""

    if ua_cloud and ru_cloud:
        cloud_html = f"""
        <div class="cloud-grid">
            <figure class="panel">
                <img src="{ua_cloud}" alt="UA-space word cloud">
                <figcaption>UA-space · demo relevance subset · 26.08</figcaption>
            </figure>

            <figure class="panel">
                <img src="{ru_cloud}" alt="RU-space word cloud">
                <figcaption>RU-space · demo relevance subset · 26.08</figcaption>
            </figure>
        </div>
        """

    examples_json = json.dumps(
        examples,
        ensure_ascii=False,
    ).replace("</", "<\\/")

    report = f"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>МІП · Демонстраційний аналітичний звіт</title>

<style>
:root {{
    --bg: #f4f6f8;
    --panel: #ffffff;
    --text: #17202a;
    --muted: #68737d;
    --line: #dfe5ea;
    --accent: #2459d3;
    --accent-soft: #e9efff;
    --good: #18864b;
    --bad: #b83a3a;
    --sidebar: #101722;
}}

* {{
    box-sizing: border-box;
}}

html {{
    scroll-behavior: smooth;
}}

body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family:
        Inter,
        "Segoe UI",
        Roboto,
        Arial,
        sans-serif;
}}

.sidebar {{
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    width: 250px;
    background: var(--sidebar);
    color: white;
    padding: 28px 18px;
    overflow-y: auto;
}}

.brand {{
    padding: 0 10px 22px;
    border-bottom: 1px solid rgba(255,255,255,.12);
    margin-bottom: 18px;
}}

.brand-title {{
    font-size: 30px;
    font-weight: 800;
    letter-spacing: .04em;
}}

.brand-sub {{
    color: #aab6c6;
    font-size: 12px;
    margin-top: 4px;
    line-height: 1.4;
}}

.nav a {{
    display: block;
    color: #c9d2df;
    text-decoration: none;
    padding: 10px 12px;
    border-radius: 8px;
    margin: 3px 0;
    font-size: 14px;
}}

.nav a:hover {{
    background: rgba(255,255,255,.08);
    color: white;
}}

.main {{
    margin-left: 250px;
    padding: 42px 48px 80px;
    max-width: 1550px;
}}

.hero {{
    background:
        linear-gradient(135deg, #ffffff 0%, #eef3ff 100%);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 34px 38px;
}}

.eyebrow {{
    color: var(--accent);
    font-weight: 700;
    font-size: 12px;
    letter-spacing: .08em;
    text-transform: uppercase;
}}

h1 {{
    margin: 8px 0 10px;
    font-size: 38px;
    line-height: 1.15;
}}

h2 {{
    font-size: 25px;
    margin: 0 0 8px;
}}

h3 {{
    margin: 0;
}}

.subtitle {{
    max-width: 850px;
    color: var(--muted);
    line-height: 1.55;
}}

.section {{
    padding-top: 58px;
}}

.section-head {{
    margin-bottom: 22px;
}}

.section-note {{
    color: var(--muted);
    max-width: 960px;
    line-height: 1.55;
}}

.kpi-grid {{
    display: grid;
    grid-template-columns: repeat(6, minmax(130px, 1fr));
    gap: 14px;
    margin-top: 28px;
}}

.kpi {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 18px;
}}

.kpi-value {{
    font-size: 27px;
    font-weight: 800;
}}

.kpi-label {{
    color: var(--muted);
    font-size: 12px;
    margin-top: 5px;
}}

.grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
}}

.panel {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 20px;
}}

.big-number {{
    font-size: 38px;
    font-weight: 800;
}}

.caption {{
    color: var(--muted);
    font-size: 13px;
}}

.coverage-row {{
    display: flex;
    justify-content: space-between;
    padding: 12px 0;
    border-bottom: 1px solid var(--line);
}}

.coverage-row:last-child {{
    border-bottom: 0;
}}

.theme-row {{
    width: 100%;
    border: 0;
    background: transparent;
    display: grid;
    grid-template-columns: 145px 1fr 100px;
    align-items: center;
    gap: 12px;
    padding: 8px 0;
    cursor: pointer;
    text-align: left;
    color: var(--text);
}}

.theme-row:hover .theme-name {{
    color: var(--accent);
}}

.theme-name {{
    font-weight: 650;
}}

.theme-bar-wrap {{
    display: block;
    height: 9px;
    background: #edf0f3;
    border-radius: 20px;
    overflow: hidden;
}}

.theme-bar {{
    display: block;
    height: 100%;
    background: var(--accent);
    border-radius: 20px;
}}

.theme-value {{
    color: var(--muted);
    text-align: right;
    font-size: 12px;
}}

.cloud-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
    margin-bottom: 18px;
}}

.cloud-grid img {{
    width: 100%;
    border-radius: 8px;
}}

.cloud-grid figure {{
    margin: 0;
}}

.cloud-grid figcaption {{
    margin-top: 10px;
    color: var(--muted);
    font-size: 12px;
}}

.shift-row {{
    border-bottom: 1px solid var(--line);
    padding: 11px 0;
}}

.shift-row:last-child {{
    border-bottom: 0;
}}

.shift-row.clickable-term {{
    cursor: pointer;
}}

.shift-row.clickable-term:hover .shift-term {{
    color: var(--accent);
}}

.shift-term {{
    font-weight: 650;
}}

.shift-values {{
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 4px;
    color: var(--muted);
    font-size: 12px;
}}

.up {{
    color: var(--good);
    font-weight: 700;
}}

.down {{
    color: var(--bad);
    font-weight: 700;
}}

.arrow {{
    color: #9aa3aa;
}}

.example-panel {{
    margin-top: 20px;
    border: 1px solid var(--line);
    background: var(--panel);
    border-radius: 14px;
    padding: 22px;
}}

.example-title {{
    font-size: 18px;
    font-weight: 750;
}}

.example-meta {{
    margin-top: 5px;
    color: var(--muted);
    font-size: 13px;
}}

.message-card {{
    margin-top: 14px;
    border-left: 3px solid var(--accent);
    padding: 10px 14px;
    background: #f8fafe;
    border-radius: 0 8px 8px 0;
}}

.message-source {{
    font-size: 12px;
    font-weight: 750;
    margin-bottom: 5px;
}}

.message-text {{
    font-size: 13px;
    line-height: 1.5;
}}

.message-time {{
    margin-top: 6px;
    color: var(--muted);
    font-size: 11px;
}}

.events-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
}}

.event-card {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 20px;
}}

.event-kicker {{
    font-size: 10px;
    font-weight: 800;
    color: var(--accent);
    letter-spacing: .08em;
    margin-bottom: 8px;
}}

.event-card h3 {{
    line-height: 1.4;
    font-size: 17px;
}}

.event-stats {{
    display: flex;
    gap: 8px;
    margin-top: 16px;
}}

.event-stats span {{
    background: var(--accent-soft);
    color: var(--accent);
    border-radius: 20px;
    padding: 5px 9px;
    font-size: 11px;
    font-weight: 700;
}}

.source-list {{
    color: var(--muted);
    margin-top: 14px;
    font-size: 12px;
    line-height: 1.5;
}}

details {{
    margin-top: 16px;
    color: var(--muted);
    font-size: 12px;
}}

details summary {{
    cursor: pointer;
}}

.trace-grid {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 14px;
}}

.trace-card {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 18px;
}}

.trace-source {{
    color: var(--accent);
    font-weight: 800;
    font-size: 12px;
}}

.trace-claim {{
    font-size: 17px;
    font-weight: 700;
    margin-top: 8px;
}}

.evidence-label {{
    margin-top: 15px;
    text-transform: uppercase;
    font-size: 9px;
    letter-spacing: .08em;
    color: var(--muted);
}}

blockquote {{
    margin: 6px 0 0;
    padding: 10px 12px;
    background: #f5f7fa;
    border-left: 3px solid #9aa8b5;
    font-size: 13px;
}}

.trace-meta {{
    margin-top: 10px;
    color: var(--muted);
    font-size: 10px;
}}

.contours {{
    display: grid;
    gap: 12px;
}}

.contour-card {{
    display: grid;
    grid-template-columns: 54px 1fr;
    gap: 14px;
    align-items: center;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 16px;
}}

.contour-card.muted {{
    opacity: .72;
}}

.contour-code {{
    height: 44px;
    width: 44px;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--accent-soft);
    color: var(--accent);
    font-weight: 850;
}}

.contour-stats {{
    color: var(--muted);
    margin-top: 5px;
    font-size: 12px;
}}

.flow {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
}}

.flow-box {{
    background: var(--panel);
    border: 1px solid var(--line);
    padding: 11px 14px;
    border-radius: 10px;
    font-weight: 650;
    font-size: 13px;
}}

.flow-arrow {{
    color: #929ba4;
}}

.notice {{
    background: #fff9e9;
    border: 1px solid #eadcae;
    border-radius: 12px;
    padding: 17px 20px;
    line-height: 1.55;
    font-size: 13px;
}}

.limit-list {{
    line-height: 1.8;
    color: var(--muted);
}}

.footer {{
    margin-top: 60px;
    color: var(--muted);
    font-size: 11px;
    border-top: 1px solid var(--line);
    padding-top: 20px;
}}

@media (max-width: 1100px) {{
    .sidebar {{
        position: static;
        width: 100%;
    }}

    .main {{
        margin-left: 0;
        padding: 24px;
    }}

    .kpi-grid {{
        grid-template-columns: repeat(3, 1fr);
    }}

    .events-grid {{
        grid-template-columns: 1fr;
    }}

    .grid-2,
    .cloud-grid,
    .trace-grid {{
        grid-template-columns: 1fr;
    }}
}}
</style>
</head>

<body>

<aside class="sidebar">
    <div class="brand">
        <div class="brand-title">МІП</div>
        <div class="brand-sub">
            Моніторинг інформаційного простору<br>
            Technical Pilot
        </div>
    </div>

    <nav class="nav">
        <a href="#overview">Огляд</a>
        <a href="#coverage">Охоплення</a>
        <a href="#themes">Тематичний профіль</a>
        <a href="#shift">Зміни між зрізами</a>
        <a href="#events">Інформаційні події</a>
        <a href="#contours">Стратегічні контури</a>
        <a href="#trace">Трасованість</a>
        <a href="#technical">Технічний контур</a>
        <a href="#limits">Межі пілота</a>
    </nav>
</aside>

<main class="main">

<section id="overview" class="hero">
    <div class="eyebrow">Technical Pilot · Demo</div>

    <h1>Демонстраційний аналітичний звіт МІП</h1>

    <p class="subtitle">
        Інтерактивний локальний зріз результатів моніторингу.
        Дані отримані з фактичного контуру МІП та підготовлені
        для демонстрації аналітичних і трасувальних можливостей.
    </p>

    <div class="kpi-grid">
        <div class="kpi">
            <div class="kpi-value">{kpi['active_sources']}</div>
            <div class="kpi-label">активних джерел</div>
        </div>

        <div class="kpi">
            <div class="kpi-value">{kpi['content_items']:,}</div>
            <div class="kpi-label">canonical materials</div>
        </div>

        <div class="kpi">
            <div class="kpi-value">{kpi['embedded_content']:,}</div>
            <div class="kpi-label">embedded materials</div>
        </div>

        <div class="kpi">
            <div class="kpi-value">{kpi['persisted_claims']:,}</div>
            <div class="kpi-label">структурованих claims</div>
        </div>

        <div class="kpi">
            <div class="kpi-value">{kpi['canonical_events']}</div>
            <div class="kpi-label">canonical events</div>
        </div>

        <div class="kpi">
            <div class="kpi-value">{confirmed_assignments}</div>
            <div class="kpi-label">confirmed contour assignments</div>
        </div>
    </div>
</section>


<section id="coverage" class="section">
    <div class="section-head">
        <h2>Охоплення інформаційного простору</h2>

        <p class="section-note">
            Source group описує походження матеріалу в межах
            моніторингового реєстру, а не мову, достовірність,
            країну походження чи напрям поширення інформації.
        </p>
    </div>

    <div class="grid-2">
        <div class="panel">
            <div class="eyebrow">CURRENT OBSERVED SLICE</div>
            <div class="big-number">
                {current_docs.get('ua_space', 0) + current_docs.get('ru_space', 0):,}
            </div>
            <div class="caption">
                матеріалів · 26.08.2026 · 07:00–08:00 UTC
            </div>

            <div class="coverage-row">
                <span>UA-space</span>
                <strong>{current_docs.get('ua_space', 0)}</strong>
            </div>

            <div class="coverage-row">
                <span>RU-space</span>
                <strong>{current_docs.get('ru_space', 0)}</strong>
            </div>
        </div>

        <div class="panel">
            <div class="eyebrow">SOURCE REGISTRY</div>
            <div class="big-number">{kpi['active_sources']}</div>
            <div class="caption">активних джерел</div>

            <div class="coverage-row">
                <span>Telegram</span>
                <strong>{source_type_counts.get('telegram', 0)}</strong>
            </div>

            <div class="coverage-row">
                <span>RSS</span>
                <strong>{source_type_counts.get('rss', 0)}</strong>
            </div>
        </div>
    </div>

    <div class="notice" style="margin-top:18px">
        МІП аналізує <strong>спостережуване інформаційне покриття</strong>.
        Порядок виявлення системою не є доказом першоджерела
        або напрямку поширення інформації.
    </div>
</section>


<section id="themes" class="section">
    <div class="section-head">
        <h2>Тематичний профіль</h2>
        <p class="section-note">
            Частка показує, у якій частині відібраної high-precision
            демонстраційної вибірки зустрічається нормалізований термін.
            Натисніть на термін, щоб переглянути репрезентативні матеріали.
        </p>
    </div>

    {cloud_html}

    <div class="grid-2">
        <div class="panel">
            <div class="eyebrow">UA-SPACE</div>
            {theme_rows('ua_space', topics)}
        </div>

        <div class="panel">
            <div class="eyebrow">RU-SPACE</div>
            {theme_rows('ru_space', topics)}
        </div>
    </div>

    <div id="theme-example-panel" class="example-panel">
        <div class="example-title">Приклади матеріалів</div>
        <div class="example-meta">
            Оберіть термін у тематичному профілі або emerging-блоці.
        </div>
        <div id="example-cards"></div>
    </div>
</section>


<section id="shift" class="section">
    <div class="section-head">
        <h2>Зміни між спостережуваними зрізами</h2>

        <p class="section-note">
            Порівняння тематичного профілю між двома дискретними
            зрізами МІП: 22.08.2026 та 26.08.2026.
            Це не безперервний чотириденний тренд.
        </p>
    </div>

    <div class="grid-2">
        <div class="panel">
            <div class="eyebrow">UA-SPACE · EMERGING</div>
            {shift_rows('ua_space', topics, 'emerging')}

            <div class="eyebrow" style="margin-top:24px">
                UA-SPACE · DECLINING
            </div>
            {shift_rows('ua_space', topics, 'declining')}
        </div>

        <div class="panel">
            <div class="eyebrow">RU-SPACE · EMERGING</div>
            {shift_rows('ru_space', topics, 'emerging')}

            <div class="eyebrow" style="margin-top:24px">
                RU-SPACE · DECLINING
            </div>
            {shift_rows('ru_space', topics, 'declining')}
        </div>
    </div>

    <div class="notice" style="margin-top:18px">
        Один із найбільш виразних синхронних сигналів у новому зрізі:
        тема <strong>ЦРУ / Джон Реткліфф</strong> різко з’являється
        як в UA-space, так і в RU-space.
    </div>
</section>


<section id="events" class="section">
    <div class="section-head">
        <h2>Інформаційні події</h2>

        <p class="section-note">
            Canonical event групує твердження, які система віднесла
            до одного інформаційного факту або події.
            Це не встановлення першоджерела і не автоматична оцінка істинності.
        </p>
    </div>

    <div class="events-grid">
        {build_event_cards(snapshot)}
    </div>
</section>


<section id="contours" class="section">
    <div class="section-head">
        <h2>Стратегічні контури</h2>
        <p class="section-note">
            Показані лише фактично persisted результати класифікації.
        </p>
    </div>

    <div class="contours">
        {build_contours(snapshot)}
    </div>
</section>


<section id="trace" class="section">
    <div class="section-head">
        <h2>Трасованість до джерела</h2>

        <p class="section-note">
            Приклад: один інформаційний факт представлений claims
            з чотирьох окремих матеріалів і може бути розгорнутий
            до конкретного evidence span.
        </p>
    </div>

    <div class="notice" style="margin-bottom:18px">
        <strong>Evidence-backed event statement:</strong><br>
        У чотирьох матеріалах зафіксовано твердження
        про 10 загиблих унаслідок удару.
    </div>

    <div class="trace-grid">
        {build_trace(snapshot)}
    </div>
</section>


<section id="technical" class="section">
    <div class="section-head">
        <h2>Технічний контур</h2>
        <p class="section-note">
            Наскрізний vertical slice, фактично перевірений у technical pilot.
        </p>
    </div>

    <div class="flow">
        <div class="flow-box">Джерела</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Збір</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Нормалізація</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Dedup</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Embeddings</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Routing</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Mamay</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Claims</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Events</div>
        <div class="flow-arrow">→</div>
        <div class="flow-box">Reporting</div>
    </div>
</section>


<section id="limits" class="section">
    <div class="section-head">
        <h2>Межі technical pilot</h2>
    </div>

    <div class="panel">
        <div class="limit-list">
            • Моніторинг виконується на обмеженому реєстрі відкритих джерел.<br>
            • Поточні дані є дискретними спостережуваними зрізами, а не доказом безперервного realtime coverage.<br>
            • first_seen_at є часом спостереження МІП, а не часом виникнення інформації.<br>
            • Система не реконструює першоджерело або напрям поширення повідомлення.<br>
            • Sentiment, virality, botnet detection та anomaly detection у цьому звіті не заявляються як готові можливості.<br>
            • Виходи ШІ є інструментом підтримки аналітика, а не автономним рішенням.
        </div>
    </div>
</section>


<div class="footer">
    Демонстраційний інтерактивний зріз.
    Інтерфейс містить агреговані результати та обмежену
    вибірку матеріалів для ілюстрації можливостей МІП.
    Повний масив даних до HTML не включено.
    Baseline: {esc(snapshot['meta']['git_baseline'])}.
</div>

</main>


<script>
const EXAMPLES = {examples_json};

function escapeHtml(value) {{
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}}

function showExamples(group, kind, term) {{
    const block = EXAMPLES?.groups?.[group]?.[kind]?.[term];
    if (!block) return;

    const panel = document.getElementById("theme-example-panel");
    const cards = document.getElementById("example-cards");

    let meta = "";

    if (kind === "top_unigrams") {{
        meta =
            `${{block.documents}} документів · ` +
            `${{Number(block.doc_pct).toFixed(1)}}% зрізу`;
    }} else {{
        meta =
            `${{Number(block.old_pct).toFixed(2)}}% → ` +
            `${{Number(block.new_pct).toFixed(2)}}% · ` +
            `${{Number(block.delta_pct) >= 0 ? "+" : ""}}` +
            `${{Number(block.delta_pct).toFixed(2)}} п.п.`;
    }}

    panel.querySelector(".example-title").textContent = term;
    panel.querySelector(".example-meta").textContent = meta;

    cards.innerHTML = block.examples.map(row => {{
        const sources = (row.source_names || []).join(" · ");
        const time = row.first_seen_at || "";

        return `
            <article class="message-card">
                <div class="message-source">
                    ${{escapeHtml(sources || "Невідоме джерело")}}
                </div>
                <div class="message-text">
                    ${{escapeHtml(row.text_preview || "")}}
                </div>
                <div class="message-time">
                    first_seen: ${{escapeHtml(time)}}
                </div>
            </article>
        `;
    }}).join("");

    panel.scrollIntoView({{
        behavior: "smooth",
        block: "center"
    }});
}}

document.querySelectorAll(".clickable-term").forEach(el => {{
    el.addEventListener("click", () => {{
        showExamples(
            el.dataset.group,
            el.dataset.kind,
            el.dataset.term
        );
    }});
}});
</script>

</body>
</html>
"""

    OUTPUT_FILE.write_text(report, encoding="utf-8")

    print(f"saved={OUTPUT_FILE}")
    print(f"bytes={OUTPUT_FILE.stat().st_size}")
    print(f"cloud_ua_embedded={bool(ua_cloud)}")
    print(f"cloud_ru_embedded={bool(ru_cloud)}")
    print("interactive_examples=yes")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
