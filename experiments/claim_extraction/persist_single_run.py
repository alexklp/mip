#!/usr/bin/env python3
"""
Мінімальний persisted claim-extraction прогін одного реального content_item.

content_items
  -> Mamay
  -> deterministic adapter
  -> validator
  -> claim_extraction_runs
  -> claims (тільки якщо весь run valid)

Без batch, queue, retry або prompt tuning.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from run_eval import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    build_prompt,
    call_model,
    resolve_offsets,
    strip_markdown_fence,
)
from validator import validate_claim_extraction_response


DB_DSN = "dbname=mip_dev"

CONTENT_ID = "4c472a60-b9fe-4e55-90b0-109f909b6e3e"
LLM_MODEL_ID = 1
PROMPT_ID = 1
ATTEMPT_NO = 1

EXPECTED_MODEL_NAME = "MamayLM-Gemma-3-27B-IT"
EXPECTED_MODEL_REVISION = "v2.0/Q4_K_M/7677fe7e2df2"
EXPECTED_PROMPT_NAME = "claim_extractor"
EXPECTED_PROMPT_VERSION = "v2"

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_FILE = Path(__file__).parent / "prompt_claim_extractor_v2.txt"

TIMEOUT = 600.0
MAX_TOKENS = 8192


def get_code_revision() -> str:
    head = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
    ).decode().strip()

    status = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "experiments/claim_extraction/persist_single_run.py",
            "experiments/claim_extraction/run_eval.py",
            "experiments/claim_extraction/validator.py",
        ],
        cwd=REPO_ROOT,
    ).decode().strip()

    return f"{head}+dirty" if status else head


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
            f"llm_model_id={LLM_MODEL_ID} не зареєстровано в llm_models"
        )

    if tuple(row) != (EXPECTED_MODEL_NAME, EXPECTED_MODEL_REVISION):
        raise RuntimeError(
            f"llm_models mismatch: БД={tuple(row)}, "
            f"очікується={(EXPECTED_MODEL_NAME, EXPECTED_MODEL_REVISION)}"
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
            f"prompt_id={PROMPT_ID} не зареєстровано "
            f"в claim_extraction_prompts"
        )

    prompt_name, prompt_version, prompt_text_db = row

    if (prompt_name, prompt_version) != (
        EXPECTED_PROMPT_NAME,
        EXPECTED_PROMPT_VERSION,
    ):
        raise RuntimeError(
            "claim_extraction_prompts mismatch: "
            f"БД={(prompt_name, prompt_version)}, "
            f"очікується={(EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION)}"
        )

    prompt_text_file = PROMPT_FILE.read_text(encoding="utf-8")

    if prompt_text_db != prompt_text_file:
        raise RuntimeError(
            f"prompt_text в БД (prompt_id={PROMPT_ID}) "
            f"НЕ збігається з {PROMPT_FILE}"
        )

    return prompt_text_db


def fetch_content(conn, content_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT text_content
            FROM content_items
            WHERE content_id = %s
            """,
            (content_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            f"content_id={content_id} не знайдено в content_items"
        )

    return row[0]


def check_not_already_run(conn, content_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, status
            FROM claim_extraction_runs
            WHERE content_id = %s
              AND llm_model_id = %s
              AND prompt_id = %s
              AND attempt_no = %s
            """,
            (
                content_id,
                LLM_MODEL_ID,
                PROMPT_ID,
                ATTEMPT_NO,
            ),
        )
        row = cur.fetchone()

    if row is not None:
        raise RuntimeError(
            f"run для content_id={content_id}/"
            f"llm_model_id={LLM_MODEL_ID}/"
            f"prompt_id={PROMPT_ID}/"
            f"attempt_no={ATTEMPT_NO} вже існує "
            f"(run_id={row[0]}, status={row[1]})"
        )


def insert_run(
    conn,
    *,
    content_id,
    status,
    claim_count,
    errors,
    raw_response,
    code_revision,
    latency_ms,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO claim_extraction_runs
                (
                    content_id,
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
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING run_id
            """,
            (
                content_id,
                LLM_MODEL_ID,
                PROMPT_ID,
                ATTEMPT_NO,
                status,
                claim_count,
                Jsonb(errors) if errors is not None else None,
                raw_response,
                code_revision,
                latency_ms,
            ),
        )

        return cur.fetchone()[0]


def insert_claims(conn, run_id, claims: list[dict]) -> None:
    with conn.cursor() as cur:
        for claim in claims:
            cur.execute(
                """
                INSERT INTO claims
                    (
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
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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


def run() -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        # Read-only preflight.
        prompt_text = fetch_and_verify_registry(conn)
        check_not_already_run(conn, CONTENT_ID)
        evidence_text = fetch_content(conn, CONTENT_ID)

        # Не тримаємо DB-транзакцію відкритою, поки Mamay думає.
        conn.commit()

        prompt = build_prompt(
            prompt_text,
            CONTENT_ID,
            evidence_text,
        )

        try:
            (
                raw_text,
                latency,
                finish_reason,
                completion_tokens,
            ) = call_model(
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

            print(
                f"[{CONTENT_ID}] TRANSPORT ERROR: {error_msg}",
                file=sys.stderr,
            )

            try:
                run_id = insert_run(
                    conn,
                    content_id=CONTENT_ID,
                    status="transport_error",
                    claim_count=None,
                    errors=[error_msg],
                    raw_response=None,
                    code_revision=code_revision,
                    latency_ms=None,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            print(
                f"run_id={run_id} written "
                f"(status=transport_error)"
            )
            return 1

        normalized_text, fence_stripped = strip_markdown_fence(
            raw_text
        )

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
            CONTENT_ID,
            evidence_text,
        )

        status = (
            "valid"
            if validation.valid
            else "invalid"
        )

        latency_ms = round(latency * 1000)

        print(
            f"[{CONTENT_ID}] {status.upper()} — "
            f"{validation.claim_count} claims, "
            f"{latency:.1f}s, "
            f"finish_reason={finish_reason}, "
            f"completion_tokens={completion_tokens}, "
            f"fence_stripped={fence_stripped}, "
            f"offset_stats={offset_stats}"
        )

        for err in validation.errors:
            print(f"    - {err}")

        try:
            run_id = insert_run(
                conn,
                content_id=CONTENT_ID,
                status=status,
                claim_count=validation.claim_count,
                errors=(
                    validation.errors
                    if validation.errors
                    else None
                ),
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

        except Exception:
            conn.rollback()
            raise

        print(
            f"run_id={run_id} written "
            f"(status={status})"
        )

        return 0


if __name__ == "__main__":
    raise SystemExit(run())
