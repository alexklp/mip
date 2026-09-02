#!/usr/bin/env python3
"""
Read-only Mamay calibration for relation candidate selection.

Builds the current source-provenance cross-group candidate space with
claim cosine >= 0.80, stratifies a deterministic sample by full-content
cosine, and sends only the sampled pairs through the existing relation_judge
v3 prompt + validator.

IMPORTANT:
- no writes to candidate_pairs or relation_judgments;
- no DB connection is held during Mamay inference;
- time is not used for eligibility, ranking, or stratification;
- intended for calibration only, not live scheduling.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
import urllib.error
from collections import Counter
from pathlib import Path

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.claim_relations import candidate_pair_dual_profile as dual  # noqa: E402
from experiments.claim_relations import candidate_pair_profile as base  # noqa: E402
from experiments.claim_relations import relation_judgment_worker as judge  # noqa: E402

CONTENT_BANDS = (
    (-1.0, 0.50),
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.80),
    (0.80, 1.000001),
)


def content_band_index(score: float) -> int | None:
    for idx, (lo, hi) in enumerate(CONTENT_BANDS):
        if lo <= score < hi:
            return idx
    return None


def collect_candidates(corpus: dict, content_vectors: np.ndarray, chunk_size: int) -> list[list[dict]]:
    claim_vectors = corpus["vectors"]
    claim_ids = corpus["claim_ids"]
    groups = corpus["groups"]
    run_ids = corpus["run_ids"]

    grouped = base.group_indices(groups)
    group_keys = sorted(grouped, key=lambda g: tuple(sorted(g)))
    bands: list[list[dict]] = [[] for _ in CONTENT_BANDS]

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
            for start in range(0, len(idx_a_all), chunk_size):
                idx_a = idx_a_all[start : start + chunk_size]
                claim_scores = claim_vectors[idx_a] @ claim_b.T
                np.clip(claim_scores, -1.0, 1.0, out=claim_scores)
                rows, cols = np.nonzero(claim_scores >= dual.CLAIM_MIN)
                if rows.size == 0:
                    continue

                global_a = idx_a[rows]
                global_b = idx_b[cols]
                selected_claim_scores = claim_scores[rows, cols]
                selected_content_scores = np.sum(
                    content_vectors[global_a] * content_vectors[global_b], axis=1, dtype=np.float32
                )
                np.clip(selected_content_scores, -1.0, 1.0, out=selected_content_scores)

                for ga, gb, claim_score, content_score in zip(
                    global_a.tolist(),
                    global_b.tolist(),
                    selected_claim_scores.tolist(),
                    selected_content_scores.tolist(),
                ):
                    bidx = content_band_index(float(content_score))
                    if bidx is None:
                        raise AssertionError(f"content score outside configured bands: {content_score}")
                    bands[bidx].append(
                        {
                            "claim_id_a": claim_ids[ga],
                            "claim_id_b": claim_ids[gb],
                            "claim_score": float(claim_score),
                            "content_score": float(content_score),
                            "group_a": groups[ga],
                            "group_b": groups[gb],
                        }
                    )
    return bands


def stable_key(item: dict, seed: int) -> bytes:
    payload = f"{seed}|{item['claim_id_a']}|{item['claim_id_b']}".encode("utf-8")
    return hashlib.sha256(payload).digest()


def choose_sample(bands: list[list[dict]], per_band: int, seed: int) -> list[list[dict]]:
    chosen: list[list[dict]] = [[] for _ in bands]
    globally_used_claims = set()

    # First pass: maximize claim diversity across the whole calibration batch.
    for idx, items in enumerate(bands):
        ordered = sorted(items, key=lambda item: stable_key(item, seed))
        for item in ordered:
            if len(chosen[idx]) >= per_band:
                break
            a = item["claim_id_a"]
            b = item["claim_id_b"]
            if a in globally_used_claims or b in globally_used_claims:
                continue
            chosen[idx].append(item)
            globally_used_claims.update((a, b))

    # Fallback only if a band is too small after the diversity constraint.
    for idx, items in enumerate(bands):
        if len(chosen[idx]) >= per_band:
            continue
        existing_pairs = {(x["claim_id_a"], x["claim_id_b"]) for x in chosen[idx]}
        for item in sorted(items, key=lambda item: stable_key(item, seed)):
            if len(chosen[idx]) >= per_band:
                break
            key = (item["claim_id_a"], item["claim_id_b"])
            if key in existing_pairs:
                continue
            chosen[idx].append(item)
            existing_pairs.add(key)

    return chosen


def pair_id_for(item: dict) -> str:
    raw = f"{item['claim_id_a']}|{item['claim_id_b']}".encode("utf-8")
    return "cal-" + hashlib.sha256(raw).hexdigest()[:20]


def preview(value: str | None, limit: int = 180) -> str:
    if not value:
        return "<null>"
    clean = " ".join(value.split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def run_inference(item: dict, meta: dict, prompt_text: str) -> dict:
    claim_id_a = item["claim_id_a"]
    claim_id_b = item["claim_id_b"]
    pair_id = pair_id_for(item)
    payload = {
        "schema_version": "relation-judgment-input/2",
        "pair_id": pair_id,
        "claim_a": judge.build_claim_payload(claim_id_a, meta),
        "claim_b": judge.build_claim_payload(claim_id_b, meta),
    }
    prompt = judge.build_prompt_safe(prompt_text, payload)

    started = time.perf_counter()
    try:
        raw_text, latency, finish_reason, completion_tokens = judge.call_model(
            judge.DEFAULT_ENDPOINT,
            judge.DEFAULT_MODEL,
            prompt,
            judge.TIMEOUT,
            judge.MAX_TOKENS,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError) as exc:
        return {
            "transport_error": f"{type(exc).__name__}: {exc}",
            "elapsed": time.perf_counter() - started,
        }

    (
        valid,
        errors,
        relation_label,
        rationale_text,
        shared_referent_status,
        shared_referent_evidence,
        fence_stripped,
    ) = judge.validate_relation_judgment(raw_text, pair_id, meta, claim_id_a, claim_id_b)

    return {
        "valid": valid,
        "errors": errors,
        "relation_label": relation_label,
        "rationale_text": rationale_text,
        "shared_referent_status": shared_referent_status,
        "shared_referent_evidence": shared_referent_evidence,
        "fence_stripped": fence_stripped,
        "latency": latency,
        "finish_reason": finish_reason,
        "completion_tokens": completion_tokens,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Mamay calibration of dual-score relation candidates")
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--per-band", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260902)
    args = parser.parse_args()

    if args.chunk_size <= 0:
        parser.error("--chunk-size must be > 0")
    if args.per_band <= 0:
        parser.error("--per-band must be > 0")

    # Short read phase only. Close before any Mamay call.
    with psycopg.connect(base.DB_DSN) as conn:
        register_vector(conn)
        base.verify_registered_model(conn)
        prompt_text = judge.fetch_and_verify_registry(conn)
        claim_rows = base.fetch_claims(conn)
        content_ids = sorted({row[3] for row in claim_rows}, key=str)
        occ_by_content = base.fetch_occurrences(conn, content_ids)
        claim_ids_all = [row[0] for row in claim_rows]
        content_by_claim = dual.fetch_content_vector_by_claim(conn, claim_ids_all)

    corpus = base.build_corpus(claim_rows, occ_by_content)
    if corpus["skipped_no_occurrence"] or corpus["skipped_no_group"]:
        raise RuntimeError(
            "calibration requires complete source-provenance coverage; "
            f"skipped_no_occurrence={corpus['skipped_no_occurrence']} "
            f"skipped_no_group={corpus['skipped_no_group']}"
        )
    if corpus["claim_ids"] != claim_ids_all:
        raise RuntimeError("claim ordering changed while building corpus")
    content_vectors = dual.build_content_matrix(corpus["claim_ids"], content_by_claim)

    bands = collect_candidates(corpus, content_vectors, args.chunk_size)
    chosen = choose_sample(bands, args.per_band, args.seed)
    selected = [item for band_items in chosen for item in band_items]
    selected_claim_ids = sorted(
        {item["claim_id_a"] for item in selected} | {item["claim_id_b"] for item in selected},
        key=str,
    )

    with psycopg.connect(base.DB_DSN) as conn:
        meta = judge.fetch_claims_meta(conn, selected_claim_ids)
    missing = [claim_id for claim_id in selected_claim_ids if claim_id not in meta]
    if missing:
        raise RuntimeError(f"claim metadata missing for sampled claim_ids: {missing}")

    print(f"code_revision={base.get_code_revision()} embedding_model_id={base.EMBEDDING_MODEL_ID}")
    print(f"claim_min={dual.CLAIM_MIN:.2f} per_band={args.per_band} seed={args.seed}")
    print("time_signal=NOT USED for candidate selection")
    print(f"selected_total={len(selected)}; DB connection closed before Mamay inference")
    for idx, ((lo, hi), items) in enumerate(zip(CONTENT_BANDS, chosen)):
        hi_label = "1.00]" if idx == len(CONTENT_BANDS) - 1 else f"{hi:.2f})"
        print(f"  content_band=[{lo:.2f},{hi_label} pool={len(bands[idx])} selected={len(items)}")

    overall_labels = Counter()
    overall_referents = Counter()
    valid_count = 0
    invalid_count = 0
    transport_count = 0
    started_all = time.perf_counter()

    for band_idx, ((lo, hi), items) in enumerate(zip(CONTENT_BANDS, chosen)):
        hi_label = "1.00]" if band_idx == len(CONTENT_BANDS) - 1 else f"{hi:.2f})"
        print(f"\n=== content band [{lo:.2f},{hi_label} ===")
        for rank, item in enumerate(items, 1):
            a = item["claim_id_a"]
            b = item["claim_id_b"]
            print(
                f"[{rank}] claim_score={item['claim_score']:.6f} "
                f"content_score={item['content_score']:.6f} "
                f"{base.group_label(item['group_a'])} x {base.group_label(item['group_b'])}"
            )
            print(f"    A claim: {preview(meta[a]['claim_text'])}")
            print(f"    A title: {preview(meta[a]['title'])}")
            print(f"    B claim: {preview(meta[b]['claim_text'])}")
            print(f"    B title: {preview(meta[b]['title'])}")

            result = run_inference(item, meta, prompt_text)
            if "transport_error" in result:
                transport_count += 1
                print(f"    RESULT transport_error: {result['transport_error']}")
                continue

            if result["valid"]:
                valid_count += 1
                overall_labels[result["relation_label"]] += 1
                overall_referents[result["shared_referent_status"]] += 1
                print(
                    f"    RESULT valid referent={result['shared_referent_status']} "
                    f"label={result['relation_label']} latency={result['latency']:.1f}s "
                    f"fence_stripped={result['fence_stripped']}"
                )
                print(f"    evidence:  {result['shared_referent_evidence']}")
                print(f"    rationale: {result['rationale_text']}")
            else:
                invalid_count += 1
                print(
                    f"    RESULT invalid latency={result['latency']:.1f}s "
                    f"fence_stripped={result['fence_stripped']}"
                )
                for err in result["errors"]:
                    print(f"      - {err}")

    print("\n--- calibration summary ---")
    print(
        f"total={len(selected)} valid={valid_count} invalid={invalid_count} "
        f"transport_error={transport_count} elapsed={time.perf_counter() - started_all:.1f}s"
    )
    print("referents: " + (", ".join(f"{k}={v}" for k, v in sorted(overall_referents.items())) or "none"))
    print("labels: " + (", ".join(f"{k}={v}" for k, v in sorted(overall_labels.items())) or "none"))
    print("READ-ONLY: no candidate_pairs/relation_judgments rows were written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
