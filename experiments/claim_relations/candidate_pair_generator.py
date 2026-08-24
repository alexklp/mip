#!/usr/bin/env python3
"""
experiments/claim_relations/candidate_pair_generator.py — детермінований candidate
generator для relation judgment: рахує cosine similarity по ВСЬОМУ поточному
corpus claim_embeddings (embedding_model_id=1), бере глобальний top-N і
персистить його в candidate_pairs.

Спирається на ту саму cross-contour/first_seen логіку, що read-only
experiments/claim_embeddings/claim_candidate_scan.py (Варіант 1, консервативний):
  - contour_set(claim) = усі distinct contour_id серед occurrences його content_id
  - пара cross-contour, ТІЛЬКИ якщо contour_set(A) і contour_set(B) НЕ перетинаються
  - той самий run_id (= той самий content) ніколи не порівнюється сам із собою

ВАЖЛИВО (append-only контракт candidate_pairs, узгоджено явно):
  - ranking рахується по ПОВНОМУ поточному eligible corpus КОЖЕН запуск,
    БЕЗ попереднього виключення вже збережених пар;
  - --top-n — це runtime budget для ЦЬОГО запуску, НЕ матеріалізований
    "поточний топ-N";
  - вже наявні пари просто пропускаються через ON CONFLICT DO NOTHING
    ПІСЛЯ відбору top-N, на сам ranking це не впливає;
  - candidate_version — immutable identity контракту генерації (алгоритм +
    embedding_model_id + правило cross-contour фільтрації). Зміна --top-n НЕ
    вимагає нової версії. Зміна алгоритму/фільтрації — вимагає.

Детерміноване обчислення поверх вже існуючих embeddings, жодного зовнішнього
непередбачуваного виклику (LLM) — як routing_worker.py/claim_embedding_worker.py.
Одна "транзакція" (fail-fast, без retry-loop): помилка тут — баг у коді, а не
флейкі зовнішній сервіс.

sql/011_add_claim_relations.sql МАЄ бути застосований ДО запуску.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

DB_DSN = "dbname=mip_dev"

EMBEDDING_MODEL_ID = 1
MODEL_NAME = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
FRAMEWORK = "sentence-transformers"
FRAMEWORK_VERSION = "5.7.0"
DIMENSION = 1024
METRIC = "cosine"
ENCODING_PARAMS = {"normalize_embeddings": True, "input_field": "text_content"}

# immutable identity: full-corpus cross-contour cosine top-N, Варіант 1
# (повністю неперетинні contour_set), same-run виключено. Зміна --top-n НЕ
# вимагає нової версії; зміна алгоритму/фільтрації — вимагає.
CANDIDATE_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parents[2]  # experiments/claim_relations/../.. = ~/mip


def get_code_revision() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


def verify_registered_model(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT model_name, model_revision, framework, framework_version,
                   dimension, metric, encoding_params
            FROM embedding_models WHERE embedding_model_id = %s
            """,
            (EMBEDDING_MODEL_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"embedding_model_id={EMBEDDING_MODEL_ID} не зареєстровано в embedding_models.")
    expected = (MODEL_NAME, MODEL_REVISION, FRAMEWORK, FRAMEWORK_VERSION, DIMENSION, METRIC, ENCODING_PARAMS)
    if tuple(row) != expected:
        raise RuntimeError(
            "Конфігурація embedding-моделі в БД НЕ збігається з константами в цьому скрипті.\n"
            f"  БД:     {tuple(row)}\n"
            f"  Скрипт: {expected}"
        )


def fetch_claims(conn) -> list[tuple]:
    """Один рядок на claim_id: claim_id, run_id, content_id, embedding.
    Той самий JOIN-набір, що claim_candidate_scan.py — ПОВНИЙ поточний eligible
    corpus, без жодного попереднього виключення вже персистованих пар."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.claim_id, c.run_id, r.content_id, ce.embedding
            FROM claim_embeddings ce
            JOIN claims c ON c.claim_id = ce.claim_id
            JOIN claim_extraction_runs r ON r.run_id = c.run_id
            WHERE ce.embedding_model_id = %s
            """,
            (EMBEDDING_MODEL_ID,),
        )
        return cur.fetchall()


