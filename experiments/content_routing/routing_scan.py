#!/usr/bin/env python3
"""
experiments/content_routing/routing_scan.py — read-only розвідка для Content
Routing v1. НІЧОГО не пише в БД. Мета: порахувати semantic routing score
(sim_rel - sim_irr, zero-shot prototypes поверх існуючих BGE-M3 embeddings)
на всьому eligible corpus, показати розподіл і приклади з трьох зон
(analyze/maybe/skip), щоб очима перевірити стартові пороги ДО того, як
писати sql/009 + routing_worker.py.

Кодування prototypes — той самий виклик, що collectors/embed_worker.py
використовує для content_items: model.encode(texts, normalize_embeddings=True),
без жодних query:/passage: префіксів, без title (тільки text_content). Якщо
цей препроцесинг розійдеться з тим, що реально в embed_worker.py, — cosine
similarity стає безглуздим мовчки, тому константи (MODEL_NAME/MODEL_REVISION/
FRAMEWORK/FRAMEWORK_VERSION/DIMENSION) продубльовані 1:1, і verify_registered_model
звіряє їх з БД так само fail-fast, як в embed_worker.py.

Пороги T_SKIP/T_ANALYZE тут — стартова гіпотеза, НЕ фінал. Мета прогону —
подивитись розподіл і приклади, після чого пороги можна посунути вручну
перед тим, як писати routing_worker.py.
"""
from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

DB_DSN = "dbname=mip_dev"

EMBEDDING_MODEL_ID = 1
MODEL_NAME = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
FRAMEWORK = "sentence-transformers"
FRAMEWORK_VERSION = "5.7.0"
DIMENSION = 1024
METRIC = "cosine"
ENCODING_PARAMS = {"normalize_embeddings": True, "input_field": "text_content"}

PROTOTYPES_FILE = Path(__file__).parent / "prototypes_v1.json"

# Стартові пороги — гіпотеза для eyeball-перевірки, не фінальне рішення.
T_SKIP = -0.07
T_ANALYZE = 0.10

N_EXAMPLES_PER_ZONE = 5
PREVIEW_LEN = 160


def verify_registered_model(conn) -> None:
    """Той самий fail-fast патерн, що в embed_worker.py — звіряємо, що embeddings
    в БД реально зроблені тією моделлю/конфігом, яку тут же використовуємо для
    кодування prototypes. Розбіжність -> RuntimeError, без мовчазного припущення."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT model_name, model_revision, framework, framework_version,
                   dimension, metric, encoding_params
            FROM embedding_models WHERE embedding_model_id = %s
            """,
            (EMBEDDING_MODEL_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"embedding_model_id={EMBEDDING_MODEL_ID} не зареєстровано в embedding_models.")
    expected = (MODEL_NAME, MODEL_REVISION, FRAMEWORK, FRAMEWORK_VERSION, DIMENSION, METRIC, ENCODING_PARAMS)
    if tuple(row) != expected:
        raise RuntimeError(
            "Конфігурація embedding-моделі в БД НЕ збігається з константами в цьому скрипті.\n"
            f"  БД:     {tuple(row)}\n"
            f"  Скрипт: {expected}"
        )


def load_model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION, device="cpu", local_files_only=True)


def load_prototype_vectors(model: SentenceTransformer) -> tuple[np.ndarray, np.ndarray, dict]:
    data = json.loads(PROTOTYPES_FILE.read_text(encoding="utf-8"))
    rel_vectors = np.asarray(model.encode(data["relevant"], normalize_embeddings=True))
    irr_vectors = np.asarray(model.encode(data["irrelevant"], normalize_embeddings=True))
    return rel_vectors, irr_vectors, data


def fetch_eligible(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ci.content_id, ci.title, ci.text_content, e.embedding
            FROM content_items ci
            JOIN embeddings e ON e.content_id = ci.content_id AND e.embedding_model_id = %s
            """,
            (EMBEDDING_MODEL_ID,),
        )
        return cur.fetchall()


def decide(score: float) -> str:
    if score >= T_ANALYZE:
        return "analyze"
    if score <= T_SKIP:
        return "skip"
    return "maybe"


def preview(title, text_content: str) -> str:
    base = title if title else text_content
    base = base.replace("\n", " ").strip()
    return base[:PREVIEW_LEN] + ("…" if len(base) > PREVIEW_LEN else "")


def run() -> None:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)

        print("loading BGE-M3 model...")
        model = load_model()
        rel_vectors, irr_vectors, proto_data = load_prototype_vectors(model)
        print(f"prototypes: {len(proto_data['relevant'])} relevant, {len(proto_data['irrelevant'])} irrelevant")

        rows = fetch_eligible(conn)
        print(f"eligible content: {len(rows)}")

        content_ids = [r[0] for r in rows]
        titles = [r[1] for r in rows]
        texts = [r[2] for r in rows]
        vectors = np.vstack([r[3].to_numpy() for r in rows]).astype(np.float32)  # (N, 1024)
        print(f"vectors shape: {vectors.shape}, dtype: {vectors.dtype}")

        sim_rel = (vectors @ rel_vectors.T).max(axis=1)
        sim_irr = (vectors @ irr_vectors.T).max(axis=1)
        score = sim_rel - sim_irr

        decisions = [decide(s) for s in score]
        counts = Counter(decisions)

        print(f"\n--- score distribution (T_SKIP={T_SKIP}, T_ANALYZE={T_ANALYZE}) ---")
        percentiles = [0, 10, 25, 50, 75, 90, 100]
        pvals = np.percentile(score, percentiles)
        for p, v in zip(percentiles, pvals):
            print(f"  p{p:>3}: {v:+.4f}")

        print(f"\n--- decision counts (total={len(rows)}) ---")
        for d in ("analyze", "maybe", "skip"):
            n = counts.get(d, 0)
            print(f"  {d:>8}: {n:>5} ({100*n/len(rows):.1f}%)")

        rng = random.Random(42)
        print(f"\n--- {N_EXAMPLES_PER_ZONE} random examples per zone ---")
        for zone in ("analyze", "maybe", "skip"):
            idxs = [i for i, d in enumerate(decisions) if d == zone]
            sample = rng.sample(idxs, min(N_EXAMPLES_PER_ZONE, len(idxs)))
            print(f"\n[{zone.upper()}] ({len(idxs)} total)")
            for i in sample:
                print(f"  score={score[i]:+.4f} content_id={content_ids[i]}")
                print(f"    {preview(titles[i], texts[i])}")

        out_path = Path(__file__).parent / "routing_scan_results.jsonl"
        with out_path.open("w", encoding="utf-8") as f:
            for i in range(len(rows)):
                f.write(json.dumps({
                    "content_id": str(content_ids[i]),
                    "decision": decisions[i],
                    "score": round(float(score[i]), 4),
                    "title": titles[i],
                }, ensure_ascii=False) + "\n")
        print(f"\nfull results written to {out_path} ({len(rows)} rows)")


if __name__ == "__main__":
    run()
