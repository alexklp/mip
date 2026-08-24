#!/usr/bin/env python3
"""
experiments/event_candidates/event_candidate_builder.py -- deterministic
event-candidate seed builder: claims -> candidate_pairs -> relation_judgments
-> event_candidates (this script) -> event_verifications (event_verifier_worker.py).

НЕ connected components, НЕ transitive closure. Для кожного anchor claim
будується seed = anchor + TOP_K=5 прямих сусідів по qualifying edges (один
hop, без рекурсивного розширення графа). seed_version=1 фіксує TOP_K=5 як
частину identity контракту -- зміна K вимагає НОВОГО seed_version, тому
--top-k НЕ винесено в CLI цього скрипта.

qualifying edge = relation_judgments.status='valid' AND relation_label IN
('same_fact','same_event','contradiction') під зафіксованим relation-judgment
контрактом (llm_model_id=1, prompt_id=3, attempt_no=1 -- поточний v3,
referent gate). related/unrelated НЕ створюють edges.

Anchor selection v1: БУДЬ-ЯКИЙ claim з >=1 qualifying edge -- anchor. Один
event_candidate на anchor, БЕЗ дедуплікації кластерів -- seed'и можуть
перетинатися. Кластеризація/мердж confirmed events -- поза межами цього
зрізу (майбутня робота).

Ідемпотентність: UNIQUE(anchor_claim_id, seed_version) в event_candidates;
скрипт пропускає anchors, що вже мають рядок під поточним SEED_VERSION.
Порядок обробки anchors у межах одного прогону -- детермінований:
ORDER BY best_edge_score DESC, anchor_claim_id ASC; --limit -- бюджет НОВИХ
anchors за прогін (runtime-параметр, не версія).

sql/011_add_claim_relations.sql, sql/014_add_shared_referent_gate.sql,
sql/016_add_event_candidates.sql МАЮТЬ бути застосовані ДО запуску.
"""
from __future__ import annotations

import argparse
import subprocess
from collections import defaultdict
from pathlib import Path

import psycopg

DB_DSN = "dbname=mip_dev"

# Qualifying-edge source contract -- зафіксована identity поточного relation
# judgment (v3, referent gate). Builder лише СПОЖИВАЄ вже провалідовані
# relation_judgments рядки, не re-валідує текст промпту побайтово (це job
# relation_judgment_worker.py при записі); тут -- легка fail-fast перевірка
# identity, щоб не збирати seeds з даних під неочікуваним контрактом.
RELATION_LLM_MODEL_ID = 1
RELATION_PROMPT_ID = 3
RELATION_ATTEMPT_NO = 1
EXPECTED_RELATION_MODEL_NAME = "MamayLM-Gemma-3-27B-IT"
EXPECTED_RELATION_MODEL_REVISION = "v2.0/Q4_K_M/7677fe7e2df2"
EXPECTED_RELATION_PROMPT_NAME = "relation_judge"
EXPECTED_RELATION_PROMPT_VERSION = "v3"

SEED_VERSION = 1
TOP_K = 5  # фіксовано seed_version=1; зміна K = новий seed_version

QUALIFYING_LABELS = ("same_fact", "same_event", "contradiction")

REPO_ROOT = Path(__file__).resolve().parents[2]  # experiments/event_candidates/../.. = ~/mip


def get_code_revision() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


