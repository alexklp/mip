#!/usr/bin/env python3
"""
Read-only full-corpus dual-score profiler for relation candidate calibration.

Для всіх source-provenance cross-group пар із claim cosine >= 0.80 рахує
другий незалежний сигнал: cosine між full-content embeddings тих самих
content_items. Нічого не пише в candidate_pairs/relation_judgments і не
використовує час як filter/ranking signal.

Мета — виміряти, наскільки content-level similarity відсікає generic claim
hubs, не заморожуючи threshold з малої human sample.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.claim_relations import candidate_pair_profile as base  # noqa: E402

CLAIM_MIN = 0.80
CLAIM_BANDS = (
    (0.80, 0.85),
    (0.85, 0.90),
    (0.90, 0.95),
    (0.95, 0.97),
    (0.97, 1.000001),
)
CONTENT_EDGES = np.asarray(
    [-1.0, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.000001],
    dtype=np.float32,
)
CONTENT_THRESHOLDS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80)


def claim_band_index(score: float) -> int | None:
    for idx, (lo, hi) in enumerate(CLAIM_BANDS):
        if lo <= score < hi:
            return idx
    return None


def fetch_content_vector_by_claim(conn, claim_ids: list) -> dict:
    if not claim_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.claim_id, e.embedding
            FROM claims c
            JOIN claim_extraction_runs r ON r.run_id = c.run_id
            JOIN embeddings e
              ON e.content_id = r.content_id
             AND e.embedding_model_id = %s
            WHERE c.claim_id = ANY(%s)
            """,
            (base.EMBEDDING_MODEL_ID, claim_ids),
        )
        rows = cur.fetchall()
    return {claim_id: embedding.to_numpy() for claim_id, embedding in rows}


def build_content_matrix(claim_ids: list, vector_by_claim: dict) -> np.ndarray:
    missing = [claim_id for claim_id in claim_ids if claim_id not in vector_by_claim]
    if missing:
        preview = ", ".join(str(v) for v in missing[:10])
        raise RuntimeError(f"content embedding missing for {len(missing)} claims; first: {preview}")
    return np.vstack([vector_by_claim[claim_id] for claim_id in claim_ids]).astype(np.float32, copy=False)


def profile(corpus: dict, content_vectors: np.ndarray, chunk_size: int) -> dict:
    claim_vectors = corpus["vectors"]
    groups = corpus["groups"]
    run_ids = corpus["run_ids"]
    n = len(corpus["claim_ids"])

    grouped = base.group_indices(groups)
    group_keys = sorted(grouped, key=lambda g: tuple(sorted(g)))

    claim_band_counts = np.zeros(len(CLAIM_BANDS), dtype=np.int64)
    content_hist = np.zeros(len(CONTENT_EDGES) - 1, dtype=np.int64)
    cross = np.zeros((len(CLAIM_BANDS), len(CONTENT_EDGES) - 1), dtype=np.int64)
    threshold_counts = {threshold: 0 for threshold in CONTENT_THRESHOLDS}
    threshold_degrees = {threshold: np.zeros(n, dtype=np.int32) for threshold in CONTENT_THRESHOLDS}
    total = 0

    for left_pos, group_a in enumerate(group_keys):
        idx_a_all = grouped[group_a]
        for group_b in group_keys[left_pos + 1 :]:
            if not group_a.isdisjoint(group_b):
                continue
            idx_b = grouped[group_b]
            overlap = {run_ids[i] for i in idx_a_all} & {run_ids[i] for i in idx_b}
            if overlap:
                raise RuntimeError(
                    "same run_id found across disjoint source_group sets; "
                    f"invariant violated for {base.group_label(group_a)} x {base.group_label(group_b)}"
                )

            claim_b = claim_vectors[idx_b]
            content_b = content_vectors[idx_b]

            for start in range(0, len(idx_a_all), chunk_size):
                idx_a = idx_a_all[start : start + chunk_size]
                claim_scores = claim_vectors[idx_a] @ claim_b.T
                np.clip(claim_scores, -1.0, 1.0, out=claim_scores)
                rows, cols = np.nonzero(claim_scores >= CLAIM_MIN)
                if rows.size == 0:
                    continue

                global_a = idx_a[rows]
                global_b = idx_b[cols]
                selected_claim_scores = claim_scores[rows, cols]
                selected_content_scores = np.sum(
                    content_vectors[global_a] * content_vectors[global_b], axis=1, dtype=np.float32
                )
                np.clip(selected_content_scores, -1.0, 1.0, out=selected_content_scores)

                total += int(rows.size)
                block_hist, _ = np.histogram(selected_content_scores, bins=CONTENT_EDGES)
                content_hist += block_hist.astype(np.int64, copy=False)

                for claim_score, content_score in zip(selected_claim_scores.tolist(), selected_content_scores.tolist()):
                    bidx = claim_band_index(float(claim_score))
                    if bidx is None:
                        raise AssertionError(f"claim score outside configured bands: {claim_score}")
                    cidx = int(np.searchsorted(CONTENT_EDGES, content_score, side="right") - 1)
                    cidx = min(max(cidx, 0), len(CONTENT_EDGES) - 2)
                    claim_band_counts[bidx] += 1
                    cross[bidx, cidx] += 1

                for threshold in CONTENT_THRESHOLDS:
                    mask = selected_content_scores >= threshold
                    count = int(np.count_nonzero(mask))
                    threshold_counts[threshold] += count
                    if count:
                        ga = global_a[mask]
                        gb = global_b[mask]
                        np.add.at(threshold_degrees[threshold], ga, 1)
                        np.add.at(threshold_degrees[threshold], gb, 1)

    return {
        "total": total,
        "claim_band_counts": claim_band_counts,
        "content_hist": content_hist,
        "cross": cross,
        "threshold_counts": threshold_counts,
        "threshold_degrees": threshold_degrees,
    }


