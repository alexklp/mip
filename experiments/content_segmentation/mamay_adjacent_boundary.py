#!/usr/bin/env python3
"""
mamay_adjacent_boundary.py — pilot: pairwise adjacent-boundary classifier.

Для КОЖНОЇ пари сусідніх sections (sectionize.py) -- ОКРЕМИЙ виклик Mamay.
Задача моделі -- ЛИШЕ визначити межу: "same" (продовження того самого
сюжету/наративу) чи "new" (починається самостійний інфопривід). Жодного
rationale, offsets, переписаного тексту чи списку segments -- один enum,
суворий валідатор.

Segments після цього збираються ДЕТЕРМІНОВАНО кодом з послідовності
same/new-рішень -- не моделлю.

Без БД-записів, без DDL, без LLD. Мережа: (num_sections - 1) викликів на
документ до живого Mamay endpoint.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path.home() / "mip"
sys.path.insert(0, str(REPO / "experiments" / "occurrence_content"))
sys.path.insert(0, str(REPO / "experiments" / "content_segmentation"))

# psycopg/occurrence_content_worker навмисно НЕ на верхньому рівні: --self-test
# має працювати без БД, лише sectionize (без DB deps).
from sectionize import sectionize, Section  # noqa: E402

DB_DSN = "dbname=mip_dev"

# Той самий підтверджений живий endpoint, що й mamay_group_sections.py
# (03.09.2026). :8000 з claude/03_MIP_Server_State застарілий.
ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf"

PROMPT_NAME = "adjacent_boundary"
PROMPT_VERSION = "v1"
SCHEMA_VERSION = "adjacent-boundary/1"

# Малий -- відповідь це один enum, не список.
MAX_TOKENS = 64
TIMEOUT = 120.0

FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def strip_markdown_fence(raw_text: str) -> tuple[str, bool]:
    """Той самий контракт, що experiments/claim_extraction/run_eval.py й
    mamay_group_sections.py: знімає огорожу ЛИШЕ якщо вона покриває ВЕСЬ
    рядок; інакше не чіпає."""
    m = FENCE_RE.match(raw_text)
    if m:
        return m.group(1), True
    return raw_text, False


def build_boundary_prompt(document_heading: str | None, left: Section, right: Section) -> str:
    heading_line = document_heading if document_heading else "(без заголовка публікації)"
    left_heading = left.heading if left.heading is not None else "(без заголовка, преамбула)"
    right_heading = right.heading if right.heading is not None else "(без заголовка, преамбула)"

    return f"""Заголовок публікації: {heading_line}

Тобі надано ДВА сусідні структурні розділи (sections) цієї публікації, у порядку
появи в тексті. Виріши: правий розділ -- це продовження ТОГО САМОГО аналітичного
сюжету/наративу, що і лівий, чи це вже самостійний, окремий інфопривід.

СЕМАНТИКА:
- "same" -- обидва розділи є послідовними частинами ОДНОГО сюжету/наративу. Зміна
  героя, прикладу, аргументу, підаспекту чи підрозділу САМА ПО СОБІ не означає новий
  segment, якщо обидва розкривають один тезис публікації.
- "new" -- праворуч починається САМОСТІЙНИЙ інфопривід, який має сенс аналізувати
  НЕЗАЛЕЖНО від лівого розділу.

Орієнтуйся на заголовок публікації вище: якщо стаття має ЄДИНИЙ тезис, а розділи
розкривають його через різних людей/приклади -- це "same".

СУВОРІ ПРАВИЛА:
- Ти НЕ пояснюєш рішення, НЕ переписуєш текст, НЕ повертаєш offsets чи список
  segments.
- Відповідь -- СУВОРО один JSON-об'єкт з ОДНИМ полем "boundary", значення ЛИШЕ
  "same" або "new". Без жодного тексту до чи після, без markdown-огорожі, без
  коментарів.

### section_id={left.section_id} (лівий)
heading: {left_heading}
text:
{left.text}

