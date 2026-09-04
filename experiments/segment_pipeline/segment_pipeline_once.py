#!/usr/bin/env python3
"""
Bounded orchestration for the accepted segment-processing path.

Policy v1:
- source content routing: analyze only;
- RSS occurrence full-text only;
- resume deepest partial state first;
- point-run existing workers, no duplicated processing logic;
- Mamay workloads serialized with the existing mamay.lock;
- segment claims: analyze only and bounded by explicit max input chars;
- valid segment claims are embedded by exact claim run_id;
- explicit occurrence and claim budgets;
- no scheduler in this module.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import psycopg

REPO = Path.home() / "mip"
DB_DSN = "dbname=mip_dev"

sys.path.insert(0, str(REPO / "experiments" / "occurrence_content"))
sys.path.insert(0, str(REPO / "experiments" / "content_segmentation"))
sys.path.insert(0, str(REPO / "experiments" / "segment_embeddings"))
sys.path.insert(0, str(REPO / "experiments" / "segment_routing"))
sys.path.insert(0, str(REPO / "experiments" / "segment_claim_extraction"))

from occurrence_content_worker import (  # noqa: E402
    DEFAULT_MAX_ATTEMPTS,
    EXTRACTOR,
    EXTRACTOR_VERSION,
    EXTRACTION_PROFILE_VERSION,
)
from segmentation_worker import (  # noqa: E402
    BOUNDARY_METHOD,
    LLM_MODEL_ID as SEG_LLM_MODEL_ID,
    PROMPT_ID as SEG_PROMPT_ID,
    SECTIONIZER_VERSION,
)
from segment_embedding_worker import (  # noqa: E402
    EMBEDDING_MODEL_ID,
)
from segment_routing_worker import (  # noqa: E402
    ROUTING_VERSION,
)
from segment_claim_extract_worker import (  # noqa: E402
    LLM_MODEL_ID as CLAIM_LLM_MODEL_ID,
    PROMPT_ID as CLAIM_PROMPT_ID,
)

OCCURRENCE_WORKER = REPO / "experiments/occurrence_content/occurrence_content_worker.py"
SEGMENTATION_WORKER = REPO / "experiments/content_segmentation/segmentation_worker.py"
SEGMENT_EMBED_WORKER = REPO / "experiments/segment_embeddings/segment_embedding_worker.py"
SEGMENT_ROUTING_WORKER = REPO / "experiments/segment_routing/segment_routing_worker.py"
SEGMENT_CLAIM_WORKER = REPO / "experiments/segment_claim_extraction/segment_claim_extract_worker.py"
CLAIM_EMBED_WORKER = REPO / "experiments/claim_embeddings/claim_embedding_worker.py"

MAMAY_LOCK = Path.home() / ".local/state/mip/locks/mamay.lock"
MAMAY_HEALTH = "http://127.0.0.1:8080/health"
FLOCK_BUSY_EXIT = 75


class MamayBusy(RuntimeError):
    pass


class MamayUnavailable(RuntimeError):
    pass


def current_fulltext(conn, occurrence_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT occurrence_content_id
            FROM occurrence_content
            WHERE occurrence_id = %s
              AND extractor = %s
              AND extractor_version = %s
              AND extraction_profile_version = %s
              AND status = 'success'
            ORDER BY attempt_no DESC
            LIMIT 1
            """,
            (
                occurrence_id,
                EXTRACTOR,
                EXTRACTOR_VERSION,
                EXTRACTION_PROFILE_VERSION,
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None


def current_segmentation(conn, occurrence_content_id):
    if occurrence_content_id is None:
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT segmentation_run_id
            FROM segmentation_runs
            WHERE occurrence_content_id = %s
              AND sectionizer_version = %s
              AND boundary_method = %s
              AND llm_model_id = %s
              AND prompt_id = %s
              AND status = 'success'
            ORDER BY attempt_no DESC
            LIMIT 1
            """,
            (
                occurrence_content_id,
                SECTIONIZER_VERSION,
                BOUNDARY_METHOD,
                SEG_LLM_MODEL_ID,
                SEG_PROMPT_ID,
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None


def fetch_work_items(conn, limit, max_claim_chars):
    """
    Finish deeper partial states before fetching fresh articles:
      rank 0: segmentation already exists but downstream is incomplete
      rank 1: full-text exists but segmentation is missing
      rank 2: full-text still needs extraction
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH current_success AS (
                SELECT DISTINCT ON (oc.occurrence_id)
                    oc.occurrence_id,
                    oc.occurrence_content_id
                FROM occurrence_content oc
                WHERE oc.extractor = %s
                  AND oc.extractor_version = %s
                  AND oc.extraction_profile_version = %s
                  AND oc.status = 'success'
                ORDER BY oc.occurrence_id, oc.attempt_no DESC
            ),
            current_seg AS (
                SELECT DISTINCT ON (sr.occurrence_content_id)
                    sr.occurrence_content_id,
                    sr.segmentation_run_id
                FROM segmentation_runs sr
                WHERE sr.sectionizer_version = %s
                  AND sr.boundary_method = %s
                  AND sr.llm_model_id = %s
                  AND sr.prompt_id = %s
                  AND sr.status = 'success'
                ORDER BY sr.occurrence_content_id, sr.attempt_no DESC
            ),
            base AS (
                SELECT
                    io.occurrence_id,
                    io.collected_at,
                    cs.occurrence_content_id,
                    sg.segmentation_run_id,
                    CASE
                        WHEN sg.segmentation_run_id IS NOT NULL THEN 0
                        WHEN cs.occurrence_content_id IS NOT NULL THEN 1
                        ELSE 2
                    END AS stage_rank
                FROM item_occurrences io
                JOIN sources s
                  ON s.source_id = io.source_id
                JOIN content_routing_decisions cr
                  ON cr.content_id = io.content_id
                 AND cr.routing_version = %s
                 AND cr.decision = 'analyze'
                LEFT JOIN current_success cs
                  ON cs.occurrence_id = io.occurrence_id
                LEFT JOIN current_seg sg
                  ON sg.occurrence_content_id = cs.occurrence_content_id
                WHERE s.source_type = 'rss'
                  AND (
                      cs.occurrence_content_id IS NOT NULL
                      OR (
                          SELECT count(*)
                          FROM occurrence_content oc
                          WHERE oc.occurrence_id = io.occurrence_id
                            AND oc.extractor = %s
                            AND oc.extractor_version = %s
                            AND oc.extraction_profile_version = %s
                            AND oc.status IN ('fetch_error', 'extraction_error')
                      ) < %s
                  )
            )
            SELECT
                b.occurrence_id,
                b.occurrence_content_id,
                b.segmentation_run_id,
                b.stage_rank
            FROM base b
            WHERE
                b.occurrence_content_id IS NULL
                OR b.segmentation_run_id IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM content_segments cs
                    WHERE cs.segmentation_run_id = b.segmentation_run_id
                      AND (
                          NOT EXISTS (
                              SELECT 1
                              FROM segment_embeddings se
                              WHERE se.segment_id = cs.segment_id
                                AND se.embedding_model_id = %s
                          )
                          OR NOT EXISTS (
                              SELECT 1
                              FROM segment_routing_decisions srd
                              WHERE srd.segment_id = cs.segment_id
                                AND srd.routing_version = %s
                          )
                          OR (
                              EXISTS (
                                  SELECT 1
                                  FROM segment_routing_decisions srd
                                  WHERE srd.segment_id = cs.segment_id
                                    AND srd.routing_version = %s
                                    AND srd.decision = 'analyze'
                              )
                              AND length(cs.text_content) <= %s
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM claim_extraction_runs r
                                  WHERE r.segment_id = cs.segment_id
                                    AND r.llm_model_id = %s
                                    AND r.prompt_id = %s
                                    AND r.status IN ('valid', 'invalid')
                              )
                          )
                          OR EXISTS (
                              SELECT 1
                              FROM claim_extraction_runs r
                              JOIN claims c
                                ON c.run_id = r.run_id
                              WHERE r.segment_id = cs.segment_id
                                AND r.llm_model_id = %s
                                AND r.prompt_id = %s
                                AND r.status = 'valid'
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM claim_embeddings ce
                                    WHERE ce.claim_id = c.claim_id
                                      AND ce.embedding_model_id = %s
                                )
                          )
                      )
                )
            ORDER BY b.stage_rank, b.collected_at DESC
            LIMIT %s
            """,
            (
                EXTRACTOR,
                EXTRACTOR_VERSION,
                EXTRACTION_PROFILE_VERSION,
                SECTIONIZER_VERSION,
                BOUNDARY_METHOD,
                SEG_LLM_MODEL_ID,
                SEG_PROMPT_ID,
                ROUTING_VERSION,
                EXTRACTOR,
                EXTRACTOR_VERSION,
                EXTRACTION_PROFILE_VERSION,
                DEFAULT_MAX_ATTEMPTS,
                EMBEDDING_MODEL_ID,
                ROUTING_VERSION,
                ROUTING_VERSION,
                max_claim_chars,
                CLAIM_LLM_MODEL_ID,
                CLAIM_PROMPT_ID,
                CLAIM_LLM_MODEL_ID,
                CLAIM_PROMPT_ID,
                EMBEDDING_MODEL_ID,
                limit,
            ),
        )
        return cur.fetchall()


def missing_analyze_segments(conn, segmentation_run_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                cs.segment_id,
                cs.segment_index,
                length(cs.text_content) AS segment_chars
            FROM content_segments cs
            JOIN segment_routing_decisions srd
              ON srd.segment_id = cs.segment_id
             AND srd.routing_version = %s
             AND srd.decision = 'analyze'
            WHERE cs.segmentation_run_id = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM claim_extraction_runs r
                  WHERE r.segment_id = cs.segment_id
                    AND r.llm_model_id = %s
                    AND r.prompt_id = %s
                    AND r.status IN ('valid', 'invalid')
              )
            ORDER BY cs.segment_index
            """,
            (
                ROUTING_VERSION,
                segmentation_run_id,
                CLAIM_LLM_MODEL_ID,
                CLAIM_PROMPT_ID,
            ),
        )
        return cur.fetchall()


