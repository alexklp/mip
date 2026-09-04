#!/usr/bin/env python3
"""
Point-run claim extraction for one routed analytical content segment.

Path:
content_segments
  -> segment_routing_decisions(analyze|maybe, routing_version=1)
  -> Mamay
  -> existing claim-extraction adapter/validator
  -> claim_extraction_runs(content_id + segment_id)
  -> claims

This is an additive pilot path only:
- no scheduler;
- no backlog scan;
- no parallel inference;
- no changes to the existing whole-content live worker.

Offsets in claims are relative to content_segments.text_content.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from uuid import UUID

import psycopg

CLAIM_DIR = Path(__file__).resolve().parents[1] / "claim_extraction"
sys.path.insert(0, str(CLAIM_DIR))

from run_eval import (  # noqa: E402
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    build_prompt,
    call_model,
    resolve_offsets,
    strip_markdown_fence,
)
from validator import validate_claim_extraction_response  # noqa: E402


DB_DSN = "dbname=mip_dev"

LLM_MODEL_ID = 1
PROMPT_ID = 1
ROUTING_VERSION = 1

EXPECTED_MODEL_NAME = "MamayLM-Gemma-3-27B-IT"
EXPECTED_MODEL_REVISION = "v2.0/Q4_K_M/7677fe7e2df2"
EXPECTED_PROMPT_NAME = "claim_extractor"
EXPECTED_PROMPT_VERSION = "v2"

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_FILE = CLAIM_DIR / "prompt_claim_extractor_v2.txt"

TIMEOUT = 600.0
MAX_TOKENS = 8192


def get_code_revision() -> str:
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
    ).decode().strip()

    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    return f"{sha}+dirty" if dirty else sha


def fetch_and_verify_registry(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT model_name, model_revision
            FROM llm_models
            WHERE llm_model_id = %s
            """,
            (LLM_MODEL_ID,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            f"llm_model_id={LLM_MODEL_ID} is not registered"
        )

    if tuple(row) != (
        EXPECTED_MODEL_NAME,
        EXPECTED_MODEL_REVISION,
    ):
        raise RuntimeError(
            f"llm model mismatch: db={tuple(row)}"
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT prompt_name, prompt_version, prompt_text
            FROM claim_extraction_prompts
            WHERE prompt_id = %s
            """,
            (PROMPT_ID,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            f"prompt_id={PROMPT_ID} is not registered"
        )

    prompt_name, prompt_version, prompt_text_db = row

    if (prompt_name, prompt_version) != (
        EXPECTED_PROMPT_NAME,
        EXPECTED_PROMPT_VERSION,
    ):
        raise RuntimeError(
            "claim prompt identity mismatch"
        )

    prompt_text_file = PROMPT_FILE.read_text(encoding="utf-8")

    if prompt_text_db != prompt_text_file:
        raise RuntimeError(
            "claim prompt text in DB does not match prompt file"
        )

    return prompt_text_db


def fetch_segment(conn, segment_id: UUID):
    """
    Resolve one analytical segment and its parent canonical content_id.

    Parent provenance path:
      content_segments
      -> segmentation_runs
      -> occurrence_content
      -> item_occurrences
      -> content_id

    Only current routing_version analyze/maybe is eligible.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cs.segment_id,
                io.content_id,
                cs.text_content,
                srd.decision,
                history.next_attempt_no,
                history.has_terminal
            FROM content_segments cs
            JOIN segmentation_runs sr
              ON sr.segmentation_run_id = cs.segmentation_run_id
            JOIN occurrence_content oc
              ON oc.occurrence_content_id = sr.occurrence_content_id
            JOIN item_occurrences io
              ON io.occurrence_id = oc.occurrence_id
            JOIN segment_routing_decisions srd
              ON srd.segment_id = cs.segment_id
             AND srd.routing_version = %s
             AND srd.decision IN ('analyze', 'maybe')
            CROSS JOIN LATERAL (
                SELECT
                    COALESCE(MAX(r.attempt_no), 0) + 1
                        AS next_attempt_no,
                    COALESCE(
                        BOOL_OR(r.status IN ('valid', 'invalid')),
                        false
                    ) AS has_terminal
                FROM claim_extraction_runs r
                WHERE r.segment_id = cs.segment_id
                  AND r.llm_model_id = %s
                  AND r.prompt_id = %s
            ) history
            WHERE cs.segment_id = %s
            """,
            (
                ROUTING_VERSION,
                LLM_MODEL_ID,
                PROMPT_ID,
                segment_id,
            ),
        )
        return cur.fetchone()


def insert_run(
    conn,
    *,
    content_id,
    segment_id,
    attempt_no,
    status,
    claim_count,
    errors,
    raw_response,
    code_revision,
    latency_ms,
) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO claim_extraction_runs (
                content_id,
                segment_id,
                llm_model_id,
                prompt_id,
                attempt_no,
                status,
                claim_count,
                errors,
                raw_response,
                code_revision,
                latency_ms
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            RETURNING run_id
            """,
            (
                content_id,
                segment_id,
                LLM_MODEL_ID,
                PROMPT_ID,
                attempt_no,
                status,
                claim_count,
                json.dumps(errors) if errors else None,
                raw_response,
                code_revision,
                latency_ms,
            ),
        )
        return cur.fetchone()[0]


