#!/usr/bin/env python3
"""
C4 provenance ablation / calibration experiment.

READ ONLY.

Сравнивает:
A. legacy source provenance:
   EXISTS occurrence -> source.contour_id = 4

B. source_group_4 + C4 semantic score

C. high C4 semantic score outside source_group_4

ВАЖНО:
- source_group_4 != strategic Content Contour 4;
- semantic facet != actor/direction truth;
- никакой threshold здесь не становится production baseline;
- threshold grid нужен только для измерения поведения.
"""

from __future__ import annotations

import random

import numpy as np
import psycopg
from pgvector.psycopg import register_vector


DB_DSN = "dbname=mip_dev"
EMBEDDING_MODEL_ID = 1

PREVIEW_LEN = 240
SAMPLE_N = 20
RANDOM_SEED = 42

EXPLORATORY_THRESHOLDS = [
    0.40,
    0.45,
    0.50,
    0.55,
    0.60,
]


def preview(text: str) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= PREVIEW_LEN else t[:PREVIEW_LEN].rstrip() + "…"


def distribution(values):
    arr = np.asarray(values, dtype=np.float32)

    if arr.size == 0:
        return None

    return {
        "min": float(arr.min()),
        "p10": float(np.quantile(arr, 0.10)),
        "p25": float(np.quantile(arr, 0.25)),
        "median": float(np.quantile(arr, 0.50)),
        "p75": float(np.quantile(arr, 0.75)),
        "p90": float(np.quantile(arr, 0.90)),
        "max": float(arr.max()),
    }


def print_distribution(label, values):
    d = distribution(values)

    print(f"\n=== {label} SCORE DISTRIBUTION ===")

    if d is None:
        print("empty")
        return

    print(
        f"min={d['min']:.3f} "
        f"p10={d['p10']:.3f} "
        f"p25={d['p25']:.3f} "
        f"median={d['median']:.3f} "
        f"p75={d['p75']:.3f} "
        f"p90={d['p90']:.3f} "
        f"max={d['max']:.3f}"
    )


