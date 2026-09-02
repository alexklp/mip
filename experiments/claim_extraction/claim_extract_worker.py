#!/usr/bin/env python3
"""
experiments/claim_extraction/claim_extract_worker.py — мінімальний batch worker
поверх вже підтвердженого persist_single_run.py path.

Шлях (без змін проти pilot): content_items -> Mamay (call_model) -> existing
deterministic adapter (strip_markdown_fence, resolve_offsets з run_eval.py) ->
existing validator (validate_claim_extraction_response з validator.py) ->
claim_extraction_runs (+ claims, якщо valid).

Відмінності від persist_single_run.py (single item):
  1. Вибір content_id: SQL-вибірка з terminal eligibility:
     valid/invalid завершують item, transport_error лишається retryable
     з наступним attempt_no.
  2. Per-item ізоляція помилок: одна "погана" item (transport error чи
     неочікуваний виняток) НЕ валить весь batch. embed_worker.py навмисно
     робить навпаки (одна помилка -> rollback усього batch, sys.exit) —
     тут це свідомий відхід, бо inference на одну статтю значно дорожчий
     і довший за embedding, втрачати весь --limit N через одну статтю
     нераціонально.
  3. Транзакція — per-item (commit/rollback на кожному content_id окремо),
     а не одна транзакція на весь batch.
  4. code_revision тепер чесно відбиває dirty working tree (git status
     --porcelain), а не тільки short SHA.
  5. Лог: по кожному item + summary наприкінці batch.

Без змін: run_eval.py, validator.py, persist_single_run.py. Без queue/broker,
без parallel inference, без prompt v3.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import psycopg

from run_eval import DEFAULT_ENDPOINT, DEFAULT_MODEL, build_prompt, call_model, resolve_offsets, strip_markdown_fence
from validator import validate_claim_extraction_response

DB_DSN = "dbname=mip_dev"

LLM_MODEL_ID = 1
PROMPT_ID = 1
ROUTING_VERSION = 1

# Очікувана identity — звіряється з БД, не встановлюється звідси.
EXPECTED_MODEL_NAME = "MamayLM-Gemma-3-27B-IT"
EXPECTED_MODEL_REVISION = "v2.0/Q4_K_M/7677fe7e2df2"
EXPECTED_PROMPT_NAME = "claim_extractor"
EXPECTED_PROMPT_VERSION = "v2"

REPO_ROOT = Path(__file__).resolve().parents[2]  # experiments/claim_extraction/../.. = ~/mip
PROMPT_FILE = Path(__file__).parent / "prompt_claim_extractor_v2.txt"

TIMEOUT = 600.0
MAX_TOKENS = 8192


def get_code_revision() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


def fetch_and_verify_registry(conn) -> str:
    """Повертає prompt_text з БД. Fail-fast при розбіжності з очікуваною identity
    (включно з побайтовою звіркою тексту промпту проти файлу на диску)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT model_name, model_revision FROM llm_models WHERE llm_model_id = %s",
            (LLM_MODEL_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"llm_model_id={LLM_MODEL_ID} не зареєстровано в llm_models")
    if tuple(row) != (EXPECTED_MODEL_NAME, EXPECTED_MODEL_REVISION):
        raise RuntimeError(f"llm_models mismatch: БД={tuple(row)}, очікується={(EXPECTED_MODEL_NAME, EXPECTED_MODEL_REVISION)}")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT prompt_name, prompt_version, prompt_text FROM claim_extraction_prompts WHERE prompt_id = %s",
            (PROMPT_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"prompt_id={PROMPT_ID} не зареєстровано в claim_extraction_prompts")
    prompt_name, prompt_version, prompt_text_db = row
    if (prompt_name, prompt_version) != (EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION):
        raise RuntimeError(f"claim_extraction_prompts mismatch: БД={(prompt_name, prompt_version)}, очікується={(EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION)}")
    prompt_text_file = PROMPT_FILE.read_text(encoding="utf-8")
    if prompt_text_db != prompt_text_file:
        raise RuntimeError(
            f"prompt_text в БД (prompt_id={PROMPT_ID}) НЕ збігається побайтово з {PROMPT_FILE}."
        )
    return prompt_text_db