def insert_claims(conn, run_id: UUID, claims: list[dict]) -> None:
    with conn.cursor() as cur:
        for claim in claims:
            cur.execute(
                """
                INSERT INTO claims (
                    run_id,
                    claim_local_id,
                    claim_text,
                    evidence_span,
                    evidence_start,
                    evidence_end,
                    epistemic_status,
                    claim_time_text,
                    attribution_text
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    run_id,
                    claim["claim_local_id"],
                    claim["claim_text"],
                    claim["evidence_span"],
                    claim["evidence_start"],
                    claim["evidence_end"],
                    claim["epistemic_status"],
                    claim.get("claim_time_text"),
                    claim.get("attribution_text"),
                ),
            )


def run(segment_id: UUID) -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        prompt_text = fetch_and_verify_registry(conn)
        row = fetch_segment(conn, segment_id)

    if row is None:
        raise RuntimeError(
            "segment not found or not routing-eligible "
            f"(routing_version={ROUTING_VERSION}, analyze|maybe)"
        )

    (
        db_segment_id,
        content_id,
        evidence_text,
        decision,
        attempt_no,
        has_terminal,
    ) = row

    if has_terminal:
        print(
            "[segment_claim_extract_worker] "
            "SKIP: current segment claim identity already terminal"
        )
        return 0

    evidence_id = str(db_segment_id)

    print(
        "[segment_claim_extract_worker] "
        f"segment_id={db_segment_id} "
        f"decision={decision} "
        f"attempt={attempt_no} "
        f"chars={len(evidence_text)} "
        f"code_revision={code_revision}"
    )

    prompt = build_prompt(
        prompt_text,
        evidence_id,
        evidence_text,
    )

    try:
        raw_text, latency, finish_reason, completion_tokens = call_model(
            DEFAULT_ENDPOINT,
            DEFAULT_MODEL,
            prompt,
            TIMEOUT,
            MAX_TOKENS,
        )
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        KeyError,
    ) as exc:
        error_msg = f"{type(exc).__name__}: {exc}"

        with psycopg.connect(DB_DSN) as conn:
            insert_run(
                conn,
                content_id=content_id,
                segment_id=db_segment_id,
                attempt_no=attempt_no,
                status="transport_error",
                claim_count=None,
                errors=[error_msg],
                raw_response=None,
                code_revision=code_revision,
                latency_ms=None,
            )
            conn.commit()

        print(
            "[segment_claim_extract_worker] "
            f"TRANSPORT_ERROR: {error_msg}",
            file=sys.stderr,
        )
        return 1

    normalized_text, fence_stripped = strip_markdown_fence(raw_text)

    offset_stats = None
    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        parsed, offset_stats = resolve_offsets(
            parsed,
            evidence_text,
        )
        text_to_validate = json.dumps(
            parsed,
            ensure_ascii=False,
        )
    else:
        text_to_validate = normalized_text

    validation = validate_claim_extraction_response(
        text_to_validate,
        evidence_id,
        evidence_text,
    )

    status = "valid" if validation.valid else "invalid"
    latency_ms = round(latency * 1000)

    with psycopg.connect(DB_DSN) as conn:
        run_id = insert_run(
            conn,
            content_id=content_id,
            segment_id=db_segment_id,
            attempt_no=attempt_no,
            status=status,
            claim_count=validation.claim_count,
            errors=validation.errors if validation.errors else None,
            raw_response=raw_text,
            code_revision=code_revision,
            latency_ms=latency_ms,
        )

        if status == "valid":
            insert_claims(
                conn,
                run_id,
                parsed["claims"],
            )

        conn.commit()

    print(
        "[segment_claim_extract_worker] "
        f"{status.upper()} "
        f"claims={validation.claim_count} "
        f"latency_ms={latency_ms} "
        f"finish_reason={finish_reason} "
        f"completion_tokens={completion_tokens} "
        f"fence_stripped={fence_stripped} "
        f"offset_stats={offset_stats}"
    )

    for error in validation.errors:
        print(f"  - {error}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Point-run claim extraction for one routed content segment"
    )
    parser.add_argument(
        "--segment-id",
        type=UUID,
        required=True,
    )
    args = parser.parse_args()

    return run(args.segment_id)


if __name__ == "__main__":
    raise SystemExit(main())
