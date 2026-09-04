#!/usr/bin/env python3
"""
mamay_group_sections.py — pilot: групує детерміновано побудовані sections
(sectionize.py) у segments через Mamay.

Mamay НЕ генерує і НЕ переписує текст — повертає ЛИШЕ групування за
section_id (+ optional exclude_section_ids для явного боілерплейту).
Уся валідація групування — code-owned, моделі на слово не віримо
(проєктна конвенція: LLM віддає candidate, код рахує рішення).

Без БД-записів. Мережа: один виклик на документ до живого Mamay endpoint.
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
# має працювати без БД і без залежностей воркера, лише sectionize (без DB deps).
from sectionize import sectionize, Section  # noqa: E402

DB_DSN = "dbname=mip_dev"

# Підтверджений живий endpoint (03.09.2026). 8000 з claude/03_MIP_Server_State
# застарілий, не використовувати.
ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf"

PROMPT_NAME = "section_grouping"
PROMPT_VERSION = "v1"
SCHEMA_VERSION = "section-grouping/1"

MAX_TOKENS = 1024
TIMEOUT = 120.0

# response_format=json_object на цьому білді підтверджено no-op
# (experiments/claim_extraction/README.md) -- НЕ додаємо його в payload,
# щоб не створювати хибне відчуття гарантії. call_model-контракт тут той
# самий, що в llm_pilot_v1/run_eval.py / claim_extraction/run_eval.py.
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def strip_markdown_fence(raw_text: str) -> tuple[str, bool]:
    """Той самий контракт, що experiments/claim_extraction/run_eval.py:
    знімає огорожу ЛИШЕ якщо вона покриває ВЕСЬ рядок; інакше не чіпає
    (це вже інша проблема, ховати її не можна)."""
    m = FENCE_RE.match(raw_text)
    if m:
        return m.group(1), True
    return raw_text, False


def build_prompt(sections: tuple[Section, ...]) -> str:
    items = []
    for s in sections:
        heading = s.heading if s.heading is not None else "(без заголовка)"
        items.append(f"### section_id={s.section_id}\nheading: {heading}\ntext:\n{s.text}\n")
    sections_block = "\n".join(items)

    return f"""Тобі надано послідовність структурних розділів (sections) ОДНІЄЇ статті,
у порядку появи в тексті. Кожен розділ вже виділений детермінованим кодом
(не тобою) за структурою HTML (h2-заголовки).

ТВОЄ ЄДИНЕ ЗАВДАННЯ: об'єднати СУСІДНІ розділи, які описують один і той
самий смисловий фрагмент (наприклад: лід/дек статті технічно позначений
як окремий h2, хоча смислово належить до наступного розділу; або кілька
h2 продовжують одну думку).

СУВОРІ ПРАВИЛА:
- Ти НЕ переписуєш, НЕ скорочуєш, НЕ переказуєш і НЕ генеруєш жоден текст.
- Ти повертаєш ЛИШЕ групування за section_id: список segments, кожен --
  об'єкт {{"section_start": ціле число, "section_end": ціле число}} --
  обидва мають бути РЕАЛЬНИМИ section_id зі списку "Розділи" нижче (не
  вигаданими), section_start <= section_end.
- Кожен section_id зі списку нижче має входити РІВНО в один результат:
  або в один segment-діапазон, або в exclude_section_ids (лише для
  явного боілерплейту/шуму — рідкісний випадок, не використовуй без
  причини).
- segments мають бути відсортовані за section_start і НЕ перетинатись.
- КІЛЬКІСТЬ segments і межі кожного залежать ВИКЛЮЧНО від реального
  змісту розділів нижче. У списку розділів може бути будь-яка кількість
  section_id (2, 3, 5, 8 -- будь-яка) -- рахуй і групуй саме ту
  кількість, що фактично надана, нічого не копіюй і не вгадуй за
  аналогією.
- Відповідь — СУВОРО один JSON-об'єкт з двома полями (segments,
  exclude_section_ids), без жодного тексту до чи після, без
  markdown-огорожі, без коментарів у JSON.

Розділи:

{sections_block}
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


class GroupingValidationError(ValueError):
    pass