def label_range(edges: np.ndarray, idx: int) -> str:
    lo = float(edges[idx])
    hi = float(edges[idx + 1])
    if idx == 0:
        return f"< {hi:.2f}"
    if idx == len(edges) - 2:
        return f"[{lo:.2f},1.00]"
    return f"[{lo:.2f},{hi:.2f})"


def print_report(corpus: dict, stats: dict, elapsed: float) -> None:
    n = len(corpus["claim_ids"])
    total = stats["total"]
    print(f"code_revision={base.get_code_revision()} embedding_model_id={base.EMBEDDING_MODEL_ID}")
    print(f"claims={n} claim_min={CLAIM_MIN:.2f} selected_pairs={total}")
    print("time_signal=DISABLED (diagnostic only; not used here)")

    print("\n--- claim-score bands ---")
    for idx, (lo, hi) in enumerate(CLAIM_BANDS):
        hi_label = "1.00]" if idx == len(CLAIM_BANDS) - 1 else f"{hi:.2f})"
        print(f"[{lo:.2f},{hi_label}  {int(stats['claim_band_counts'][idx])}")

    print("\n--- content-score distribution among claim_score >= 0.80 ---")
    for idx, count in enumerate(stats["content_hist"]):
        pct = 100.0 * int(count) / total if total else 0.0
        print(f"{label_range(CONTENT_EDGES, idx):>12}  {int(count):>5}  ({pct:6.2f}%)")

    print("\n--- joint candidate volume / claim coverage ---")
    for threshold in CONTENT_THRESHOLDS:
        count = stats["threshold_counts"][threshold]
        degree = stats["threshold_degrees"][threshold]
        covered = int(np.count_nonzero(degree))
        pct_pairs = 100.0 * count / total if total else 0.0
        pct_claims = 100.0 * covered / n if n else 0.0
        max_degree = int(degree.max()) if degree.size else 0
        p99 = float(np.percentile(degree, 99)) if degree.size else 0.0
        print(
            f"claim>=0.80 + content>={threshold:.2f}: pairs={count} ({pct_pairs:.2f}%) "
            f"covered_claims={covered}/{n} ({pct_claims:.2f}%) p99_degree={p99:.1f} max_degree={max_degree}"
        )

    print("\n--- 2D counts: claim band x content band ---")
    header = "claim_band" + "".join(f" | {label_range(CONTENT_EDGES, i):>11}" for i in range(len(CONTENT_EDGES) - 1))
    print(header)
    for r, (lo, hi) in enumerate(CLAIM_BANDS):
        hi_label = "1.00]" if r == len(CLAIM_BANDS) - 1 else f"{hi:.2f})"
        label = f"[{lo:.2f},{hi_label}"
        print(label + "".join(f" | {int(stats['cross'][r, c]):>11}" for c in range(stats['cross'].shape[1])))

    print(f"\nDUAL PROFILE elapsed={elapsed:.2f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only full-corpus claim/content cosine profiler")
    parser.add_argument("--chunk-size", type=int, default=1000)
    args = parser.parse_args()
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be > 0")

    with psycopg.connect(base.DB_DSN) as conn:
        register_vector(conn)
        base.verify_registered_model(conn)
        claim_rows = base.fetch_claims(conn)
        content_ids = sorted({row[3] for row in claim_rows}, key=str)
        occ_by_content = base.fetch_occurrences(conn, content_ids)
        claim_ids = [row[0] for row in claim_rows]
        content_by_claim = fetch_content_vector_by_claim(conn, claim_ids)

    corpus = base.build_corpus(claim_rows, occ_by_content)
    if corpus["skipped_no_occurrence"] or corpus["skipped_no_group"]:
        raise RuntimeError(
            "profiler requires complete source-provenance coverage; "
            f"skipped_no_occurrence={corpus['skipped_no_occurrence']} "
            f"skipped_no_group={corpus['skipped_no_group']}"
        )

    # fetch_claims() is ORDER BY claim_id and build_corpus() preserves that order.
    if corpus["claim_ids"] != claim_ids:
        raise RuntimeError("claim ordering changed while building corpus")
    content_vectors = build_content_matrix(corpus["claim_ids"], content_by_claim)

    print(f"loaded claims={len(claim_rows)}; DB connection closed before profiling")
    started = time.perf_counter()
    stats = profile(corpus, content_vectors, args.chunk_size)
    elapsed = time.perf_counter() - started
    print_report(corpus, stats, elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
