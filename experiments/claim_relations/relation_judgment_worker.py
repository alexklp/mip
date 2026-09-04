#!/usr/bin/env python3
"""
experiments/claim_relations/relation_judgment_worker.py — LLM-based relation
judgment для candidate_pairs (persisted candidate_pair_generator.py).

Шлях: candidate_pairs -> payload (claim A + claim B + provenance context,
безпечно серіалізований json.dumps) -> Mamay (call_model, reuse з
run_eval.py) -> локальний strip_markdown_fence + суворий парсинг ->
relation_judgments.

v3 / pilot contract 2 (22.08, referent gate): v1 і v2 промпти намагалися
текстовою інструкцією змусити модель підтверджувати спільний референт перед
same_fact/same_event/contradiction — емпірично провалилося (модель системно
інвертувала burden of proof: "немає ознак відмінності" трактувала як
"підтверджено спільний референт", на 11+ з 20 тестових пар під v2, включно з
майже дослівним negative-прикладом у самому промпті). v3 замінює текстову
інструкцію на ОКРЕМИЙ, ВАЛІДОВАНИЙ крок:
  1. Схема відповіді розширена: shared_referent_status (confirmed/
     not_confirmed/insufficient) + shared_referent_evidence — окремі
     обов'язкові поля, а не частина вільного rationale_text.
  2. Локальний валідатор ЗАБОРОНЯЄ (не просто не заохочує) relation_label
     IN (same_fact, same_event, contradiction), якщо shared_referent_status
     != "confirmed" — той самий gate додатково закодований як DB CHECK
     (sql/014_add_shared_referent_gate.sql, defense in depth).
  3. Коли status="confirmed", shared_referent_evidence МАЄ містити дослівну
     цитату в «» — валідатор перевіряє, що цитата реально є підрядком
     наданих даних (claim_text/evidence_span/title/context_snippet будь-якого
     з двох claims). Той самий verbatim-substring принцип, що evidence_span
     у claim_extraction — не нове правило, той самий стандарт застосований
     до нового контексту.
  4. Input payload розширено provenance-контекстом на claim: evidence_span,
     title (може бути null), context_snippet (вікно тексту content_item
     навколо evidence_span) — щоб модель мала дані, з яких можна реально
     процитувати ідентифікуючу ознаку, а не лише голий claim_text.

Відмінності від claim_extract_worker.py (та сама архітектура: LLM-based,
per-item ізоляція, per-item транзакція):
  1. Джерело candidate: candidate_pairs, а не content_items; batch береться
     ORDER BY score DESC. Idempotent eligibility — NOT EXISTS проти
     relation_judgments на (llm_model_id, prompt_id, attempt_no).
  2. Claim text/контекст — НЕДОВІРЕНІ зовнішні дані. У промпт вони
     підставляються ОДНИМ json.dumps()-блоком (build_prompt_safe), а НЕ через
     str.replace() по кожному полю окремо, як у run_eval.py/build_prompt().
  3. Валідація відповіді — локальна, компактна (validate_relation_judgment),
     НЕ validator.py з claim_extraction (інша схема).
  4. status='valid' <=> relation_label і rationale_text обидва NOT NULL —
     той самий all-or-nothing контракт, що claims для claim_extraction_runs,
     на рівні одного judgment-рядка.

Без змін: claim_extraction schema/дані, candidate_pairs (тільки читання),
routing/embeddings. Без queue/broker, без retry, без parallel inference.

sql/011_add_claim_relations.sql, sql/014_add_shared_referent_gate.sql та
relation_judgment_prompts seed (prompt_id=3) МАЮТЬ бути застосовані ДО
запуску.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
from collections import defaultdict
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

# run_eval.py живе в experiments/claim_extraction/, а не тут — reuse
# call_model/strip_markdown_fence БЕЗ копіювання коду, тому явно додаємо той
# каталог у sys.path перед імпортом (інакше `from run_eval import ...` впаде
# з ModuleNotFoundError, бо це інша директорія, ніж claim_extract_worker.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "claim_extraction"))
from run_eval import DEFAULT_ENDPOINT, DEFAULT_MODEL, call_model, strip_markdown_fence  # noqa: E402

DB_DSN = "dbname=mip_dev"

LLM_MODEL_ID = 1
PROMPT_ID = 3
ATTEMPT_NO = 1

# Очікувана identity — звіряється з БД, не встановлюється звідси.
EXPECTED_MODEL_NAME = "MamayLM-Gemma-3-27B-IT"
EXPECTED_MODEL_REVISION = "v2.0/Q4_K_M/7677fe7e2df2"
EXPECTED_PROMPT_NAME = "relation_judge"
EXPECTED_PROMPT_VERSION = "v3"

REPO_ROOT = Path(__file__).resolve().parents[2]  # experiments/claim_relations/../.. = ~/mip
PROMPT_FILE = Path(__file__).parent / "prompt_relation_judge_v3.txt"

TIMEOUT = 600.0  # reuse claim_extraction timeout as-is; не звужуємо без
                  # вимірювань на живому прогоні.
MAX_TOKENS = 1024

CONTEXT_WINDOW_CHARS = 300  # символів до/після evidence_span з text_content

ALLOWED_LABELS = {"same_fact", "same_event", "contradiction", "related", "unrelated"}
REFERENT_GATED_LABELS = {"same_fact", "same_event", "contradiction"}
ALLOWED_REFERENT_STATUS = {"confirmed", "not_confirmed", "insufficient"}
SCHEMA_VERSION = "relation-judgment/2"

QUOTE_PATTERN = re.compile(r"«([^»]+)»|\"([^\"]+)\"")


def get_code_revision() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


def fetch_and_verify_registry(conn) -> str:
    """Повертає prompt_text з БД. Fail-fast при розбіжності з очікуваною identity
    (включно з побайтовою звіркою тексту промпту проти файлу на диску) — той
    самий паттерн, що claim_extract_worker.py."""
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
            "SELECT prompt_name, prompt_version, prompt_text FROM relation_judgment_prompts WHERE prompt_id = %s",
            (PROMPT_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"prompt_id={PROMPT_ID} не зареєстровано в relation_judgment_prompts")
    prompt_name, prompt_version, prompt_text_db = row
    if (prompt_name, prompt_version) != (EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION):
        raise RuntimeError(
            f"relation_judgment_prompts mismatch: БД={(prompt_name, prompt_version)}, "
            f"очікується={(EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION)}"
        )
    prompt_text_file = PROMPT_FILE.read_text(encoding="utf-8")
    if prompt_text_db != prompt_text_file:
        raise RuntimeError(f"prompt_text в БД (prompt_id={PROMPT_ID}) НЕ збігається побайтово з {PROMPT_FILE}.")
    return prompt_text_db


def fetch_batch(conn, limit: int) -> list[tuple]:
    """Idempotent eligibility: candidate_pairs без існуючого judgment для
    поточної (llm_model_id, prompt_id, attempt_no). ORDER BY score DESC —
    спочатку найсильніші кандидати."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cp.candidate_pair_id, cp.claim_id_a, cp.claim_id_b, cp.score
            FROM candidate_pairs cp
            WHERE NOT EXISTS (
                SELECT 1 FROM relation_judgments rj
                WHERE rj.candidate_pair_id = cp.candidate_pair_id
                  AND rj.llm_model_id = %s AND rj.prompt_id = %s AND rj.attempt_no = %s
            )
            ORDER BY cp.score DESC, cp.candidate_pair_id
            LIMIT %s
            """,
            (LLM_MODEL_ID, PROMPT_ID, ATTEMPT_NO, limit),
        )
        return cur.fetchall()


def build_context_snippet(text_content: str, evidence_start: int, evidence_end: int) -> str:
    start = max(0, evidence_start - CONTEXT_WINDOW_CHARS)
    end = min(len(text_content), evidence_end + CONTEXT_WINDOW_CHARS)
    return text_content[start:end]


def fetch_claims_meta(conn, claim_ids: list) -> dict:
    """claim_id -> dict(claim_text, evidence_span, title, context_snippet,
    contour_set, first_seen, display_source). Provenance-контекст (evidence_
    span/title/context_snippet) — те, з чого модель МАЄ цитувати
    shared_referent_evidence при status=confirmed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.claim_id, c.claim_text, c.evidence_span, c.evidence_start, c.evidence_end,
                   r.content_id,
                   ci.title,
                   CASE
                       WHEN r.segment_id IS NULL THEN ci.text_content
                       ELSE cs.text_content
                   END AS evidence_text
            FROM claims c
            JOIN claim_extraction_runs r ON r.run_id = c.run_id
            JOIN content_items ci ON ci.content_id = r.content_id
            LEFT JOIN content_segments cs ON cs.segment_id = r.segment_id
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

    occ_by_content = defaultdict(list)
    for content_id, contour_id, collected_at, name, url_or_handle, source_type in occ_rows:
        occ_by_content[content_id].append((contour_id, collected_at, name, url_or_handle, source_type))

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