def load_data(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ci.content_id,
                ci.title,
                ci.text_content,
                e.embedding,
                EXISTS (
                    SELECT 1
                    FROM item_occurrences io
                    JOIN sources s
                      ON s.source_id = io.source_id
                    WHERE io.content_id = ci.content_id
                      AND s.contour_id = 4
                ) AS source_group_4
            FROM content_items ci
            JOIN embeddings e
              ON e.content_id = ci.content_id
             AND e.embedding_model_id = %s
            ORDER BY ci.content_id
            """,
            (EMBEDDING_MODEL_ID,),
        )
        corpus = cur.fetchall()

        cur.execute(
            """
            SELECT
                e.reference_id,
                e.facet_code,
                e.reference_text,
                re.embedding
            FROM contour_reference_entries e
            JOIN contour_reference_embeddings re
              ON re.reference_id = e.reference_id
             AND re.embedding_model_id = %s
            WHERE e.monitoring_contour_id = 4
              AND e.active
              AND e.match_mode IN ('semantic', 'both')
            ORDER BY e.reference_id
            """,
            (EMBEDDING_MODEL_ID,),
        )
        refs = cur.fetchall()

    return corpus, refs


def main() -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        corpus, refs = load_data(conn)

    if not corpus:
        raise RuntimeError("No content embeddings found")

    if not refs:
        raise RuntimeError("No C4 semantic references found")

    print(
        f"semantic_corpus={len(corpus)} "
        f"C4_semantic_refs={len(refs)}"
    )

    content_vectors = np.vstack(
        [
            row[3].to_numpy().astype(np.float32)
            for row in corpus
        ]
    )

    ref_vectors = np.vstack(
        [
            row[3].to_numpy().astype(np.float32)
            for row in refs
        ]
    )

    scores_matrix = content_vectors @ ref_vectors.T

    best_ref_idx = np.argmax(scores_matrix, axis=1)
    best_scores = scores_matrix[
        np.arange(len(corpus)),
        best_ref_idx,
    ]

    items = {}

    for row_idx, row in enumerate(corpus):
        content_id, title, text_content, _, source_group_4 = row

        ref = refs[int(best_ref_idx[row_idx])]

        items[content_id] = {
            "title": title,
            "text": text_content,
            "source_group_4": bool(source_group_4),
            "facet": ref[1],
            "reference_text": ref[2],
            "score": float(best_scores[row_idx]),
        }

    group4_ids = [
        cid
        for cid, item in items.items()
        if item["source_group_4"]
    ]

    non_group4_ids = [
        cid
        for cid, item in items.items()
        if not item["source_group_4"]
    ]

    group4_scores = [
        items[cid]["score"]
        for cid in group4_ids
    ]

    non_group4_scores = [
        items[cid]["score"]
        for cid in non_group4_ids
    ]

    print()
    print("=== A. PROVENANCE-ONLY BASELINE ===")
    print(
        "source_group_4 = EXISTS occurrence -> "
        "legacy sources.contour_id=4"
    )
    print(f"source_group_4_total={len(group4_ids)}")
    print(f"non_group4_total={len(non_group4_ids)}")

    print_distribution(
        "ALL CONTENT",
        [item["score"] for item in items.values()],
    )
    print_distribution(
        "SOURCE_GROUP_4",
        group4_scores,
    )
    print_distribution(
        "NON_SOURCE_GROUP_4",
        non_group4_scores,
    )

    print()
    print("=== B. EXPLORATORY SEMANTIC GATE GRID ===")
    print(
        "NOT production thresholds. "
        "Population counts only."
    )
    print(
        "threshold | group4_keep | group4_drop | "
        "non_group4_high"
    )

    for threshold in EXPLORATORY_THRESHOLDS:
        g4_keep = sum(
            items[cid]["score"] >= threshold
            for cid in group4_ids
        )
        g4_drop = len(group4_ids) - g4_keep

        outside_high = sum(
            items[cid]["score"] >= threshold
            for cid in non_group4_ids
        )

        print(
            f"{threshold:9.2f} | "
            f"{g4_keep:11d} | "
            f"{g4_drop:11d} | "
            f"{outside_high:15d}"
        )

    g4_arr = np.asarray(group4_scores, dtype=np.float32)
    ng4_arr = np.asarray(non_group4_scores, dtype=np.float32)

    g4_q10 = float(np.quantile(g4_arr, 0.10))
    g4_q45 = float(np.quantile(g4_arr, 0.45))
    g4_q55 = float(np.quantile(g4_arr, 0.55))
    g4_q90 = float(np.quantile(g4_arr, 0.90))

    ng4_q90 = float(np.quantile(ng4_arr, 0.90))

    strata = {
        "GROUP4 HIGH (top decile)": [
            cid
            for cid in group4_ids
            if items[cid]["score"] >= g4_q90
        ],
        "GROUP4 MIDDLE (45-55 percentile)": [
            cid
            for cid in group4_ids
            if g4_q45
            <= items[cid]["score"]
            <= g4_q55
        ],
        "GROUP4 LOW (bottom decile)": [
            cid
            for cid in group4_ids
            if items[cid]["score"] <= g4_q10
        ],
        "NON-GROUP4 HIGH (top decile outside provenance)": [
            cid
            for cid in non_group4_ids
            if items[cid]["score"] >= ng4_q90
        ],
    }

    print()
    print("=== C. STRATIFIED MANUAL-REVIEW SAMPLE ===")
    print(
        "Directionality errors are intentionally NOT corrected here."
    )

    rng = random.Random(RANDOM_SEED)

    for label, ids in strata.items():
        print()
        print(
            f"--- {label}: "
            f"sample={min(SAMPLE_N, len(ids))}/{len(ids)} ---"
        )

        sample = rng.sample(
            ids,
            min(SAMPLE_N, len(ids)),
        )

        sample.sort(
            key=lambda cid: items[cid]["score"],
            reverse=True,
        )

        for cid in sample:
            item = items[cid]

            print()
            print(
                f"[{cid}] "
                f"source_group_4={item['source_group_4']} "
                f"facet={item['facet']} "
                f"score={item['score']:.3f}"
            )
            print(
                f"reference={item['reference_text']}"
            )
            print(
                f"title: {item['title'] or '(no title)'}"
            )
            print(
                f"    {preview(item['text'])}"
            )

    print()
    print("=== SUMMARY ===")
    print(f"source_group_4_total={len(group4_ids)}")
    print(f"non_group4_total={len(non_group4_ids)}")
    print(
        "Manual review questions:"
    )
    print(
        "1. Is GROUP4 LOW actually irrelevant noise, "
        "or relevant C4 content missed by semantic retrieval?"
    )
    print(
        "2. Is NON-GROUP4 HIGH real C4 material outside "
        "legacy hostile sources, or semantic false positives?"
    )
    print(
        "3. Does a semantic gate add useful filtering "
        "over provenance-only?"
    )
    print(
        "4. Record actor/direction mistakes separately; "
        "do not repair them by threshold tuning."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
