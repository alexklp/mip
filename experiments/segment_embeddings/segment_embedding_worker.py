#!/usr/bin/env python3
"""Minimal BGE-M3 embedding worker for persisted content segments.

Point-run only: one successful segmentation_run at a time.
No scheduler, backlog scan or production batching.
"""
from __future__ import annotations

import argparse
import sys

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
ENCODING_PARAMS = {
    "normalize_embeddings": True,
    "input_field": "text_content",
}


def verify_registered_model(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT model_name, model_revision, framework, framework_version,
                   dimension, metric, encoding_params
            FROM embedding_models
            WHERE embedding_model_id = %s
            """,
            (EMBEDDING_MODEL_ID,),
        )
        row = cur.fetchone()

    expected = (
        MODEL_NAME,
        MODEL_REVISION,
        FRAMEWORK,
        FRAMEWORK_VERSION,
        DIMENSION,
        METRIC,
        ENCODING_PARAMS,
    )

    if row is None:
        raise RuntimeError(
            f"embedding_model_id={EMBEDDING_MODEL_ID} not found"
        )

    if tuple(row) != expected:
        raise RuntimeError(
            "embedding model registry mismatch\n"
            f"DB:       {tuple(row)}\n"
            f"expected: {expected}"
        )


def load_model() -> SentenceTransformer:
    return SentenceTransformer(
        MODEL_NAME,
        revision=MODEL_REVISION,
        device="cpu",
        local_files_only=True,
    )


def fetch_segments(conn, segmentation_run_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cs.segment_id, cs.segment_index, cs.text_content
            FROM content_segments cs
            JOIN segmentation_runs sr
              ON sr.segmentation_run_id = cs.segmentation_run_id
            WHERE sr.segmentation_run_id = %s
              AND sr.status = 'success'
              AND NOT EXISTS (
                  SELECT 1
                  FROM segment_embeddings se
                  WHERE se.segment_id = cs.segment_id
                    AND se.embedding_model_id = %s
              )
            ORDER BY cs.segment_index
            """,
            (segmentation_run_id, EMBEDDING_MODEL_ID),
        )
        return cur.fetchall()


def verify_run_exists(conn, segmentation_run_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, segment_count
            FROM segmentation_runs
            WHERE segmentation_run_id = %s
            """,
            (segmentation_run_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError("segmentation_run_id not found")

    status, segment_count = row
    if status != "success":
        raise RuntimeError(
            f"segmentation_run status={status!r}, expected 'success'"
        )

    if segment_count is None or segment_count <= 0:
        raise RuntimeError(
            f"invalid segment_count={segment_count!r}"
        )


def process(segmentation_run_id: str) -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)

        verify_registered_model(conn)
        verify_run_exists(conn, segmentation_run_id)

        segments = fetch_segments(conn, segmentation_run_id)

        if not segments:
            print("[segment_embedding_worker] SKIP: no missing embeddings")
            return 0

        print(
            f"[segment_embedding_worker] eligible_segments={len(segments)}"
        )
        print("[segment_embedding_worker] loading BGE-M3...")
        model = load_model()

        segment_ids = [row[0] for row in segments]
        texts = [row[2] for row in segments]

        vectors = model.encode(
            texts,
            normalize_embeddings=ENCODING_PARAMS["normalize_embeddings"],
        )

        for segment_id, vector in zip(segment_ids, vectors):
            if len(vector) != DIMENSION:
                raise RuntimeError(
                    f"dimension mismatch for segment_id={segment_id}: "
                    f"got {len(vector)}, expected {DIMENSION}"
                )

        try:
            with conn.cursor() as cur:
                for segment_id, vector in zip(segment_ids, vectors):
                    cur.execute(
                        """
                        INSERT INTO segment_embeddings (
                            segment_id,
                            embedding_model_id,
                            embedding
                        )
                        VALUES (%s, %s, %s)
                        ON CONFLICT (segment_id, embedding_model_id)
                        DO NOTHING
                        """,
                        (segment_id, EMBEDDING_MODEL_ID, vector),
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        print(
            f"[segment_embedding_worker] committed={len(segment_ids)}"
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--segmentation-run-id",
        required=True,
        help="one successful segmentation_runs.segmentation_run_id",
    )
    args = parser.parse_args()

    try:
        return process(args.segmentation_run_id)
    except Exception as exc:
        print(
            f"[segment_embedding_worker] FAILED: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
