#!/usr/bin/env python3
"""Minimal persistence worker for accepted content-segmentation v1.

Point-run only:
  occurrence_content(success)
    -> sectionize()
    -> Mamay adjacent-boundary same/new
    -> deterministic segment assembly
    -> segmentation_runs + content_segments

No scheduler, batch mode, backfill or downstream rewiring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REPO = Path.home() / "mip"
sys.path.insert(0, str(REPO / "experiments" / "occurrence_content"))
sys.path.insert(0, str(REPO / "experiments" / "content_segmentation"))

from occurrence_content_worker import (  # noqa: E402
    get_code_revision,
    resolve_current_occurrence_content,
)
from sectionize import Section, sectionize  # noqa: E402
from mamay_adjacent_boundary import (  # noqa: E402
    BoundaryValidationError,
    assemble_segments,
    call_mamay,
    strip_markdown_fence,
    validate_boundary,
)

DB_DSN = "dbname=mip_dev"

SECTIONIZER_VERSION = "v1"
BOUNDARY_METHOD = "mamay_adjacent_boundary"

LLM_MODEL_ID = 1
PROMPT_ID = 1

EXPECTED_PROMPT_NAME = "adjacent_boundary"
EXPECTED_PROMPT_VERSION = "v1"
EXPECTED_SCHEMA_VERSION = "adjacent-boundary/1"

TOKEN_RE = re.compile(
    r"<<("
    r"DOCUMENT_HEADING|"
    r"LEFT_SECTION_ID|LEFT_HEADING|LEFT_TEXT|"
    r"RIGHT_SECTION_ID|RIGHT_HEADING|RIGHT_TEXT"
    r")>>"
)


def load_source_and_prompt(occurrence_id: str):
    with psycopg.connect(DB_DSN) as conn:
        source = resolve_current_occurrence_content(conn, occurrence_id)
        if source is None:
            return None, None, False

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT prompt_id, prompt_name, prompt_version,
                       schema_version, prompt_text
                FROM segmentation_prompts
                WHERE prompt_id = %s
                """,
                (PROMPT_ID,),
            )
            prompt = cur.fetchone()

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM segmentation_runs
                    WHERE occurrence_content_id = %s
                      AND sectionizer_version = %s
                      AND boundary_method = %s
                      AND llm_model_id = %s
                      AND prompt_id = %s
                      AND status = 'success'
                ) AS already_success
                """,
                (
                    source["occurrence_content_id"],
                    SECTIONIZER_VERSION,
                    BOUNDARY_METHOD,
                    LLM_MODEL_ID,
                    PROMPT_ID,
                ),
            )
            already_success = cur.fetchone()["already_success"]

        return source, prompt, already_success


def validate_registry(prompt) -> None:
    if prompt is None:
        raise RuntimeError(f"segmentation prompt_id={PROMPT_ID} not found")

    expected = (
        EXPECTED_PROMPT_NAME,
        EXPECTED_PROMPT_VERSION,
        EXPECTED_SCHEMA_VERSION,
    )
    actual = (
        prompt["prompt_name"],
        prompt["prompt_version"],
        prompt["schema_version"],
    )
    if actual != expected:
        raise RuntimeError(
            f"unexpected segmentation prompt identity: {actual!r}, expected {expected!r}"
        )


def render_prompt(template: str, heading: str | None, left: Section, right: Section) -> str:
    values = {
        "DOCUMENT_HEADING": heading or "(без заголовка публікації)",
        "LEFT_SECTION_ID": str(left.section_id),
        "LEFT_HEADING": left.heading or "(без заголовка, преамбула)",
        "LEFT_TEXT": left.text,
        "RIGHT_SECTION_ID": str(right.section_id),
        "RIGHT_HEADING": right.heading or "(без заголовка, преамбула)",
        "RIGHT_TEXT": right.text,
    }

    # Regex replacement over the ORIGINAL template prevents placeholder-like
    # strings inside untrusted source text from being recursively substituted.
    return TOKEN_RE.sub(lambda m: values[m.group(1)], template)


def compute_next_attempt_no(conn, occurrence_content_id) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(attempt_no), 0) + 1
            FROM segmentation_runs
            WHERE occurrence_content_id = %s
              AND sectionizer_version = %s
              AND boundary_method = %s
              AND llm_model_id = %s
              AND prompt_id = %s
            """,
            (
                occurrence_content_id,
                SECTIONIZER_VERSION,
                BOUNDARY_METHOD,
                LLM_MODEL_ID,
                PROMPT_ID,
            ),
        )
        return cur.fetchone()[0]


