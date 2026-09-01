#!/usr/bin/env python3
"""
Batch Mamay worker for strategic content-contour classification.

Pipeline:
    routing-eligible content_items
    -> Mamay contour-classification/1
    -> schema validation
    -> deterministic C4 provenance gate
    -> contour_classification_runs
    -> positive effective assignments in content_contour_assignments

Contract:
    - multi-label C1..C4;
    - only effective decision=yes is materialized as an assignment;
    - LLM assignments are candidate;
    - C4 requires at least one occurrence from sources.contour_id=4;
    - C4 provenance eligibility is necessary, not sufficient;
    - raw model response remains preserved in contour_classification_runs;
    - deterministic/existing assignments win scope conflicts via
      ON CONFLICT DO NOTHING;
    - one failed item does not abort the whole batch.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


DB_DSN = "dbname=mip_dev"

LLM_MODEL_ID = 1
PROMPT_ID = 1
ROUTING_VERSION = 1
ASSIGNMENT_VERSION = 1

# Facets not yet calibrated well enough for automatic materialization.
SUPPRESSED_C1_FACETS = {"movement_opsec", "personnel"}

EXPECTED_MODEL_NAME = "MamayLM-Gemma-3-27B-IT"
EXPECTED_MODEL_REVISION = "v2.0/Q4_K_M/7677fe7e2df2"

EXPECTED_PROMPT_NAME = "contour_classifier"
EXPECTED_PROMPT_VERSION = "v1"
EXPECTED_SCHEMA_VERSION = "contour-classification/1"

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_DIR = Path(__file__).parent / "llm_pilot_v1"
PROMPT_FILE = PILOT_DIR / "prompt_v1.txt"
PILOT_RUN_EVAL = PILOT_DIR / "run_eval.py"

TIMEOUT = 600.0
MAX_TOKENS = 8192


def _load_pilot_module():
    """Reuse the accepted pilot inference + validator implementation."""
    spec = importlib.util.spec_from_file_location(
        "_mip_contour_pilot_run_eval",
        PILOT_RUN_EVAL,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load pilot module: {PILOT_RUN_EVAL}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PILOT = _load_pilot_module()

call_model = PILOT.call_model
validate_result = PILOT.validate_result
strip_markdown_fence = PILOT.strip_markdown_fence

ENDPOINT = PILOT.ENDPOINT
MODEL = PILOT.MODEL


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
    """Return prompt text and fail fast on registry/file identity drift."""
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
            f"llm_model_id={LLM_MODEL_ID} not registered"
        )

    if tuple(row) != (
        EXPECTED_MODEL_NAME,
        EXPECTED_MODEL_REVISION,
    ):
        raise RuntimeError(
            "llm_models mismatch: "
            f"DB={tuple(row)}, "
            f"expected={(EXPECTED_MODEL_NAME, EXPECTED_MODEL_REVISION)}"
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                prompt_name,
                prompt_version,
                schema_version,
                prompt_text
            FROM contour_classification_prompts
            WHERE prompt_id = %s
            """,
            (PROMPT_ID,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            f"prompt_id={PROMPT_ID} not registered"
        )

    (
        prompt_name,
        prompt_version,
        schema_version,
        prompt_text_db,
    ) = row

    expected_identity = (
        EXPECTED_PROMPT_NAME,
        EXPECTED_PROMPT_VERSION,
        EXPECTED_SCHEMA_VERSION,
    )

    actual_identity = (
        prompt_name,
        prompt_version,
        schema_version,
    )

    if actual_identity != expected_identity:
        raise RuntimeError(
            "contour_classification_prompts mismatch: "
            f"DB={actual_identity}, expected={expected_identity}"
        )

    prompt_text_file = PROMPT_FILE.read_text(encoding="utf-8")

    if prompt_text_db != prompt_text_file:
        raise RuntimeError(
            f"prompt_text in DB (prompt_id={PROMPT_ID}) "
            f"does not byte-match {PROMPT_FILE}"
        )

    return prompt_text_db


def load_catalog(conn) -> tuple[list[dict], dict[int, set[str]]]:
    """Same catalog contract as accepted pilot."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                monitoring_contour_id,
                code,
                name,
                description
            FROM monitoring_contours
            WHERE active
            ORDER BY monitoring_contour_id
            """
        )
        contours = cur.fetchall()

        cur.execute(
            """
            SELECT
                monitoring_contour_id,
                facet_code,
                reference_text
            FROM contour_reference_entries
            WHERE active
              AND facet_code IS NOT NULL
            ORDER BY
                monitoring_contour_id,
                facet_code,
                reference_id
            """
        )
        refs = cur.fetchall()

    facets: dict[int, dict[str, list[str]]] = {}

    for contour_id, facet_code, reference_text in refs:
        facets.setdefault(
            int(contour_id),
            {},
        ).setdefault(
            facet_code,
            [],
        ).append(reference_text)

    catalog = [
        {
            "contour_id": int(contour_id),
            "code": code,
            "name": name,
            "description": description,
            "facets": facets.get(int(contour_id), {}),
        }
        for contour_id, code, name, description in contours
    ]

    allowed_facets = {
        int(item["contour_id"]): set(item["facets"].keys())
        for item in catalog
    }

    return catalog, allowed_facets


