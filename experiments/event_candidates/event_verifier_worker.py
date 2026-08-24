#!/usr/bin/env python3
"""
experiments/event_candidates/event_verifier_worker.py -- LLM-based event
candidate verification: event_candidates (built by event_candidate_builder.py)
-> event_verifications + event_verification_members.

Той самий архітектурний паттерн, що relation_judgment_worker.py /
claim_extract_worker.py: LLM-based, per-item ізоляція, per-item транзакція,
registry-verified prompt (fail-fast, побайтова звірка тексту промпту з
файлом на диску), claim-дані -- недовірені зовнішні дані, підставляються в
промпт ОДНИМ json.dumps()-блоком.

За рішенням (не importувати helpers з ІНШОГО виконуваного worker-скрипта --
relation_judgment_worker.py): get_code_revision/build_context_snippet/
fetch_claims_meta/build_prompt_safe тут ПРОДУБЛЬОВАНІ як мінімальні pure
helpers, а не імпортовані. Спільний модуль між relation_judgment_worker.py
та цим скриптом винесемо пізніше, якщо дублювання реально почне заважати --
не наперед. call_model/strip_markdown_fence/DEFAULT_ENDPOINT/DEFAULT_MODEL
й далі reuse з run_eval.py -- це вже усталена спільна інфраструктура
(claim_extract_worker.py, relation_judgment_worker.py, цей скрипт), а не
per-vertical worker.

Відоме обмеження (задокументоване і в промпті, і в sql/016_...): upstream
relation_label/shared_referent_status -- сигнал евристики, НЕ доведена
істина; verifier зобов'язаний переоцінювати кожен claim самостійно.

event_decision: 'accepted_seed' | 'rejected_seed' -- verifier перевіряє
coherence/membership кандидата, не встановлює об'єктивну істинність події.
accepted_seed валідний лише якщо anchor.included=true І >=1 neighbor.included
=true -- validate_event_verification() перевіряє цю односторонню
узгодженість (внутрішня суперечність відповіді моделі = invalid, той самий
принцип, що referent gate в relation_judgment_worker.py). members
зберігаються для БУДЬ-ЯКОГО valid verification, включно з rejected_seed.

sql/011_add_claim_relations.sql, sql/014_add_shared_referent_gate.sql,
sql/016_add_event_candidates.sql та event_verification_prompts seed
(prompt_id=1) МАЮТЬ бути застосовані ДО запуску.
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
EXPECTED_PROMPT_NAME = "event_verify"
EXPECTED_PROMPT_VERSION = "v1"

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_FILE = Path(__file__).parent / "event_verifier_prompt_v1.txt"

TIMEOUT = 600.0
MAX_TOKENS = 2048  # seed до 6 claims (anchor + TOP_K=5), кожен потребує member-рішення + rationale
CONTEXT_WINDOW_CHARS = 300

ALLOWED_DECISIONS = {"accepted_seed", "rejected_seed"}
SCHEMA_VERSION = "event-verification/1"
INPUT_SCHEMA_VERSION = "event-verification-input/1"


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
            "SELECT prompt_name, prompt_version, prompt_text FROM event_verification_prompts WHERE prompt_id = %s",
            (PROMPT_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"prompt_id={PROMPT_ID} не зареєстровано в event_verification_prompts")
    prompt_name, prompt_version, prompt_text_db = row
    if (prompt_name, prompt_version) != (EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION):
        raise RuntimeError(
            f"event_verification_prompts mismatch: БД={(prompt_name, prompt_version)}, "
            f"очікується={(EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION)}"
        )
    prompt_text_file = PROMPT_FILE.read_text(encoding="utf-8")
    if prompt_text_db != prompt_text_file:
        raise RuntimeError(f"prompt_text в БД (prompt_id={PROMPT_ID}) НЕ збігається побайтово з {PROMPT_FILE}.")
    return prompt_text_db


def fetch_batch(conn, limit: int) -> list[tuple]:
    """Idempotent eligibility: event_candidates без існуючого verification для
    поточної (llm_model_id, prompt_id, attempt_no). ORDER BY neighbor_count
    DESC -- спочатку насиченіші seeds, детермінований tiebreak по id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ec.event_candidate_id, ec.anchor_claim_id, ec.neighbor_count
            FROM event_candidates ec
            WHERE NOT EXISTS (
                SELECT 1 FROM event_verifications ev
                WHERE ev.event_candidate_id = ec.event_candidate_id
                  AND ev.llm_model_id = %s AND ev.prompt_id = %s AND ev.attempt_no = %s
            )
            ORDER BY ec.neighbor_count DESC, ec.event_candidate_id
            LIMIT %s
            """,
            (LLM_MODEL_ID, PROMPT_ID, ATTEMPT_NO, limit),
        )
        return cur.fetchall()


def build_context_snippet(text_content: str, evidence_start: int, evidence_end: int) -> str:
    start = max(0, evidence_start - CONTEXT_WINDOW_CHARS)
    end = min(len(text_content), evidence_end + CONTEXT_WINDOW_CHARS)
    return text_content[start:end]


def fetch_seed_members(conn, event_candidate_id) -> list[dict]:
    """Члени seed'а разом з relation-до-anchor даними для neighbors (score,
    relation_label, shared_referent_status, shared_referent_evidence --
    з relation_judgments/candidate_pairs через збережені source_* id)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ecm.claim_id, ecm.role, ecm.rank_in_seed,
                   cp.score, rj.relation_label, rj.shared_referent_status, rj.shared_referent_evidence
            FROM event_candidate_members ecm
            LEFT JOIN candidate_pairs cp ON cp.candidate_pair_id = ecm.source_candidate_pair_id
            LEFT JOIN relation_judgments rj ON rj.judgment_id = ecm.source_relation_judgment_id
            WHERE ecm.event_candidate_id = %s
            ORDER BY ecm.role DESC, ecm.rank_in_seed NULLS FIRST
            """,
            (event_candidate_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "claim_id": claim_id, "role": role, "rank_in_seed": rank_in_seed,
            "score": score, "relation_label": relation_label,
            "shared_referent_status": shared_referent_status,
            "shared_referent_evidence": shared_referent_evidence,
        }
        for claim_id, role, rank_in_seed, score, relation_label, shared_referent_status, shared_referent_evidence in rows
    ]


def fetch_claims_meta(conn, claim_ids: list) -> dict:
    """claim_id -> dict(claim_text, evidence_span, title, context_snippet,
    source, contour_set, first_seen). Продубльовано з relation_judgment_worker.py
    (за рішенням не ділити виконувані worker-скрипти як library)."""
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
            "display_source": f"{earliest[4]}:{earliest[2]} ({earliest[3]})" if earliest else None,
        }
    return meta


def build_prompt_safe(template: str, payload: dict) -> str:
    return template.replace("<<PAYLOAD_JSON>>", json.dumps(payload, ensure_ascii=False, indent=2))


def build_seed_payload(event_candidate_id, anchor_claim_id, seed_members: list[dict], meta: dict) -> dict:
    claims_payload = []
    for m in seed_members:
        cm = meta[m["claim_id"]]
        entry = {
            "claim_id": str(m["claim_id"]),
            "role": m["role"],
            "claim_text": cm["claim_text"],
            "evidence_span": cm["evidence_span"],
            "title": cm["title"],
            "context_snippet": cm["context_snippet"],
            "source": cm["display_source"],
            "contour_set": cm["contour_set"],
            "first_seen": cm["first_seen"].isoformat() if cm["first_seen"] else None,
            "relation_to_anchor": None if m["role"] == "anchor" else {
                "relation_label": m["relation_label"],
                "shared_referent_status": m["shared_referent_status"],
                "shared_referent_evidence": m["shared_referent_evidence"],
                "score": m["score"],
            },
        }
        claims_payload.append(entry)
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "event_candidate_id": str(event_candidate_id),
        "anchor_claim_id": str(anchor_claim_id),
        "claims": claims_payload,
    }


def validate_event_verification(
    raw_text: str, event_candidate_id: str, anchor_claim_id: str, expected_claim_ids: set
) -> tuple[bool, list[str], str | None, str | None, str | None, list[dict] | None, bool]:
    """Повертає (valid, errors, event_decision, event_summary, rationale_text,
    members, fence_stripped). members -- list of dict(claim_id, included,
    member_rationale), присутній лише коли valid=True."""
    errors: list[str] = []
    normalized_text, fence_stripped = strip_markdown_fence(raw_text)

    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError as exc:
        return False, [f"invalid JSON: {exc}"], None, None, None, None, fence_stripped

    if not isinstance(parsed, dict):
        return False, ["response is not a JSON object"], None, None, None, None, fence_stripped

    expected_keys = {"schema_version", "event_candidate_id", "event_decision", "event_summary", "members", "rationale_text"}
    extra_keys = set(parsed.keys()) - expected_keys
    missing_keys = expected_keys - set(parsed.keys())
    if extra_keys:
        errors.append(f"unexpected extra keys: {sorted(extra_keys)}")
    if missing_keys:
        errors.append(f"missing required keys: {sorted(missing_keys)}")
    if missing_keys:
        return False, errors, None, None, None, None, fence_stripped

    if parsed.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version mismatch: got {parsed.get('schema_version')!r}, expected {SCHEMA_VERSION!r}")

    if parsed.get("event_candidate_id") != event_candidate_id:
        errors.append(f"event_candidate_id mismatch: got {parsed.get('event_candidate_id')!r}, expected {event_candidate_id!r}")

    event_decision = parsed.get("event_decision")
    if event_decision not in ALLOWED_DECISIONS:
        errors.append(f"event_decision not in allowed set: {event_decision!r}")
        event_decision = None

    event_summary = parsed.get("event_summary")
    if event_decision == "accepted_seed":
        if not isinstance(event_summary, str) or not event_summary.strip():
            errors.append("event_summary is required and must be non-empty when event_decision='accepted_seed'")
            event_summary = None
    elif event_decision == "rejected_seed":
        if event_summary is not None:
            errors.append(f"event_summary must be null when event_decision='rejected_seed', got {event_summary!r}")
            event_summary = None

    rationale_text = parsed.get("rationale_text")
    if not isinstance(rationale_text, str) or not rationale_text.strip():
        errors.append("rationale_text is missing or empty")
        rationale_text = None

    members_raw = parsed.get("members")
    members: list[dict] | None = None
    included_by_claim: dict = {}
    if not isinstance(members_raw, list):
        errors.append("members must be a JSON array")
    else:
        parsed_members = []
        seen_claim_ids = set()
        member_keys_ok = True
        for i, m in enumerate(members_raw):
            if not isinstance(m, dict) or set(m.keys()) != {"claim_id", "included", "member_rationale"}:
                errors.append(f"members[{i}] has unexpected shape: {m!r}")
                member_keys_ok = False
                continue
            claim_id_str = m.get("claim_id")
            included = m.get("included")
            member_rationale = m.get("member_rationale")
            if not isinstance(claim_id_str, str):
                errors.append(f"members[{i}].claim_id is not a string: {claim_id_str!r}")
                continue
            if not isinstance(included, bool):
                errors.append(f"members[{i}].included is not a boolean: {included!r}")
                continue
            if not isinstance(member_rationale, str) or not member_rationale.strip():
                errors.append(f"members[{i}].member_rationale is missing or empty (claim_id={claim_id_str})")
                continue
            seen_claim_ids.add(claim_id_str)
            included_by_claim[claim_id_str] = included
            parsed_members.append({"claim_id": claim_id_str, "included": included, "member_rationale": member_rationale})

        if member_keys_ok:
            missing_ids = expected_claim_ids - seen_claim_ids
            extra_ids = seen_claim_ids - expected_claim_ids
            if missing_ids:
                errors.append(f"members missing claim_id(s): {sorted(missing_ids)}")
            if extra_ids:
                errors.append(f"members has unexpected claim_id(s) not in seed: {sorted(extra_ids)}")
            if not missing_ids and not extra_ids:
                members = parsed_members

    # ACCEPTED/REJECTED CONSISTENCY (одностороння перевірка, за рішенням):
    # accepted_seed валідний ЛИШЕ якщо anchor.included=true І >=1 neighbor.
    # included=true. Внутрішня суперечність відповіді моделі = invalid, той
    # самий принцип, що referent gate в relation_judgment_worker.py.
    if event_decision == "accepted_seed" and members is not None:
        anchor_included = included_by_claim.get(anchor_claim_id)
        neighbor_included_count = sum(
            1 for cid, inc in included_by_claim.items() if cid != anchor_claim_id and inc
        )
        if anchor_included is not True or neighbor_included_count < 1:
            errors.append(
                f"accepted_seed requires anchor included=true and >=1 neighbor included=true "
                f"(anchor_included={anchor_included!r}, neighbor_included_count={neighbor_included_count})"
            )

    valid = not errors
    return (
        valid,
        errors,
        event_decision if valid else None,
        event_summary if valid else None,
        rationale_text if valid else None,
        members if valid else None,
        fence_stripped,
    )


def insert_verification(
    conn, event_candidate_id, status: str, event_decision, event_summary, rationale_text,
    errors: list[str] | None, raw_response: str | None, code_revision: str, latency_ms: int | None,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event_verifications
                (event_candidate_id, llm_model_id, prompt_id, attempt_no, status,
                 event_decision, event_summary, rationale_text, errors, raw_response, code_revision, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING verification_id
            """,
            (
                event_candidate_id, LLM_MODEL_ID, PROMPT_ID, ATTEMPT_NO, status,
                event_decision, event_summary, rationale_text,
                Jsonb(errors) if errors else None, raw_response, code_revision, latency_ms,
            ),
        )
        return cur.fetchone()[0]