def claim_runs_missing_embeddings(conn, segmentation_run_id):
    """Return exact valid segment claim runs with incomplete embedding coverage."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT
                r.run_id,
                cs.segment_index
            FROM content_segments cs
            JOIN claim_extraction_runs r
              ON r.segment_id = cs.segment_id
             AND r.llm_model_id = %s
             AND r.prompt_id = %s
             AND r.status = 'valid'
            JOIN claims c
              ON c.run_id = r.run_id
            WHERE cs.segmentation_run_id = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM claim_embeddings ce
                  WHERE ce.claim_id = c.claim_id
                    AND ce.embedding_model_id = %s
              )
            ORDER BY cs.segment_index, r.run_id
            """,
            (
                CLAIM_LLM_MODEL_ID,
                CLAIM_PROMPT_ID,
                segmentation_run_id,
                EMBEDDING_MODEL_ID,
            ),
        )
        return cur.fetchall()


def mamay_healthy():
    result = subprocess.run(
        [
            "/usr/bin/curl",
            "-fsS",
            "--max-time",
            "5",
            MAMAY_HEALTH,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def run_worker(script, args, *, mamay=False):
    cmd = [sys.executable, str(script), *args]

    if mamay:
        if not mamay_healthy():
            raise MamayUnavailable("Mamay endpoint is unavailable")

        MAMAY_LOCK.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "/usr/bin/flock",
            "-n",
            "-E",
            str(FLOCK_BUSY_EXIT),
            str(MAMAY_LOCK),
            *cmd,
        ]

    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, cwd=REPO, check=False)

    if mamay and result.returncode == FLOCK_BUSY_EXIT:
        raise MamayBusy("another Mamay workload owns mamay.lock")

    return result.returncode


