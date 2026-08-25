#!/usr/bin/env python3
"""
experiments/semantic_search/semantic_search.py -- MIP Semantic Search v1.

Read-only CLI semantic search поверх вже існуючих BGE-M3 embeddings
(embedding_model_id=1): `embeddings` (content_items) та `claim_embeddings`
(claims). НІЧОГО не пише в БД, не змінює жоден existing pipeline, не додає
DDL. Query кодується тим самим викликом, що вже використовує весь MIP
embedding pipeline (collectors/embed_worker.py,
experiments/content_routing/routing_worker.py,
experiments/claim_embeddings/claim_embedding_worker.py):
model.encode([text], normalize_embeddings=True), БЕЗ query:/passage:
префіксів, CPU, local_files_only=True.

Fail-fast звірка embedding_model_id=1 з БД -- той самий verify_registered_model
патерн, що і в усіх embedding-скриптах вище. Розбіжність -> RuntimeError,
скрипт нічого не рахує на "чужій" моделі мовчки.

Ranking: cosine similarity через pgvector `<=>` (cosine distance) прямо в SQL
поверх `embeddings`/`claim_embeddings` (embedding_model_id=1), similarity =
1 - distance. Без relevance threshold в v1 -- тільки deterministic top-K за
distance ASC, потім за id (детермінований tie-break при рівних similarity).
Запит НЕ кастується до partial HNSW-індексу (idx_*_hnsw_bge_m3) явним
виразом -- на поточному обсязі корпусу (тисячі content, сотні claims) seq
scan + сортування досить швидкі; якщо колись стане повільно -- окрема задача
на явний expression-cast під індекс, не зараз (без передчасної оптимізації).

Claim -> canonical event linkage (якщо існує): claim_id -> (event_verification_members,
included=true) -> (event_verifications, status='valid', event_decision='accepted_seed')
-> event_candidate_id -> canonical_event_members -> canonical_events. Відсутність
canonical event -- нормальний, очікуваний результат (claim ще не дійшов до event
pipeline, або claim належить rejected_seed/invalid verification).

v1 explicit non-goals (за ТЗ): без LLM/RAG, без query expansion, без reranker,
без hybrid BM25/FTS, без нових thresholds, без web UI, без persistence search
history, без DDL, read-only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
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

REPO_ROOT = Path(__file__).resolve().parents[2]  # experiments/semantic_search/../.. = ~/mip

SNIPPET_LEN = 240
EVIDENCE_PREVIEW_LEN = 160


def get_code_revision() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sha}+dirty" if dirty else sha


def verify_registered_model(conn) -> None:
    """Той самий fail-fast патерн, що в embed_worker.py/routing_worker.py/
    claim_embedding_worker.py: hardcoded константи проти embedding_models,
    розбіжність -> RuntimeError, без мовчазного припущення."""
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
    actual = tuple(row)
    if actual != expected:
        raise RuntimeError(
            "Конфігурація embedding-моделі в БД НЕ збігається з константами в цьому скрипті.\n"
            f"  БД:     {actual}\n"
            f"  Скрипт: {expected}"
        )


def load_model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION, device="cpu", local_files_only=True)


def encode_query(model: SentenceTransformer, query: str) -> np.ndarray:
    """Той самий виклик, що весь MIP embedding pipeline: без query:/passage:
    префіксів (routing_scan.py), normalize_embeddings=True."""
    vec = model.encode([query], normalize_embeddings=ENCODING_PARAMS["normalize_embeddings"])[0]
    vec = np.asarray(vec, dtype=np.float32)
    if len(vec) != DIMENSION:
        raise RuntimeError(f"query embedding dimension mismatch: got {len(vec)}, expected {DIMENSION}")
    return vec


def preview(text: str, length: int = SNIPPET_LEN) -> str:
    text = text.replace("\n", " ").strip()
    return text[:length] + ("…" if len(text) > length else "")


def fetch_content_sources(conn, content_ids: list) -> dict:
    """content_id -> {"sources": [{name, source_type, contour_id}, ...] (distinct),
    "occurrence_count": int (усі occurrences, включно з дублями по джерелу)}."""
    if not content_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT io.content_id, s.name, s.source_type, s.contour_id
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            WHERE io.content_id = ANY(%s)
            """,
            (content_ids,),
        )
        rows = cur.fetchall()

    by_content: dict = {}
    for content_id, name, source_type, contour_id in rows:
        entry = by_content.setdefault(content_id, {"sources": [], "occurrence_count": 0})
        entry["occurrence_count"] += 1
        src = {"name": name, "source_type": source_type, "contour_id": contour_id}
        if src not in entry["sources"]:
            entry["sources"].append(src)
    return by_content


