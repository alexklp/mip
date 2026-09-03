"""ТЕСТОВИЙ STUB candidate_pair_dual_profile."""
from __future__ import annotations
import numpy as np
from experiments.claim_relations import candidate_pair_profile as base


def fetch_content_vector_by_claim(conn, claim_ids):
    s = base.SYNTHETIC
    by_id = {s["claim_ids"][i]: s["content_vecs"][i] for i in range(len(s["claim_ids"]))}
    return {cid: by_id[cid] for cid in claim_ids if cid in by_id}


def build_content_matrix(claim_ids, vector_by_claim):
    missing = [c for c in claim_ids if c not in vector_by_claim]
    if missing:
        raise RuntimeError(f"content embedding missing for {len(missing)} claims")
    return np.vstack([vector_by_claim[c] for c in claim_ids]).astype(np.float32)
