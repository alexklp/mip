#!/usr/bin/env python3
"""
experiments/content_routing/routing_worker.py — Content Routing v1 backfill.
Рахує semantic routing score (та сама логіка, що routing_scan.py, вже
перевірена вручну на 6211 eligible content: analyze=11.6%, maybe=75.1%,
skip=13.3%, keyword-audit на SKIP чистий після T_SKIP=-0.07) і пише
content_routing_decisions для routing_version=1.

На відміну від claim_extract_worker.py — тут НЕМАЄ зовнішнього непередбачуваного
виклику (LLM), тільки детерміноване обчислення поверх вже існуючих embeddings.
Тому per-item isolation НЕ потрібна: помилка означає баг у коді, а не флейкі
зовнішній сервіс, і має зупиняти прогін — той самий fail-fast патерн, що
collectors/embed_worker.py (batch commit, будь-яка помилка -> rollback +
stop, без retry-loop, ідемпотентний resume через NOT EXISTS).

sql/009_add_content_routing.sql МАЄ бути застосований ДО запуску.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

DB_DSN = "dbname=mip_dev"

EMBEDDING_MODEL_ID = 1
MODEL_NAME = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
FRAMEWORK = "sentence-transformers"
FRAMEWORK_VERSION = "5.7.0"
DIMENSION = 1024
METRIC = "cosine"
ENCODING_PARAMS = {"normalize_embeddings": True, "input_field": "text_content"}

ROUTING_VERSION = 1
PROTOTYPES_FILE = Path(__file__).parent / "prototypes_v1.json"

# Зафіксовано після routing_scan.py + keyword-audit перевірки на живому corpus.
T_SKIP = -0.07
T_ANALYZE = 0.10

REPO_ROOT = Path(__file__).resolve().parents[2]  # experiments/content_routing/../.. = ~/mip
BATCH_SIZE = 500


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


def load_model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION, device="cpu", local_files_only=True)


def load_prototype_vectors(model: SentenceTransformer) -> tuple[np.ndarray, np.ndarray]:
    data = json.loads(PROTOTYPES_FILE.read_text(encoding="utf-8"))
    rel_vectors = np.asarray(model.encode(data["relevant"], normalize_embeddings=True))
    irr_vectors = np.asarray(model.encode(data["irrelevant"], normalize_embeddings=True))
    return rel_vectors, irr_vectors


def decide(score: float) -> str:
    if score >= T_ANALYZE:
        return "analyze"
    if score <= T_SKIP:
        return "skip"
    return "maybe"


def fetch_batch(conn, limit: int) -> list[tuple]:
    """Idempotent eligibility: content з BGE-M3 embedding, без існуючого
    routing decision для поточної routing_version."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ci.content_id, e.embedding
            FROM content_items ci
            JOIN embeddings e ON e.content_id = ci.content_id AND e.embedding_model_id = %s
            WHERE NOT EXISTS (
                SELECT 1 FROM content_routing_decisions r
                WHERE r.content_id = ci.content_id AND r.routing_version = %s
            )
            ORDER BY ci.first_seen_at
            LIMIT %s
            """,
            (EMBEDDING_MODEL_ID, ROUTING_VERSION, limit),
        )
        return cur.fetchall()


def process_batch(conn, batch, rel_vectors, irr_vectors, code_revision: str) -> list[str]:
    content_ids = [row[0] for row in batch]
    vectors = np.vstack([row[1].to_numpy() for row in batch]).astype(np.float32)

    sim_rel = (vectors @ rel_vectors.T).max(axis=1)
    sim_irr = (vectors @ irr_vectors.T).max(axis=1)
    scores = sim_rel - sim_irr
    decisions = [decide(float(s)) for s in scores]

    with conn.cursor() as cur:
        for content_id, score, decision in zip(content_ids, scores, decisions):
            cur.execute(
                """
                INSERT INTO content_routing_decisions
                    (content_id, routing_version, embedding_model_id, decision, score, code_revision)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (content_id, routing_version) DO NOTHING
                """,
                (content_id, ROUTING_VERSION, EMBEDDING_MODEL_ID, decision, float(score), code_revision),
            )
    conn.commit()
    return decisions


def run(limit: int | None) -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)

        print("loading BGE-M3 model...")
        model = load_model()
        rel_vectors, irr_vectors = load_prototype_vectors(model)
        print(f"code_revision={code_revision}, routing_version={ROUTING_VERSION}, "
              f"T_SKIP={T_SKIP}, T_ANALYZE={T_ANALYZE}")

        totals = {"analyze": 0, "maybe": 0, "skip": 0}
        processed = 0
        while True:
            batch_limit = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - processed)
            if batch_limit <= 0:
                break
            batch = fetch_batch(conn, batch_limit)
            if not batch:
                break
            try:
                decisions = process_batch(conn, batch, rel_vectors, irr_vectors, code_revision)
            except Exception as exc:
                conn.rollback()
                content_ids = [row[0] for row in batch]
                print(f"BATCH FAILED, content_ids={content_ids}: {exc}", file=sys.stderr)
                print("STOPPING run — no retry loop, no skip-and-continue.", file=sys.stderr)
                return 1
            for d in decisions:
                totals[d] += 1
            processed += len(batch)
            print(f"batch committed: {len(batch)} content_ids, total processed={processed}")

        print(f"\nSUMMARY: processed={processed} analyze={totals['analyze']} "
              f"maybe={totals['maybe']} skip={totals['skip']}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Content Routing v1 backfill worker")
    parser.add_argument("--limit", type=int, default=None, help="max content_items to process (default: whole backlog)")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be > 0")
    return run(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