def build_claim_payload(claim_id, meta: dict) -> dict:
    m = meta[claim_id]
    return {
        "claim_text": m["claim_text"],
        "evidence_span": m["evidence_span"],
        "title": m["title"],
        "context_snippet": m["context_snippet"],
        "source": m["display_source"],
        "contour_set": m["contour_set"],
        "first_seen": m["first_seen"].isoformat() if m["first_seen"] else None,
    }


def claim_groundable_text(meta: dict, claim_id) -> str:
    """Об'єднаний текст усіх полів claim, проти яких перевіряється дослівність
    цитат у shared_referent_evidence."""
    m = meta[claim_id]
    parts = [m["claim_text"], m["evidence_span"], m["title"] or "", m["context_snippet"]]
    return "\n".join(parts)


def build_prompt_safe(template: str, payload: dict) -> str:
    """Єдина точка підстановки недовірених даних claim у промпт: весь payload
    (обидва claims) серіалізується ОДНИМ json.dumps()-блоком і вставляється в
    єдиний плейсхолдер. Жодне значення з claim_text/context_snippet не може
    "розірвати" текстову структуру промпту чи бути сприйняте як інструкція —
    JSON-серіалізація коректно екранує все всередині значень."""
    return template.replace("<<PAYLOAD_JSON>>", json.dumps(payload, ensure_ascii=False, indent=2))


