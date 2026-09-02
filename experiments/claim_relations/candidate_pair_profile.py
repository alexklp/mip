#!/usr/bin/env python3
"""
Read-only blockwise profiler простору relation candidates (семантика candidate v1).

Скрипт навмисно НЕ пише в candidate_pairs. Він вимірює поточний cosine-простір
між неперетинними source-provenance групами без materialization повної N×N
матриці та без створення Python-об’єктів для всіх eligible пар.
"""
from __future__ import annotations

import argparse
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

DB_DSN = "dbname=mip_dev"
EMBEDDING_MODEL_ID = 1
MODEL_NAME = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
FRAMEWORK = "sentence-transformers"
FRAMEWORK_VERSION = "5.7.0"
DIMENSION = 1024
METRIC = "cosine"
ENCODING_PARAMS = {"normalize_embeddings": True, "input_field": "text_content"}

DEGREE_THRESHOLDS = (0.80, 0.85, 0.90)
TIME_SCORE_THRESHOLD = 0.85
NEAR_DUP_THRESHOLD = 0.97
TIME_WINDOWS_DAYS = (1, 3, 7, 14)
HIST_EDGES = np.linspace(0.50, 1.00, 11, dtype=np.float32)
REPO_ROOT = Path(__file__).resolve().parents[2]


def get_code_revision() -> str:
    return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()


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
    if row is None:
        raise RuntimeError(f"embedding_model_id={EMBEDDING_MODEL_ID} не зареєстровано в embedding_models")
    expected = (MODEL_NAME, MODEL_REVISION, FRAMEWORK, FRAMEWORK_VERSION, DIMENSION, METRIC, ENCODING_PARAMS)
    if tuple(row) != expected:
        raise RuntimeError(
            "Конфігурація embedding-моделі в БД не збігається з profiler contract.\n"
            f"  БД:       {tuple(row)}\n"
            f"  profiler: {expected}"
        )


def fetch_claims(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.claim_id, c.claim_text, c.run_id, r.content_id, ce.embedding
            FROM claim_embeddings ce
            JOIN claims c ON c.claim_id = ce.claim_id
            JOIN claim_extraction_runs r ON r.run_id = c.run_id
            WHERE ce.embedding_model_id = %s
            ORDER BY c.claim_id
            """,
            (EMBEDDING_MODEL_ID,),
        )
        return cur.fetchall()


def fetch_occurrences(conn, content_ids: list) -> dict:
    if not content_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT io.content_id, s.contour_id, io.collected_at
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            WHERE io.content_id = ANY(%s)
            """,
            (content_ids,),
        )
        rows = cur.fetchall()
    by_content = defaultdict(list)
    for content_id, source_group_id, collected_at in rows:
        by_content[content_id].append((source_group_id, collected_at))
    return by_content


def build_corpus(claim_rows, occ_by_content):
    claim_ids, claim_texts, run_ids, groups, first_seen_seconds, vectors = [], [], [], [], [], []
    skipped_no_group = 0
    skipped_no_occurrence = 0
    for claim_id, claim_text, run_id, content_id, embedding in claim_rows:
        occs = occ_by_content.get(content_id)
        if not occs:
            skipped_no_occurrence += 1
            continue
        source_group_set = frozenset(group_id for group_id, _ in occs if group_id is not None)
        if not source_group_set:
            skipped_no_group += 1
            continue
        first_seen = min(collected_at for _, collected_at in occs)
        claim_ids.append(claim_id)
        claim_texts.append(claim_text)
        run_ids.append(run_id)
        groups.append(source_group_set)
        first_seen_seconds.append(int(first_seen.timestamp()))
        vectors.append(embedding.to_numpy())
    matrix = np.vstack(vectors).astype(np.float32, copy=False) if vectors else np.empty((0, DIMENSION), dtype=np.float32)
    return {
        "claim_ids": claim_ids,
        "claim_texts": claim_texts,
        "run_ids": run_ids,
        "groups": groups,
        "first_seen": np.asarray(first_seen_seconds, dtype=np.int64),
        "vectors": matrix,
        "skipped_no_group": skipped_no_group,
        "skipped_no_occurrence": skipped_no_occurrence,
    }


def group_indices(groups) -> dict[frozenset[int], np.ndarray]:
    grouped = defaultdict(list)
    for idx, group in enumerate(groups):
        grouped[group].append(idx)
    return {group: np.asarray(indices, dtype=np.int64) for group, indices in grouped.items()}


def group_label(group: frozenset[int]) -> str:
    return "{" + ",".join(str(v) for v in sorted(group)) + "}"