def build_prompt(
    prompt_text: str,
    *,
    catalog: list[dict],
    content_id: str,
    title: str | None,
    text_content: str,
) -> str:
    payload = {
        "evidence_id": content_id,
        "title": title,
        "text": text_content,
    }

    replacements = {
        "<<CONTOUR_CATALOG_JSON>>": json.dumps(
            catalog,
            ensure_ascii=False,
            indent=2,
        ),
        "<<PAYLOAD_JSON>>": json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        "<<EVIDENCE_ID>>": content_id,
        "<<CONTENT_ID>>": content_id,
    }

    result = prompt_text

    for token, value in replacements.items():
        result = result.replace(token, value)

    unresolved = sorted(
        set(re.findall(r"<<[A-Z0-9_]+>>", result))
    )

    if unresolved:
        raise RuntimeError(
            f"unresolved prompt placeholders: {unresolved}"
        )

    return result


def fetch_batch(
    conn,
    limit: int,
    decision: str | None,
    order: str,
) -> list[tuple]:
    """
    Idempotent eligibility:
      - content has at least one occurrence;
      - routing_version=1 and decision analyze/maybe;
      - no existing contour classification run for the same
        model/prompt/attempt;
      - C4 eligibility is calculated from provenance, not source_group labels.
    """
    if order not in {"oldest", "newest"}:
        raise ValueError(f"unsupported order: {order}")

    order_sql = "ASC" if order == "oldest" else "DESC"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                ci.content_id,
                ci.title,
                ci.text_content,
                EXISTS (
                    SELECT 1
                    FROM item_occurrences io
                    JOIN sources s
                      ON s.source_id = io.source_id
                    WHERE io.content_id = ci.content_id
                      AND s.contour_id = 4
                ) AS c4_eligible,
                history.next_attempt_no
            FROM content_items ci
            CROSS JOIN LATERAL (
                SELECT
                    COALESCE(MAX(r.attempt_no), 0) + 1
                        AS next_attempt_no,
                    COALESCE(
                        BOOL_OR(r.status IN ('valid', 'invalid')),
                        false
                    ) AS has_terminal
                FROM contour_classification_runs r
                WHERE r.content_id = ci.content_id
                  AND r.llm_model_id = %s
                  AND r.prompt_id = %s
            ) history
            WHERE EXISTS (
                SELECT 1
                FROM item_occurrences io
                WHERE io.content_id = ci.content_id
            )
            AND EXISTS (
                SELECT 1
                FROM content_routing_decisions cr
                WHERE cr.content_id = ci.content_id
                  AND cr.routing_version = %s
                  AND cr.decision IN ('analyze', 'maybe')
                  AND (
                      %s::text IS NULL
                      OR cr.decision = %s::text
                  )
            )
            AND NOT history.has_terminal
            ORDER BY ci.first_seen_at {order_sql}, ci.content_id {order_sql}
            LIMIT %s
            """,
            (
                LLM_MODEL_ID,
                PROMPT_ID,
                ROUTING_VERSION,
                decision,
                decision,
                limit,
            ),
        )
        return cur.fetchall()


def apply_c4_gate(
    parsed: dict,
    *,
    c4_eligible: bool,
) -> tuple[dict, bool]:
    """
    Return effective response.

    C4 provenance gate:
      - eligible=true does NOT make C4 positive;
      - eligible=false forces effective C4=no.
    """
    effective = copy.deepcopy(parsed)
    overridden = False

    if c4_eligible:
        return effective, overridden

    c4 = next(
        d
        for d in effective["decisions"]
        if d["contour_id"] == 4
    )

    overridden = (
        c4.get("decision") != "no"
        or bool(c4.get("facet_codes"))
        or c4.get("evidence_span") is not None
    )

    c4["decision"] = "no"
    c4["facet_codes"] = []
    c4["evidence_span"] = None
    c4["reason"] = (
        "Deterministic C4 provenance gate: content has no "
        "item_occurrence from a source with sources.contour_id=4."
    )

    return effective, overridden



def apply_uncalibrated_facet_gate(
    effective: dict,
) -> list[str]:
    """Suppress uncalibrated C1 facets while preserving C1 decision."""
    c1 = next(
        d
        for d in effective["decisions"]
        if d["contour_id"] == 1
    )

    original = list(c1.get("facet_codes", []))
    suppressed = [
        facet
        for facet in original
        if facet in SUPPRESSED_C1_FACETS
    ]

    if not suppressed:
        return []

    c1["facet_codes"] = [
        facet
        for facet in original
        if facet not in SUPPRESSED_C1_FACETS
    ]

    c1["reason"] = (
        c1["reason"]
        + " Deterministic facet gate: "
        + ", ".join(sorted(suppressed))
        + " suppressed from effective classification pending calibration."
    )

    return suppressed


def insert_run(
    conn,
    *,
    content_id: str,
    attempt_no: int,
    status: str,
    errors: list[str] | None,
    raw_response: str | None,
    effective_response: dict | None,
    c4_eligible: bool,
    c4_overridden: bool,
    finish_reason: str | None,
    completion_tokens: int | None,
    code_revision: str,
    latency_ms: int | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO contour_classification_runs (
                content_id,
                llm_model_id,
                prompt_id,
                attempt_no,
                status,
                errors,
                raw_response,
                effective_response,
                c4_eligible,
                c4_overridden,
                finish_reason,
                completion_tokens,
                code_revision,
                latency_ms
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s
            )
            RETURNING run_id
            """,
            (
                content_id,
                LLM_MODEL_ID,
                PROMPT_ID,
                attempt_no,
                status,
                Jsonb(errors) if errors is not None else None,
                raw_response,
                (
                    Jsonb(effective_response)
                    if effective_response is not None
                    else None
                ),
                c4_eligible,
                c4_overridden,
                finish_reason,
                completion_tokens,
                code_revision,
                latency_ms,
            ),
        )
        return str(cur.fetchone()[0])