def fetch_contour_sets(conn, content_ids: list) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT io.content_id, s.contour_id
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            WHERE io.content_id = ANY(%s)
            """,
            (content_ids,),
        )
        rows = cur.fetchall()

    by_content = defaultdict(set)
    for content_id, contour_id in rows:
        if contour_id is not None:
            by_content[content_id].add(contour_id)
    return by_content


def build_claim_meta(claims_rows, contour_sets_by_content) -> dict:
    """claim_id -> dict(run_id, contour_set). Пропускає claims без жодного
    contour_id — той самий WARNING-паттерн, що claim_candidate_scan.py."""
    meta = {}
    for claim_id, run_id, content_id, _embedding in claims_rows:
        contour_set = contour_sets_by_content.get(content_id, set())
        if not contour_set:
            print(f"WARNING: claim_id={claim_id} content_id={content_id} — немає contour_id, пропускаю")
            continue
        meta[claim_id] = {"run_id": run_id, "contour_set": contour_set}
    return meta


def compute_ranked_pairs(claims_rows, meta: dict) -> list[tuple]:
    """Повертає ВСІ cross-contour candidate pairs, відсортовані за score desc.
    Рахується по ПОВНОМУ corpus, незалежно від того, що вже є в candidate_pairs
    — це і є узгоджений контракт append-only ranking."""
    usable_rows = [row for row in claims_rows if row[0] in meta]
    claim_ids = [row[0] for row in usable_rows]
    vectors = np.vstack([row[3].to_numpy() for row in usable_rows]).astype(np.float32)
    sim_matrix = vectors @ vectors.T  # embeddings normalized -> це вже cosine similarity

    n = len(claim_ids)
    pairs = []
    for i in range(n):
        meta_i = meta[claim_ids[i]]
        for j in range(i + 1, n):
            meta_j = meta[claim_ids[j]]
            if meta_i["run_id"] == meta_j["run_id"]:
                continue  # той самий run (=той самий content) — не порівнюємо
            if meta_i["contour_set"] & meta_j["contour_set"]:
                continue  # є спільний contour — не cross-contour у варіанті 1
            score = float(sim_matrix[i, j])
            id_a, id_b = claim_ids[i], claim_ids[j]
            if id_a > id_b:
                id_a, id_b = id_b, id_a
            pairs.append((score, id_a, id_b))

    pairs.sort(key=lambda x: x[0], reverse=True)
    return pairs


def persist_top_n(conn, ranked_pairs: list[tuple], top_n: int, code_revision: str) -> tuple[int, int]:
    """INSERT top-N з ON CONFLICT DO NOTHING. Виключення вже наявних пар
    відбувається ТІЛЬКИ тут, ПІСЛЯ відбору top-N — не до ranking.
    Повертає (attempted, inserted)."""
    top = ranked_pairs[:top_n]
    inserted = 0
    with conn.cursor() as cur:
        for score, id_a, id_b in top:
            cur.execute(
                """
                INSERT INTO candidate_pairs
                    (claim_id_a, claim_id_b, candidate_version, embedding_model_id, score, code_revision)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (claim_id_a, claim_id_b, candidate_version) DO NOTHING
                """,
                (id_a, id_b, CANDIDATE_VERSION, EMBEDDING_MODEL_ID, score, code_revision),
            )
            inserted += cur.rowcount
    conn.commit()
    return len(top), inserted


def run(top_n: int) -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)

        claims_rows = fetch_claims(conn)
        print(f"claims with embeddings: {len(claims_rows)}")

        content_ids = list({row[2] for row in claims_rows})
        contour_sets_by_content = fetch_contour_sets(conn, content_ids)

        meta = build_claim_meta(claims_rows, contour_sets_by_content)
        print(f"claims with valid contour metadata: {len(meta)}")

        try:
            ranked_pairs = compute_ranked_pairs(claims_rows, meta)
        except Exception as exc:
            print(f"RANKING FAILED: {exc}", file=sys.stderr)
            print("STOPPING run — no retry loop, no partial persistence.", file=sys.stderr)
            return 1

        print(f"cross-contour candidate pairs (full corpus, before top-N cut): {len(ranked_pairs)}")

        if not ranked_pairs:
            print("SUMMARY: attempted=0 inserted=0 (no eligible pairs)")
            return 0

        try:
            attempted, inserted = persist_top_n(conn, ranked_pairs, top_n, code_revision)
        except Exception as exc:
            conn.rollback()
            print(f"PERSIST FAILED: {exc}", file=sys.stderr)
            print("STOPPING run — no retry loop, no skip-and-continue.", file=sys.stderr)
            return 1

        print(
            f"\nSUMMARY: candidate_version={CANDIDATE_VERSION} code_revision={code_revision} "
            f"attempted={attempted} inserted={inserted} already_existed={attempted - inserted}"
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic candidate pair generator (full-corpus ranking, append-only persistence)"
    )
    parser.add_argument("--top-n", type=int, required=True, help="runtime budget for THIS run (no default)")
    args = parser.parse_args()
    if args.top_n <= 0:
        parser.error("--top-n must be > 0")
    return run(args.top_n)


if __name__ == "__main__":
    raise SystemExit(main())
