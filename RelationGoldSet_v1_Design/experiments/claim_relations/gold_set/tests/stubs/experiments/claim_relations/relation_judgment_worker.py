"""ТЕСТОВИЙ STUB relation_judgment_worker (лише fetch_claims_meta)."""
from __future__ import annotations
import datetime
from experiments.claim_relations import candidate_pair_profile as base

CONTEXT_WINDOW_CHARS = 400


def fetch_claims_meta(conn, claim_ids):
    s = base.SYNTHETIC
    pos = {s["claim_ids"][i]: i for i in range(len(s["claim_ids"]))}
    base_time = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    meta = {}
    for cid in claim_ids:
        i = pos[cid]
        meta[cid] = {
            "claim_text": s["texts"][i],
            "evidence_span": f"evidence for {s['texts'][i]}",
            "title": f"Title {i}",
            "context_snippet": f"context around claim {i} " * 5,
            "contour_set": sorted({s["groups"][i]}),
            "first_seen": base_time + datetime.timedelta(hours=i),
            "display_source": f"telegram:src{s['groups'][i]} (https://example/{i})",
        }
    return meta
