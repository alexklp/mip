#!/usr/bin/env python3
"""
Read-only stratified sampler relation-candidate space.

Працює поверх тієї самої source-provenance cross-group семантики, що
candidate_pair_profile.py / candidate_version=1, але не намагається будувати
глобальний ranking і НІЧОГО не пише в candidate_pairs.

Мета: дати детерміновану репрезентативну вибірку пар із cosine-смуг
0.80-0.85 / 0.85-0.90 / 0.90-0.95 / 0.95-0.97 / 0.97-1.00 перед тим,
як фіксувати параметри candidate v2 або витрачати Mamay budget.

Вибірка у кожній смузі робиться reservoir sampling із фіксованим seed;
пам'ять bounded розміром вибірки, а не кількістю eligible пар.
"""
from __future__ import annotations

import argparse
import random
import time

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from experiments.claim_relations import candidate_pair_profile as base

BANDS = (
    (0.80, 0.85),
    (0.85, 0.90),
    (0.90, 0.95),
    (0.95, 0.97),
    (0.97, 1.000001),
)


def band_index(score: float) -> int | None:
    for idx, (lo, hi) in enumerate(BANDS):
        if lo <= score < hi:
            return idx
    return None


def canonical_pair(claim_id_a, claim_id_b):
    return (claim_id_a, claim_id_b) if claim_id_a < claim_id_b else (claim_id_b, claim_id_a)


def sample_corpus(corpus: dict, chunk_size: int, per_band: int, seed: int) -> tuple[list[int], list[list[dict]]]:
    vectors = corpus["vectors"]
    claim_ids = corpus["claim_ids"]
    claim_texts = corpus["claim_texts"]
    groups = corpus["groups"]
    first_seen = corpus["first_seen"]
    run_ids = corpus["run_ids"]

    grouped = base.group_indices(groups)
    group_keys = sorted(grouped, key=lambda g: tuple(sorted(g)))

    rng = random.Random(seed)
    seen = [0 for _ in BANDS]
    reservoirs: list[list[dict]] = [[] for _ in BANDS]

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

            b_vectors = vectors[idx_b]
            b_times = first_seen[idx_b]

            for start in range(0, len(idx_a_all), chunk_size):
                idx_a = idx_a_all[start : start + chunk_size]
                scores = vectors[idx_a] @ b_vectors.T
                np.clip(scores, -1.0, 1.0, out=scores)

                rows, cols = np.nonzero(scores >= BANDS[0][0])
                for local_i, local_j in zip(rows.tolist(), cols.tolist()):
                    score = float(scores[local_i, local_j])
                    bidx = band_index(score)
                    if bidx is None:
                        continue

                    global_i = int(idx_a[local_i])
                    global_j = int(idx_b[local_j])
                    claim_id_a, claim_id_b = canonical_pair(claim_ids[global_i], claim_ids[global_j])
                    item = {
                        "score": score,
                        "claim_id_a": claim_id_a,
                        "claim_id_b": claim_id_b,
                        "group_a": groups[global_i],
                        "group_b": groups[global_j],
                        "delta_hours": abs(int(first_seen[global_i]) - int(b_times[local_j])) / 3600.0,
                        "text_a": claim_texts[global_i],
                        "text_b": claim_texts[global_j],
                    }

                    seen[bidx] += 1
                    if len(reservoirs[bidx]) < per_band:
                        reservoirs[bidx].append(item)
                    elif per_band > 0:
                        slot = rng.randrange(seen[bidx])
                        if slot < per_band:
                            reservoirs[bidx][slot] = item

    return seen, reservoirs


def print_report(corpus: dict, seen: list[int], reservoirs: list[list[dict]], elapsed: float, per_band: int, seed: int) -> None:
    print(f"code_revision={base.get_code_revision()} embedding_model_id={base.EMBEDDING_MODEL_ID}")
    print(f"claims profiled={len(corpus['claim_ids'])} per_band={per_band} seed={seed}")
    print(
        f"skipped: no_occurrence={corpus['skipped_no_occurrence']} "
        f"no_source_group={corpus['skipped_no_group']}"
    )

    for idx, ((lo, hi), items) in enumerate(zip(BANDS, reservoirs)):
        label_hi = "1.00]" if idx == len(BANDS) - 1 else f"{hi:.2f})"
        print(f"\n=== band [{lo:.2f},{label_hi} total={seen[idx]} sampled={len(items)} ===")
        # Presentation order is deterministic and easier to inspect than reservoir order.
        items_sorted = sorted(
            items,
            key=lambda item: (-item["score"], str(item["claim_id_a"]), str(item["claim_id_b"])),
        )
        for rank, item in enumerate(items_sorted, 1):
            print(
                f"[{rank}] score={item['score']:.6f} |Δt|={item['delta_hours']:.2f}h "
                f"{base.group_label(item['group_a'])} x {base.group_label(item['group_b'])}"
            )
            print(f"    A {item['claim_id_a']}: {base.preview(item['text_a'])}")
            print(f"    B {item['claim_id_b']}: {base.preview(item['text_b'])}")

    print(f"\nSAMPLE elapsed={elapsed:.2f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only stratified sampler relation-candidate cosine bands")
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--per-band", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260902)
    args = parser.parse_args()

    if args.chunk_size <= 0:
        parser.error("--chunk-size must be > 0")
    if args.per_band < 0:
        parser.error("--per-band must be >= 0")

    with psycopg.connect(base.DB_DSN) as conn:
        register_vector(conn)
        base.verify_registered_model(conn)
        claim_rows = base.fetch_claims(conn)
        content_ids = sorted({row[3] for row in claim_rows}, key=str)
        occ_by_content = base.fetch_occurrences(conn, content_ids)

    corpus = base.build_corpus(claim_rows, occ_by_content)
    print(f"loaded claims={len(claim_rows)}; DB connection closed before sampling")

    started = time.perf_counter()
    seen, reservoirs = sample_corpus(corpus, args.chunk_size, args.per_band, args.seed)
    elapsed = time.perf_counter() - started
    print_report(corpus, seen, reservoirs, elapsed, args.per_band, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
