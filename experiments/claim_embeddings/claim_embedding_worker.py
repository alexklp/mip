#!/usr/bin/env python3
"""
experiments/claim_embeddings/claim_embedding_worker.py — Claim embeddings backfill.

Кодує claim_text тим самим BGE-M3 (embedding_model_id=1), що вже
використовується для content_items (sql/006_add_embeddings.sql,
collectors/embed_worker.py) та routing prototypes
(experiments/content_routing/routing_worker.py) — той самий виклик
model.encode(texts, normalize_embeddings=True). Джерело тексту тут —
claims.claim_text, а не content_items.text_content, але саме кодування
(модель/revision/normalize) ідентичне, тому нової embedding_model
реєстрації не потрібно — reuse embedding_model_id=1 як є.

Як і routing_worker.py: жодного зовнішнього непередбачуваного виклику
(LLM), тільки детерміноване обчислення поверх вже існуючого claim_text.
Per-batch fail-fast (rollback + stop, без retry-loop) — помилка тут
означає баг у коді, а не флейкі зовнішній сервіс.
Ідемпотентний resume через NOT EXISTS на (claim_id, embedding_model_id).

sql/010_add_claim_embeddings.sql МАЄ бути застосований ДО запуску.
"""
from __future__ import annotations

import argparse
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

REPO_ROOT = Path(__file__).resolve().parents[2]  # experiments/claim_embeddings/../.. = ~/mip
BATCH_SIZE = 500


def get_code_revision() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


def verify_registered_model(conn) -> None:
    """Той самий fail-fast патерн, що routing_worker.py/embed_worker.py.
    ENCODING_PARAMS описує сам виклик кодувальника (normalize/модель), а не
    конкретне вхідне поле, тому reuse того самого embedding_model_id=1 для
    claim_text коректний — нова реєстрація моделі не потрібна."""
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


def fetch_batch(conn, limit: int) -> list[tuple]:
    """Idempotent eligibility: claims без існуючого embedding для поточної
    embedding_model_id. Той самий паттерн, що fetch_batch() в routing_worker.py."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.claim_id, c.claim_text
            FROM claims c
            WHERE NOT EXISTS (
                SELECT 1 FROM claim_embeddings ce
                WHERE ce.claim_id = c.claim_id AND ce.embedding_model_id = %s
            )
            ORDER BY c.created_at
            LIMIT %s
            """,
            (EMBEDDING_MODEL_ID, limit),
        )
        return cur.fetchall()


def process_batch(conn, batch, model: SentenceTransformer) -> int:
    claim_ids = [row[0] for row in batch]
    texts = [row[1] for row in batch]
    vectors = model.encode(texts, normalize_embeddings=True)

    with conn.cursor() as cur:
        for claim_id, vector in zip(claim_ids, vectors):
            cur.execute(
                """
                INSERT INTO claim_embeddings (claim_id, embedding_model_id, embedding)
                VALUES (%s, %s, %s)
                ON CONFLICT (claim_id, embedding_model_id) DO NOTHING
                """,
                (claim_id, EMBEDDING_MODEL_ID, np.asarray(vector, dtype=np.float32)),
            )
    conn.commit()
    return len(claim_ids)


def run(limit: int | None) -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)

        print("loading BGE-M3 model...")
        model = load_model()
        print(f"code_revision={code_revision}, embedding_model_id={EMBEDDING_MODEL_ID}")

        processed = 0
        while True:
            batch_limit = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - processed)
            if batch_limit <= 0:
                break
            batch = fetch_batch(conn, batch_limit)
            if not batch:
                break
            try:
                n = process_batch(conn, batch, model)
            except Exception as exc:
                conn.rollback()
                claim_ids = [row[0] for row in batch]
                print(f"BATCH FAILED, claim_ids={claim_ids}: {exc}", file=sys.stderr)
                print("STOPPING run — no retry loop, no skip-and-continue.", file=sys.stderr)
                return 1
            processed += n
            print(f"batch committed: {n} claims, total processed={processed}")

        print(f"\nSUMMARY: processed={processed}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Claim embeddings backfill worker (embedding_model_id=1)")
    parser.add_argument("--limit", type=int, default=None, help="max claims to process (default: whole backlog)")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be > 0")
    return run(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