def extract_quotes(text: str) -> list[str]:
    """Дістає всі підрядки в «...» або "..." з тексту."""
    quotes = []
    for m in QUOTE_PATTERN.finditer(text):
        quotes.append(m.group(1) if m.group(1) is not None else m.group(2))
    return quotes


def validate_relation_judgment(
    raw_text: str, pair_id: str, meta: dict, claim_id_a, claim_id_b
) -> tuple[bool, list[str], str | None, str | None, str | None, str | None, bool]:
    """Локальна сувора валідація. Повертає (valid, errors, relation_label,
    rationale_text, shared_referent_status, shared_referent_evidence,
    fence_stripped).

    fence_stripped — інформаційний факт (модель обгорнула JSON у markdown-
    огорожу попри пряму заборону в промпті), А НЕ причина invalid: strip_
    markdown_fence вже дістає чистий JSON, і далі він валідується як завжди.
    """
    errors: list[str] = []
    normalized_text, fence_stripped = strip_markdown_fence(raw_text)

    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError as exc:
        return False, [f"invalid JSON: {exc}"], None, None, None, None, fence_stripped

    if not isinstance(parsed, dict):
        return False, ["response is not a JSON object"], None, None, None, None, fence_stripped

    expected_keys = {
        "schema_version", "pair_id", "shared_referent_status",
        "shared_referent_evidence", "relation_label", "rationale_text",
    }
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

    if parsed.get("pair_id") != pair_id:
        errors.append(f"pair_id mismatch: got {parsed.get('pair_id')!r}, expected {pair_id!r}")

    shared_referent_status = parsed.get("shared_referent_status")
    if shared_referent_status not in ALLOWED_REFERENT_STATUS:
        errors.append(f"shared_referent_status not in allowed set: {shared_referent_status!r}")
        shared_referent_status = None

    shared_referent_evidence = parsed.get("shared_referent_evidence")
    if not isinstance(shared_referent_evidence, str) or not shared_referent_evidence.strip():
        errors.append("shared_referent_evidence is missing or empty")
        shared_referent_evidence = None

    relation_label = parsed.get("relation_label")
    if relation_label not in ALLOWED_LABELS:
        errors.append(f"relation_label not in allowed set: {relation_label!r}")
        relation_label = None

    rationale_text = parsed.get("rationale_text")
    if not isinstance(rationale_text, str) or not rationale_text.strip():
        errors.append("rationale_text is missing or empty")
        rationale_text = None

    # REFERENT GATE: same_fact/same_event/contradiction заборонені без
    # shared_referent_status="confirmed" — те саме правило, що DB CHECK
    # relation_judgments_referent_gate, продубльоване тут як перша лінія
    # захисту (app-level fail-fast, а не покладання лише на DB constraint).
    if relation_label in REFERENT_GATED_LABELS and shared_referent_status != "confirmed":
        errors.append(
            f"referent gate violation: relation_label={relation_label!r} "
            f"requires shared_referent_status='confirmed', got {shared_referent_status!r}"
        )

    # GROUNDING CHECK: при status=confirmed shared_referent_evidence МАЄ
    # містити хоча б одну дослівну цитату («...» або "...") з наданих claim
    # A/B даних. Той самий verbatim-substring принцип, що evidence_span у
    # claim_extraction, застосований до нового поля.
    if shared_referent_status == "confirmed" and shared_referent_evidence:
        quotes = extract_quotes(shared_referent_evidence)
        if not quotes:
            errors.append("shared_referent_status=confirmed but shared_referent_evidence contains no «...» quote")
        else:
            groundable = claim_groundable_text(meta, claim_id_a) + "\n" + claim_groundable_text(meta, claim_id_b)
            if not any(q in groundable for q in quotes):
                errors.append(
                    "shared_referent_evidence quote(s) not found verbatim in provided claim data "
                    "(possible hallucinated grounding)"
                )

    valid = not errors
    return (
        valid,
        errors,
        relation_label if valid else None,
        rationale_text if valid else None,
        shared_referent_status if valid else None,
        shared_referent_evidence if valid else None,
        fence_stripped,
    )