def insert_verification_members(conn, verification_id, members: list[dict]) -> None:
    """Зберігається для БУДЬ-ЯКОГО valid verification, включно з rejected_seed
    (усі included=false у цьому випадку, але rationale лишається)."""
    with conn.cursor() as cur:
        for m in members:
            cur.execute(
                """
                INSERT INTO event_verification_members (verification_id, claim_id, included, member_rationale)
                VALUES (%s, %s, %s, %s)
                """,
                (verification_id, m["claim_id"], m["included"], m["member_rationale"]),
            )


def process_candidate(conn, event_candidate_id, anchor_claim_id, seed_members: list[dict], meta: dict, prompt_text: str, code_revision: str) -> str:
    event_candidate_id_str = str(event_candidate_id)
    anchor_claim_id_str = str(anchor_claim_id)
    expected_claim_ids = {str(m["claim_id"]) for m in seed_members}

    payload = build_seed_payload(event_candidate_id, anchor_claim_id, seed_members, meta)
    prompt = build_prompt_safe(prompt_text, payload)

    try:
        raw_text, latency, finish_reason, completion_tokens = call_model(
            DEFAULT_ENDPOINT, DEFAULT_MODEL, prompt, TIMEOUT, MAX_TOKENS
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError) as e:
        error_msg = f"{type(e).__name__}: {e}"
        print(f"[{event_candidate_id_str}] TRANSPORT ERROR: {error_msg}", file=sys.stderr)
        try:
            insert_verification(
                conn, event_candidate_id, status="transport_error",
                event_decision=None, event_summary=None, rationale_text=None,
                errors=[error_msg], raw_response=None, code_revision=code_revision, latency_ms=None,
            )
            conn.commit()
        except Exception as db_err:
            conn.rollback()
            print(f"[{event_candidate_id_str}] DB ERROR while recording transport_error: {db_err}", file=sys.stderr)
            return "error"
        return "transport_error"

    (
        valid, errors, event_decision, event_summary, rationale_text, members, fence_stripped,
    ) = validate_event_verification(raw_text, event_candidate_id_str, anchor_claim_id_str, expected_claim_ids)
    status = "valid" if valid else "invalid"
    latency_ms = round(latency * 1000)

    print(
        f"[{event_candidate_id_str}] {status.upper()} decision={event_decision} "
        f"neighbors={len(seed_members) - 1} fence_stripped={fence_stripped} {latency:.1f}s"
    )
    for err in errors:
        print(f"    - {err}")

    try:
        verification_id = insert_verification(
            conn, event_candidate_id, status=status,
            event_decision=event_decision, event_summary=event_summary, rationale_text=rationale_text,
            errors=errors if errors else None, raw_response=raw_text,
            code_revision=code_revision, latency_ms=latency_ms,
        )
        if status == "valid":
            insert_verification_members(conn, verification_id, members)
        conn.commit()
    except Exception as db_err:
        conn.rollback()
        print(f"[{event_candidate_id_str}] DB ERROR while persisting: {db_err}", file=sys.stderr)
        return "error"

    return status


