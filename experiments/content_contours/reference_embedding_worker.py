#!/usr/bin/env python3
"""Embed active semantic/both contour reference entries with BGE-M3."""

from __future__ import annotations

import sys

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

DB_DSN = "dbname=mip_dev"

EMBEDDING_MODEL_ID = 1
MODEL_NAME = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"

BATCH_SIZE = 500


def load_model() -> SentenceTransformer:
    return SentenceTransformer(
        MODEL_NAME,
        revision=MODEL_REVISION,
        device="cpu",
        local_files_only=True,
    )


def fetch_batch(conn, limit: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.reference_id, e.reference_text
            FROM contour_reference_entries e
            WHERE e.active
              AND e.match_mode IN ('semantic', 'both')
              AND NOT EXISTS (
                  SELECT 1
                  FROM contour_reference_embeddings re
                  WHERE re.reference_id = e.reference_id
                    AND re.embedding_model_id = %s
              )
            ORDER BY e.reference_id
            LIMIT %s
            """,
            (EMBEDDING_MODEL_ID, limit),
        )
        return cur.fetchall()


def process_batch(conn, batch, model) -> int:
    ids = [row[0] for row in batch]
    texts = [row[1] for row in batch]

    vectors = model.encode(
        texts,
        normalize_embeddings=True,
    )

    with conn.cursor() as cur:
        for reference_id, vector in zip(ids, vectors):
            cur.execute(
                """
                INSERT INTO contour_reference_embeddings (
                    reference_id,
                    embedding_model_id,
                    embedding
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (reference_id, embedding_model_id) DO NOTHING
                """,
                (
                    reference_id,
                    EMBEDDING_MODEL_ID,
                    np.asarray(vector, dtype=np.float32),
                ),
            )

    conn.commit()
    return len(ids)


def main() -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)

        print("loading BGE-M3...")
        model = load_model()

        processed = 0

        while True:
            batch = fetch_batch(conn, BATCH_SIZE)
            if not batch:
                break

            try:
                n = process_batch(conn, batch, model)
            except Exception as exc:
                conn.rollback()
                print(f"BATCH FAILED: {exc}", file=sys.stderr)
                return 1

            processed += n
            print(f"batch committed: {n}, total processed={processed}")

        print(f"SUMMARY: processed={processed}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