def fetch_batch(
    conn,
    limit: int,
    contour_id: int | None,
    decision: str | None,
    order: str,
) -> list[tuple[str, str, int]]:
    """Idempotent eligibility: content з occurrence, routing-eligible
    (analyze/maybe для поточної routing_version), без terminal run
    (valid/invalid) для поточних llm_model_id/prompt_id. transport_error
    залишається retryable з наступним attempt_no. Той самий паттерн, що
    fetch_batch() в collectors/embed_worker.py.

    contour_id (опційно): фільтр по sources.contour_id через item_occurrences,
    ordering по найсвіжішому occurrence САМЕ в межах цього контуру (не
    first_seen_at і не найсвіжіший occurrence взагалі, якщо у content є
    occurrences з інших контурів теж). Base FROM лишається content_items —
    contour-фільтр і ordering через EXISTS/corelated subquery, без JOIN-
    фанауту, тому один content_id ніколи не дублюється в batch навіть при
    кількох occurrences в межах контуру."""
    if order not in {"oldest", "newest"}:
        raise ValueError(f"unsupported order: {order}")

    order_sql = "ASC" if order == "oldest" else "DESC"

    if contour_id is None:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    ci.content_id,
                    ci.text_content,
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
                    FROM claim_extraction_runs r
                    WHERE r.content_id = ci.content_id
                      AND r.llm_model_id = %s
                      AND r.prompt_id = %s
                ) history
                WHERE EXISTS (
                    SELECT 1 FROM item_occurrences io WHERE io.content_id = ci.content_id
                )
                AND EXISTS (
                    SELECT 1 FROM content_routing_decisions cr
                    WHERE cr.content_id = ci.content_id
                      AND cr.routing_version = %s
                      AND cr.decision IN ('analyze', 'maybe')
                      AND (%s::text IS NULL OR cr.decision = %s::text)
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

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                ci.content_id,
                ci.text_content,
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
                FROM claim_extraction_runs r
                WHERE r.content_id = ci.content_id
                  AND r.llm_model_id = %s
                  AND r.prompt_id = %s
            ) history
            WHERE EXISTS (
                SELECT 1 FROM item_occurrences io WHERE io.content_id = ci.content_id
            )
            AND EXISTS (
                SELECT 1 FROM content_routing_decisions cr
                WHERE cr.content_id = ci.content_id
                  AND cr.routing_version = %s
                  AND cr.decision IN ('analyze', 'maybe')
                  AND (%s::text IS NULL OR cr.decision = %s::text)
            )
            AND NOT history.has_terminal
            AND EXISTS (
                SELECT 1 FROM item_occurrences io
                JOIN sources s ON s.source_id = io.source_id
                WHERE io.content_id = ci.content_id AND s.contour_id = %s
            )
            ORDER BY (
                SELECT max(io.collected_at)
                FROM item_occurrences io
                JOIN sources s ON s.source_id = io.source_id
                WHERE io.content_id = ci.content_id AND s.contour_id = %s
            ) {order_sql}, ci.content_id {order_sql}
            LIMIT %s
            """,
            (
                LLM_MODEL_ID,
                PROMPT_ID,
                ROUTING_VERSION,
                decision,
                decision,
                contour_id,
                contour_id,
                limit,
            ),
        )
        return cur.fetchall()


def insert_run(
    conn,
    *,
    content_id,
    attempt_no,
    status,
    claim_count,
    errors,
    raw_response,
    code_revision,
    latency_ms,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO claim_extraction_runs
                (content_id, llm_model_id, prompt_id, attempt_no, status,
                 claim_count, errors, raw_response, code_revision, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING run_id
            """,
            (
                content_id, LLM_MODEL_ID, PROMPT_ID, attempt_no, status,
                claim_count, json.dumps(errors) if errors else None, raw_response,
                code_revision, latency_ms,
            ),
        )
        return cur.fetchone()[0]