def embed_missing_claim_runs(segmentation_run_id):
    """Embed only exact valid claim runs belonging to one segmentation run."""
    with psycopg.connect(DB_DSN) as conn:
        runs = claim_runs_missing_embeddings(
            conn,
            segmentation_run_id,
        )

    for run_id, segment_index in runs:
        print(
            f"[segment_pipeline] claim embeddings "
            f"segment_index={segment_index} "
            f"run_id={run_id}"
        )
        rc = run_worker(
            CLAIM_EMBED_WORKER,
            ["--run-id", str(run_id)],
        )
        if rc != 0:
            return rc

    return 0


def run(occurrence_limit, claim_limit, max_claim_chars, dry_run):
    with psycopg.connect(DB_DSN) as conn:
        items = fetch_work_items(conn, occurrence_limit, max_claim_chars)

    print(
        f"[segment_pipeline] selected={len(items)} "
        f"occurrence_limit={occurrence_limit} "
        f"claim_limit={claim_limit} "
        f"max_claim_chars={max_claim_chars}"
    )

    stage_names = {
        0: "resume_downstream",
        1: "fulltext_ready",
        2: "needs_fulltext",
    }

    for occurrence_id, occurrence_content_id, segmentation_run_id, stage_rank in items:
        print(
            f"  {occurrence_id} "
            f"stage={stage_names[stage_rank]} "
            f"occurrence_content={'yes' if occurrence_content_id else 'no'} "
            f"segmentation={'yes' if segmentation_run_id else 'no'}"
        )

    if dry_run:
        return 0

    claims_started = 0

    try:
        for occurrence_id, _, _, _ in items:
            print(f"\n=== occurrence {occurrence_id} ===")

            with psycopg.connect(DB_DSN) as conn:
                occurrence_content_id = current_fulltext(conn, occurrence_id)

            if occurrence_content_id is None:
                rc = run_worker(
                    OCCURRENCE_WORKER,
                    [
                        "--limit", "1",
                        "--max-attempts", str(DEFAULT_MAX_ATTEMPTS),
                        "--occurrence-id", str(occurrence_id),
                    ],
                )
                if rc != 0:
                    print(f"[segment_pipeline] fulltext worker rc={rc}; continue")
                    continue

                with psycopg.connect(DB_DSN) as conn:
                    occurrence_content_id = current_fulltext(conn, occurrence_id)

                if occurrence_content_id is None:
                    print("[segment_pipeline] no successful fulltext; continue")
                    continue

            with psycopg.connect(DB_DSN) as conn:
                segmentation_run_id = current_segmentation(
                    conn,
                    occurrence_content_id,
                )

            if segmentation_run_id is None:
                rc = run_worker(
                    SEGMENTATION_WORKER,
                    ["--occurrence-id", str(occurrence_id)],
                    mamay=True,
                )
                if rc != 0:
                    print(f"[segment_pipeline] segmentation rc={rc}; continue")
                    continue

                with psycopg.connect(DB_DSN) as conn:
                    segmentation_run_id = current_segmentation(
                        conn,
                        occurrence_content_id,
                    )

                if segmentation_run_id is None:
                    print("[segment_pipeline] no successful segmentation; continue")
                    continue

            rc = run_worker(
                SEGMENT_EMBED_WORKER,
                ["--segmentation-run-id", str(segmentation_run_id)],
            )
            if rc != 0:
                print(f"[segment_pipeline] segment embeddings rc={rc}; continue")
                continue

            rc = run_worker(
                SEGMENT_ROUTING_WORKER,
                ["--segmentation-run-id", str(segmentation_run_id)],
            )
            if rc != 0:
                print(f"[segment_pipeline] segment routing rc={rc}; continue")
                continue

            # Resume cheap deterministic downstream state before starting
            # any new Mamay claim extraction.
            rc = embed_missing_claim_runs(segmentation_run_id)
            if rc != 0:
                print(
                    f"[segment_pipeline] claim embeddings rc={rc}; continue"
                )
                continue

            with psycopg.connect(DB_DSN) as conn:
                segments = missing_analyze_segments(
                    conn,
                    segmentation_run_id,
                )

            for segment_id, segment_index, segment_chars in segments:
                if segment_chars > max_claim_chars:
                    print(
                        f"[segment_pipeline] DEFER oversized "
                        f"segment_index={segment_index} "
                        f"segment_id={segment_id} "
                        f"chars={segment_chars} "
                        f"max_claim_chars={max_claim_chars}"
                    )
                    continue

                if claims_started >= claim_limit:
                    print("[segment_pipeline] claim budget exhausted")
                    break

                print(
                    f"[segment_pipeline] claim segment_index={segment_index} "
                    f"segment_id={segment_id}"
                )

                rc = run_worker(
                    SEGMENT_CLAIM_WORKER,
                    ["--segment-id", str(segment_id)],
                    mamay=True,
                )
                claims_started += 1

                if rc != 0:
                    print(
                        f"[segment_pipeline] segment claim rc={rc}; "
                        "continuing with bounded run"
                    )

            # Embed claims created successfully during this occurrence.
            # INVALID/transport-error runs have no eligible persisted claims.
            rc = embed_missing_claim_runs(segmentation_run_id)
            if rc != 0:
                print(
                    f"[segment_pipeline] claim embeddings rc={rc}; continue"
                )
                continue

        print(
            f"\n[segment_pipeline] done "
            f"selected_occurrences={len(items)} "
            f"claim_attempts_started={claims_started}"
        )
        return 0

    except MamayBusy as exc:
        print(f"[segment_pipeline] SKIP: {exc}")
        return 0
    except MamayUnavailable as exc:
        print(f"[segment_pipeline] SKIP: {exc}")
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--occurrence-limit", type=int, required=True)
    parser.add_argument("--claim-limit", type=int, required=True)
    parser.add_argument("--max-claim-chars", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.occurrence_limit <= 0:
        parser.error("--occurrence-limit must be > 0")
    if args.claim_limit <= 0:
        parser.error("--claim-limit must be > 0")
    if args.max_claim_chars <= 0:
        parser.error("--max-claim-chars must be > 0")

    return run(
        occurrence_limit=args.occurrence_limit,
        claim_limit=args.claim_limit,
        max_claim_chars=args.max_claim_chars,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