def insert_judgment(
    conn, candidate_pair_id, status: str, relation_label, rationale_text,
    shared_referent_status, shared_referent_evidence,
    errors: list[str] | None, raw_response: str | None, code_revision: str, latency_ms: int | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO relation_judgments
                (candidate_pair_id, llm_model_id, prompt_id, attempt_no, status,
                 relation_label, rationale_text, shared_referent_status, shared_referent_evidence,
                 errors, raw_response, code_revision, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                candidate_pair_id, LLM_MODEL_ID, PROMPT_ID, ATTEMPT_NO, status,
                relation_label, rationale_text, shared_referent_status, shared_referent_evidence,
                Jsonb(errors) if errors else None,
                raw_response, code_revision, latency_ms,
            ),
        )


def process_pair(conn, pair, meta: dict, prompt_text: str, code_revision: str) -> str:
    """Per-item ізоляція + per-item транзакція — той самий паттерн, що
    process_item() в claim_extract_worker.py."""
    candidate_pair_id, claim_id_a, claim_id_b, score = pair
    pair_id_str = str(candidate_pair_id)

    payload = {
        "schema_version": "relation-judgment-input/2",
        "pair_id": pair_id_str,
        "claim_a": build_claim_payload(claim_id_a, meta),
        "claim_b": build_claim_payload(claim_id_b, meta),
    }
    prompt = build_prompt_safe(prompt_text, payload)

    try:
        raw_text, latency, finish_reason, completion_tokens = call_model(
            DEFAULT_ENDPOINT, DEFAULT_MODEL, prompt, TIMEOUT, MAX_TOKENS
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError) as e:
        error_msg = f"{type(e).__name__}: {e}"
        print(f"[{pair_id_str}] TRANSPORT ERROR: {error_msg}", file=sys.stderr)
        try:
            insert_judgment(
                conn, candidate_pair_id, status="transport_error",
                relation_label=None, rationale_text=None,
                shared_referent_status=None, shared_referent_evidence=None,
                errors=[error_msg], raw_response=None, code_revision=code_revision, latency_ms=None,
            )
            conn.commit()
        except Exception as db_err:
            conn.rollback()
            print(f"[{pair_id_str}] DB ERROR while recording transport_error: {db_err}", file=sys.stderr)
            return "error"
        return "transport_error"

    (
        valid, errors, relation_label, rationale_text,
        shared_referent_status, shared_referent_evidence, fence_stripped,
    ) = validate_relation_judgment(raw_text, pair_id_str, meta, claim_id_a, claim_id_b)
    status = "valid" if valid else "invalid"
    latency_ms = round(latency * 1000)

    print(
        f"[{pair_id_str}] {status.upper()} score={score:.4f} referent={shared_referent_status} "
        f"label={relation_label} fence_stripped={fence_stripped} {latency:.1f}s"
    )
    for err in errors:
        print(f"    - {err}")

    try:
        insert_judgment(
            conn, candidate_pair_id, status=status,
            relation_label=relation_label, rationale_text=rationale_text,
            shared_referent_status=shared_referent_status, shared_referent_evidence=shared_referent_evidence,
            errors=errors if errors else None, raw_response=raw_text,
            code_revision=code_revision, latency_ms=latency_ms,
        )
        conn.commit()
    except Exception as db_err:
        conn.rollback()
        print(f"[{pair_id_str}] DB ERROR while persisting: {db_err}", file=sys.stderr)
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
            print("SUMMARY: nothing to judge (backlog empty)")
            return 0

        claim_ids = sorted({cid for _pid, a, b, _s in batch for cid in (a, b)})
        meta = fetch_claims_meta(conn, claim_ids)

        for pair in batch:
            status = process_pair(conn, pair, meta, prompt_text, code_revision)
            summary[status] = summary.get(status, 0) + 1

        print(f"\nSUMMARY: total={len(batch)} " + " ".join(f"{k}={v}" for k, v in summary.items()))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM relation judgment worker for candidate_pairs (v3, referent gate)")
    parser.add_argument("--limit", type=int, required=True, help="max candidate pairs to judge this run")
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    return run(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
