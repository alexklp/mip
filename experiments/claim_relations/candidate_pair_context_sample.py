#!/usr/bin/env python3
"""
Read-only context-rich sampler for relation-candidate calibration.

Повторює детерміновану stratified sample з candidate_pair_band_sample.py,
після чого для відібраних claim pairs підтягує ТОЙ САМИЙ provenance context,
який relation_judgment_worker передає Mamay: evidence_span, title,
context_snippet, source, contour_set, first_seen.

Скрипт нічого не пише в candidate_pairs / relation_judgments і не викликає
LLM. Мета — human review реальної якості cosine bands до фіксації candidate v2.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.claim_relations import candidate_pair_band_sample as sampler  # noqa: E402
from experiments.claim_relations import candidate_pair_profile as base  # noqa: E402
from experiments.claim_relations import relation_judgment_worker as judge  # noqa: E402


def context_preview(value: str | None, limit: int) -> str:
    if not value:
        return "<null>"
    clean = " ".join(value.split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def flatten_sample(reservoirs: list[list[dict]]) -> list[dict]:
    return [item for band_items in reservoirs for item in band_items]


def fetch_sample_meta(items: list[dict]) -> dict:
    claim_ids = sorted(
        {item["claim_id_a"] for item in items} | {item["claim_id_b"] for item in items},
        key=str,
    )
    with psycopg.connect(base.DB_DSN) as conn:
        return judge.fetch_claims_meta(conn, claim_ids)


def print_claim(label: str, claim_id, meta: dict, context_chars: int) -> None:
    item = meta[claim_id]
    first_seen = item["first_seen"].isoformat() if item["first_seen"] else None
    print(f"    {label} {claim_id}")
    print(f"      claim:   {context_preview(item['claim_text'], context_chars)}")
    print(f"      title:   {context_preview(item['title'], context_chars)}")
    print(f"      source:  {item['display_source']}")
    print(f"      first:   {first_seen}")
    print(f"      groups:  {item['contour_set']}")
    print(f"      evidence:{context_preview(item['evidence_span'], context_chars)}")
    print(f"      context: {context_preview(item['context_snippet'], context_chars)}")


def print_report(
    seen: list[int],
    reservoirs: list[list[dict]],
    meta: dict,
    elapsed: float,
    per_band: int,
    seed: int,
    context_chars: int,
) -> None:
    print(f"code_revision={base.get_code_revision()} embedding_model_id={base.EMBEDDING_MODEL_ID}")
    print(f"per_band={per_band} seed={seed} context_chars={context_chars}")

    for idx, ((lo, hi), items) in enumerate(zip(sampler.BANDS, reservoirs)):
        label_hi = "1.00]" if idx == len(sampler.BANDS) - 1 else f"{hi:.2f})"
        print(f"\n=== band [{lo:.2f},{label_hi} total={seen[idx]} sampled={len(items)} ===")
        items_sorted = sorted(
            items,
            key=lambda item: (-item["score"], str(item["claim_id_a"]), str(item["claim_id_b"])),
        )
        for rank, item in enumerate(items_sorted, 1):
            print(
                f"[{rank}] score={item['score']:.6f} |Δt|={item['delta_hours']:.2f}h "
                f"{base.group_label(item['group_a'])} x {base.group_label(item['group_b'])}"
            )
            print_claim("A", item["claim_id_a"], meta, context_chars)
            print_claim("B", item["claim_id_b"], meta, context_chars)

    print(f"\nCONTEXT SAMPLE elapsed={elapsed:.2f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only context-rich relation candidate sampler")
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--per-band", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20260902)
    parser.add_argument("--context-chars", type=int, default=260)
    args = parser.parse_args()

    if args.chunk_size <= 0:
        parser.error("--chunk-size must be > 0")
    if args.per_band < 0:
        parser.error("--per-band must be >= 0")
    if args.context_chars <= 0:
        parser.error("--context-chars must be > 0")

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
    sample_items = flatten_sample(reservoirs)
    meta = fetch_sample_meta(sample_items) if sample_items else {}
    missing = sorted(
        {
            claim_id
            for item in sample_items
            for claim_id in (item["claim_id_a"], item["claim_id_b"])
            if claim_id not in meta
        },
        key=str,
    )
    if missing:
        raise RuntimeError(f"metadata missing for sampled claim_ids: {missing}")

    elapsed = time.perf_counter() - started
    print_report(seen, reservoirs, meta, elapsed, args.per_band, args.seed, args.context_chars)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