def search_content(conn, model, query: str, limit: int) -> list[dict]:
    query_vec = encode_query(model, query)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.content_id, 1 - (e.embedding <=> %s) AS similarity,
                   ci.title, ci.text_content, ci.first_seen_at
            FROM embeddings e
            JOIN content_items ci ON ci.content_id = e.content_id
            WHERE e.embedding_model_id = %s
            ORDER BY e.embedding <=> %s, e.content_id
            LIMIT %s
            """,
            (query_vec, EMBEDDING_MODEL_ID, query_vec, limit),
        )
        rows = cur.fetchall()

    content_ids = [row[0] for row in rows]
    meta = fetch_content_sources(conn, content_ids)

    results = []
    for content_id, similarity, title, text_content, first_seen_at in rows:
        cm = meta.get(content_id, {"sources": [], "occurrence_count": 0})
        results.append(
            {
                "content_id": str(content_id),
                "similarity": float(similarity),
                "title": title,
                "snippet": preview(text_content),
                "first_observed_at": first_seen_at.isoformat(),
                "sources": cm["sources"],
                "occurrence_count": cm["occurrence_count"],
            }
        )
    return results


def fetch_canonical_event(conn, claim_id) -> dict | None:
    """claim_id -> {canonical_event_id, canonical_event_summary} якщо claim
    дійшов до event pipeline через якийсь accepted_seed verification, інакше
    None (нормальний, очікуваний результат -- не помилка)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ce.canonical_event_id, ce.canonical_event_summary
            FROM event_verification_members evm
            JOIN event_verifications ev ON ev.verification_id = evm.verification_id
            JOIN canonical_event_members cem ON cem.event_candidate_id = ev.event_candidate_id
            JOIN canonical_events ce ON ce.canonical_event_id = cem.canonical_event_id
            WHERE evm.claim_id = %s AND evm.included = true
              AND ev.status = 'valid' AND ev.event_decision = 'accepted_seed'
            ORDER BY ce.created_at
            LIMIT 1
            """,
            (claim_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"canonical_event_id": str(row[0]), "canonical_event_summary": row[1]}


def search_claims(conn, model, query: str, limit: int) -> list[dict]:
    query_vec = encode_query(model, query)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.claim_id, 1 - (ce.embedding <=> %s) AS similarity,
                   c.claim_text, c.epistemic_status, c.evidence_span, r.content_id
            FROM claim_embeddings ce
            JOIN claims c ON c.claim_id = ce.claim_id
            JOIN claim_extraction_runs r ON r.run_id = c.run_id
            WHERE ce.embedding_model_id = %s
            ORDER BY ce.embedding <=> %s, c.claim_id
            LIMIT %s
            """,
            (query_vec, EMBEDDING_MODEL_ID, query_vec, limit),
        )
        rows = cur.fetchall()

    content_ids = list({row[5] for row in rows})
    content_meta: dict = {}
    if content_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content_id, title, text_content, first_seen_at "
                "FROM content_items WHERE content_id = ANY(%s)",
                (content_ids,),
            )
            for content_id, title, text_content, first_seen_at in cur.fetchall():
                content_meta[content_id] = {
                    "title": title,
                    "snippet": preview(text_content),
                    "first_observed_at": first_seen_at.isoformat(),
                }
    source_meta = fetch_content_sources(conn, content_ids)

    results = []
    for claim_id, similarity, claim_text, epistemic_status, evidence_span, content_id in rows:
        cm = content_meta.get(content_id, {})
        sm = source_meta.get(content_id, {"sources": []})
        canonical = fetch_canonical_event(conn, claim_id)
        results.append(
            {
                "claim_id": str(claim_id),
                "similarity": float(similarity),
                "claim_text": claim_text,
                "epistemic_status": epistemic_status,
                "evidence_span": evidence_span,
                "parent_content": {
                    "content_id": str(content_id),
                    "title": cm.get("title"),
                    "snippet": cm.get("snippet"),
                },
                "sources": sm["sources"],
                "observed_at": cm.get("first_observed_at"),
                "canonical_event": canonical,
            }
        )
    return results


