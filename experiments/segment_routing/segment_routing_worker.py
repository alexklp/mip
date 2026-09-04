#!/usr/bin/env python3
"""Minimal point-run routing worker for persisted content segments.

Pipeline:
successful segmentation_run
  -> complete BGE-M3 segment embedding coverage
  -> deterministic prototype routing
  -> segment_routing_decisions

No scheduler, backlog scan or production batching.
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
ENCODING_PARAMS = {
    "normalize_embeddings": True,
    "input_field": "text_content",
}

ROUTING_VERSION = 1

# Segment-level pilot-calibrated thresholds.
T_SKIP = -0.07
T_ANALYZE = 0.095

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOTYPES_FILE = (
    REPO_ROOT / "experiments" / "content_routing" / "prototypes_v1.json"
)


def get_code_revision() -> str:
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
    ).decode().strip()

    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    return f"{sha}+dirty" if dirty else sha


def verify_registered_model(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                model_name,
                model_revision,
                framework,
                framework_version,
                dimension,
                metric,
                encoding_params
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


def verify_run_and_embedding_coverage(
    conn,
    segmentation_run_id: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                sr.status,
                sr.segment_count,
                count(DISTINCT cs.segment_id) AS persisted_segments,
                count(DISTINCT se.segment_id) AS embedded_segments
            FROM segmentation_runs sr
            LEFT JOIN content_segments cs
              ON cs.segmentation_run_id = sr.segmentation_run_id
            LEFT JOIN segment_embeddings se
              ON se.segment_id = cs.segment_id
             AND se.embedding_model_id = %s
            WHERE sr.segmentation_run_id = %s
            GROUP BY sr.status, sr.segment_count
            """,
            (EMBEDDING_MODEL_ID, segmentation_run_id),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError("segmentation_run_id not found")

    status, segment_count, persisted_segments, embedded_segments = row

    if status != "success":
        raise RuntimeError(
            f"segmentation_run status={status!r}, expected 'success'"
        )

    if segment_count is None or segment_count <= 0:
        raise RuntimeError(
            f"invalid segment_count={segment_count!r}"
        )

    if persisted_segments != segment_count:
        raise RuntimeError(
            "segment persistence coverage mismatch: "
            f"expected={segment_count}, persisted={persisted_segments}"
        )

    if embedded_segments != segment_count:
        raise RuntimeError(
            "segment embedding coverage incomplete: "
            f"expected={segment_count}, embedded={embedded_segments}"
        )

    return segment_count


def fetch_unrouted_segments(conn, segmentation_run_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cs.segment_id,
                cs.segment_index,
                se.embedding
            FROM content_segments cs
            JOIN segment_embeddings se
              ON se.segment_id = cs.segment_id
             AND se.embedding_model_id = %s
            WHERE cs.segmentation_run_id = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM segment_routing_decisions srd
                  WHERE srd.segment_id = cs.segment_id
                    AND srd.routing_version = %s
              )
            ORDER BY cs.segment_index
            """,
            (
                EMBEDDING_MODEL_ID,
                segmentation_run_id,
                ROUTING_VERSION,
            ),
        )
        return cur.fetchall()


def load_model() -> SentenceTransformer:
    return SentenceTransformer(
        MODEL_NAME,
        revision=MODEL_REVISION,
        device="cpu",
        local_files_only=True,
    )


def load_prototype_vectors(model: SentenceTransformer):
    data = json.loads(
        PROTOTYPES_FILE.read_text(encoding="utf-8")
    )

    relevant = np.asarray(
        model.encode(
            data["relevant"],
            normalize_embeddings=True,
        ),
        dtype=np.float32,
    )

    irrelevant = np.asarray(
        model.encode(
            data["irrelevant"],
            normalize_embeddings=True,
        ),
        dtype=np.float32,
    )

    return relevant, irrelevant


def decide(score: float) -> str:
    if score >= T_ANALYZE:
        return "analyze"

    if score <= T_SKIP:
        return "skip"

    return "maybe"


def process(segmentation_run_id: str) -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)

        verify_registered_model(conn)

        segment_count = verify_run_and_embedding_coverage(
            conn,
            segmentation_run_id,
        )

        rows = fetch_unrouted_segments(
            conn,
            segmentation_run_id,
        )

        if not rows:
            print(
                "[segment_routing_worker] "
                "SKIP: current routing identity already complete"
            )
            return 0

        if len(rows) != segment_count:
            raise RuntimeError(
                "partial existing routing state detected: "
                f"segment_count={segment_count}, unrouted={len(rows)}"
            )

        print(
            f"[segment_routing_worker] eligible_segments={len(rows)}"
        )
        print("[segment_routing_worker] loading BGE-M3 prototypes...")

        model = load_model()
        relevant, irrelevant = load_prototype_vectors(model)

        segment_ids = [row[0] for row in rows]
        segment_indexes = [row[1] for row in rows]

        vectors = np.vstack(
            [
                row[2].to_numpy()
                for row in rows
            ]
        ).astype(np.float32)

        if vectors.shape != (len(rows), DIMENSION):
            raise RuntimeError(
                "embedding matrix dimension mismatch: "
                f"got={vectors.shape}, "
                f"expected=({len(rows)}, {DIMENSION})"
            )

        sim_rel = (vectors @ relevant.T).max(axis=1)
        sim_irr = (vectors @ irrelevant.T).max(axis=1)

        scores = sim_rel - sim_irr
        decisions = [
            decide(float(score))
            for score in scores
        ]

        try:
            with conn.cursor() as cur:
                for segment_id, score, decision in zip(
                    segment_ids,
                    scores,
                    decisions,
                ):
                    cur.execute(
                        """
                        INSERT INTO segment_routing_decisions (
                            segment_id,
                            routing_version,
                            embedding_model_id,
                            decision,
                            score,
                            code_revision
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            segment_id,
                            ROUTING_VERSION,
                            EMBEDDING_MODEL_ID,
                            decision,
                            float(score),
                            code_revision,
                        ),
                    )

            conn.commit()

        except Exception:
            conn.rollback()
            raise

        for segment_index, score, decision in zip(
            segment_indexes,
            scores,
            decisions,
        ):
            print(
                "[segment_routing_worker] "
                f"segment={segment_index} "
                f"score={float(score):.4f} "
                f"decision={decision}"
            )

        totals = {
            "analyze": decisions.count("analyze"),
            "maybe": decisions.count("maybe"),
            "skip": decisions.count("skip"),
        }

        print(
            "[segment_routing_worker] committed="
            f"{len(rows)} "
            f"analyze={totals['analyze']} "
            f"maybe={totals['maybe']} "
            f"skip={totals['skip']}"
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
            "[segment_routing_worker] FAILED: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