### section_id={right.section_id} (правий)
heading: {right_heading}
text:
{right.text}
"""


def call_mamay(prompt: str) -> tuple[str, float, str | None, int | None]:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "seed": 42,
        "top_k": 1,
        "samplers": ["top_k"],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    latency = time.monotonic() - start

    choice = body["choices"][0]
    raw_text = choice["message"]["content"]
    finish_reason = choice.get("finish_reason")
    completion_tokens = body.get("usage", {}).get("completion_tokens")
    return raw_text, latency, finish_reason, completion_tokens


class BoundaryValidationError(ValueError):
    pass


def validate_boundary(parsed) -> str:
    """Суворий валідатор -- РІВНО одне поле, значення лише з енуму."""
    if not isinstance(parsed, dict):
        raise BoundaryValidationError(f"top-level не dict: {type(parsed)}")
    if set(parsed.keys()) != {"boundary"}:
        raise BoundaryValidationError(f"очікувалось РІВНО одне поле 'boundary': {sorted(parsed.keys())}")
    value = parsed["boundary"]
    if value not in ("same", "new"):
        raise BoundaryValidationError(f"'boundary' має бути 'same' або 'new': {value!r}")
    return value


def classify_boundary(document_heading: str | None, left: Section, right: Section) -> tuple[str, dict]:
    prompt = build_boundary_prompt(document_heading, left, right)
    raw_text, latency, finish_reason, completion_tokens = call_mamay(prompt)

    content, fenced = strip_markdown_fence(raw_text)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise BoundaryValidationError(
            f"невалідний JSON після strip_markdown_fence(fenced={fenced}): {exc}\nraw={raw_text!r}"
        ) from exc

    try:
        label = validate_boundary(parsed)
    except BoundaryValidationError as exc:
        raise BoundaryValidationError(f"{exc}\nparsed={parsed!r}\nraw={raw_text!r}") from exc

    meta = {
        "latency_s": round(latency, 2), "finish_reason": finish_reason,
        "completion_tokens": completion_tokens, "fenced": fenced,
    }
    return label, meta


def assemble_segments(num_sections: int, labels: list[str]) -> list[tuple[int, int]]:
    """Детермінований збір segments із послідовності same/new-рішень.
    labels[i] -- межа МІЖ section i і section i+1."""
    if num_sections == 0:
        return []
    assert len(labels) == num_sections - 1, f"labels={len(labels)}, очікувалось {num_sections - 1}"

    segments: list[tuple[int, int]] = []
    start = 0
    for i, label in enumerate(labels):
        if label == "new":
            segments.append((start, i))
            start = i + 1
    segments.append((start, num_sections - 1))
    return segments


OCCURRENCE_IDS = {
    "brave1_digest": "ea29f3e7-b168-4f6c-955d-4ca67e2d970f",
    "single_topic": "a8698043-4665-4981-ba22-6c789ea62b90",
    "daily_frontline_summary": "a374a319-15ff-46cd-896a-7b6b81f8ca3e",
}


def run_self_test() -> int:
    """Офлайн перевірка validate_boundary() і assemble_segments() -- без мережі, без БД."""
    failed = 0

    ok_cases = [({"boundary": "same"}, "same"), ({"boundary": "new"}, "new")]
    for parsed, expected in ok_cases:
        try:
            got = validate_boundary(parsed)
            if got != expected:
                print(f"FAIL: {parsed} -> {got}, очікувалось {expected}")
                failed += 1
        except BoundaryValidationError as exc:
            print(f"FAIL (мало пройти): {parsed} -> {exc}")
            failed += 1

    bad_cases = [
        {},
        {"boundary": "same", "extra": 1},
        {"boundary": "maybe"},
        {"boundary": 123},
        {"Boundary": "same"},
        "not a dict",
        ["same"],
    ]
    for parsed in bad_cases:
        try:
            validate_boundary(parsed)
            print(f"FAIL (мало впасти): {parsed!r} -> пройшло без помилки")
            failed += 1
        except BoundaryValidationError:
            pass

    segment_cases = [
        (1, [], [(0, 0)]),
        (4, ["same", "new", "same"], [(0, 1), (2, 3)]),
        (3, ["new", "new"], [(0, 0), (1, 1), (2, 2)]),
        (3, ["same", "same"], [(0, 2)]),
    ]
    for num_sections, labels, expected in segment_cases:
        got = assemble_segments(num_sections, labels)
        if got != expected:
            print(f"FAIL: assemble_segments({num_sections}, {labels}) -> {got}, очікувалось {expected}")
            failed += 1

    total = len(ok_cases) + len(bad_cases) + len(segment_cases)
    print(f"self-test: {total - failed}/{total} passed")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                         help="офлайн-перевірка validate_boundary()/assemble_segments(), без мережі й без БД")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    import psycopg
    from psycopg.rows import dict_row
    from occurrence_content_worker import EXTRACTOR, EXTRACTOR_VERSION, EXTRACTION_PROFILE_VERSION

    def resolve_structured_content(conn, occurrence_id: str) -> str | None:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT structured_content
                FROM occurrence_content
                WHERE occurrence_id = %s
                  AND extractor = %s AND extractor_version = %s AND extraction_profile_version = %s
                  AND status = 'success'
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (occurrence_id, EXTRACTOR, EXTRACTOR_VERSION, EXTRACTION_PROFILE_VERSION),
            )
            row = cur.fetchone()
            return row["structured_content"] if row else None

    conn = psycopg.connect(DB_DSN)
    try:
        for label_name, occurrence_id in OCCURRENCE_IDS.items():
            print(f"=== {label_name} ({occurrence_id}) ===")
            structured_content = resolve_structured_content(conn, occurrence_id)
            if structured_content is None:
                print("  NOT FOUND: немає успішного occurrence_content.")
                continue

            result = sectionize(structured_content)
            sections = result.sections
            print(f"  document_heading={result.document_heading!r}")
            print(f"  sections={len(sections)}")

            labels: list[str] = []
            aborted = False
            for i in range(len(sections) - 1):
                left, right = sections[i], sections[i + 1]
                try:
                    boundary_label, meta = classify_boundary(result.document_heading, left, right)
                except BoundaryValidationError as exc:
                    print(f"  boundary {i}→{i + 1} FAILED: {exc}")
                    aborted = True
                    break
                labels.append(boundary_label)
                print(f"  boundary {i}→{i + 1} = {boundary_label}  (latency={meta['latency_s']}s)")

            if aborted:
                print("  resulting segments = ABORTED (validation failure)\n")
                continue

            segments = assemble_segments(len(sections), labels)
            print(f"  resulting segments = {segments}\n")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
