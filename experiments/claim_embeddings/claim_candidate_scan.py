#!/usr/bin/env python3
"""
experiments/claim_embeddings/claim_candidate_scan.py — read-only розвідка:
серед усіх claims з claim_embeddings (embedding_model_id=1) шукає
найсильніші cross-contour candidate pairs за cosine similarity.

НІЧОГО не пише в БД. Мета — швидко подивитись, чи ловимо ми вже спільні
факти/події між contour, перш ніж думати про будь-які canonical
relations/events/entities.

Cross-contour контракт (узгоджено явно, варіант 1 — консервативний):
  - у claim НЕМАЄ одного contour_id: його content_id може мати occurrences
    в кількох contour одночасно (--contour-id у claim_extract_worker.py
    фільтрував через EXISTS, не ексклюзивно).
  - contour_set(claim) = множина усіх distinct contour_id серед occurrences
    його content_id.
  - пара (A, B) вважається cross-contour, ТІЛЬКИ якщо contour_set(A) і
    contour_set(B) повністю НЕ перетинаються.

first_seen(claim) = MIN(item_occurrences.collected_at) серед occurrences
його content_id. Це "коли вперше побачили" (спостережуваний час), НЕ event
time і НЕ publication time — item_occurrences.published_at свідомо не
використовується тут.

Провенанс: кожен claim враховується РІВНО ОДИН РАЗ у similarity matrix —
один рядок на claim_id, contour_set і first_seen вже агреговані по ВСІХ
occurrences його content на етапі збору даних, тому жодна пара не
розмножується через кількість occurrences одного content. Для короткого
display source береться occurrence з мінімальним collected_at.

Часове вікно НЕ фільтрується — лише виводиться |Δt|, щоб подивитись
розподіл перед тим, як перетворювати поріг на контракт.
"""
from __future__ import annotations

from collections import defaultdict

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

TOP_N = 30
PREVIEW_LEN = 200


def verify_registered_model(conn) -> None:
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


def fetch_claims(conn) -> list[tuple]:
    """Один рядок на claim_id: claim_id, claim_text, run_id, content_id, embedding."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.claim_id, c.claim_text, c.run_id, r.content_id, ce.embedding
            FROM claim_embeddings ce
            JOIN claims c ON c.claim_id = ce.claim_id
            JOIN claim_extraction_runs r ON r.run_id = c.run_id
            WHERE ce.embedding_model_id = %s
            """,
            (EMBEDDING_MODEL_ID,),
        )
        return cur.fetchall()


def fetch_occurrences(conn, content_ids: list) -> dict:
    """Для кожного content_id: усі (contour_id, collected_at, name, url_or_handle,
    source_type) — щоб агрегувати contour_set/first_seen/display source один раз."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT io.content_id, s.contour_id, io.collected_at, s.name, s.url_or_handle, s.source_type
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            WHERE io.content_id = ANY(%s)
            """,
            (content_ids,),
        )
        rows = cur.fetchall()

    by_content = defaultdict(list)
    for content_id, contour_id, collected_at, name, url_or_handle, source_type in rows:
        by_content[content_id].append((contour_id, collected_at, name, url_or_handle, source_type))
    return by_content


def build_claim_meta(claims_rows, occ_by_content) -> dict:
    """claim_id -> dict(claim_text, run_id, content_id, contour_set, first_seen, display_source)."""
    meta = {}
    for claim_id, claim_text, run_id, content_id, _embedding in claims_rows:
        occs = occ_by_content.get(content_id, [])
        if not occs:
            print(f"WARNING: claim_id={claim_id} content_id={content_id} — немає occurrences, пропускаю")
            continue
        contour_ids = {c for c, *_ in occs if c is not None}
        if not contour_ids:
            print(f"WARNING: claim_id={claim_id} content_id={content_id} — жоден occurrence без contour_id, пропускаю")
            continue
        earliest = min(occs, key=lambda o: o[1])  # (contour_id, collected_at, name, url_or_handle, source_type)
        meta[claim_id] = {
            "claim_text": claim_text,
            "run_id": run_id,
            "content_id": content_id,
            "contour_set": contour_ids,
            "first_seen": min(o[1] for o in occs),
            "display_source": f"{earliest[4]}:{earliest[2]} ({earliest[3]})",
        }
    return meta


def preview(text: str) -> str:
    text = text.replace("\n", " ").strip()
    return text[:PREVIEW_LEN] + ("…" if len(text) > PREVIEW_LEN else "")


def run() -> None:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)

        claims_rows = fetch_claims(conn)
        print(f"claims with embeddings: {len(claims_rows)}")

        content_ids = list({row[3] for row in claims_rows})
        occ_by_content = fetch_occurrences(conn, content_ids)

        meta = build_claim_meta(claims_rows, occ_by_content)
        print(f"claims with valid contour/timestamp metadata: {len(meta)}")

        usable_rows = [row for row in claims_rows if row[0] in meta]
        claim_ids = [row[0] for row in usable_rows]
        vectors = np.vstack([row[4].to_numpy() for row in usable_rows]).astype(np.float32)
        print(f"vectors shape: {vectors.shape}")

        sim_matrix = vectors @ vectors.T  # embeddings normalized -> це вже cosine similarity

        n = len(claim_ids)
        candidates = []
        for i in range(n):
            meta_i = meta[claim_ids[i]]
            for j in range(i + 1, n):
                meta_j = meta[claim_ids[j]]
                if meta_i["run_id"] == meta_j["run_id"]:
                    continue  # той самий run (=той самий content) — не порівнюємо
                if meta_i["contour_set"] & meta_j["contour_set"]:
                    continue  # є спільний contour — не cross-contour у варіанті 1
                delta_t = abs((meta_i["first_seen"] - meta_j["first_seen"]).total_seconds())
                candidates.append((float(sim_matrix[i, j]), i, j, delta_t))

        print(f"cross-contour candidate pairs (all, before top-N cut): {len(candidates)}")
        candidates.sort(key=lambda x: x[0], reverse=True)

        print(f"\n--- top {TOP_N} cross-contour pairs by cosine similarity ---\n")
        for rank, (sim, i, j, delta_t) in enumerate(candidates[:TOP_N], 1):
            a, b = meta[claim_ids[i]], meta[claim_ids[j]]
            delta_h = delta_t / 3600
            print(f"[{rank}] cosine={sim:.4f}  |Δt|={delta_h:.1f}h")
            print(f"    A: claim_id={claim_ids[i]} run_id={a['run_id']} content_id={a['content_id']}")
            print(f"       contour_set={sorted(a['contour_set'])} source={a['display_source']} first_seen={a['first_seen']}")
            print(f"       {preview(a['claim_text'])}")
            print(f"    B: claim_id={claim_ids[j]} run_id={b['run_id']} content_id={b['content_id']}")
            print(f"       contour_set={sorted(b['contour_set'])} source={b['display_source']} first_seen={b['first_seen']}")
            print(f"       {preview(b['claim_text'])}")
            print()


if __name__ == "__main__":
    run()