def insert_assignments(
    conn,
    *,
    content_id: str,
    run_id: str,
    effective_response: dict,
    code_revision: str,
) -> int:
    inserted = 0

    with conn.cursor() as cur:
        for decision in effective_response["decisions"]:
            if decision["decision"] != "yes":
                continue

            facet_codes = decision["facet_codes"]

            # Positive contour without a facet is still an assignment.
            scopes = facet_codes if facet_codes else [None]

            # Avoid duplicate facet rows from a malformed-but-schema-valid
            # repeated facet list.
            scopes = list(dict.fromkeys(scopes))

            for facet_code in scopes:
                cur.execute(
                    """
                    INSERT INTO content_contour_assignments (
                        content_id,
                        monitoring_contour_id,
                        assignment_version,
                        status,
                        object_id,
                        facet_code,
                        evidence_type,
                        reference_id,
                        embedding_model_id,
                        rule_code,
                        score,
                        reason,
                        code_revision,
                        classification_run_id
                    )
                    VALUES (
                        %s, %s, %s,
                        'candidate',
                        NULL,
                        %s,
                        'llm_classification',
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        %s,
                        %s,
                        %s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        content_id,
                        decision["contour_id"],
                        ASSIGNMENT_VERSION,
                        facet_code,
                        decision["reason"],
                        code_revision,
                        run_id,
                    ),
                )
                inserted += cur.rowcount

    return inserted


def process_one(
    conn,
    *,
    content_id: str,
    attempt_no: int,
    title: str | None,
    text_content: str,
    c4_eligible: bool,
    prompt_text: str,
    catalog: list[dict],
    allowed_facets: dict[int, set[str]],
    code_revision: str,
) -> str:
    prompt = build_prompt(
        prompt_text,
        catalog=catalog,
        content_id=content_id,
        title=title,
        text_content=text_content,
    )

    # Validation accepts evidence from either title or body.
    source_text = (
        f"{title}\n{text_content}"
        if title
        else text_content
    )

    try:
        (
            raw_text,
            latency,
            finish_reason,
            completion_tokens,
        ) = call_model(
            ENDPOINT,
            MODEL,
            prompt,
            TIMEOUT,
            MAX_TOKENS,
        )
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        error_msg = f"{type(exc).__name__}: {exc}"

        print(
            f"[{content_id}] TRANSPORT ERROR "
            f"attempt={attempt_no}: {error_msg}",
            file=sys.stderr,
        )

        try:
            insert_run(
                conn,
                content_id=content_id,
                attempt_no=attempt_no,
                status="transport_error",
                errors=[error_msg],
                raw_response=None,
                effective_response=None,
                c4_eligible=c4_eligible,
                c4_overridden=False,
                finish_reason=None,
                completion_tokens=None,
                code_revision=code_revision,
                latency_ms=None,
            )
            conn.commit()
        except Exception as db_exc:
            conn.rollback()
            print(
                f"[{content_id}] DB ERROR while recording "
                f"transport_error: {db_exc}",
                file=sys.stderr,
            )
            return "error"

        return "transport_error"

    latency_ms = round(latency * 1000)

    normalized_text, fence_stripped = strip_markdown_fence(
        raw_text
    )

    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError:
        parsed = None

    model_errors = validate_result(
        parsed,
        content_id,
        source_text,
        allowed_facets,
    )

    effective_response = None
    c4_overridden = False
    suppressed_facets: list[str] = []
    errors = list(model_errors)

    if not model_errors:
        effective_response, c4_overridden = apply_c4_gate(
            parsed,
            c4_eligible=c4_eligible,
        )

        suppressed_facets = apply_uncalibrated_facet_gate(
            effective_response
        )

        effective_errors = validate_result(
            effective_response,
            content_id,
            source_text,
            allowed_facets,
        )

        errors.extend(
            f"effective: {error}"
            for error in effective_errors
        )

    status = "valid" if not errors else "invalid"

    print(
        f"[{content_id}] {status.upper()} "
        f"attempt={attempt_no} "
        f"{latency:.1f}s "
        f"c4_eligible={c4_eligible} "
        f"c4_overridden={c4_overridden} "
        f"facet_suppressed={suppressed_facets} "
        f"fence_stripped={fence_stripped}"
    )

    for error in errors:
        print(f"    - {error}")

    try:
        run_id = insert_run(
            conn,
            content_id=content_id,
            attempt_no=attempt_no,
            status=status,
            errors=errors if errors else None,
            raw_response=raw_text,
            effective_response=effective_response,
            c4_eligible=c4_eligible,
            c4_overridden=c4_overridden,
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
            code_revision=code_revision,
            latency_ms=latency_ms,
        )

        assignment_count = 0

        if status == "valid":
            assignment_count = insert_assignments(
                conn,
                content_id=content_id,
                run_id=run_id,
                effective_response=effective_response,
                code_revision=code_revision,
            )

        conn.commit()

    except Exception as db_exc:
        conn.rollback()
        print(
            f"[{content_id}] DB ERROR while persisting: {db_exc}",
            file=sys.stderr,
        )
        return "error"

    if status == "valid":
        positive = [
            d["contour_id"]
            for d in effective_response["decisions"]
            if d["decision"] == "yes"
        ]

        print(
            f"    effective_yes={positive} "
            f"assignments_inserted={assignment_count}"
        )

    return status


def run(
    limit: int,
    decision: str | None,
    order: str,
) -> int:
    code_revision = get_code_revision()

    summary = {
        "valid": 0,
        "invalid": 0,
        "transport_error": 0,
        "error": 0,
    }

    with psycopg.connect(DB_DSN) as conn:
        prompt_text = fetch_and_verify_registry(conn)
        catalog, allowed_facets = load_catalog(conn)
        batch = fetch_batch(conn, limit, decision, order)

        print(
            f"batch: {len(batch)} content_id(s) eligible "
            f"(limit={limit}, decision={decision}, order={order}), "
            f"code_revision={code_revision}"
        )

        for (
            content_id,
            title,
            text_content,
            c4_eligible,
            attempt_no,
        ) in batch:
            try:
                outcome = process_one(
                    conn,
                    content_id=str(content_id),
                    attempt_no=attempt_no,
                    title=title,
                    text_content=text_content,
                    c4_eligible=bool(c4_eligible),
                    prompt_text=prompt_text,
                    catalog=catalog,
                    allowed_facets=allowed_facets,
                    code_revision=code_revision,
                )
            except Exception as exc:
                conn.rollback()
                print(
                    f"[{content_id}] ERROR: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                outcome = "error"

            summary[outcome] = summary.get(outcome, 0) + 1

    total = sum(summary.values())

    print(
        "SUMMARY: "
        f"total={total} "
        f"valid={summary['valid']} "
        f"invalid={summary['invalid']} "
        f"transport_error={summary['transport_error']} "
        f"error={summary['error']}"
    )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Batch Mamay strategic content-contour classifier "
            "(model_id=1, prompt_id=1)"
        )
    )

    parser.add_argument(
        "--limit",
        type=int,
        required=True,
        help="maximum content_items to process",
    )

    parser.add_argument(
        "--decision",
        choices=("analyze", "maybe"),
        default=None,
        help=(
            "optional routing decision filter; "
            "default processes analyze+maybe"
        ),
    )

    parser.add_argument(
        "--order",
        choices=("oldest", "newest"),
        default="oldest",
        help=(
            "eligible content ordering by first_seen_at; "
            "default: oldest"
        ),
    )

    args = parser.parse_args()

    if args.limit <= 0:
        parser.error("--limit must be > 0")

    return run(args.limit, args.decision, args.order)


if __name__ == "__main__":
    raise SystemExit(main())