def insert_run(
    conn,
    *,
    occurrence_content_id,
    attempt_no: int,
    status: str,
    section_count: int | None,
    segment_count: int | None,
    boundary_decisions: list,
    errors: list | None,
    code_revision: str,
    latency_ms: int,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO segmentation_runs (
                occurrence_content_id,
                sectionizer_version,
                boundary_method,
                llm_model_id,
                prompt_id,
                attempt_no,
                status,
                section_count,
                segment_count,
                boundary_decisions,
                errors,
                code_revision,
                latency_ms
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING segmentation_run_id
            """,
            (
                occurrence_content_id,
                SECTIONIZER_VERSION,
                BOUNDARY_METHOD,
                LLM_MODEL_ID,
                PROMPT_ID,
                attempt_no,
                status,
                section_count,
                segment_count,
                Jsonb(boundary_decisions),
                Jsonb(errors) if errors is not None else None,
                code_revision,
                latency_ms,
            ),
        )
        return cur.fetchone()[0]


def persist_error(
    *,
    occurrence_content_id,
    status: str,
    section_count: int | None,
    boundary_decisions: list,
    error: str,
    code_revision: str,
    latency_ms: int,
) -> None:
    with psycopg.connect(DB_DSN) as conn:
        attempt_no = compute_next_attempt_no(conn, occurrence_content_id)
        insert_run(
            conn,
            occurrence_content_id=occurrence_content_id,
            attempt_no=attempt_no,
            status=status,
            section_count=section_count,
            segment_count=None,
            boundary_decisions=boundary_decisions,
            errors=[error],
            code_revision=code_revision,
            latency_ms=latency_ms,
        )
        conn.commit()


def validate_partition(section_count: int, ranges: list[tuple[int, int]]) -> None:
    if section_count <= 0:
        raise ValueError("section_count must be > 0")
    if not ranges:
        raise ValueError("segment list is empty")

    previous_end = -1

    for segment_index, (start, end) in enumerate(ranges):
        if start < 0 or end < start or end >= section_count:
            raise ValueError(
                f"invalid segment[{segment_index}] range ({start}, {end}) "
                f"for section_count={section_count}"
            )

        expected_start = previous_end + 1
        if start != expected_start:
            raise ValueError(
                f"segment[{segment_index}] starts at {start}, expected {expected_start}"
            )

        previous_end = end

    if previous_end != section_count - 1:
        raise ValueError(
            f"partition ends at section {previous_end}, "
            f"expected {section_count - 1}"
        )


def build_segment_text(sections: tuple[Section, ...], start: int, end: int) -> str:
    parts: list[str] = []

    for section in sections[start : end + 1]:
        if section.heading:
            parts.append(section.heading.strip())
        if section.text and section.text.strip():
            parts.append(section.text.strip())

    return "\n\n".join(parts).strip()


def persist_success(
    *,
    occurrence_content_id,
    sections: tuple[Section, ...],
    ranges: list[tuple[int, int]],
    boundary_decisions: list,
    code_revision: str,
    latency_ms: int,
) -> str:
    validate_partition(len(sections), ranges)

    frozen_segments = []
    for segment_index, (start, end) in enumerate(ranges):
        text = build_segment_text(sections, start, end)
        if not text:
            raise ValueError(f"segment[{segment_index}] has empty text")

        frozen_segments.append(
            {
                "segment_index": segment_index,
                "section_start": start,
                "section_end": end,
                "text_content": text,
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )

    # Contract from sql/037: successful run + ALL children are one transaction.
    with psycopg.connect(DB_DSN) as conn:
        attempt_no = compute_next_attempt_no(conn, occurrence_content_id)

        run_id = insert_run(
            conn,
            occurrence_content_id=occurrence_content_id,
            attempt_no=attempt_no,
            status="success",
            section_count=len(sections),
            segment_count=len(frozen_segments),
            boundary_decisions=boundary_decisions,
            errors=None,
            code_revision=code_revision,
            latency_ms=latency_ms,
        )

        with conn.cursor() as cur:
            for segment in frozen_segments:
                cur.execute(
                    """
                    INSERT INTO content_segments (
                        segmentation_run_id,
                        segment_index,
                        section_start,
                        section_end,
                        text_content,
                        text_hash
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        segment["segment_index"],
                        segment["section_start"],
                        segment["section_end"],
                        segment["text_content"],
                        segment["text_hash"],
                    ),
                )

        conn.commit()

    return str(run_id)


def process_occurrence(occurrence_id: str) -> int:
    source, prompt, already_success = load_source_and_prompt(occurrence_id)

    if source is None:
        print("[segmentation_worker] no successful current occurrence_content")
        return 2

    validate_registry(prompt)

    if already_success:
        print("[segmentation_worker] SKIP: current segmentation identity already successful")
        return 0

    occurrence_content_id = source["occurrence_content_id"]
    code_revision = get_code_revision()
    started = time.monotonic()

    try:
        sectionized = sectionize(source["structured_content"])
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        persist_error(
            occurrence_content_id=occurrence_content_id,
            status="sectionize_error",
            section_count=None,
            boundary_decisions=[],
            error=f"{type(exc).__name__}: {exc}",
            code_revision=code_revision,
            latency_ms=latency_ms,
        )
        print(f"[segmentation_worker] sectionize_error: {exc}")
        return 1

    sections = sectionized.sections
    if not sections:
        latency_ms = round((time.monotonic() - started) * 1000)
        persist_error(
            occurrence_content_id=occurrence_content_id,
            status="sectionize_error",
            section_count=None,
            boundary_decisions=[],
            error="sectionize produced zero analytical sections",
            code_revision=code_revision,
            latency_ms=latency_ms,
        )
        print("[segmentation_worker] sectionize_error: zero sections")
        return 1

    labels: list[str] = []
    boundary_decisions: list[dict] = []

    for i in range(len(sections) - 1):
        left = sections[i]
        right = sections[i + 1]
        rendered_prompt = render_prompt(
            prompt["prompt_text"],
            sectionized.document_heading,
            left,
            right,
        )

        try:
            raw_text, boundary_latency_s, finish_reason, completion_tokens = call_mamay(
                rendered_prompt
            )
        except Exception as exc:
            boundary_decisions.append(
                {
                    "left_section_id": left.section_id,
                    "right_section_id": right.section_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            latency_ms = round((time.monotonic() - started) * 1000)
            persist_error(
                occurrence_content_id=occurrence_content_id,
                status="boundary_error",
                section_count=len(sections),
                boundary_decisions=boundary_decisions,
                error=f"{type(exc).__name__}: {exc}",
                code_revision=code_revision,
                latency_ms=latency_ms,
            )
            print(f"[segmentation_worker] boundary_error {i}->{i + 1}: {exc}")
            return 1

        content, fenced = strip_markdown_fence(raw_text)

        decision = {
            "left_section_id": left.section_id,
            "right_section_id": right.section_id,
            "raw_response": raw_text,
            "latency_ms": round(boundary_latency_s * 1000),
            "finish_reason": finish_reason,
            "completion_tokens": completion_tokens,
            "fenced": fenced,
        }

        try:
            parsed = json.loads(content)
            label = validate_boundary(parsed)
        except (json.JSONDecodeError, BoundaryValidationError) as exc:
            decision["error"] = f"{type(exc).__name__}: {exc}"
            boundary_decisions.append(decision)

            latency_ms = round((time.monotonic() - started) * 1000)
            persist_error(
                occurrence_content_id=occurrence_content_id,
                status="boundary_error",
                section_count=len(sections),
                boundary_decisions=boundary_decisions,
                error=f"{type(exc).__name__}: {exc}",
                code_revision=code_revision,
                latency_ms=latency_ms,
            )
            print(f"[segmentation_worker] boundary_error {i}->{i + 1}: {exc}")
            return 1

        decision["boundary"] = label
        boundary_decisions.append(decision)
        labels.append(label)

        print(
            f"[segmentation_worker] boundary {i}->{i + 1} = {label} "
            f"({decision['latency_ms']} ms)"
        )

    try:
        ranges = assemble_segments(len(sections), labels)
        validate_partition(len(sections), ranges)
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        persist_error(
            occurrence_content_id=occurrence_content_id,
            status="assembly_error",
            section_count=len(sections),
            boundary_decisions=boundary_decisions,
            error=f"{type(exc).__name__}: {exc}",
            code_revision=code_revision,
            latency_ms=latency_ms,
        )
        print(f"[segmentation_worker] assembly_error: {exc}")
        return 1

    latency_ms = round((time.monotonic() - started) * 1000)

    try:
        run_id = persist_success(
            occurrence_content_id=occurrence_content_id,
            sections=sections,
            ranges=ranges,
            boundary_decisions=boundary_decisions,
            code_revision=code_revision,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        # No success run survives if child insertion fails: transaction rolls back.
        print(f"[segmentation_worker] persistence_error: {type(exc).__name__}: {exc}")
        return 1

    print(
        f"[segmentation_worker] success run={run_id} "
        f"sections={len(sections)} segments={len(ranges)} ranges={ranges}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--occurrence-id",
        required=True,
        help="one item_occurrences.occurrence_id; no batch mode in v1 pilot",
    )
    args = parser.parse_args()
    return process_occurrence(args.occurrence_id)


if __name__ == "__main__":
    raise SystemExit(main())