def preview(text: str, limit: int = 180) -> str:
    clean = " ".join(text.split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def percentile_int(values: np.ndarray, q: float) -> float:
    return float(np.percentile(values, q)) if values.size else 0.0


def profile_corpus(corpus: dict, chunk_size: int, sample_size: int) -> dict:
    vectors = corpus["vectors"]
    claim_ids = corpus["claim_ids"]
    claim_texts = corpus["claim_texts"]
    run_ids = corpus["run_ids"]
    groups = corpus["groups"]
    first_seen = corpus["first_seen"]
    n = len(claim_ids)

    grouped = group_indices(groups)
    group_keys = sorted(grouped, key=lambda g: tuple(sorted(g)))
    degrees = {threshold: np.zeros(n, dtype=np.int32) for threshold in DEGREE_THRESHOLDS}
    hist_counts = np.zeros(len(HIST_EDGES) - 1, dtype=np.int64)
    below_hist = 0
    above_counts = {threshold: 0 for threshold in (0.80, 0.85, 0.90, 0.95, 0.97)}
    time_counts = {days: 0 for days in TIME_WINDOWS_DAYS}
    time_pair_total = 0
    near_dup_total = 0
    near_dup_sample = []
    eligible_total = 0
    block_count = 0
    group_pair_spaces = []

    for left_pos, group_a in enumerate(group_keys):
        idx_a_all = grouped[group_a]
        for group_b in group_keys[left_pos + 1:]:
            if not group_a.isdisjoint(group_b):
                continue
            idx_b = grouped[group_b]
            overlap = {run_ids[i] for i in idx_a_all} & {run_ids[i] for i in idx_b}
            if overlap:
                raise RuntimeError(
                    "same run_id found across disjoint source_group sets; "
                    f"invariant violated for {group_label(group_a)} x {group_label(group_b)}"
                )
            group_pair_space = len(idx_a_all) * len(idx_b)
            group_pair_spaces.append((group_a, group_b, group_pair_space))
            b_vectors = vectors[idx_b]
            b_times = first_seen[idx_b]

            for start in range(0, len(idx_a_all), chunk_size):
                idx_a = idx_a_all[start:start + chunk_size]
                scores = vectors[idx_a] @ b_vectors.T
                np.clip(scores, -1.0, 1.0, out=scores)
                block_count += 1
                eligible_total += scores.size
                below_hist += int(np.count_nonzero(scores < HIST_EDGES[0]))
                block_hist, _ = np.histogram(scores, bins=HIST_EDGES)
                hist_counts += block_hist.astype(np.int64, copy=False)
                for threshold in above_counts:
                    above_counts[threshold] += int(np.count_nonzero(scores >= threshold))

                mask_085 = None
                for threshold in DEGREE_THRESHOLDS:
                    mask = scores >= threshold
                    degrees[threshold][idx_a] += mask.sum(axis=1, dtype=np.int32)
                    degrees[threshold][idx_b] += mask.sum(axis=0, dtype=np.int32)
                    if threshold == TIME_SCORE_THRESHOLD:
                        mask_085 = mask
                if mask_085 is None:
                    raise AssertionError("TIME_SCORE_THRESHOLD must be in DEGREE_THRESHOLDS")

                a_times = first_seen[idx_a]
                for local_i in range(len(idx_a)):
                    matching_j = np.flatnonzero(mask_085[local_i])
                    if matching_j.size == 0:
                        continue
                    deltas = np.abs(a_times[local_i] - b_times[matching_j])
                    time_pair_total += int(matching_j.size)
                    for days in TIME_WINDOWS_DAYS:
                        time_counts[days] += int(np.count_nonzero(deltas <= days * 86400))

                near_mask = scores >= NEAR_DUP_THRESHOLD
                near_dup_total += int(np.count_nonzero(near_mask))
                if len(near_dup_sample) < sample_size:
                    for local_i in range(len(idx_a)):
                        if len(near_dup_sample) >= sample_size:
                            break
                        for local_j in np.flatnonzero(near_mask[local_i]):
                            global_i = int(idx_a[local_i])
                            global_j = int(idx_b[local_j])
                            near_dup_sample.append({
                                "score": float(scores[local_i, local_j]),
                                "claim_id_a": claim_ids[global_i],
                                "claim_id_b": claim_ids[global_j],
                                "group_a": groups[global_i],
                                "group_b": groups[global_j],
                                "delta_hours": abs(int(first_seen[global_i]) - int(first_seen[global_j])) / 3600.0,
                                "text_a": claim_texts[global_i],
                                "text_b": claim_texts[global_j],
                            })
                            if len(near_dup_sample) >= sample_size:
                                break

    return {
        "degrees": degrees,
        "hist_counts": hist_counts,
        "below_hist": below_hist,
        "above_counts": above_counts,
        "time_counts": time_counts,
        "time_pair_total": time_pair_total,
        "near_dup_total": near_dup_total,
        "near_dup_sample": near_dup_sample,
        "eligible_total": eligible_total,
        "block_count": block_count,
        "grouped": grouped,
        "group_pair_spaces": group_pair_spaces,
    }


def print_report(corpus: dict, stats: dict, elapsed_seconds: float, chunk_size: int) -> None:
    n = len(corpus["claim_ids"])
    print(f"code_revision={get_code_revision()} embedding_model_id={EMBEDDING_MODEL_ID}")
    print(f"claims profiled: {n}")
    print(f"skipped: no_occurrence={corpus['skipped_no_occurrence']} no_source_group={corpus['skipped_no_group']}")
    print(f"vectors shape: {corpus['vectors'].shape}")
    print(f"chunk_size={chunk_size} matrix_blocks={stats['block_count']}")

    print("\n--- source_group_set sizes ---")
    for group in sorted(stats["grouped"], key=lambda g: tuple(sorted(g))):
        print(f"{group_label(group):>10}  claims={len(stats['grouped'][group])}")

    print("\n--- eligible disjoint group-pair space ---")
    for group_a, group_b, count in stats["group_pair_spaces"]:
        print(f"{group_label(group_a):>10} x {group_label(group_b):<10} pairs={count}")
    print(f"TOTAL eligible pairs={stats['eligible_total']}")

    print("\n--- cosine histogram ---")
    print(f"<0.50       {stats['below_hist']}")
    for i, count in enumerate(stats["hist_counts"]):
        lo = float(HIST_EDGES[i])
        hi = float(HIST_EDGES[i + 1])
        right = "]" if i == len(stats["hist_counts"]) - 1 else ")"
        print(f"[{lo:.2f},{hi:.2f}{right}  {int(count)}")
    print("\ncounts above thresholds:")
    for threshold, count in stats["above_counts"].items():
        pct = 100.0 * count / stats["eligible_total"] if stats["eligible_total"] else 0.0
        print(f"  >= {threshold:.2f}: {count} ({pct:.6f}%)")

    print("\n--- per-claim degree ---")
    for threshold, values in stats["degrees"].items():
        zero = int(np.count_nonzero(values == 0))
        max_degree = int(values.max()) if values.size else 0
        print(
            f"threshold={threshold:.2f} zero={zero}/{n} ({(100.0 * zero / n if n else 0.0):.2f}%) "
            f"median={percentile_int(values, 50):.1f} p90={percentile_int(values, 90):.1f} "
            f"p99={percentile_int(values, 99):.1f} max={max_degree}"
        )
        top_idx = np.argsort(-values.astype(np.int64), kind="stable")[:5]
        for rank, idx in enumerate(top_idx, 1):
            if values[idx] <= 0:
                break
            print(
                f"    hub#{rank} degree={int(values[idx])} claim_id={corpus['claim_ids'][idx]} "
                f"group={group_label(corpus['groups'][idx])} text={preview(corpus['claim_texts'][idx])}"
            )

    print(f"\n--- |Δt| for score >= {TIME_SCORE_THRESHOLD:.2f} ---")
    total = stats["time_pair_total"]
    print(f"pairs={total}")
    for days in TIME_WINDOWS_DAYS:
        count = stats["time_counts"][days]
        pct = 100.0 * count / total if total else 0.0
        print(f"  <= {days:>2}d: {count} ({pct:.2f}%)")

    print(f"\n--- near-duplicate band score >= {NEAR_DUP_THRESHOLD:.2f} ---")
    print(f"pairs={stats['near_dup_total']}")
    for rank, item in enumerate(stats["near_dup_sample"], 1):
        print(
            f"[{rank}] score={item['score']:.6f} |Δt|={item['delta_hours']:.2f}h "
            f"{group_label(item['group_a'])} x {group_label(item['group_b'])}"
        )
        print(f"    A {item['claim_id_a']}: {preview(item['text_a'])}")
        print(f"    B {item['claim_id_b']}: {preview(item['text_b'])}")
    print(f"\nPROFILE elapsed={elapsed_seconds:.2f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only blockwise profiler relation candidate v1")
    parser.add_argument("--chunk-size", type=int, default=1000, help="рядків лівої групи на matrix block (default: 1000)")
    parser.add_argument("--sample-size", type=int, default=20, help="кількість near-duplicate прикладів у звіті (default: 20)")
    args = parser.parse_args()
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be > 0")
    if args.sample_size < 0:
        parser.error("--sample-size must be >= 0")

    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)
        claim_rows = fetch_claims(conn)
        content_ids = sorted({row[3] for row in claim_rows}, key=str)
        occ_by_content = fetch_occurrences(conn, content_ids)

    corpus = build_corpus(claim_rows, occ_by_content)
    print(f"loaded claims={len(claim_rows)}; DB connection closed before profiling")
    start = time.perf_counter()
    stats = profile_corpus(corpus, args.chunk_size, args.sample_size)
    elapsed = time.perf_counter() - start
    print_report(corpus, stats, elapsed, args.chunk_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
