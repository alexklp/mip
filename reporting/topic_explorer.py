#!/usr/bin/env python3
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from wordcloud import WordCloud


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"

DATA_FILE = OUTPUT_DIR / "topic_explorer_data.json"
OUTPUT_FILE = OUTPUT_DIR / "topic_explorer.html"

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

WIDTH = 1200
HEIGHT = 680


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def window_label(block: dict) -> str:
    start = datetime.fromisoformat(block["start"])
    end = datetime.fromisoformat(block["end"])

    if start.date() == end.date():
        return (
            f"{start:%d.%m.%Y} "
            f"{start:%H:%M}–{end:%H:%M} UTC"
        )

    return (
        f"{start:%d.%m.%Y %H:%M} – "
        f"{end:%d.%m.%Y %H:%M} UTC"
    )


def rows_for_stage(
    group: dict,
    mode: str,
    unit: str,
) -> list[dict]:
    if mode == "themes":
        return group["themes"][unit]

    changes = group["changes"][unit]

    return (
        changes["growing"]
        + changes["declining"]
    )


def svg_stage(
    group_name: str,
    group: dict,
    mode: str,
    unit: str,
) -> str:
    rows = rows_for_stage(
        group,
        mode,
        unit,
    )

    if mode == "themes":
        frequencies = {
            row["term"]: max(float(row["documents"]), 0.1)
            for row in rows
        }

        max_font_size = 110 if unit == "words" else 72
        min_font_size = 24 if unit == "words" else 19
        relative_scaling = 0.40

    else:
        frequencies = {
            row["term"]: max(abs(float(row["delta_pct"])), 0.15)
            for row in rows
        }

        max_font_size = 66 if unit == "words" else 58
        min_font_size = 21 if unit == "words" else 17
        relative_scaling = 0.28

    lookup = {
        row["term"]: row
        for row in rows
    }

    cloud = WordCloud(
        width=WIDTH,
        height=HEIGHT,
        background_color="white",
        font_path=FONT_PATH,
        max_words=len(rows),
        collocations=False,
        prefer_horizontal=1.0,
        random_state=42,
        margin=18,
        max_font_size=max_font_size,
        min_font_size=min_font_size,
        relative_scaling=relative_scaling,
    ).generate_from_frequencies(frequencies)

    words = []

    for rank, item in enumerate(cloud.layout_):
        (term, _freq), font_size, position, _orientation, _color = item

        row = lookup[term]
        y, x = map(int, position)

        if mode == "themes":
            cls = f"theme-word tone-{rank % 4}"
            tooltip = (
                f"{row['label']} · "
                f"{row['documents']} документів · "
                f"{row['doc_pct']:.1f}%"
            )
            marker = ""

        else:
            delta = float(row["delta_pct"])
            cls = "change-up" if delta > 0 else "change-down"
            marker = "↑" if delta > 0 else "↓"

            tooltip = (
                f"{row['label']} · "
                f"{row['old_pct']:.1f}% → "
                f"{row['new_pct']:.1f}% · "
                f"{delta:+.1f} п.п."
            )

        marker_html = ""

        if marker:
            marker_html = (
                f'<tspan class="change-marker" '
                f'dx="5" baseline-shift="super" '
                f'font-size="{max(11, int(font_size * 0.28))}">'
                f'{marker}</tspan>'
            )

        words.append(
            f"""
            <text
                class="cloud-word {cls}"
                data-group="{esc(group_name)}"
                data-mode="{esc(mode)}"
                data-unit="{esc(unit)}"
                data-term="{esc(term)}"
                x="{x}"
                y="{y}"
                font-size="{int(font_size)}"
                dominant-baseline="hanging"
            >
                <title>{esc(tooltip)}</title>
                {esc(row["label"])}{marker_html}
            </text>
            """
        )

    return f"""
    <svg
        class="cloud-stage"
        data-group="{esc(group_name)}"
        data-mode="{esc(mode)}"
        data-unit="{esc(unit)}"
        viewBox="-35 -30 1270 740"
        preserveAspectRatio="xMidYMid meet"
    >
        {''.join(words)}
    </svg>
    """


