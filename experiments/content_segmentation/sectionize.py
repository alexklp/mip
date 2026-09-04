#!/usr/bin/env python3
"""
sectionize.py — deterministic structural sectionizer над Trafilatura XML
(occurrence_content.structured_content, structured_content_format='trafilatura_xml').

Без БД, без LLM, без побічних ефектів. Чиста функція:
    xml-рядок -> SectionizeResult

Правило межі (виправлено після ревʼю на живих документах — h1 НЕ boundary,
на реальних Brave1/frontline документах h1 = publication title, а не
семантичний розділ, включення його як boundary давало порожню/сирітську
h1-секцію):
  - `h2` — ЄДИНИЙ top-level candidate boundary;
  - `h3`+ лишається ВСЕРЕДИНІ поточної section (не відкриває нову);
  - `h1` НЕ входить у жодну section — текст та індекс(и) виносяться окремо
    в document_heading / document_heading_block_indexes, ігноруються при
    формуванні sections;
  - блоки ДО першого h2 (за вирахуванням h1) формують "preamble" секцію
    (heading=None);
  - якщо h2 немає взагалі — увесь body (крім h1) = одна section.

Перевірено емпірично на встановленій trafilatura: document.body — це
ПЛОСКИЙ список siblings, заголовки й параграфи НЕ вкладені одне в одне.
"""
from __future__ import annotations

from dataclasses import dataclass
from lxml import etree
from trafilatura.xml import xmltotxt

DOCUMENT_HEADING_REND = "h1"
SECTION_BOUNDARY_RENDS = frozenset({"h2"})


@dataclass(frozen=True)
class Section:
    section_id: int
    heading: str | None
    heading_level: str | None        # "h2" / None (preamble)
    block_indexes: tuple[int, ...]   # індекси в ОРИГІНАЛЬНИХ children body, включно з блоком заголовка h2
    text: str                        # детермінований текст ТІЛА секції (без дублювання heading)


@dataclass(frozen=True)
class SectionizeResult:
    document_heading: str | None
    document_heading_block_indexes: tuple[int, ...]
    sections: tuple[Section, ...]


def _block_text(element) -> str:
    """Детермінований текст одного top-level блоку. xmltotxt() на ОДНОМУ
    ізольованому блоці — чистий, самодостатній виклик (returnlist щоразу
    порожній, lookback/lookahead не виходить за межі переданого
    піддерева) — перевикористовує серіалізатор trafilatura per-block."""
    return xmltotxt(element, False).strip()


def sectionize(structured_content: str) -> SectionizeResult:
    root = etree.fromstring(structured_content.encode("utf-8"))
    body = root if root.tag == "body" else root.find(".//body")
    if body is None:
        raise ValueError("no <body> element in structured_content")

    children = list(body)

    heading_parts: list[str] = []
    heading_indexes: list[int] = []

    sections: list[Section] = []
    current_heading: str | None = None
    current_heading_level: str | None = None
    current_indexes: list[int] = []
    current_texts: list[str] = []

    def flush():
        if not current_indexes:
            return
        sections.append(Section(
            section_id=len(sections),
            heading=current_heading,
            heading_level=current_heading_level,
            block_indexes=tuple(current_indexes),
            text="\n\n".join(t for t in current_texts if t),
        ))

    for idx, el in enumerate(children):
        rend = el.get("rend") if el.tag == "head" else None

        if el.tag == "head" and rend == DOCUMENT_HEADING_REND:
            heading_parts.append(_block_text(el))
            heading_indexes.append(idx)
            continue

        is_boundary = el.tag == "head" and rend in SECTION_BOUNDARY_RENDS

        if is_boundary:
            flush()
            current_heading = _block_text(el)
            current_heading_level = rend
            current_indexes = [idx]
            current_texts = []
        else:
            current_indexes.append(idx)
            current_texts.append(_block_text(el))

    flush()

    return SectionizeResult(
        document_heading="\n\n".join(heading_parts) if heading_parts else None,
        document_heading_block_indexes=tuple(heading_indexes),
        sections=tuple(sections),
    )
