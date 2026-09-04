#!/usr/bin/env python3
"""
Targeted relation judgment for candidate_version=2 segment claim pairs.

Contract:
- explicit target segment claim run (--run-id);
- only candidate_version=2 pairs involving that run;
- current relation-judge model/prompt/attempt identity;
- exact occurrence provenance for both claims;
- reuses existing relation_judgment_worker prompt, validator,
  Mamay transport and persistence logic;
- no global candidate backlog scan.
"""

from __future__ import annotations

import argparse
from uuid import UUID

import psycopg

from relation_judgment_worker import (
    ATTEMPT_NO,
    DB_DSN,
    LLM_MODEL_ID,
    PROMPT_ID,
    build_context_snippet,
    fetch_and_verify_registry,
    get_code_revision,
    process_pair,
)

CANDIDATE_VERSION = 2


def verify_target_run(conn, run_id: UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT segment_id, status
            FROM claim_extraction_runs
            WHERE run_id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(f"run_id={run_id} not found")

    segment_id, status = row

    if segment_id is None:
        raise RuntimeError(
            f"run_id={run_id} is whole-content scope; segment scope required"
        )

    if status != "valid":
        raise RuntimeError(
            f"run_id={run_id} status={status!r}; valid required"
        )


def fetch_batch(conn, run_id: UUID, limit: int) -> list[tuple]:
    """
    Only unjudged candidate_version=2 pairs involving the explicit target run.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cp.candidate_pair_id,
                cp.claim_id_a,
                cp.claim_id_b,
                cp.score
            FROM candidate_pairs cp
            JOIN claims ca
              ON ca.claim_id = cp.claim_id_a
            JOIN claims cb
              ON cb.claim_id = cp.claim_id_b
            JOIN claim_extraction_runs ra
              ON ra.run_id = ca.run_id
            JOIN claim_extraction_runs rb
              ON rb.run_id = cb.run_id
            WHERE cp.candidate_version = %s
              AND (ra.run_id = %s OR rb.run_id = %s)
              AND NOT EXISTS (
                  SELECT 1
                  FROM relation_judgments rj
                  WHERE rj.candidate_pair_id = cp.candidate_pair_id
                    AND rj.llm_model_id = %s
                    AND rj.prompt_id = %s
                    AND rj.attempt_no = %s
              )
            ORDER BY cp.score DESC, cp.candidate_pair_id
            LIMIT %s
            """,
            (
                CANDIDATE_VERSION,
                run_id,
                run_id,
                LLM_MODEL_ID,
                PROMPT_ID,
                ATTEMPT_NO,
                limit,
            ),
        )
        return cur.fetchall()


def fetch_claims_meta_exact(conn, claim_ids: list[UUID]) -> dict:
    """
    Exact provenance:
      claim -> segment -> segmentation_run -> occurrence_content
            -> item_occurrence -> source

    candidate_version=2 requires segment-scoped claims on both sides.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.claim_id,
                c.claim_text,
                c.evidence_span,
                c.evidence_start,
                c.evidence_end,
                ci.title,
                cs.text_content AS evidence_text,
                r.segment_id,
                oc.occurrence_id,
                io.collected_at,
                s.contour_id,
                s.name,
                s.url_or_handle,
                s.source_type
            FROM claims c
            JOIN claim_extraction_runs r
              ON r.run_id = c.run_id
             AND r.segment_id IS NOT NULL
             AND r.status = 'valid'
            JOIN content_items ci
              ON ci.content_id = r.content_id
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
            WHERE c.claim_id = ANY(%s)
            ORDER BY c.claim_id
            """,
            (claim_ids,),
        )
        rows = cur.fetchall()

    by_claim: dict = {}

    for (
        claim_id,
        claim_text,
        evidence_span,
        evidence_start,
        evidence_end,
        title,
        evidence_text,
        segment_id,
        occurrence_id,
        collected_at,
        contour_id,
        source_name,
        url_or_handle,
        source_type,
    ) in rows:
        if claim_id in by_claim:
            raise RuntimeError(
                f"claim_id={claim_id} resolved to multiple exact occurrences"
            )

        if contour_id is None:
            raise RuntimeError(
                f"claim_id={claim_id} exact occurrence has no contour_id"
            )

        by_claim[claim_id] = {
            "claim_text": claim_text,
            "evidence_span": evidence_span,
            "title": title,
            "context_snippet": build_context_snippet(
                evidence_text,
                evidence_start,
                evidence_end,
            ),
            # Keep existing relation prompt payload schema intact.
            "contour_set": [contour_id],
            "first_seen": collected_at,
            "display_source": (
                f"{source_type}:{source_name} ({url_or_handle})"
            ),
            "segment_id": segment_id,
            "occurrence_id": occurrence_id,
        }

    missing = sorted(set(claim_ids) - set(by_claim), key=lambda x: x.int)
    if missing:
        raise RuntimeError(
            "candidate_version=2 contains claims without exact segment "
            f"provenance: {missing}"
        )

    return by_claim


def run(run_id: UUID, limit: int, dry_run: bool) -> int:
    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        verify_target_run(conn, run_id)
        prompt_text = fetch_and_verify_registry(conn)
        batch = fetch_batch(conn, run_id, limit)

        print(
            f"[segment_relation_judgment_worker] "
            f"run_id={run_id} "
            f"candidate_version={CANDIDATE_VERSION} "
            f"batch={len(batch)} "
            f"code_revision={code_revision}"
        )

        if not batch:
            print("SUMMARY: nothing to judge")
            return 0

        claim_ids = sorted(
            {
                claim_id
                for _pair_id, claim_a, claim_b, _score in batch
                for claim_id in (claim_a, claim_b)
            },
            key=lambda x: x.int,
        )

        meta = fetch_claims_meta_exact(conn, claim_ids)

        for pair_id, claim_a, claim_b, score in batch:
            ma = meta[claim_a]
            mb = meta[claim_b]

            print(
                f"  pair={pair_id} score={score:.4f} "
                f"A=C{ma['contour_set'][0]}/{ma['occurrence_id']} "
                f"B=C{mb['contour_set'][0]}/{mb['occurrence_id']}"
            )

            if ma["occurrence_id"] == mb["occurrence_id"]:
                raise RuntimeError(
                    f"pair={pair_id} violates same-occurrence exclusion"
                )

            if ma["contour_set"][0] == mb["contour_set"][0]:
                raise RuntimeError(
                    f"pair={pair_id} violates cross-contour contract"
                )

        if dry_run:
            print(
                f"SUMMARY: dry_run=true "
                f"pairs={len(batch)} exact_claims={len(meta)}"
            )
            return 0

        summary = {
            "valid": 0,
            "invalid": 0,
            "transport_error": 0,
            "error": 0,
        }

        for pair in batch:
            status = process_pair(
                conn,
                pair,
                meta,
                prompt_text,
                code_revision,
            )
            summary[status] = summary.get(status, 0) + 1

        print(
            f"SUMMARY: total={len(batch)} "
            + " ".join(f"{k}={v}" for k, v in summary.items())
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        type=UUID,
        required=True,
        help="target valid segment claim-extraction run",
    )
    parser.add_argument(
        "--limit",
        type=int,
        required=True,
        help="max candidate_version=2 pairs to judge",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate targeted selection/provenance without Mamay",
    )

    args = parser.parse_args()

    if args.limit <= 0:
        parser.error("--limit must be > 0")

    return run(args.run_id, args.limit, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