def main() -> int:
    data = json.loads(
        DATA_FILE.read_text(encoding="utf-8")
    )

    stages = []

    for group_name, group in data["groups"].items():
        for mode in ("themes", "changes"):
            for unit in ("words", "phrases"):
                stages.append(
                    svg_stage(
                        group_name,
                        group,
                        mode,
                        unit,
                    )
                )

    current_label = window_label(
        data["meta"]["current"]
    )

    previous_label = window_label(
        data["meta"]["previous"]
    )

    data_json = json.dumps(
        data,
        ensure_ascii=False,
    ).replace("</", "<\\/")

    template = r'''<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>МІП · Тематичний простір</title>

<style>
:root {
    --bg: #f3f6f9;
    --panel: #fff;
    --text: #172033;
    --muted: #697386;
    --line: #dfe5ec;
    --accent: #245bd7;
    --up: #148a4b;
    --down: #c04444;
    --shadow: 0 8px 28px rgba(20,32,55,.055);
}

* { box-sizing: border-box; }

body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: Inter, "Segoe UI", Arial, sans-serif;
}

.page {
    max-width: 1720px;
    margin: 0 auto;
    padding: 30px 34px 70px;
}

.eyebrow {
    color: var(--accent);
    font-size: 12px;
    font-weight: 800;
    letter-spacing: .08em;
}

h1 {
    margin: 5px 0;
    font-size: 31px;
}

.subtitle {
    color: var(--muted);
    font-size: 13px;
}

.toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 9px;
    align-items: center;
    margin-top: 22px;
    padding: 13px 15px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    box-shadow: var(--shadow);
}

.label {
    color: var(--muted);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .06em;
}

.chip {
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 11px;
    background: #fafbfd;
}

.segment {
    display: inline-flex;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 8px;
}

.segment button {
    border: 0;
    border-right: 1px solid var(--line);
    padding: 8px 13px;
    background: #fafbfd;
    color: #455064;
    cursor: pointer;
    font-weight: 650;
}

.segment button:last-child {
    border-right: 0;
}

.segment button.active {
    color: white;
    background: var(--accent);
}

.workspace {
    display: grid;
    grid-template-columns: minmax(0, 1.8fr) minmax(340px, .72fr);
    gap: 18px;
    margin-top: 18px;
}

.panel {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 15px;
    box-shadow: var(--shadow);
}

.panel-head {
    display: flex;
    justify-content: space-between;
    gap: 18px;
    padding: 17px 20px 12px;
    border-bottom: 1px solid var(--line);
}

.panel-title {
    font-size: 16px;
    font-weight: 750;
}

.panel-note {
    margin-top: 3px;
    color: var(--muted);
    font-size: 11px;
}

.legend {
    display: flex;
    gap: 12px;
    align-items: center;
    color: var(--muted);
    font-size: 10px;
}

.legend i {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 4px;
    border-radius: 50%;
}

.legend .up { background: var(--up); }
.legend .down { background: var(--down); }

.legend[hidden] {
    display: none !important;
}

.cloud {
    position: relative;
    aspect-ratio: 1200 / 680;
    min-height: 500px;
    overflow: hidden;
}

.cloud-stage {
    display: none;
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
}

.cloud-stage.active { display: block; }

.cloud-word {
    font-family: "DejaVu Sans", "Segoe UI", Arial, sans-serif;
    font-weight: 500;
    cursor: pointer;
    transition: fill .15s, filter .15s;
}

.theme-word { fill: #285686; }
.theme-word.tone-1 { fill: #163f73; }
.theme-word.tone-2 { fill: #2d7393; }
.theme-word.tone-3 { fill: #355f86; }

.change-up { fill: var(--up); }
.change-down { fill: var(--down); }

.cloud-word:hover,
.cloud-word.selected {
    fill: var(--accent) !important;
    filter: drop-shadow(0 1px 4px rgba(36,91,215,.25));
}

.change-marker { font-weight: 800; }

.detail {
    padding: 20px;
    min-height: 500px;
}

.detail-kicker {
    color: var(--accent);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .08em;
}

.detail-term {
    margin: 7px 0 18px;
    font-size: 28px;
}

.metrics {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 9px;
}

.detail.themes .metrics {
    grid-template-columns: repeat(3, 1fr);
}

.detail.themes #metric-change-card {
    display: none;
}

.metric {
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: #fafbfd;
}

.metric-value {
    font-size: 20px;
    font-weight: 800;
}

.metric-label {
    margin-top: 3px;
    color: var(--muted);
    font-size: 10px;
}

.positive { color: var(--up); }
.negative { color: var(--down); }

.compare {
    margin-top: 18px;
    padding-top: 16px;
    border-top: 1px solid var(--line);
}

.compare-title,
.source-title {
    color: var(--muted);
    font-size: 11px;
}

.compare-row {
    display: grid;
    grid-template-columns: 70px 1fr 48px;
    gap: 7px;
    align-items: center;
    margin-top: 9px;
    font-size: 11px;
}

.bar {
    height: 7px;
    overflow: hidden;
    border-radius: 20px;
    background: #edf0f4;
}

.fill {
    display: block;
    height: 100%;
    background: var(--accent);
}

.sources {
    margin-top: 18px;
    padding-top: 16px;
    border-top: 1px solid var(--line);
}

.source-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
}

.source {
    padding: 5px 8px;
    border: 1px solid var(--line);
    border-radius: 20px;
    background: #f8fafc;
    font-size: 10px;
}

.note {
    margin-top: 18px;
    color: var(--muted);
    font-size: 10px;
    line-height: 1.45;
}

.examples {
    margin-top: 18px;
    padding: 20px;
}

.examples-head {
    display: flex;
    justify-content: space-between;
    margin-bottom: 13px;
}

.examples-title {
    font-size: 16px;
    font-weight: 750;
}

.examples-count {
    color: var(--muted);
    font-size: 10px;
}

.example-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 11px;
}

.example {
    padding: 13px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: #fbfcfd;
}

.example-source {
    color: var(--accent);
    font-size: 11px;
    font-weight: 750;
}

.example-meta {
    margin-top: 3px;
    color: var(--muted);
    font-size: 9px;
}

.example-text {
    margin-top: 8px;
    font-size: 12px;
    line-height: 1.5;
}

.empty {
    color: var(--muted);
    font-size: 12px;
}

@media (max-width: 1100px) {
    .workspace { grid-template-columns: 1fr; }
    .example-grid { grid-template-columns: 1fr; }
}
</style>

<style>
/* MIP Topic Explorer design pass v1.1 */

html {
    font-size: 16px;
}

body {
    font-size: 1rem;
    line-height: 1.45;
}

/* Page hierarchy */

.page {
    max-width: 1740px;
    padding: 2rem 2.25rem 4.5rem;
}

.eyebrow {
    font-size: .75rem;
    letter-spacing: .07em;
}

h1 {
    margin: .35rem 0;
    font-size: 2.125rem;
    line-height: 1.12;
    letter-spacing: -.02em;
}

.subtitle {
    font-size: .9375rem;
    line-height: 1.45;
}

/* Filters */

.toolbar {
    gap: .7rem;
    margin-top: 1.4rem;
    padding: .9rem 1rem;
}

.label {
    font-size: .7rem;
    letter-spacing: .055em;
}

.chip {
    min-height: 2.25rem;
    display: inline-flex;
    align-items: center;
    padding: .55rem .75rem;
    font-size: .8125rem;
}

.segment button {
    min-height: 2.25rem;
    padding: .55rem .9rem;
    font-size: .8125rem;
    line-height: 1;
}

.segment button.active {
    font-weight: 700;
}

/* Main analytical surface */

.workspace {
    grid-template-columns:
        minmax(0, 1.75fr)
        minmax(390px, .72fr);
    gap: 1.15rem;
    margin-top: 1.15rem;
}

.panel-head {
    padding: 1.1rem 1.25rem .9rem;
}

.panel-title {
    font-size: 1.125rem;
    line-height: 1.25;
}

.panel-note {
    margin-top: .3rem;
    font-size: .8125rem;
    line-height: 1.4;
}

.cloud {
    min-height: 540px;
}

/* Legend exists only in change mode */

.legend {
    display: none !important;
    gap: .8rem;
    font-size: .75rem;
}

.legend.is-visible {
    display: flex !important;
}

/* Detail panel */

.detail {
    padding: 1.5rem;
    min-height: 540px;
}

.detail-kicker {
    font-size: .75rem;
    letter-spacing: .04em;
    text-transform: none;
}

.detail-term {
    margin: .45rem 0 1.25rem;
    font-size: 2rem;
    line-height: 1.15;
    letter-spacing: -.015em;
}

.metrics {
    gap: .65rem;
}

.metric {
    padding: .9rem;
}

.metric-value {
    font-size: 1.5rem;
    line-height: 1.1;
}

.metric-label {
    margin-top: .35rem;
    font-size: .75rem;
    line-height: 1.3;
}

.compare {
    margin-top: 1.25rem;
    padding-top: 1.1rem;
}

.compare-title,
.source-title {
    font-size: .8125rem;
    line-height: 1.35;
}

.compare-row {
    grid-template-columns: 72px 1fr 52px;
    gap: .55rem;
    margin-top: .65rem;
    font-size: .8125rem;
}

.sources {
    margin-top: 1.25rem;
    padding-top: 1.1rem;
}

.source-list {
    gap: .45rem;
    margin-top: .65rem;
}

.source {
    padding: .4rem .6rem;
    font-size: .75rem;
    line-height: 1.2;
}

.note {
    margin-top: 1.25rem;
    font-size: .75rem;
    line-height: 1.55;
    max-width: 62ch;
}

/* Evidence / representative messages */

.examples {
    margin-top: 1.15rem;
    padding: 1.35rem;
}

.examples-head {
    align-items: baseline;
    margin-bottom: 1rem;
}

.examples-title {
    font-size: 1.125rem;
}

.examples-count {
    font-size: .75rem;
}

.example-grid {
    gap: .9rem;
}

.example {
    padding: 1rem;
}

.example-source {
    font-size: .8125rem;
    line-height: 1.3;
}

.example-meta {
    margin-top: .3rem;
    font-size: .7rem;
    line-height: 1.35;
}

.example-text {
    margin-top: .7rem;
    font-size: .9375rem;
    line-height: 1.55;
}

.empty {
    font-size: .875rem;
    line-height: 1.5;
}

@media (max-width: 1200px) {
    .workspace {
        grid-template-columns: 1fr;
    }

    .detail {
        min-height: auto;
    }
}
</style>


<style>
/* MIP Topic Explorer design pass v1.2 */

/* Компактніша робоча поверхня:
   cloud не повинен витісняти аналітичні деталі та evidence. */

.page {
    max-width: 1600px;
    padding: 1.75rem 2rem 3.5rem;
}

.workspace {
    grid-template-columns:
        minmax(0, 1.55fr)
        minmax(430px, .8fr);
    gap: 1rem;
    align-items: stretch;
}

/* Cloud: менша висота, без зайвого hero-ефекту. */

.cloud {
    height: clamp(430px, 49vh, 500px);
    min-height: 0;
    aspect-ratio: auto;
}

.cloud-stage {
    width: 100%;
    height: 100%;
}

/* Права панель тієї ж висоти. */

.detail {
    height: clamp(430px, 49vh, 500px);
    min-height: 0;
    padding: 1.4rem;
    overflow: auto;
}

/* Загальна читабельність. */

body {
    font-size: 1rem;
}

.subtitle {
    font-size: 1rem;
}

.toolbar {
    padding: .9rem 1rem;
    gap: .75rem;
}

.label {
    font-size: .75rem;
}

.chip,
.segment button {
    font-size: .875rem;
}

.panel-title {
    font-size: 1.125rem;
}

.panel-note {
    font-size: .875rem;
}

/* Detail panel */

.detail-kicker {
    font-size: .8125rem;
}

.detail-term {
    font-size: 2.125rem;
    margin-bottom: 1.15rem;
}

.metric {
    padding: .9rem 1rem;
}

.metric-value {
    font-size: 1.625rem;
}

.metric-label {
    font-size: .8125rem;
}

.compare-title,
.source-title {
    font-size: .875rem;
}

.compare-row {
    font-size: .875rem;
}

.source {
    font-size: .8125rem;
    padding: .42rem .65rem;
}

.note {
    font-size: .8125rem;
    line-height: 1.5;
}

/* Evidence має читатися як основний аналітичний контент,
   а не як дрібний footer. */

.examples {
    padding: 1.35rem;
}

.examples-title {
    font-size: 1.25rem;
}

.examples-count {
    font-size: .8125rem;
}

.example-grid {
    gap: .85rem;
}

.example {
    padding: 1rem 1.05rem;
}

.example-source {
    font-size: .875rem;
}

.example-meta {
    font-size: .75rem;
}

.example-text {
    font-size: 1rem;
    line-height: 1.55;
}

/* На середніх екранах теж не даємо cloud роздуватися. */

@media (max-width: 1350px) {
    .workspace {
        grid-template-columns:
            minmax(0, 1.45fr)
            minmax(400px, .85fr);
    }

    .cloud,
    .detail {
        height: 450px;
    }
}

@media (max-width: 1100px) {
    .workspace {
        grid-template-columns: 1fr;
    }

    .cloud,
    .detail {
        height: auto;
        min-height: 420px;
    }

    .detail {
        min-height: auto;
    }
}
</style>


<style>
/* MIP Topic Explorer scroll fix v1.2.1 */

/*
 * Аналітична картка не створює власний scroll-container.
 * Прокруткою керує сторінка.
 */
.detail {
    height: auto;
    min-height: clamp(430px, 49vh, 500px);
    overflow: visible;
}

/*
 * Не розтягуємо cloud-картку до висоти detail,
 * якщо правій панелі потрібно трохи більше місця.
 */
.workspace {
    align-items: start;
}

/*
 * Cloud зберігає компактну контрольовану висоту.
 */
.cloud {
    height: clamp(430px, 49vh, 500px);
}

@media (max-width: 1350px) {
    .detail {
        height: auto;
        min-height: 450px;
    }

    .cloud {
        height: 450px;
    }
}

@media (max-width: 1100px) {
    .detail {
        min-height: 0;
    }
}
</style>


<style>
/* MIP Topic Explorer analytical context v1.3b */

.example-context {
    display: flex;
    flex-wrap: wrap;
    gap: .4rem;
    align-items: center;
    margin-top: .35rem;
}

.slice-badge {
    display: inline-flex;
    align-items: center;
    padding: .22rem .45rem;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: #f5f7fa;
    color: #596579;
    font-size: .7rem;
    font-weight: 650;
    line-height: 1;
}

.slice-badge.current {
    color: #1757c2;
    border-color: #c9d8fb;
    background: #f1f5ff;
}

.slice-badge.previous {
    color: #76511d;
    border-color: #ead8b8;
    background: #fff8eb;
}

.example-date {
    color: var(--muted);
    font-size: .75rem;
    line-height: 1.2;
}

.compare-row {
    grid-template-columns: 58px 1fr 125px;
}

.compare-row > :last-child {
    text-align: right;
    white-space: nowrap;
}
</style>


<style>
/* MIP Topic Explorer evidence drill-down v1.4 */

.evidence-toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: .75rem;
    align-items: end;
    margin-bottom: 1rem;
    padding: .85rem 0;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
}

.evidence-filter-group,
.evidence-filter {
    display: flex;
    flex-direction: column;
    gap: .35rem;
}

.evidence-filter-label {
    color: var(--muted);
    font-size: .75rem;
    line-height: 1;
}

.evidence-filter select {
    min-width: 180px;
    height: 2.25rem;
    padding: 0 .65rem;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: white;
    color: var(--text);
    font: inherit;
    font-size: .8125rem;
}

.evidence-reset,
.evidence-more-button {
    min-height: 2.25rem;
    padding: .5rem .8rem;
    border: 1px solid #cbd6e6;
    border-radius: 8px;
    background: #f7f9fc;
    color: #31506f;
    font: inherit;
    font-size: .8125rem;
    font-weight: 650;
    cursor: pointer;
}

.evidence-reset:hover,
.evidence-more-button:hover {
    background: #eef3fa;
}

.evidence-more-button.secondary {
    background: white;
}

.evidence-more {
    display: flex;
    gap: .6rem;
    justify-content: center;
    margin-top: 1rem;
}

.evidence-more[hidden] {
    display: none !important;
}

.example-source-list {
    display: flex;
    flex-wrap: wrap;
    gap: .35rem;
}

.example-source-button {
    appearance: none;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--accent);
    font: inherit;
    font-size: .875rem;
    font-weight: 750;
    cursor: pointer;
    text-align: left;
}

.example-source-button:hover {
    text-decoration: underline;
}

.example-date-button,
.slice-badge {
    cursor: pointer;
}

.example-date-button {
    appearance: none;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: .75rem;
}

.example-date-button:hover {
    color: var(--accent);
    text-decoration: underline;
}

.source {
    cursor: pointer;
}

.source.active-filter {
    border-color: #8aacef;
    background: #edf3ff;
    color: #174fa8;
}

.compare-filter {
    cursor: pointer;
    border-radius: 6px;
    padding: .2rem .25rem;
    margin-left: -.25rem;
    margin-right: -.25rem;
}

.compare-filter:hover,
.compare-filter.active-filter {
    background: #f2f6fd;
}

.compare-note {
    margin-top: .75rem;
    color: var(--muted);
    font-size: .72rem;
    line-height: 1.4;
}

.evidence-slice-filter button {
    font-size: .75rem;
    padding: .45rem .65rem;
}

.evidence-slice-filter .count {
    margin-left: .25rem;
    opacity: .72;
}

@media (max-width: 900px) {
    .evidence-filter {
        flex: 1 1 180px;
    }

    .evidence-filter select {
        width: 100%;
    }
}
</style>

</head>

<body>
<div class="page">

<header>
    <div class="eyebrow">МІП · TECHNICAL PILOT</div>
    <h1>Тематичний простір</h1>
    <div class="subtitle">
        Інтерактивний огляд тематичного профілю спостережуваного інформаційного простору
    </div>
</header>

<section class="toolbar">

    <span class="label">Зріз</span>
    <span class="chip">@@CURRENT@@</span>

    <span class="label">Порівняльний зріз</span>
    <span class="chip">@@PREVIOUS@@</span>

    <span class="label">Простір</span>
    <div class="segment" id="group-filter">
        <button class="active" data-value="ua_space">UA-space</button>
        <button data-value="ru_space">RU-space</button>
    </div>

    <span class="label">Режим</span>
    <div class="segment" id="mode-filter">
        <button class="active" data-value="themes">Теми</button>
        <button data-value="changes">Зміни</button>
    </div>

    <span class="label">Одиниця</span>
    <div class="segment" id="unit-filter">
        <button data-value="words">Слова</button>
        <button class="active" data-value="phrases">Фрази</button>
    </div>

</section>

<main class="workspace">

<section class="panel">
    <div class="panel-head">
        <div>
            <div class="panel-title" id="cloud-title"></div>
            <div class="panel-note" id="cloud-note"></div>
        </div>

        <div class="legend" id="legend">
            <span><i class="up"></i>зростання</span>
            <span><i class="down"></i>зниження</span>
        </div>
    </div>

    <div class="cloud">
        @@STAGES@@
    </div>
</section>

<aside class="panel detail themes" id="detail">

    <div class="detail-kicker" id="detail-kicker">ТЕМА</div>
    <div class="detail-term" id="detail-term">—</div>

    <div class="metrics">

        <div class="metric">
            <div class="metric-value" id="metric-docs">—</div>
            <div class="metric-label">документів</div>
        </div>

        <div class="metric">
            <div class="metric-value" id="metric-share">—</div>
            <div class="metric-label">частка вибірки</div>
        </div>

        <div class="metric" id="metric-change-card">
            <div class="metric-value" id="metric-change">—</div>
            <div class="metric-label">зміна</div>
        </div>

        <div class="metric">
            <div class="metric-value" id="metric-sources">—</div>
            <div class="metric-label">джерел</div>
        </div>

    </div>

    <div class="compare" id="compare" hidden>
        <div class="compare-title">Порівняння частки документів</div>

        <div
            class="compare-row compare-filter"
            data-filter-slice="previous"
        >
            <span id="old-slice-label">22.08</span>
            <span class="bar"><span class="fill" id="old-bar"></span></span>
            <span id="old-value"></span>
        </div>

        <div
            class="compare-row compare-filter"
            data-filter-slice="current"
        >
            <span id="new-slice-label">26.08</span>
            <span class="bar"><span class="fill" id="new-bar"></span></span>
            <span id="new-value"></span>
        </div>

        <div class="compare-note" id="compare-note"></div>
    </div>

    <div class="sources">
        <div class="source-title">Джерела маркера</div>
        <div class="source-list" id="sources"></div>
    </div>

    <div class="note">
        Клік по слову або фразі змінює аналітичний контекст екрану.
        Порядок виявлення не є доказом першоджерела або напрямку поширення інформації.
    </div>

</aside>

</main>

<section class="panel examples">

    <div class="examples-head">
        <div class="examples-title">Репрезентативні повідомлення</div>
        <div class="examples-count" id="examples-count"></div>
    </div>

    <div class="evidence-toolbar" id="evidence-toolbar">

        <div
            class="evidence-filter-group"
            id="evidence-slice-group"
        >
            <span class="evidence-filter-label">Зріз</span>
            <div
                class="segment evidence-slice-filter"
                id="evidence-slice-filter"
            ></div>
        </div>

        <label class="evidence-filter">
            <span class="evidence-filter-label">
                Дата публікації
            </span>
            <select id="evidence-date-filter"></select>
        </label>

        <label class="evidence-filter">
            <span class="evidence-filter-label">
                Джерело
            </span>
            <select id="evidence-source-filter"></select>
        </label>

        <button
            type="button"
            class="evidence-reset"
            id="evidence-reset"
        >
            Скинути
        </button>

    </div>

    <div class="example-grid" id="example-grid"></div>

    <div class="evidence-more" id="evidence-more-row">
        <button
            type="button"
            class="evidence-more-button"
            id="evidence-more"
        >
            Показати ще
        </button>

        <button
            type="button"
            class="evidence-more-button secondary"
            id="evidence-all"
        >
            Показати всі
        </button>
    </div>

</section>

</div>

<script>
const DATA = @@DATA@@;

let activeGroup = "ua_space";
let activeMode = "themes";
let activeUnit = "phrases";
let activeTerm = null;

let activeItem = null;
let activeEvidenceKey = null;

let evidenceSlice = "all";
let evidenceDate = "all";
let evidenceSource = "all";
let evidenceVisible = 9;

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function activeRows() {
    const group = DATA.groups[activeGroup];

    if (activeMode === "themes") {
        return group.themes[activeUnit];
    }

    return [
        ...group.changes[activeUnit].growing,
        ...group.changes[activeUnit].declining
    ];
}

function formatExampleDate(example) {
    const raw =
        example.published_at ||
        example.first_seen_at;

    if (!raw) {
        return {
            text: "дата невідома",
            kind: "unknown"
        };
    }

    const dt = new Date(raw);

    if (Number.isNaN(dt.getTime())) {
        return {
            text: String(raw),
            kind: "unknown"
        };
    }

    const date = new Intl.DateTimeFormat(
        "uk-UA",
        {
            timeZone: "UTC",
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
        }
    ).format(dt);

    return {
        text:
            (example.published_at ? "Опубліковано: " : "Виявлено: ")
            + date
            + " UTC",
        kind:
            example.published_at
                ? "published"
                : "observed"
    };
}

function sourceSummary(examples) {
    const counts = new Map();

    for (const example of examples || []) {
        for (const source of example.source_names || []) {
            counts.set(
                source,
                (counts.get(source) || 0) + 1
            );
        }
    }

    return [...counts.entries()]
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function evidenceSlices(example) {
    if (
        Array.isArray(example.evidence_slices)
        && example.evidence_slices.length
    ) {
        return example.evidence_slices;
    }

    if (example.evidence_slice === "both") {
        return ["current", "previous"];
    }

    if (
        example.evidence_slice === "current"
        || example.evidence_slice === "previous"
    ) {
        return [example.evidence_slice];
    }

    return ["current"];
}

function evidenceDateKey(example) {
    const raw =
        example.published_at
        || example.first_seen_at;

    if (!raw) {
        return "unknown";
    }

    const dt = new Date(raw);

    if (Number.isNaN(dt.getTime())) {
        return "unknown";
    }

    return dt.toISOString().slice(0, 10);
}

function dateLabel(dateKey) {
    if (dateKey === "unknown") {
        return "Дата невідома";
    }

    const [year, month, day] =
        dateKey.split("-");

    return `${day}.${month}.${year}`;
}

function shortSliceDate(value) {
    const dt = new Date(value);

    if (Number.isNaN(dt.getTime())) {
        return "—";
    }

    return new Intl.DateTimeFormat(
        "uk-UA",
        {
            timeZone: "UTC",
            day: "2-digit",
            month: "2-digit",
        }
    ).format(dt);
}

function windowHours(block) {
    const start = new Date(block.start);
    const end = new Date(block.end);

    if (
        Number.isNaN(start.getTime())
        || Number.isNaN(end.getTime())
    ) {
        return null;
    }

    return (
        (end.getTime() - start.getTime())
        / 3_600_000
    );
}

function resetEvidenceFilters() {
    evidenceSlice = "all";
    evidenceDate = "all";
    evidenceSource = "all";
    evidenceVisible = 9;
}

function filteredEvidence(examples) {
    return (examples || []).filter(example => {
        if (
            evidenceSlice !== "all"
            && !evidenceSlices(example).includes(
                evidenceSlice
            )
        ) {
            return false;
        }

        if (
            evidenceDate !== "all"
            && evidenceDateKey(example) !== evidenceDate
        ) {
            return false;
        }

        if (
            evidenceSource !== "all"
            && !(example.source_names || []).includes(
                evidenceSource
            )
        ) {
            return false;
        }

        return true;
    });
}

function renderSources(item) {
    const root = document.getElementById("sources");
    const sources = sourceSummary(item.examples || []);

    document.getElementById("metric-sources").textContent =
        String(sources.length);

    root.innerHTML = sources.length
        ? sources.map(([name, count]) => {
            const active =
                evidenceSource === name
                    ? " active-filter"
                    : "";

            return `
                <button
                    type="button"
                    class="source${active}"
                    data-filter-source="${escapeHtml(name)}"
                >
                    ${escapeHtml(name)} · ${count}
                </button>
            `;
        }).join("")
        : '<span class="empty">—</span>';
}

function renderEvidenceControls(item) {
    const examples = item.examples || [];

    const currentCount = examples.filter(
        example =>
            evidenceSlices(example).includes("current")
    ).length;

    const previousCount = examples.filter(
        example =>
            evidenceSlices(example).includes("previous")
    ).length;

    const sliceGroup =
        document.getElementById(
            "evidence-slice-group"
        );

    const sliceRoot =
        document.getElementById(
            "evidence-slice-filter"
        );

    if (activeMode === "changes") {
        sliceGroup.hidden = false;

        const slices = [
            ["all", "Усі", examples.length],
            ["current", "Поточний", currentCount],
            ["previous", "Порівняльний", previousCount],
        ];

        sliceRoot.innerHTML = slices.map(
            ([value, label, count]) => `
                <button
                    type="button"
                    class="${evidenceSlice === value ? "active" : ""}"
                    data-evidence-slice="${value}"
                >
                    ${label}
                    <span class="count">${count}</span>
                </button>
            `
        ).join("");
    } else {
        sliceGroup.hidden = true;
        sliceRoot.innerHTML = "";
    }

    const dates = new Map();

    for (const example of examples) {
        const key = evidenceDateKey(example);

        dates.set(
            key,
            (dates.get(key) || 0) + 1
        );
    }

    const dateSelect =
        document.getElementById(
            "evidence-date-filter"
        );

    const sortedDates = [...dates.entries()]
        .sort((a, b) => b[0].localeCompare(a[0]));

    dateSelect.innerHTML =
        `<option value="all">Усі дати · ${examples.length}</option>`
        + sortedDates.map(
            ([key, count]) =>
                `<option value="${escapeHtml(key)}">`
                + `${escapeHtml(dateLabel(key))} · ${count}`
                + `</option>`
        ).join("");

    dateSelect.value = evidenceDate;

    const sourceSelect =
        document.getElementById(
            "evidence-source-filter"
        );

    const sources = sourceSummary(examples);

    sourceSelect.innerHTML =
        `<option value="all">Усі джерела · ${sources.length}</option>`
        + sources.map(
            ([name, count]) =>
                `<option value="${escapeHtml(name)}">`
                + `${escapeHtml(name)} · ${count}`
                + `</option>`
        ).join("");

    sourceSelect.value = evidenceSource;
}

function renderExamples(item) {
    activeItem = item;

    renderSources(item);
    renderEvidenceControls(item);

    const allExamples = item.examples || [];
    const filtered = filteredEvidence(allExamples);

    const visible = filtered.slice(
        0,
        evidenceVisible,
    );

    const root =
        document.getElementById(
            "example-grid"
        );

    const countRoot =
        document.getElementById(
            "examples-count"
        );

    const hasFilters =
        evidenceSlice !== "all"
        || evidenceDate !== "all"
        || evidenceSource !== "all";

    countRoot.textContent =
        `Показано: ${visible.length} з ${filtered.length}`
        + (
            hasFilters
                ? ` · усього ${allExamples.length}`
                : ""
        );

    if (!filtered.length) {
        root.innerHTML =
            '<div class="empty">'
            + 'За вибраними фільтрами повідомлень немає.'
            + '</div>';

        document.getElementById(
            "evidence-more-row"
        ).hidden = true;

        return;
    }

    root.innerHTML = visible.map(example => {
        const sources =
            example.source_names || [];

        const types =
            (example.source_types || []).join(" · ");

        const dateInfo =
            formatExampleDate(example);

        const dateKey =
            evidenceDateKey(example);

        const slices =
            evidenceSlices(example);

        const sliceBadges = slices.map(slice => {
            const isPrevious =
                slice === "previous";

            const label =
                isPrevious
                    ? "Порівняльний зріз"
                    : "Поточний зріз";

            const cls =
                isPrevious
                    ? "previous"
                    : "current";

            return `
                <button
                    type="button"
                    class="slice-badge ${cls}"
                    data-filter-slice="${slice}"
                >
                    ${label}
                </button>
            `;
        }).join("");

        const sourceButtons = sources.length
            ? sources.map(name => `
                <button
                    type="button"
                    class="example-source-button"
                    data-filter-source="${escapeHtml(name)}"
                >
                    ${escapeHtml(name)}
                </button>
            `).join("")
            : '<span class="example-source">unknown</span>';

        return `
            <article class="example">

                <div class="example-source-list">
                    ${sourceButtons}
                </div>

                <div class="example-context">

                    ${sliceBadges}

                    <button
                        type="button"
                        class="example-date-button"
                        data-filter-date="${escapeHtml(dateKey)}"
                    >
                        ${escapeHtml(dateInfo.text)}
                    </button>

                </div>

                <div class="example-meta">
                    ${escapeHtml(types)}
                </div>

                <div class="example-text">
                    ${escapeHtml(example.text_preview || "")}
                </div>

            </article>
        `;
    }).join("");

    const moreRow =
        document.getElementById(
            "evidence-more-row"
        );

    const moreButton =
        document.getElementById(
            "evidence-more"
        );

    const allButton =
        document.getElementById(
            "evidence-all"
        );

    const hasMore =
        visible.length < filtered.length;

    moreRow.hidden = !hasMore;
    moreButton.hidden = !hasMore;
    allButton.hidden = !hasMore;
}

function rerenderEvidence() {
    if (!activeItem) {
        return;
    }

    renderExamples(activeItem);

    document
        .querySelectorAll(".compare-filter")
        .forEach(row => {
            row.classList.toggle(
                "active-filter",
                evidenceSlice !== "all"
                && row.dataset.filterSlice === evidenceSlice
            );
        });
}

function selectTerm(term) {
    const item = activeRows().find(
        row => row.term === term
    );

    if (!item) {
        return;
    }

    const evidenceKey = [
        activeGroup,
        activeMode,
        activeUnit,
        term,
    ].join("|");

    if (evidenceKey !== activeEvidenceKey) {
        resetEvidenceFilters();
        activeEvidenceKey = evidenceKey;
    }

    activeTerm = term;

    document.querySelectorAll(".cloud-word").forEach(node => {
        node.classList.toggle(
            "selected",
            node.dataset.group === activeGroup
            && node.dataset.mode === activeMode
            && node.dataset.unit === activeUnit
            && node.dataset.term === term
        );
    });

    document.getElementById(
        "detail-term"
    ).textContent =
        item.label || item.term;

    const detail =
        document.getElementById("detail");

    const compare =
        document.getElementById("compare");

    const change =
        document.getElementById(
            "metric-change"
        );

    change.classList.remove(
        "positive",
        "negative",
    );

    if (activeMode === "themes") {
        detail.classList.add("themes");

        document.getElementById(
            "detail-kicker"
        ).textContent =
            activeUnit === "words"
                ? "Тематичний маркер · слово"
                : "Тематичний маркер · фраза";

        document.getElementById(
            "metric-docs"
        ).textContent =
            item.documents;

        document.getElementById(
            "metric-share"
        ).textContent =
            `${Number(item.doc_pct).toFixed(1)}%`;

        compare.hidden = true;

    } else {
        detail.classList.remove("themes");

        const delta =
            Number(item.delta_pct);

        const oldPct =
            Number(item.old_pct);

        const newPct =
            Number(item.new_pct);

        let changeLabel;

        if (oldPct === 0 && newPct > 0) {
            changeLabel =
                "Новий маркер у зрізі";

        } else if (oldPct > 0 && newPct === 0) {
            changeLabel =
                "Маркер не зафіксовано у поточному зрізі";

        } else if (delta > 0) {
            changeLabel =
                "Частка маркера зросла";

        } else if (delta < 0) {
            changeLabel =
                "Частка маркера знизилась";

        } else {
            changeLabel =
                "Частка маркера без змін";
        }

        document.getElementById(
            "detail-kicker"
        ).textContent =
            changeLabel;

        document.getElementById(
            "metric-docs"
        ).textContent =
            item.new_documents;

        document.getElementById(
            "metric-share"
        ).textContent =
            `${newPct.toFixed(1)}%`;

        change.textContent =
            `${delta > 0 ? "+" : ""}`
            + `${delta.toFixed(1)} п.п.`;

        if (delta > 0) {
            change.classList.add("positive");
        } else if (delta < 0) {
            change.classList.add("negative");
        }

        compare.hidden = false;

        const maxValue = Math.max(
            oldPct,
            newPct,
            0.1,
        );

        document.getElementById(
            "old-bar"
        ).style.width =
            `${100 * oldPct / maxValue}%`;

        document.getElementById(
            "new-bar"
        ).style.width =
            `${100 * newPct / maxValue}%`;

        const sample =
            DATA.groups[activeGroup].sample;

        document.getElementById(
            "old-value"
        ).textContent =
            `${item.old_documents}`
            + ` / ${sample.previous_documents}`
            + ` · ${oldPct.toFixed(1)}%`;

        document.getElementById(
            "new-value"
        ).textContent =
            `${item.new_documents}`
            + ` / ${sample.current_documents}`
            + ` · ${newPct.toFixed(1)}%`;

        document.getElementById(
            "old-slice-label"
        ).textContent =
            shortSliceDate(
                DATA.meta.previous.start
            );

        document.getElementById(
            "new-slice-label"
        ).textContent =
            shortSliceDate(
                DATA.meta.current.start
            );

        const currentHours =
            windowHours(DATA.meta.current);

        const previousHours =
            windowHours(DATA.meta.previous);

        const note =
            document.getElementById(
                "compare-note"
            );

        if (
            currentHours !== null
            && previousHours !== null
            && currentHours !== previousHours
        ) {
            note.textContent =
                "Порівнюється частка документів "
                + "у кожному зрізі. "
                + `Тривалість: поточний ${Number(currentHours.toFixed(1))} год, `
                + `порівняльний ${Number(previousHours.toFixed(1))} год.`;

        } else {
            note.textContent =
                "Порівнюється частка документів "
                + "у кожному зрізі.";
        }
    }

    renderExamples(item);
}

function clearSelection() {
    activeTerm = null;
    activeItem = null;
    activeEvidenceKey = null;

    resetEvidenceFilters();

    document.querySelectorAll(
        ".cloud-word"
    ).forEach(node => {
        node.classList.remove("selected");
    });

    const detail =
        document.getElementById("detail");

    detail.classList.add("themes");

    document.getElementById(
        "detail-kicker"
    ).textContent =
        "Тематичний профіль";

    document.getElementById(
        "detail-term"
    ).textContent =
        "Маркерів не виявлено";

    document.getElementById(
        "metric-docs"
    ).textContent = "—";

    document.getElementById(
        "metric-share"
    ).textContent = "—";

    document.getElementById(
        "metric-change"
    ).textContent = "—";

    document.getElementById(
        "metric-sources"
    ).textContent = "—";

    document.getElementById(
        "compare"
    ).hidden = true;

    document.getElementById(
        "sources"
    ).innerHTML =
        '<span class="empty">'
        + 'У цьому зрізі маркерів не виявлено.'
        + '</span>';

    document.getElementById(
        "examples-count"
    ).textContent =
        "Показано: 0";

    document.getElementById(
        "evidence-toolbar"
    ).hidden = true;

    document.getElementById(
        "example-grid"
    ).innerHTML =
        '<div class="empty">'
        + 'Немає повідомлень для відображення.'
        + '</div>';

    document.getElementById(
        "evidence-more-row"
    ).hidden = true;
}

function firstTerm() {
    const stage = document.querySelector(
        `.cloud-stage[data-group="${activeGroup}"]` +
        `[data-mode="${activeMode}"]` +
        `[data-unit="${activeUnit}"]`
    );

    return stage?.querySelector(".cloud-word")?.dataset.term || null;
}

function updateView() {
    document.querySelectorAll(".cloud-stage").forEach(stage => {
        stage.classList.toggle(
            "active",
            stage.dataset.group === activeGroup &&
            stage.dataset.mode === activeMode &&
            stage.dataset.unit === activeUnit
        );
    });

    for (const [id, value] of [
        ["group-filter", activeGroup],
        ["mode-filter", activeMode],
        ["unit-filter", activeUnit],
    ]) {
        document.querySelectorAll(`#${id} button`).forEach(button => {
            button.classList.toggle(
                "active",
                button.dataset.value === value
            );
        });
    }

    const groupLabel =
        activeGroup === "ua_space"
            ? "UA-space"
            : "RU-space";

    const unitLabel =
        activeUnit === "words"
            ? "слова"
            : "фрази";

    document.getElementById("cloud-title").textContent =
        activeMode === "themes"
            ? `Тематичний профіль · ${groupLabel} · ${unitLabel}`
            : `Зміна тематичного профілю · ${groupLabel} · ${unitLabel}`;

    const sample =
        DATA.groups[activeGroup].sample;

    document.getElementById("cloud-note").textContent =
        activeMode === "themes"
            ? (
                `Розмір = кількість документів, де зустрічається `
                + `${activeUnit === "words" ? "слово" : "фраза"}`
                + ` · вибірка: ${sample.current_documents}`
            )
            : (
                "Розмір = абсолютна зміна частки; "
                + "зелений = зростання, червоний = зниження"
                + ` · поточний: ${sample.current_documents}`
                + ` · порівняльний: ${sample.previous_documents}`
            );

    document.getElementById("legend").classList.toggle(
        "is-visible",
        activeMode === "changes"
    );

    const term = firstTerm();

    if (term) {
        document.getElementById(
            "evidence-toolbar"
        ).hidden = false;

        selectTerm(term);
    } else {
        clearSelection();
    }
}

document.querySelectorAll("#group-filter button").forEach(button => {
    button.addEventListener("click", () => {
        activeGroup = button.dataset.value;
        updateView();
    });
});

document.querySelectorAll("#mode-filter button").forEach(button => {
    button.addEventListener("click", () => {
        activeMode = button.dataset.value;
        updateView();
    });
});

document.querySelectorAll("#unit-filter button").forEach(button => {
    button.addEventListener("click", () => {
        activeUnit = button.dataset.value;
        updateView();
    });
});

document.querySelectorAll(".cloud-word").forEach(node => {
    node.addEventListener("click", () => {
        activeGroup = node.dataset.group;
        activeMode = node.dataset.mode;
        activeUnit = node.dataset.unit;
        selectTerm(node.dataset.term);
    });
});


document.getElementById(
    "evidence-date-filter"
).addEventListener("change", event => {
    evidenceDate = event.target.value;
    evidenceVisible = 9;
    rerenderEvidence();
});

document.getElementById(
    "evidence-source-filter"
).addEventListener("change", event => {
    evidenceSource = event.target.value;
    evidenceVisible = 9;
    rerenderEvidence();
});

document.getElementById(
    "evidence-slice-filter"
).addEventListener("click", event => {
    const button =
        event.target.closest(
            "[data-evidence-slice]"
        );

    if (!button) {
        return;
    }

    evidenceSlice =
        button.dataset.evidenceSlice;

    evidenceVisible = 9;
    rerenderEvidence();
});

document.getElementById(
    "evidence-reset"
).addEventListener("click", () => {
    resetEvidenceFilters();
    rerenderEvidence();
});

document.getElementById(
    "evidence-more"
).addEventListener("click", () => {
    evidenceVisible += 9;
    rerenderEvidence();
});

document.getElementById(
    "evidence-all"
).addEventListener("click", () => {
    evidenceVisible =
        Number.MAX_SAFE_INTEGER;

    rerenderEvidence();
});

document.getElementById(
    "sources"
).addEventListener("click", event => {
    const button =
        event.target.closest(
            "[data-filter-source]"
        );

    if (!button) {
        return;
    }

    evidenceSource =
        button.dataset.filterSource;

    evidenceVisible = 9;
    rerenderEvidence();
});

document.getElementById(
    "example-grid"
).addEventListener("click", event => {
    const source =
        event.target.closest(
            "[data-filter-source]"
        );

    if (source) {
        evidenceSource =
            source.dataset.filterSource;

        evidenceVisible = 9;
        rerenderEvidence();
        return;
    }

    const date =
        event.target.closest(
            "[data-filter-date]"
        );

    if (date) {
        evidenceDate =
            date.dataset.filterDate;

        evidenceVisible = 9;
        rerenderEvidence();
        return;
    }

    const slice =
        event.target.closest(
            "[data-filter-slice]"
        );

    if (slice) {
        evidenceSlice =
            slice.dataset.filterSlice;

        evidenceVisible = 9;
        rerenderEvidence();
    }
});

document.querySelectorAll(
    ".compare-filter"
).forEach(row => {
    row.addEventListener("click", () => {
        evidenceSlice =
            row.dataset.filterSlice;

        evidenceDate = "all";
        evidenceSource = "all";
        evidenceVisible = 9;

        rerenderEvidence();

        document
            .querySelector(".examples")
            ?.scrollIntoView({
                behavior: "smooth",
                block: "start",
            });
    });
});


updateView();
</script>

</body>
</html>
'''

    report = (
        template
        .replace("@@CURRENT@@", esc(current_label))
        .replace("@@PREVIOUS@@", esc(previous_label))
        .replace("@@STAGES@@", "".join(stages))
        .replace("@@DATA@@", data_json)
    )

    OUTPUT_FILE.write_text(
        report,
        encoding="utf-8",
    )

    print(f"saved={OUTPUT_FILE}")
    print(f"bytes={OUTPUT_FILE.stat().st_size}")
    print("groups=ua_space,ru_space")
    print("modes=themes,changes")
    print("units=words,phrases")
    print("examples=all_visible_terms")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
