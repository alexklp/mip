#!/usr/bin/env python3
"""
Targeted candidate generation for live segment claims.

candidate_version=2 contract:
- source scope is one explicit valid segment claim-extraction run (--run-id);
- both target and candidate claims MUST come from valid segment-scoped runs;
- embeddings: registered BGE-M3 embedding_model_id=1;
- provenance is resolved through the exact occurrence path:
    claim -> segment -> segmentation_run -> occurrence_content
          -> item_occurrence -> source;
- same run, same occurrence and same exact contour are excluded;
- similarity is exact cosine over normalized embeddings;
- top-K is selected independently for each target claim;
- --per-claim-k is a runtime budget; changing it does NOT require a new candidate_version;
- persistence is append-only into existing candidate_pairs;
- no LLM, no global all-pairs ranking, no score threshold in v2.
"""

from __future__ import annotations

import argparse
from uuid import UUID

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from candidate_pair_generator import (
    EMBEDDING_MODEL_ID,
    get_code_revision,
    verify_registered_model,
)

DB_DSN = "dbname=mip_dev"

# Immutable identity:
# targeted segment-only exact-cosine retrieval with exact occurrence provenance,
# cross-contour filtering and top-K per target claim.
CANDIDATE_VERSION = 2


def verify_target_run(conn, run_id: UUID) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                r.segment_id,
                r.status,
                count(c.claim_id) AS claims_total,
                count(ce.claim_id) AS embedded_total
            FROM claim_extraction_runs r
            LEFT JOIN claims c
              ON c.run_id = r.run_id
            LEFT JOIN claim_embeddings ce
              ON ce.claim_id = c.claim_id
             AND ce.embedding_model_id = %s
            WHERE r.run_id = %s
            GROUP BY r.segment_id, r.status
            """,
            (EMBEDDING_MODEL_ID, run_id),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(f"run_id={run_id} not found")

    segment_id, status, claims_total, embedded_total = row

    if segment_id is None:
        raise RuntimeError(
            f"run_id={run_id} is whole-content scope; segment scope required"
        )

    if status != "valid":
        raise RuntimeError(
            f"run_id={run_id} status={status!r}; valid required"
        )

    if claims_total <= 0:
        raise RuntimeError(f"run_id={run_id} has no claims")

    if claims_total != embedded_total:
        raise RuntimeError(
            f"run_id={run_id} embedding coverage incomplete: "
            f"{embedded_total}/{claims_total}"
        )

    return segment_id, claims_total


def fetch_target_claims(conn, run_id: UUID) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.claim_id,
                c.claim_text,
                r.run_id,
                oc.occurrence_id,
                s.name AS source_name,
                s.contour_id,
                ce.embedding
            FROM claim_extraction_runs r
            JOIN claims c
              ON c.run_id = r.run_id
            JOIN claim_embeddings ce
              ON ce.claim_id = c.claim_id
             AND ce.embedding_model_id = %s
            JOIN content_segments cs
              ON cs.segment_id = r.segment_id
            JOIN segmentation_runs sr
              ON sr.segmentation_run_id = cs.segmentation_run_id
            JOIN occurrence_content oc
              ON oc.occurrence_content_id = sr.occurrence_content_id
            JOIN item_occurrences io
              ON io.occurrence_id = oc.occurrence_id
            JOIN sources s
              ON s.source_id = io.source_id
            WHERE r.run_id = %s
            ORDER BY c.created_at, c.claim_id
            """,
            (EMBEDDING_MODEL_ID, run_id),
        )
        rows = cur.fetchall()

    if not rows:
        raise RuntimeError(f"run_id={run_id} produced no target rows")

    provenance = {
        (row[3], row[4], row[5])
        for row in rows
    }
    if len(provenance) != 1:
        raise RuntimeError(
            f"run_id={run_id} resolved to multiple exact provenances: "
            f"{provenance}"
        )

    occurrence_id, source_name, contour_id = next(iter(provenance))

    if contour_id is None:
        raise RuntimeError(
            f"run_id={run_id} exact occurrence has no contour_id"
        )

    return rows


def fetch_candidate_corpus(conn, target_run_id: UUID) -> list[tuple]:
    """
    Segment claims only. Exact provenance is resolved per claim through its
    own segment occurrence, never through aggregated content_id occurrences.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.claim_id,
                c.claim_text,
                r.run_id,
                oc.occurrence_id,
                s.name AS source_name,
                s.contour_id,
                ce.embedding
            FROM claims c
            JOIN claim_extraction_runs r
              ON r.run_id = c.run_id
             AND r.segment_id IS NOT NULL
             AND r.status = 'valid'
            JOIN claim_embeddings ce
              ON ce.claim_id = c.claim_id
             AND ce.embedding_model_id = %s
            JOIN content_segments cs
              ON cs.segment_id = r.segment_id
            JOIN segmentation_runs sr
              ON sr.segmentation_run_id = cs.segmentation_run_id
            JOIN occurrence_content oc
              ON oc.occurrence_content_id = sr.occurrence_content_id
            JOIN item_occurrences io
              ON io.occurrence_id = oc.occurrence_id
            JOIN sources s
              ON s.source_id = io.source_id
            WHERE r.run_id <> %s
              AND s.contour_id IS NOT NULL
            ORDER BY c.claim_id
            """,
            (EMBEDDING_MODEL_ID, target_run_id),
        )
        return cur.fetchall()