def insert_claims(conn, run_id: str, claims: list[dict]) -> None:
    with conn.cursor() as cur:
        for claim in claims:
            cur.execute(
                """
                INSERT INTO claims
                    (run_id, claim_local_id, claim_text, evidence_span,
                     evidence_start, evidence_end, epistemic_status,
                     claim_time_text, attribution_text)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id, claim["claim_local_id"], claim["claim_text"], claim["evidence_span"],
                    claim["evidence_start"], claim["evidence_end"], claim["epistemic_status"],
                    claim.get("claim_time_text"), claim.get("attribution_text"),
                ),
            )


def process_one(
    content_id: str,
    evidence_text: str,
    prompt_text: str,
    code_revision: str,
    attempt_no: int,
) -> str:
    """Обробляє один content_id. Транзакція per-item: commit/rollback тут,
    виняток НЕ пробрасується нагору — повертає статус для логу/summary,
    щоб один поганий item не валив batch."""
    prompt = build_prompt(prompt_text, content_id, evidence_text)

    try:
        raw_text, latency, finish_reason, completion_tokens = call_model(
            DEFAULT_ENDPOINT, DEFAULT_MODEL, prompt, TIMEOUT, MAX_TOKENS
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError) as e:
        error_msg = f"{type(e).__name__}: {e}"
        print(
            f"[{content_id}] TRANSPORT ERROR "
            f"attempt={attempt_no}: {error_msg}",
            file=sys.stderr,
        )
        db_conn = None
        try:
            db_conn = psycopg.connect(DB_DSN)
            insert_run(
                db_conn,
                content_id=content_id,
                attempt_no=attempt_no,
                status="transport_error",
                claim_count=None, errors=[error_msg], raw_response=None,
                code_revision=code_revision, latency_ms=None,
            )
            db_conn.commit()
        except Exception as db_err:
            print(
                f"[{content_id}] DB ERROR while recording transport_error: "
                f"{type(db_err).__name__}: {db_err}",
                file=sys.stderr,
            )
            return "error"
        finally:
            if db_conn is not None:
                try:
                    db_conn.close()
                except Exception:
                    pass
        return "transport_error"

    # той самий orchestration, що persist_single_run.py
    normalized_text, fence_stripped = strip_markdown_fence(raw_text)
    offset_stats = None
    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        parsed, offset_stats = resolve_offsets(parsed, evidence_text)
        text_to_validate = json.dumps(parsed, ensure_ascii=False)
    else:
        text_to_validate = normalized_text
    validation = validate_claim_extraction_response(text_to_validate, content_id, evidence_text)

    status = "valid" if validation.valid else "invalid"
    latency_ms = round(latency * 1000)
    print(
        f"[{content_id}] {status.upper()} "
        f"attempt={attempt_no} — {validation.claim_count} claims, "
        f"{latency:.1f}s, fence_stripped={fence_stripped}, offset_stats={offset_stats}"
    )
    for err in validation.errors:
        print(f"    - {err}")

    db_conn = None
    try:
        db_conn = psycopg.connect(DB_DSN)
        run_id = insert_run(
            db_conn,
            content_id=content_id,
            attempt_no=attempt_no,
            status=status,
            claim_count=validation.claim_count,
            errors=validation.errors if validation.errors else None,
            raw_response=raw_text, code_revision=code_revision, latency_ms=latency_ms,
        )
        if status == "valid":
            insert_claims(db_conn, run_id, parsed["claims"])
        db_conn.commit()
    except Exception as db_err:
        print(
            f"[{content_id}] DB ERROR while persisting: "
            f"{type(db_err).__name__}: {db_err}",
            file=sys.stderr,
        )
        return "error"
    finally:
        if db_conn is not None:
            try:
                db_conn.close()
            except Exception:
                pass

    return status


def run(
    limit: int,
    contour_id: int | None,
    decision: str | None,
    order: str,
) -> int:
    code_revision = get_code_revision()

    summary = {"valid": 0, "invalid": 0, "transport_error": 0, "error": 0}

    with psycopg.connect(DB_DSN) as conn:
        prompt_text = fetch_and_verify_registry(conn)
        batch = fetch_batch(conn, limit, contour_id, decision, order)

    print(
        f"batch: {len(batch)} content_id(s) eligible "
        f"(limit={limit}, contour_id={contour_id}, "
        f"decision={decision}, order={order}), "
        f"code_revision={code_revision}"
    )

    for content_id, evidence_text, attempt_no in batch:
        outcome = process_one(
            str(content_id),
            evidence_text,
            prompt_text,
            code_revision,
            attempt_no,
        )
        summary[outcome] = summary.get(outcome, 0) + 1

    total = sum(summary.values())
    print(
        f"SUMMARY: total={total} valid={summary['valid']} invalid={summary['invalid']} "
        f"transport_error={summary['transport_error']} error={summary['error']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch claim-extraction worker (llm_model_id=1, prompt_id=1)")
    parser.add_argument("--limit", type=int, required=True, help="max content_items to process this run")
    parser.add_argument(
        "--contour-id",
        type=int,
        default=None,
        help="optional: restrict to sources.contour_id",
    )
    parser.add_argument("--decision", choices=("analyze", "maybe"), default=None,
                         help="optional: restrict routing decision; default keeps analyze+maybe")
    parser.add_argument(
        "--order",
        choices=("oldest", "newest"),
        default=None,
        help=(
            "eligible content ordering; default preserves legacy behavior: "
            "oldest without --contour-id, newest with --contour-id"
        ),
    )
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    if args.contour_id is not None and not (1 <= args.contour_id <= 4):
        parser.error("--contour-id must be between 1 and 4")
    order = args.order
    if order is None:
        order = "newest" if args.contour_id is not None else "oldest"

    return run(args.limit, args.contour_id, args.decision, order)


if __name__ == "__main__":
    raise SystemExit(main())
