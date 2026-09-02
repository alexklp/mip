#!/usr/bin/env python3
"""
experiments/event_candidates/event_merge_worker.py -- LLM-based Event Merge
verification: event_merge_candidates (built by
event_merge_candidate_builder.py) -> event_merges (+ canonical_events /
canonical_event_members side-effects on same_event).

Той самий архітектурний паттерн, що event_verifier_worker.py: LLM-based,
per-candidate ізоляція, per-candidate транзакція, registry-verified prompt
(fail-fast, побайтова звірка тексту промпту з файлом на диску). Мінімальні
pure helpers (get_code_revision/build_context_snippet/fetch_claims_meta)
продубльовані, не імпортовані з інших worker-скриптів -- те саме рішення.

Ключове рішення (див. sql/018_add_event_merge.sql):
жодних connected components по merge edges автоматично. Eligibility (яка
"сторона" пари -- сирий seed чи вже canonical_event, і чи взагалі пара
eligible для LLM виклику ЦЬОГО run) ПЕРЕВІРЯЄТЬСЯ НАЖИВО перед КОЖНИМ
кандидатом (не з одного статичного batch-запиту на старті) -- бо canonical
membership може змінитися ВСЕРЕДИНІ цього самого run: same_event на
кандидаті N може створити/розширити canonical_event, від чого залежить
eligibility кандидата N+1. Це і є механізм, що забороняє ланцюжок
seed A<->seed B + seed B<->seed C => автоматичне A+B+C без окремої
перевірки C проти вже об'єднаного A+B.

Чотири стани пари (seed_a_id, seed_b_id) на момент обробки:
  1. жоден seed не в canonical_event   -> eligible: seed vs seed packet.
  2. рівно один seed вже в canonical   -> eligible: canonical packet
     (ПОВНИЙ, union усіх його seeds) vs "голий" seed packet.
  3. обидва в ОДНОМУ canonical_event   -> already_same_canonical: SKIP,
     жодного LLM виклику, жодного event_merges рядка -- лише SUMMARY-лічильник.
  4. обидва в РІЗНИХ canonical_events  -> deferred_both_canonical: SKIP,
     жодного LLM виклику, жодного event_merges рядка -- лише SUMMARY-лічильник.
     canonical<->canonical merge -- поза межами v1, окремий v2 pass.

different_event і insufficient зберігаються як валідні результати
(event_merges.status='valid'), НЕ відкидаються -- аналогічно rejected_seed
на попередньому шарі. same_event створює НОВИЙ canonical_event (якщо
жодна сторона ще не canonical) або РОЗШИРЮЄ існуючий (якщо одна зі сторін
вже canonical) -- у другому випадку canonical_event_summary та updated_at
оновлюються з merged_event_summary цього event_merges рядка.

event_merges.input_payload зберігає ТОЧНИЙ JSON packet, що реально пішов у
LLM (side_a+side_b snapshot на момент виклику) -- side_a_ref/side_b_ref
лишаються як легкі покажчики (kind+id), НЕ замінюють input_payload, бо
canonical_event packet змінюється у часі.

Порядок обробки v1: jsonb_array_length(shared_claim_ids) DESC, created_at,
merge_candidate_id -- спочатку пари з найбільшим overlap, детермінований
tiebreak.

sql/016_add_event_candidates.sql, sql/017_seed_event_verification_registry_v1.sql,
sql/018_add_event_merge.sql та sql/019_seed_event_merge_registry_v1.sql
МАЮТЬ бути застосовані ДО запуску.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "claim_extraction"))
from run_eval import DEFAULT_ENDPOINT, DEFAULT_MODEL, call_model, strip_markdown_fence  # noqa: E402

DB_DSN = "dbname=mip_dev"

LLM_MODEL_ID = 1
PROMPT_ID = 1
ATTEMPT_NO = 1

EXPECTED_MODEL_NAME = "MamayLM-Gemma-3-27B-IT"
EXPECTED_MODEL_REVISION = "v2.0/Q4_K_M/7677fe7e2df2"
EXPECTED_PROMPT_NAME = "event_merge"
EXPECTED_PROMPT_VERSION = "v1"

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_FILE = Path(__file__).parent / "event_merge_prompt_v1.txt"

TIMEOUT = 600.0
MAX_TOKENS = 1024  # decision + merged_event_summary + rationale_text -- невеликий payload
CONTEXT_WINDOW_CHARS = 300

ALLOWED_DECISIONS = {"same_event", "different_event", "insufficient"}


def get_code_revision() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


def fetch_and_verify_registry(conn) -> str:
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
            "SELECT prompt_name, prompt_version, prompt_text FROM event_merge_prompts WHERE prompt_id = %s",
            (PROMPT_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"prompt_id={PROMPT_ID} не зареєстровано в event_merge_prompts")
    prompt_name, prompt_version, prompt_text_db = row
    if (prompt_name, prompt_version) != (EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION):
        raise RuntimeError(
            f"event_merge_prompts mismatch: БД={(prompt_name, prompt_version)}, "
            f"очікується={(EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION)}"
        )
    prompt_text_file = PROMPT_FILE.read_text(encoding="utf-8")
    if prompt_text_db != prompt_text_file:
        raise RuntimeError(f"prompt_text в БД (prompt_id={PROMPT_ID}) НЕ збігається побайтово з {PROMPT_FILE}.")
    return prompt_text_db


def fetch_pending_candidates(conn, limit: int) -> list[tuple]:
    """merge_candidate(и), для яких ще немає спроби (llm_model_id, prompt_id,
    attempt_no) в event_merges. Порядок v1: jsonb_array_length(shared_claim_ids)
    DESC, created_at, merge_candidate_id. Це СИРИЙ список -- eligibility
    (already_same_canonical / deferred_both_canonical) перевіряється НАЖИВО
    в run(), не тут."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mc.merge_candidate_id, mc.seed_a_id, mc.seed_b_id, mc.shared_claim_ids
            FROM event_merge_candidates mc
            WHERE NOT EXISTS (
                SELECT 1 FROM event_merges m
                WHERE m.merge_candidate_id = mc.merge_candidate_id
                  AND m.llm_model_id = %s AND m.prompt_id = %s AND m.attempt_no = %s
            )
            ORDER BY jsonb_array_length(mc.shared_claim_ids) DESC, mc.created_at, mc.merge_candidate_id
            LIMIT %s
            """,
            (LLM_MODEL_ID, PROMPT_ID, ATTEMPT_NO, limit),
        )
        return cur.fetchall()


def get_canonical_membership(conn, seed_id) -> object | None:
    """seed_id (event_candidate_id) -> canonical_event_id, або None. Читається
    НАЖИВО безпосередньо перед обробкою кожного кандидата -- поточний run
    міг щойно розширити/створити canonical_event на попередньому кроці."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_event_id FROM canonical_event_members WHERE event_candidate_id = %s",
            (seed_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def build_context_snippet(text_content: str, evidence_start: int, evidence_end: int) -> str:
    start = max(0, evidence_start - CONTEXT_WINDOW_CHARS)
    end = min(len(text_content), evidence_end + CONTEXT_WINDOW_CHARS)
    return text_content[start:end]


def fetch_claims_meta(conn, claim_ids: list) -> dict:
    """claim_id -> dict(claim_text, evidence_span, title, context_snippet,
    source_name, source_type, contour_set, first_seen). Продубльовано з
    event_verifier_worker.py (за тим самим рішенням -- не ділити worker-и
    як library)."""
    if not claim_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.claim_id, c.claim_text, c.evidence_span, c.evidence_start, c.evidence_end,
                   r.content_id, ci.title, ci.text_content
            FROM claims c
            JOIN claim_extraction_runs r ON r.run_id = c.run_id
            JOIN content_items ci ON ci.content_id = r.content_id
            WHERE c.claim_id = ANY(%s)
            """,
            (claim_ids,),
        )
        claim_rows = cur.fetchall()

        content_ids = list({row[5] for row in claim_rows})
        cur.execute(
            """
            SELECT io.content_id, s.contour_id, io.collected_at, s.name, s.url_or_handle, s.source_type
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            WHERE io.content_id = ANY(%s)
            """,
            (content_ids,),
        )
        occ_rows = cur.fetchall()

    occ_by_content = {}
    for content_id, contour_id, collected_at, name, url_or_handle, source_type in occ_rows:
        occ_by_content.setdefault(content_id, []).append((contour_id, collected_at, name, url_or_handle, source_type))

    meta = {}
    for claim_id, claim_text, evidence_span, evidence_start, evidence_end, content_id, title, text_content in claim_rows:
        occs = occ_by_content.get(content_id, [])
        contour_ids = sorted({c for c, *_ in occs if c is not None})
        earliest = min(occs, key=lambda o: o[1]) if occs else None
        meta[claim_id] = {
            "claim_text": claim_text,
            "evidence_span": evidence_span,
            "title": title,
            "context_snippet": build_context_snippet(text_content, evidence_start, evidence_end),
            "contour_set": contour_ids,
            "first_seen": min((o[1] for o in occs), default=None),
            "source_name": earliest[2] if earliest else None,
            "source_type": earliest[4] if earliest else None,
        }
    return meta


def build_member_entry(claim_id, role, cm: dict) -> dict:
    return {
        "claim_id": str(claim_id),
        "role": role,
        "claim_text": cm["claim_text"],
        "evidence_span": cm["evidence_span"],
        "title": cm["title"],
        "context_snippet": cm["context_snippet"],
        "source_name": cm["source_name"],
        "source_type": cm["source_type"],
        "contour_set": cm["contour_set"],
        "observed_time": cm["first_seen"].isoformat() if cm["first_seen"] else None,
    }


def fetch_seed_included_claims(conn, seed_id) -> list:
    """(claim_id, role) для included=true членів ЄДИНОЇ valid accepted_seed
    verification цього seed_id. role -- з event_candidate_members (anchor/
    neighbor), лише інформативно."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT evm.claim_id
            FROM event_verifications ev
            JOIN event_verification_members evm ON evm.verification_id = ev.verification_id
            WHERE ev.event_candidate_id = %s AND ev.status = 'valid' AND ev.event_decision = 'accepted_seed'
              AND evm.included = true
            """,
            (seed_id,),
        )
        claim_ids = [row[0] for row in cur.fetchall()]
        cur.execute(
            "SELECT claim_id, role FROM event_candidate_members WHERE event_candidate_id = %s",
            (seed_id,),
        )
        role_by_claim = {row[0]: row[1] for row in cur.fetchall()}
    return [(cid, role_by_claim.get(cid)) for cid in claim_ids]


def build_side_payload_seed(conn, seed_id) -> dict:
    """kind='seed' packet: event_summary + members[] (included=true claims
    of seed_id's accepted_seed verification)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_summary FROM event_verifications
            WHERE event_candidate_id = %s AND status = 'valid' AND event_decision = 'accepted_seed'
            """,
            (seed_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"No valid accepted_seed verification found for event_candidate_id={seed_id}")
    event_summary = row[0]

    claim_roles = fetch_seed_included_claims(conn, seed_id)
    meta = fetch_claims_meta(conn, [cid for cid, _role in claim_roles])
    members = [build_member_entry(cid, role, meta[cid]) for cid, role in claim_roles]

    return {"kind": "seed", "id": str(seed_id), "summary": event_summary, "members": members}


def build_side_payload_canonical(conn, canonical_event_id) -> dict:
    """kind='canonical_event' packet: canonical_event_summary + members[]
    (UNION of included=true claims across ALL seeds already merged into it)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_event_summary FROM canonical_events WHERE canonical_event_id = %s",
            (canonical_event_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"canonical_event_id={canonical_event_id} not found")
    canonical_event_summary = row[0]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_candidate_id FROM canonical_event_members WHERE canonical_event_id = %s",
            (canonical_event_id,),
        )
        member_seed_ids = [r[0] for r in cur.fetchall()]

    role_by_claim: dict = {}
    all_claim_ids: set = set()
    for seed_id in member_seed_ids:
        for cid, role in fetch_seed_included_claims(conn, seed_id):
            all_claim_ids.add(cid)
            role_by_claim.setdefault(cid, role)

    claim_ids = sorted(all_claim_ids)
    meta = fetch_claims_meta(conn, claim_ids)
    members = [build_member_entry(cid, role_by_claim.get(cid), meta[cid]) for cid in claim_ids]

    return {
        "kind": "canonical_event",
        "id": str(canonical_event_id),
        "summary": canonical_event_summary,
        "members": members,
    }


def build_prompt_safe(template: str, payload: dict) -> str:
    return template.replace("<<PAYLOAD_JSON>>", json.dumps(payload, ensure_ascii=False, indent=2))


def validate_merge_decision(raw_text: str) -> tuple[bool, list[str], str | None, str | None, str | None, bool]:
    """Повертає (valid, errors, decision, merged_event_summary, rationale_text,
    fence_stripped). Контракт -- РІВНО 3 поля (decision, merged_event_summary,
    rationale_text), без schema_version/id echo -- так, як реально описано в
    event_merge_prompt_v1.txt."""
    errors: list[str] = []
    normalized_text, fence_stripped = strip_markdown_fence(raw_text)

    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError as exc:
        return False, [f"invalid JSON: {exc}"], None, None, None, fence_stripped

    if not isinstance(parsed, dict):
        return False, ["response is not a JSON object"], None, None, None, fence_stripped

    expected_keys = {"decision", "merged_event_summary", "rationale_text"}
    extra_keys = set(parsed.keys()) - expected_keys
    missing_keys = expected_keys - set(parsed.keys())
    if extra_keys:
        errors.append(f"unexpected extra keys: {sorted(extra_keys)}")
    if missing_keys:
        errors.append(f"missing required keys: {sorted(missing_keys)}")
    if missing_keys:
        return False, errors, None, None, None, fence_stripped

    decision = parsed.get("decision")
    if decision not in ALLOWED_DECISIONS:
        errors.append(f"decision not in allowed set: {decision!r}")
        decision = None

    merged_event_summary = parsed.get("merged_event_summary")
    if decision == "same_event":
        if not isinstance(merged_event_summary, str) or not merged_event_summary.strip():
            errors.append("merged_event_summary is required and must be non-empty when decision='same_event'")
            merged_event_summary = None
    elif decision in ("different_event", "insufficient"):
        if merged_event_summary is not None:
            errors.append(f"merged_event_summary must be null when decision={decision!r}, got {merged_event_summary!r}")
            merged_event_summary = None

    rationale_text = parsed.get("rationale_text")
    if not isinstance(rationale_text, str) or not rationale_text.strip():
        errors.append("rationale_text is missing or empty")
        rationale_text = None

    valid = not errors
    return (
        valid,
        errors,
        decision if valid else None,
        merged_event_summary if valid else None,
        rationale_text if valid else None,
        fence_stripped,
    )


def insert_merge(
    conn, merge_candidate_id, status: str, decision, merged_event_summary, rationale_text,
    input_payload: dict, side_a_ref: dict, side_b_ref: dict,
    errors: list[str] | None, raw_response: str | None, code_revision: str, latency_ms: int | None,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event_merges
                (merge_candidate_id, llm_model_id, prompt_id, attempt_no, status,
                 decision, merged_event_summary, rationale_text, input_payload,
                 side_a_ref, side_b_ref, errors, raw_response, code_revision, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING merge_id
            """,
            (
                merge_candidate_id, LLM_MODEL_ID, PROMPT_ID, ATTEMPT_NO, status,
                decision, merged_event_summary, rationale_text, Jsonb(input_payload),
                Jsonb(side_a_ref), Jsonb(side_b_ref),
                Jsonb(errors) if errors else None, raw_response, code_revision, latency_ms,
            ),
        )
        return cur.fetchone()[0]


def create_canonical_event(conn, merge_id, merged_event_summary: str, code_revision: str, seed_a_id, seed_b_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO canonical_events (canonical_event_summary, founding_merge_id, code_revision)
            VALUES (%s, %s, %s)
            RETURNING canonical_event_id
            """,
            (merged_event_summary, merge_id, code_revision),
        )
        canonical_event_id = cur.fetchone()[0]
        for seed_id in (seed_a_id, seed_b_id):
            cur.execute(
                """
                INSERT INTO canonical_event_members (canonical_event_id, event_candidate_id, added_via_merge_id)
                VALUES (%s, %s, %s)
                """,
                (canonical_event_id, seed_id, merge_id),
            )
    return canonical_event_id


def extend_canonical_event(conn, canonical_event_id, merge_id, merged_event_summary: str, new_seed_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE canonical_events
            SET canonical_event_summary = %s, updated_at = now()
            WHERE canonical_event_id = %s
            """,
            (merged_event_summary, canonical_event_id),
        )
        cur.execute(
            """
            INSERT INTO canonical_event_members (canonical_event_id, event_candidate_id, added_via_merge_id)
            VALUES (%s, %s, %s)
            """,
            (canonical_event_id, new_seed_id, merge_id),
        )


def process_candidate(conn, merge_candidate_id, seed_a_id, seed_b_id, prompt_text: str, code_revision: str) -> str:
    """Повертає один з: 'same_event_new', 'same_event_extend', 'different_event',
    'insufficient', 'invalid', 'transport_error', 'error',
    'already_same_canonical', 'deferred_both_canonical' (останні два -- SKIP,
    без event_merges рядка)."""
    canonical_a = get_canonical_membership(conn, seed_a_id)
    canonical_b = get_canonical_membership(conn, seed_b_id)

    if canonical_a is not None and canonical_b is not None:
        if canonical_a == canonical_b:
            print(f"[{merge_candidate_id}] SKIP already_same_canonical ({canonical_a})")
            return "already_same_canonical"
        print(f"[{merge_candidate_id}] SKIP deferred_both_canonical ({canonical_a} vs {canonical_b})")
        return "deferred_both_canonical"

    # рівно одна сторона canonical, або жодна -- eligible
    extend_target = None
    new_seed_for_extend = None
    if canonical_a is not None:
        side_a = build_side_payload_canonical(conn, canonical_a)
        side_b = build_side_payload_seed(conn, seed_b_id)
        extend_target, new_seed_for_extend = canonical_a, seed_b_id
    elif canonical_b is not None:
        side_a = build_side_payload_seed(conn, seed_a_id)
        side_b = build_side_payload_canonical(conn, canonical_b)
        extend_target, new_seed_for_extend = canonical_b, seed_a_id
    else:
        side_a = build_side_payload_seed(conn, seed_a_id)
        side_b = build_side_payload_seed(conn, seed_b_id)

    side_a_ref = {"kind": side_a["kind"], "id": side_a["id"]}
    side_b_ref = {"kind": side_b["kind"], "id": side_b["id"]}
    input_payload = {"side_a": side_a, "side_b": side_b}

    prompt = build_prompt_safe(prompt_text, input_payload)

    try:
        raw_text, latency, finish_reason, completion_tokens = call_model(
            DEFAULT_ENDPOINT, DEFAULT_MODEL, prompt, TIMEOUT, MAX_TOKENS
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError) as e:
        error_msg = f"{type(e).__name__}: {e}"
        print(f"[{merge_candidate_id}] TRANSPORT ERROR: {error_msg}", file=sys.stderr)
        try:
            insert_merge(
                conn, merge_candidate_id, status="transport_error",
                decision=None, merged_event_summary=None, rationale_text=None,
                input_payload=input_payload, side_a_ref=side_a_ref, side_b_ref=side_b_ref,
                errors=[error_msg], raw_response=None, code_revision=code_revision, latency_ms=None,
            )
            conn.commit()
        except Exception as db_err:
            conn.rollback()
            print(f"[{merge_candidate_id}] DB ERROR while recording transport_error: {db_err}", file=sys.stderr)
            return "error"
        return "transport_error"

    valid, errors, decision, merged_event_summary, rationale_text, fence_stripped = validate_merge_decision(raw_text)
    status = "valid" if valid else "invalid"
    latency_ms = round(latency * 1000)

    print(
        f"[{merge_candidate_id}] {status.upper()} decision={decision} "
        f"({side_a_ref['kind']}:{side_a_ref['id']} vs {side_b_ref['kind']}:{side_b_ref['id']}) "
        f"fence_stripped={fence_stripped} {latency:.1f}s"
    )
    for err in errors:
        print(f"    - {err}")

    try:
        merge_id = insert_merge(
            conn, merge_candidate_id, status=status,
            decision=decision, merged_event_summary=merged_event_summary, rationale_text=rationale_text,
            input_payload=input_payload, side_a_ref=side_a_ref, side_b_ref=side_b_ref,
            errors=errors if errors else None, raw_response=raw_text,
            code_revision=code_revision, latency_ms=latency_ms,
        )
        if status == "valid" and decision == "same_event":
            if extend_target is not None:
                extend_canonical_event(conn, extend_target, merge_id, merged_event_summary, new_seed_for_extend)
                conn.commit()
                return "same_event_extend"
            create_canonical_event(conn, merge_id, merged_event_summary, code_revision, seed_a_id, seed_b_id)
            conn.commit()
            return "same_event_new"
        conn.commit()
    except Exception as db_err:
        conn.rollback()
        print(f"[{merge_candidate_id}] DB ERROR while persisting: {db_err}", file=sys.stderr)
        return "error"

    return status if status != "valid" else decision


def run(limit: int) -> int:
    code_revision = get_code_revision()
    summary: dict = {}

    with psycopg.connect(DB_DSN) as conn:
        prompt_text = fetch_and_verify_registry(conn)
        batch = fetch_pending_candidates(conn, limit)
        print(f"batch size: {len(batch)}")
        if not batch:
            print("SUMMARY: nothing to process (backlog empty)")
            return 0

        for merge_candidate_id, seed_a_id, seed_b_id, _shared_claim_ids in batch:
            outcome = process_candidate(conn, merge_candidate_id, seed_a_id, seed_b_id, prompt_text, code_revision)
            summary[outcome] = summary.get(outcome, 0) + 1

        print(f"\nSUMMARY: total={len(batch)} " + " ".join(f"{k}={v}" for k, v in summary.items()))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM Event Merge verification worker (v1)")
    parser.add_argument("--limit", type=int, required=True, help="max merge candidates to examine this run")
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    return run(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
