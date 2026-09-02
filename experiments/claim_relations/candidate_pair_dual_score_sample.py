#!/usr/bin/env python3
"""
Read-only dual-score sampler for relation candidate calibration.

Для тієї самої детермінованої stratified sample, що
candidate_pair_band_sample.py, показує два незалежні cosine-сигнали:

1. claim_score   — similarity між claim embeddings (поточний v1 recall signal);
2. content_score — similarity між already-existing full content embeddings
   для content_items, з яких походять ці claims.

Час НЕ використовується як eligibility/filter/ranking signal. На поточному
corpus історія збору нерівномірна (batch/backfill/live змішані), тому time
залишається лише окремою діагностичною ознакою і тут навмисно не виводиться.

Скрипт read-only: не пише в candidate_pairs/relation_judgments і не викликає LLM.
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

from experiments.claim_relations import candidate_pair_band_sample as sampler  # noqa: E402
from experiments.claim_relations import candidate_pair_profile as base  # noqa: E402
from experiments.claim_relations import relation_judgment_worker as judge  # noqa: E402


def flatten_sample(reservoirs: list[list[dict]]) -> list[dict]:
    return [item for band_items in reservoirs for item in band_items]


def fetch_content_embeddings(claim_ids: list) -> dict:
    if not claim_ids:
        return {}
    with psycopg.connect(base.DB_DSN) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.claim_id, r.content_id, e.embedding
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
    return {
        claim_id: {
            "content_id": content_id,
            "vector": embedding.to_numpy().astype(np.float32, copy=False),
        }
        for claim_id, content_id, embedding in rows
    }


def cosine_from_normalized(a: np.ndarray, b: np.ndarray) -> float:
    score = float(np.dot(a, b))
    return float(np.clip(score, -1.0, 1.0))


def preview(value: str | None, limit: int = 220) -> str:
    if not value:
        return "<null>"
    clean = " ".join(value.split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only claim-vs-content cosine sampler")
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--per-band", type=int, default=4)
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
    seen, reservoirs = sampler.sample_corpus(corpus, args.chunk_size, args.per_band, args.seed)
    items = flatten_sample(reservoirs)
    claim_ids = sorted(
        {item["claim_id_a"] for item in items} | {item["claim_id_b"] for item in items},
        key=str,
    )
    content_meta = fetch_content_embeddings(claim_ids)
    with psycopg.connect(base.DB_DSN) as conn:
        meta = judge.fetch_claims_meta(conn, claim_ids)

    missing_content = [claim_id for claim_id in claim_ids if claim_id not in content_meta]
    if missing_content:
        raise RuntimeError(f"content embedding missing for sampled claim_ids: {missing_content}")
    missing_meta = [claim_id for claim_id in claim_ids if claim_id not in meta]
    if missing_meta:
        raise RuntimeError(f"claim metadata missing for sampled claim_ids: {missing_meta}")

    print(f"code_revision={base.get_code_revision()} embedding_model_id={base.EMBEDDING_MODEL_ID}")
    print(f"per_band={args.per_band} seed={args.seed}; time intentionally not used")

    for idx, ((lo, hi), band_items) in enumerate(zip(sampler.BANDS, reservoirs)):
        label_hi = "1.00]" if idx == len(sampler.BANDS) - 1 else f"{hi:.2f})"
        print(f"\n=== claim band [{lo:.2f},{label_hi} total={seen[idx]} sampled={len(band_items)} ===")
        band_items = sorted(
            band_items,
            key=lambda item: (-item["score"], str(item["claim_id_a"]), str(item["claim_id_b"])),
        )
        for rank, item in enumerate(band_items, 1):
            a = item["claim_id_a"]
            b = item["claim_id_b"]
            content_score = cosine_from_normalized(content_meta[a]["vector"], content_meta[b]["vector"])
            delta = content_score - float(item["score"])
            print(
                f"[{rank}] claim_score={item['score']:.6f} content_score={content_score:.6f} "
                f"delta={delta:+.6f} {base.group_label(item['group_a'])} x {base.group_label(item['group_b'])}"
            )
            print(f"    A claim: {preview(meta[a]['claim_text'])}")
            print(f"    A title: {preview(meta[a]['title'])}")
            print(f"    B claim: {preview(meta[b]['claim_text'])}")
            print(f"    B title: {preview(meta[b]['title'])}")

    print(f"\nDUAL SCORE elapsed={time.perf_counter() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