def run(limit: int) -> int:
    code_revision = get_code_revision()
    summary = {"valid": 0, "invalid": 0, "transport_error": 0, "error": 0}

    with psycopg.connect(DB_DSN) as conn:
        prompt_text = fetch_and_verify_registry(conn)
        batch = fetch_batch(conn, limit)
        print(f"batch size: {len(batch)}")
        if not batch:
            print("SUMMARY: nothing to verify (backlog empty)")
            return 0

        all_claim_ids = set()
        seed_members_by_candidate = {}
        for event_candidate_id, anchor_claim_id, _neighbor_count in batch:
            seed_members = fetch_seed_members(conn, event_candidate_id)
            seed_members_by_candidate[event_candidate_id] = (anchor_claim_id, seed_members)
            all_claim_ids.update(m["claim_id"] for m in seed_members)

        meta = fetch_claims_meta(conn, sorted(all_claim_ids, key=str))

        for event_candidate_id, anchor_claim_id, _neighbor_count in batch:
            _anchor, seed_members = seed_members_by_candidate[event_candidate_id]
            status = process_candidate(conn, event_candidate_id, anchor_claim_id, seed_members, meta, prompt_text, code_revision)
            summary[status] = summary.get(status, 0) + 1

        print(f"\nSUMMARY: total={len(batch)} " + " ".join(f"{k}={v}" for k, v in summary.items()))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM event-candidate verification worker (v1)")
    parser.add_argument("--limit", type=int, required=True, help="max event candidates to verify this run")
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    return run(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