def validate_grouping(parsed: dict, num_sections: int) -> dict:
    """Суворий, code-owned валідатор. Кожне порушення -- explicit
    GroupingValidationError, жодного мовчазного fallback."""
    if not isinstance(parsed, dict):
        raise GroupingValidationError(f"top-level не dict: {type(parsed)}")

    segments = parsed.get("segments")
    if not isinstance(segments, list) or not segments:
        raise GroupingValidationError(f"'segments' відсутній або порожній: {segments!r}")

    exclude_ids = parsed.get("exclude_section_ids", [])
    if not isinstance(exclude_ids, list):
        raise GroupingValidationError(f"'exclude_section_ids' не list: {exclude_ids!r}")
    for eid in exclude_ids:
        if not isinstance(eid, int) or not (0 <= eid < num_sections):
            raise GroupingValidationError(f"exclude_section_ids містить невалідний id: {eid!r}")
    if len(set(exclude_ids)) != len(exclude_ids):
        raise GroupingValidationError(f"exclude_section_ids має дублікати: {exclude_ids}")
    excluded = set(exclude_ids)

    parsed_segments = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise GroupingValidationError(f"segments[{i}] не dict: {seg!r}")
        start, end = seg.get("section_start"), seg.get("section_end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise GroupingValidationError(f"segments[{i}] section_start/section_end не int: {seg!r}")
        if not (0 <= start <= end < num_sections):
            raise GroupingValidationError(
                f"segments[{i}] діапазон поза межами [0,{num_sections - 1}] або start>end: {seg!r}"
            )
        parsed_segments.append((start, end))

    if parsed_segments != sorted(parsed_segments):
        raise GroupingValidationError(f"segments не відсортовані за section_start: {parsed_segments}")

    assigned: set[int] = set()
    for start, end in parsed_segments:
        for sid in range(start, end + 1):
            if sid in excluded:
                raise GroupingValidationError(
                    f"section_id={sid} одночасно в segment ({start},{end}) і в exclude_section_ids"
                )
            if sid in assigned:
                raise GroupingValidationError(f"section_id={sid} потрапляє у два segments (перетин)")
            assigned.add(sid)

    full = set(range(num_sections))
    covered = assigned | excluded
    if covered != full:
        missing = sorted(full - covered)
        raise GroupingValidationError(f"section_id не враховані ЖОДНИМ шляхом: {missing}")

    return {
        "segments": [{"section_start": s, "section_end": e} for s, e in parsed_segments],
        "exclude_section_ids": sorted(excluded),
    }


def group_sections(sections: tuple[Section, ...]) -> dict:
    if not sections:
        return {"segments": [], "exclude_section_ids": []}
    if len(sections) == 1:
        return {"segments": [{"section_start": 0, "section_end": 0}], "exclude_section_ids": []}

    prompt = build_prompt(sections)
    raw_text, latency, finish_reason, completion_tokens = call_mamay(prompt)

    content, fenced = strip_markdown_fence(raw_text)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise GroupingValidationError(
            f"не валідний JSON після strip_markdown_fence(fenced={fenced}): {exc}\nraw={raw_text!r}"
        ) from exc

    try:
        result = validate_grouping(parsed, num_sections=len(sections))
    except GroupingValidationError as exc:
        raise GroupingValidationError(
            f"{exc}\nparsed={parsed!r}\nraw={raw_text!r}"
        ) from exc
    result["_meta"] = {
        "latency_s": round(latency, 2), "finish_reason": finish_reason,
        "completion_tokens": completion_tokens, "fenced": fenced,
        "model": MODEL, "prompt_name": PROMPT_NAME,
        "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION,
    }
    return result


OCCURRENCE_IDS = {
    "brave1_digest": "ea29f3e7-b168-4f6c-955d-4ca67e2d970f",
    "single_topic": "a8698043-4665-4981-ba22-6c789ea62b90",
    "daily_frontline_summary": "a374a319-15ff-46cd-896a-7b6b81f8ca3e",
}


def run_self_test() -> int:
    """Офлайн перевірка validate_grouping() -- без мережі, без БД."""
    ok_cases = [
        ({"segments": [{"section_start": 0, "section_end": 2}, {"section_start": 3, "section_end": 3}]}, 4),
        ({"segments": [{"section_start": 0, "section_end": 0}]}, 1),
        ({"segments": [{"section_start": 0, "section_end": 0}, {"section_start": 2, "section_end": 3}],
          "exclude_section_ids": [1]}, 4),
    ]
    bad_cases = [
        ({"segments": [{"section_start": 0, "section_end": 5}]}, 4),
        ({"segments": [{"section_start": 1, "section_end": 0}]}, 4),
        ({"segments": [{"section_start": 0, "section_end": 1}, {"section_start": 1, "section_end": 2}]}, 4),
        ({"segments": [{"section_start": 0, "section_end": 1}]}, 4),
        ({"segments": [{"section_start": 0, "section_end": 1}], "exclude_section_ids": [1, 2, 3]}, 4),
    ]

    failed = 0
    for parsed, n in ok_cases:
        try:
            validate_grouping(parsed, n)
        except GroupingValidationError as exc:
            print(f"FAIL (мало пройти): {parsed} / n={n} -> {exc}")
            failed += 1
    for parsed, n in bad_cases:
        try:
            validate_grouping(parsed, n)
            print(f"FAIL (мало впасти): {parsed} / n={n} -> пройшло без помилки")
            failed += 1
        except GroupingValidationError:
            pass

    total = len(ok_cases) + len(bad_cases)
    print(f"self-test: {total - failed}/{total} passed")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                         help="офлайн-перевірка validate_grouping(), без мережі й без БД")
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
        for label, occurrence_id in OCCURRENCE_IDS.items():
            print(f"=== {label} ({occurrence_id}) ===")
            structured_content = resolve_structured_content(conn, occurrence_id)
            if structured_content is None:
                print("  NOT FOUND: немає успішного occurrence_content.")
                continue

            result = sectionize(structured_content)
            sections = result.sections
            print(f"  sections={len(sections)}")

            try:
                grouping = group_sections(sections)
            except GroupingValidationError as exc:
                print(f"  GROUPING FAILED: {exc}")
                continue

            meta = grouping.pop("_meta", {})
            print(f"  meta={meta}")
            print(f"  segments={len(grouping['segments'])} exclude={grouping['exclude_section_ids']}")
            for seg in grouping["segments"]:
                lo, hi = seg["section_start"], seg["section_end"]
                headings = [sections[i].heading or "(preamble)" for i in range(lo, hi + 1)]
                print(f"    [{lo}-{hi}] -> {headings}")
            print()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