def fetch_and_verify_registry(conn) -> None:
    """Fail-fast: qualifying-edge джерело (llm_models + relation_judgment_prompts
    identity) відповідає очікуваному v3 контракту. Без побайтової звірки
    тексту промпту -- це не job цього скрипта."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT model_name, model_revision FROM llm_models WHERE llm_model_id = %s",
            (RELATION_LLM_MODEL_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"llm_model_id={RELATION_LLM_MODEL_ID} не зареєстровано в llm_models")
    if tuple(row) != (EXPECTED_RELATION_MODEL_NAME, EXPECTED_RELATION_MODEL_REVISION):
        raise RuntimeError(f"llm_models mismatch: БД={tuple(row)}, очікується={(EXPECTED_RELATION_MODEL_NAME, EXPECTED_RELATION_MODEL_REVISION)}")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT prompt_name, prompt_version FROM relation_judgment_prompts WHERE prompt_id = %s",
            (RELATION_PROMPT_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"prompt_id={RELATION_PROMPT_ID} не зареєстровано в relation_judgment_prompts")
    if tuple(row) != (EXPECTED_RELATION_PROMPT_NAME, EXPECTED_RELATION_PROMPT_VERSION):
        raise RuntimeError(
            f"relation_judgment_prompts mismatch: БД={tuple(row)}, "
            f"очікується={(EXPECTED_RELATION_PROMPT_NAME, EXPECTED_RELATION_PROMPT_VERSION)}"
        )


def fetch_qualifying_edges(conn) -> list[tuple]:
    """Усі qualifying edges під поточним relation-judgment контрактом:
    (claim_id_a, claim_id_b, score, relation_label, judgment_id,
    candidate_pair_id). claim_id_a < claim_id_b -- канонічний порядок з
    candidate_pairs, напрямок anchor/neighbor визначається пізніше в Python
    для КОЖНОГО boku окремо (кожен claim з пари може бути anchor з іншим як
    neighbor)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cp.claim_id_a, cp.claim_id_b, cp.score, rj.relation_label,
                   rj.judgment_id, cp.candidate_pair_id
            FROM relation_judgments rj
            JOIN candidate_pairs cp ON cp.candidate_pair_id = rj.candidate_pair_id
            WHERE rj.llm_model_id = %s AND rj.prompt_id = %s AND rj.attempt_no = %s
              AND rj.status = 'valid'
              AND rj.relation_label = ANY(%s)
              AND rj.shared_referent_status = 'confirmed'
            """,
            (RELATION_LLM_MODEL_ID, RELATION_PROMPT_ID, RELATION_ATTEMPT_NO, list(QUALIFYING_LABELS)),
        )
        return cur.fetchall()


def build_adjacency(edges: list[tuple]) -> dict:
    """claim_id -> list of dict(neighbor_claim_id, score, relation_label,
    judgment_id, candidate_pair_id), симетрично для обох боків кожної edge."""
    adjacency = defaultdict(list)
    for claim_id_a, claim_id_b, score, relation_label, judgment_id, candidate_pair_id in edges:
        adjacency[claim_id_a].append({
            "neighbor_claim_id": claim_id_b, "score": score, "relation_label": relation_label,
            "judgment_id": judgment_id, "candidate_pair_id": candidate_pair_id,
        })
        adjacency[claim_id_b].append({
            "neighbor_claim_id": claim_id_a, "score": score, "relation_label": relation_label,
            "judgment_id": judgment_id, "candidate_pair_id": candidate_pair_id,
        })
    return adjacency


def fetch_existing_anchors(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT anchor_claim_id FROM event_candidates WHERE seed_version = %s", (SEED_VERSION,))
        return {row[0] for row in cur.fetchall()}


def insert_event_candidate(conn, anchor_claim_id, neighbors: list[dict], code_revision: str) -> bool:
    """Повертає True, якщо event_candidate реально вставлено (False -- race,
    ON CONFLICT DO NOTHING спрацював, member-рядки НЕ пишемо)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event_candidates (anchor_claim_id, seed_version, neighbor_count, code_revision)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (anchor_claim_id, seed_version) DO NOTHING
            RETURNING event_candidate_id
            """,
            (anchor_claim_id, SEED_VERSION, len(neighbors), code_revision),
        )
        row = cur.fetchone()
        if row is None:
            return False
        event_candidate_id = row[0]

        cur.execute(
            """
            INSERT INTO event_candidate_members
                (event_candidate_id, claim_id, role, source_candidate_pair_id, source_relation_judgment_id, rank_in_seed)
            VALUES (%s, %s, 'anchor', NULL, NULL, NULL)
            """,
            (event_candidate_id, anchor_claim_id),
        )
        for rank, n in enumerate(neighbors, start=1):
            cur.execute(
                """
                INSERT INTO event_candidate_members
                    (event_candidate_id, claim_id, role, source_candidate_pair_id, source_relation_judgment_id, rank_in_seed)
                VALUES (%s, %s, 'neighbor', %s, %s, %s)
                """,
                (event_candidate_id, n["neighbor_claim_id"], n["candidate_pair_id"], n["judgment_id"], rank),
            )
    return True


def run(limit: int) -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        fetch_and_verify_registry(conn)

        edges = fetch_qualifying_edges(conn)
        adjacency = build_adjacency(edges)
        print(f"claims with >=1 qualifying edge: {len(adjacency)}")

        existing_anchors = fetch_existing_anchors(conn)

        candidates = []
        for claim_id, neighbors in adjacency.items():
            if claim_id in existing_anchors:
                continue
            best_score = max(n["score"] for n in neighbors)
            candidates.append((best_score, claim_id))
        candidates.sort(key=lambda t: (-t[0], str(t[1])))

        batch = candidates[:limit]
        print(f"new anchors this run: {len(batch)} (already existed: {len(adjacency) - len(candidates)})")

        built = 0
        skipped_race = 0
        for _score, claim_id in batch:
            neighbors = sorted(adjacency[claim_id], key=lambda n: (-n["score"], str(n["neighbor_claim_id"])))[:TOP_K]
            inserted = insert_event_candidate(conn, claim_id, neighbors, code_revision)
            conn.commit()
            if inserted:
                built += 1
            else:
                skipped_race += 1

        print(f"SUMMARY: seed_version={SEED_VERSION} top_k={TOP_K} code_revision={code_revision} "
              f"attempted={len(batch)} built={built} skipped_race={skipped_race}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic event-candidate seed builder (v1)")
    parser.add_argument("--limit", type=int, required=True, help="max NEW anchors to process this run")
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    return run(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