def print_text(query: str, scope: str, content_results, claim_results) -> None:
    print(f'Query: "{query}"  scope={scope}')
    print()

    if content_results is not None:
        print(f"=== CONTENT RESULTS ({len(content_results)}) ===")
        for i, r in enumerate(content_results, 1):
            print(f"[{i}] similarity={r['similarity']:.4f}  content_id={r['content_id']}")
            print(f"    title: {r['title'] or '(no title)'}")
            print(f"    {r['snippet']}")
            print(f"    first_observed_at={r['first_observed_at']}  occurrence_count={r['occurrence_count']}")
            src_str = ", ".join(f"{s['name']} (contour={s['contour_id']})" for s in r["sources"])
            print(f"    sources: {src_str or '(none)'}")
            print()

    if claim_results is not None:
        print(f"=== CLAIM RESULTS ({len(claim_results)}) ===")
        for i, r in enumerate(claim_results, 1):
            print(
                f"[{i}] similarity={r['similarity']:.4f}  claim_id={r['claim_id']}  "
                f"epistemic_status={r['epistemic_status']}"
            )
            print(f"    {r['claim_text']}")
            print(f"    evidence_span: {preview(r['evidence_span'], EVIDENCE_PREVIEW_LEN)}")
            pc = r["parent_content"]
            print(f"    parent: {pc['title'] or '(no title)'} -- {pc['snippet']}")
            src_str = ", ".join(f"{s['name']} (contour={s['contour_id']})" for s in r["sources"])
            print(f"    sources: {src_str or '(none)'}  observed_at={r['observed_at']}")
            if r["canonical_event"]:
                ce = r["canonical_event"]
                print(f"    canonical_event: {ce['canonical_event_id']} -- {ce['canonical_event_summary']}")
            else:
                print("    canonical_event: (none)")
            print()


def main() -> int:
    parser = argparse.ArgumentParser(description="MIP Semantic Search v1 (read-only)")
    parser.add_argument("query", type=str, help="пошуковий запит (укр/рос/eng)")
    parser.add_argument("--scope", choices=["content", "claims", "all"], default="all")
    parser.add_argument("--limit", type=int, required=True, help="top-K на кожен scope")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    if args.limit <= 0:
        parser.error("--limit must be > 0")
    if not args.query.strip():
        parser.error("query must not be empty")

    code_revision = get_code_revision()

    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)
        model = load_model()

        content_results = None
        claim_results = None
        if args.scope in ("content", "all"):
            content_results = search_content(conn, model, args.query, args.limit)
        if args.scope in ("claims", "all"):
            claim_results = search_claims(conn, model, args.query, args.limit)

    if args.format == "json":
        output = {
            "query": args.query,
            "scope": args.scope,
            "limit": args.limit,
            "embedding_model": {
                "model_name": MODEL_NAME,
                "model_revision": MODEL_REVISION,
                "framework": FRAMEWORK,
                "framework_version": FRAMEWORK_VERSION,
            },
            "code_revision": code_revision,
            "content_results": content_results,
            "claim_results": claim_results,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print_text(args.query, args.scope, content_results, claim_results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