def canonical_pair(claim_id_a: UUID, claim_id_b: UUID) -> tuple[UUID, UUID]:
    if claim_id_a.int < claim_id_b.int:
        return claim_id_a, claim_id_b
    return claim_id_b, claim_id_a


def build_ranked_pairs(
    targets: list[tuple],
    corpus: list[tuple],
    per_claim_k: int,
) -> list[dict]:
    results: list[dict] = []

    for target in targets:
        (
            target_claim_id,
            target_text,
            target_run_id,
            target_occurrence_id,
            target_source,
            target_contour,
            target_embedding,
        ) = target

        target_vector = target_embedding.to_numpy().astype(np.float32)

        ranked = []

        for candidate in corpus:
            (
                candidate_claim_id,
                candidate_text,
                candidate_run_id,
                candidate_occurrence_id,
                candidate_source,
                candidate_contour,
                candidate_embedding,
            ) = candidate

            if candidate_run_id == target_run_id:
                continue
            if candidate_occurrence_id == target_occurrence_id:
                continue
            if candidate_contour == target_contour:
                continue

            candidate_vector = candidate_embedding.to_numpy().astype(np.float32)

            score = float(np.dot(target_vector, candidate_vector))
            score = max(-1.0, min(1.0, score))

            ranked.append(
                (
                    score,
                    candidate_claim_id,
                    candidate_text,
                    candidate_source,
                    candidate_contour,
                )
            )

        ranked.sort(
            key=lambda row: (-row[0], row[1].int)
        )

        for (
            score,
            candidate_claim_id,
            candidate_text,
            candidate_source,
            candidate_contour,
        ) in ranked[:per_claim_k]:
            claim_id_a, claim_id_b = canonical_pair(
                target_claim_id,
                candidate_claim_id,
            )

            results.append(
                {
                    "claim_id_a": claim_id_a,
                    "claim_id_b": claim_id_b,
                    "score": score,
                    "target_claim_id": target_claim_id,
                    "target_text": target_text,
                    "target_source": target_source,
                    "target_contour": target_contour,
                    "candidate_claim_id": candidate_claim_id,
                    "candidate_text": candidate_text,
                    "candidate_source": candidate_source,
                    "candidate_contour": candidate_contour,
                }
            )

    return results


def persist_pairs(conn, pairs: list[dict], code_revision: str) -> tuple[int, int]:
    inserted = 0

    with conn.cursor() as cur:
        for pair in pairs:
            cur.execute(
                """
                INSERT INTO candidate_pairs
                    (
                        claim_id_a,
                        claim_id_b,
                        candidate_version,
                        embedding_model_id,
                        score,
                        code_revision
                    )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT
                    (claim_id_a, claim_id_b, candidate_version)
                DO NOTHING
                """,
                (
                    pair["claim_id_a"],
                    pair["claim_id_b"],
                    CANDIDATE_VERSION,
                    EMBEDDING_MODEL_ID,
                    pair["score"],
                    code_revision,
                ),
            )
            inserted += cur.rowcount

    conn.commit()
    return len(pairs), inserted


def run(run_id: UUID, per_claim_k: int, dry_run: bool) -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)

        segment_id, claims_total = verify_target_run(conn, run_id)
        targets = fetch_target_claims(conn, run_id)
        corpus = fetch_candidate_corpus(conn, run_id)

        occurrence_id = targets[0][3]
        source_name = targets[0][4]
        contour_id = targets[0][5]

        print(
            f"[segment_candidate_pair_worker] "
            f"run_id={run_id} "
            f"segment_id={segment_id} "
            f"claims={claims_total} "
            f"source={source_name!r} "
            f"contour={contour_id} "
            f"candidate_corpus={len(corpus)} "
            f"candidate_version={CANDIDATE_VERSION} "
            f"code_revision={code_revision}"
        )

        pairs = build_ranked_pairs(
            targets,
            corpus,
            per_claim_k,
        )

        print(
            f"[segment_candidate_pair_worker] "
            f"proposed_pairs={len(pairs)} "
            f"per_claim_k={per_claim_k}"
        )

        for pair in pairs:
            print(
                f"  score={pair['score']:.4f} "
                f"target={pair['target_claim_id']} "
                f"{pair['target_source']}[C{pair['target_contour']}] "
                f"{pair['target_text'][:80]!r} "
                f"-> candidate={pair['candidate_claim_id']} "
                f"{pair['candidate_source']}[C{pair['candidate_contour']}] "
                f"{pair['candidate_text'][:80]!r}"
            )

        if dry_run:
            print(
                f"SUMMARY: dry_run=true proposed={len(pairs)} inserted=0"
            )
            return 0

        try:
            attempted, inserted = persist_pairs(
                conn,
                pairs,
                code_revision,
            )
        except Exception:
            conn.rollback()
            raise

        print(
            f"SUMMARY: candidate_version={CANDIDATE_VERSION} "
            f"run_id={run_id} "
            f"attempted={attempted} "
            f"inserted={inserted} "
            f"already_existed={attempted - inserted}"
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        type=UUID,
        required=True,
        help="valid segment claim_extraction_runs.run_id",
    )
    parser.add_argument(
        "--per-claim-k",
        type=int,
        required=True,
        help="maximum candidates retained for each target claim",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print candidates without persistence",
    )

    args = parser.parse_args()

    if args.per_claim_k <= 0:
        parser.error("--per-claim-k must be > 0")

    return run(
        run_id=args.run_id,
        per_claim_k=args.per_claim_k,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
