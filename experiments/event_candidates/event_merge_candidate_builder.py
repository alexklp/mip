#!/usr/bin/env python3
"""
experiments/event_candidates/event_merge_candidate_builder.py -- deterministic
overlap-based Event Merge candidate generator (v1): accepted_seed event
candidates (from event_candidate_builder.py + event_verifier_worker.py) ->
event_merge_candidates.

Той самий deterministic паттерн, що event_candidate_builder.py: жодних LLM
викликів, ідемпотентність через ON CONFLICT DO NOTHING, code_revision
дублюється (не імпортується з worker-скриптів -- за тим самим рішенням, що
й раніше).

Ключове рішення (див. sql/018_add_event_merge.sql):
merge candidate генерується лише для ДВОХ accepted_seed event_candidates,
якщо у них є хоча б один спільний claim з included=true в ОБОХ. Сам overlap
НЕ означає merge -- це лише кандидат для LLM merge-verifier
(event_merge_worker.py, окремий скрипт). Жодних thresholds/евристик тут
крім самого факту overlap не застосовується.

Eligibility щодо ВЖЕ існуючих canonical_events (already_same_canonical /
deferred_both_canonical) НЕ обробляється тут -- за рішенням, це job
event_merge_worker.py на момент читання eligibility для LLM виклику, не
цього білдера. Цей скрипт лише генерує сирі overlap-пари між accepted_seed,
не знаючи і не питаючи про поточний canonical_events стан.

Contract pinning (за вимогою): перш ніж читати event_verification_members,
скрипт перевіряє, що (llm_model_id, prompt_id), реально використані в
існуючих valid accepted_seed event_verifications, відповідають пінованому
Event Verifier v1 контракту (EXPECTED_MODEL_NAME/REVISION,
EXPECTED_PROMPT_NAME/VERSION). Це легка перевірка identity (mirrors
event_candidate_builder.py's перевірку relation_judgment v3 контракту) --
БЕЗ побайтової звірки prompt_text з файлом на диску, це не job цього
скрипта (тим займається event_verifier_worker.py).

v1 обмеження: якщо для одного event_candidate_id знайдено valid
accepted_seed verification з БІЛЬШ НІЖ одним attempt_no -- скрипт падає
явно (RuntimeError), а не мовчки обирає якийсь attempt. Ретраїв на pilot
не було, тому це поки що чисто захисний код.

sql/016_add_event_candidates.sql, sql/017_seed_event_verification_registry_v1.sql
та sql/018_add_event_merge.sql МАЮТЬ бути застосовані ДО запуску.
"""
from __future__ import annotations

import argparse
import itertools
import subprocess
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

DB_DSN = "dbname=mip_dev"

EXPECTED_MODEL_NAME = "MamayLM-Gemma-3-27B-IT"
EXPECTED_MODEL_REVISION = "v2.0/Q4_K_M/7677fe7e2df2"
EXPECTED_PROMPT_NAME = "event_verify"
EXPECTED_PROMPT_VERSION = "v1"

REPO_ROOT = Path(__file__).resolve().parents[2]


def get_code_revision() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


def fetch_and_verify_upstream_contract(conn) -> int:
    """Легка identity-перевірка (mirrors event_candidate_builder.py): яка
    (llm_model_id, prompt_id) пара реально стоїть за існуючими valid
    accepted_seed event_verifications, і чи вона збігається з пінованим
    Event Verifier v1 контрактом. Без побайтової звірки prompt_text --
    не job цього скрипта. Повертає prompt_id для подальших запитів."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ev.llm_model_id, ev.prompt_id
            FROM event_verifications ev
            WHERE ev.status = 'valid' AND ev.event_decision = 'accepted_seed'
            """
        )
        rows = cur.fetchall()

    if not rows:
        raise RuntimeError("No valid accepted_seed event_verifications found -- nothing to build merge candidates from.")
    if len(rows) > 1:
        raise RuntimeError(
            f"Multiple distinct (llm_model_id, prompt_id) pairs found among accepted_seed "
            f"verifications: {rows} -- refusing to proceed without explicit review."
        )
    llm_model_id, prompt_id = rows[0]

    with conn.cursor() as cur:
        cur.execute("SELECT model_name, model_revision FROM llm_models WHERE llm_model_id = %s", (llm_model_id,))
        model_row = cur.fetchone()
        cur.execute(
            "SELECT prompt_name, prompt_version FROM event_verification_prompts WHERE prompt_id = %s",
            (prompt_id,),
        )
        prompt_row = cur.fetchone()

    if model_row is None or tuple(model_row) != (EXPECTED_MODEL_NAME, EXPECTED_MODEL_REVISION):
        raise RuntimeError(
            f"llm_models mismatch for llm_model_id={llm_model_id}: БД={model_row}, "
            f"очікується={(EXPECTED_MODEL_NAME, EXPECTED_MODEL_REVISION)}"
        )
    if prompt_row is None or tuple(prompt_row) != (EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION):
        raise RuntimeError(
            f"event_verification_prompts mismatch for prompt_id={prompt_id}: БД={prompt_row}, "
            f"очікується={(EXPECTED_PROMPT_NAME, EXPECTED_PROMPT_VERSION)}"
        )

    return prompt_id


def fetch_accepted_seed_members(conn, prompt_id: int) -> dict:
    """event_candidate_id -> set(claim_id) для included=true членів ЄДИНОЇ
    valid accepted_seed verification цього candidate. Падає явно, якщо
    знайдено >1 attempt_no для одного event_candidate_id -- v1 не обирає
    мовчки."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ev.event_candidate_id, ev.attempt_no, evm.claim_id
            FROM event_verifications ev
            JOIN event_verification_members evm ON evm.verification_id = ev.verification_id
            WHERE ev.status = 'valid' AND ev.event_decision = 'accepted_seed'
              AND ev.prompt_id = %s AND evm.included = true
            ORDER BY ev.event_candidate_id, evm.claim_id
            """,
            (prompt_id,),
        )
        rows = cur.fetchall()

    seed_claims: dict = {}
    seed_attempts: dict = {}
    for event_candidate_id, attempt_no, claim_id in rows:
        seed_claims.setdefault(event_candidate_id, set()).add(claim_id)
        seed_attempts.setdefault(event_candidate_id, set()).add(attempt_no)

    multi_attempt = {cid: attempts for cid, attempts in seed_attempts.items() if len(attempts) > 1}
    if multi_attempt:
        raise RuntimeError(
            f"event_candidate(s) with more than one valid accepted_seed verification attempt "
            f"found -- v1 builder does not know which to prefer, resolve manually: {multi_attempt}"
        )

    return seed_claims


def build_merge_candidates(seed_claims: dict) -> dict:
    """claim_id -> список accepted_seed, що включають цей claim
    (included=true). Для кожного claim, спільного для >=2 seeds, додає цей
    claim_id до shared-множини КОЖНОЇ пари seeds, що його поділяють.
    Повертає (seed_a_id, seed_b_id) [seed_a_id < seed_b_id] ->
    відсортований список shared claim_id."""
    claim_to_seeds: dict = {}
    for seed_id, claims in seed_claims.items():
        for claim_id in claims:
            claim_to_seeds.setdefault(claim_id, []).append(seed_id)

    pair_shared: dict = {}
    for claim_id, seeds in claim_to_seeds.items():
        if len(seeds) < 2:
            continue
        for seed_a, seed_b in itertools.combinations(sorted(seeds), 2):
            pair_shared.setdefault((seed_a, seed_b), set()).add(claim_id)

    return {pair: sorted(claims) for pair, claims in pair_shared.items()}


def insert_merge_candidates(conn, pair_shared: dict, code_revision: str) -> tuple[int, int]:
    """ON CONFLICT (seed_a_id, seed_b_id) DO NOTHING -- ідемпотентно, той
    самий append-only паттерн, що candidate_pairs/event_candidates."""
    inserted = 0
    already_existed = 0
    with conn.cursor() as cur:
        for (seed_a, seed_b), claim_ids in pair_shared.items():
            cur.execute(
                """
                INSERT INTO event_merge_candidates (seed_a_id, seed_b_id, shared_claim_ids, code_revision)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (seed_a_id, seed_b_id) DO NOTHING
                RETURNING merge_candidate_id
                """,
                (seed_a, seed_b, Jsonb([str(c) for c in claim_ids]), code_revision),
            )
            if cur.fetchone() is not None:
                inserted += 1
            else:
                already_existed += 1
    conn.commit()
    return inserted, already_existed


def run() -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        prompt_id = fetch_and_verify_upstream_contract(conn)

        seed_claims = fetch_accepted_seed_members(conn, prompt_id)
        print(f"accepted_seed event_candidates with >=1 included claim: {len(seed_claims)}")

        pair_shared = build_merge_candidates(seed_claims)
        print(f"overlapping seed pairs found: {len(pair_shared)}")

        inserted, already_existed = insert_merge_candidates(conn, pair_shared, code_revision)

        print(
            f"SUMMARY: code_revision={code_revision} accepted_seeds={len(seed_claims)} "
            f"overlapping_pairs={len(pair_shared)} inserted={inserted} already_existed={already_existed}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic overlap-based Event Merge candidate generator (v1)."
    )
    parser.parse_args()  # немає опцій у v1 -- кожен запуск обробляє ВСІ accepted_seed пари,
    # ідемпотентно через ON CONFLICT DO NOTHING.
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
