#!/usr/bin/env python3
"""
experiments/content_routing/routing_v2_calibration_audit.py -- Relevance Gate v2
calibration audit. READ-ONLY, does NOT change content_routing_decisions or
any retention/deletion state.

Мета (за ТЗ користувача): перевірити, чи Content Routing v1 (routing_version=1)
не губить реальний контент контурів 1-4 (ДШВ/world/national/enemy_media) в
decision='skip', і чи потрібно розширювати/уточнювати irrelevant baseline
(experiments/content_routing/prototypes_v1.json) так, щоб побутовий шум
(гороскопи, рецепти, шоу-бізнес, спорт поза контекстом, погода, побутові
поради, тварини, lifestyle, звичайна кримінальна хроніка, нерелевантні
внутрішні новини РФ) стабільно йшов у skip.

Використовує ІСНУЮЧИЙ Content Routing v1 як baseline (LEFT JOIN
content_routing_decisions WHERE routing_version=1) -- НЕ створює паралельний
незалежний класифікатор. Контурні score (dshv_objects/world_context/
national_context/enemy_media, prototypes experiments/content_contours/
prototypes_v1.json) використовуються ТІЛЬКИ як діагностичний сигнал для
пошуку leakage-кейсів (routing=skip, але контур виглядає релевантним),
не як заміна routing.

Показує: routing decision x contour (high/low, EXPLORATORY p75-split,
той самий підхід що experiments/content_contours/contour_scan.py) -- повна
crosstab, потім детальний лістинг leakage-кейсів:
  routing=skip AND enemy_media high
  routing=skip AND national_context high
  routing=skip AND world_context high
(dshv_objects навмисно виключено -- відомо, що поточний prototype-набір
для нього зашумлений, окрема задача).
"""
from __future__ import annotations

import argparse
import json
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

ROUTING_VERSION = 1
CONTOUR_PROTOTYPES_FILE = Path(__file__).resolve().parents[1] / "content_contours" / "prototypes_v1.json"
SNIPPET_LEN = 200
LEAKAGE_CONTOURS = ["enemy_media", "national_context", "world_context"]
TOP_N_PER_LEAKAGE = 30


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
        raise RuntimeError(f"embedding_model_id={EMBEDDING_MODEL_ID} не зареєстровано.")
    expected = (MODEL_NAME, MODEL_REVISION, FRAMEWORK, FRAMEWORK_VERSION, DIMENSION, METRIC, ENCODING_PARAMS)
    if tuple(row) != expected:
        raise RuntimeError(
            f"Конфігурація моделі в БД не збігається зі скриптом.\n  БД: {tuple(row)}\n  Скрипт: {expected}"
        )


def load_model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION, device="cpu", local_files_only=True)


def load_prototype_vectors(model: SentenceTransformer) -> dict[str, np.ndarray]:
    data = json.loads(CONTOUR_PROTOTYPES_FILE.read_text(encoding="utf-8"))
    return {code: np.asarray(model.encode(texts, normalize_embeddings=True)) for code, texts in data.items()}


def fetch_corpus_with_routing(conn) -> list[tuple]:
    """LEFT JOIN -- контент без рядка в content_routing_decisions (ще не
    оброблений routing backfill) позначається decision=None, окрема
    категорія в crosstab, не помилка."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ci.content_id, ci.title, ci.text_content, e.embedding,
                   r.decision, r.score
            FROM content_items ci
            JOIN embeddings e ON e.content_id = ci.content_id AND e.embedding_model_id = %s
            LEFT JOIN content_routing_decisions r
                ON r.content_id = ci.content_id AND r.routing_version = %s
            ORDER BY ci.content_id
            """,
            (EMBEDDING_MODEL_ID, ROUTING_VERSION),
        )
        return cur.fetchall()


def preview(text: str, length: int = SNIPPET_LEN) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[:length].rstrip() + "…"


def percentile(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    return sorted_vals[int(p * (n - 1))]


def run() -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)
        print("loading BGE-M3 model...")
        model = load_model()
        prototype_vectors = load_prototype_vectors(model)
        print(f"prototype sets: {[(k, len(v)) for k, v in prototype_vectors.items()]}")

        print(f"fetching full corpus with routing_version={ROUTING_VERSION} decisions (LEFT JOIN)...")
        rows = fetch_corpus_with_routing(conn)
        print(f"corpus size: {len(rows)}")

        results = []
        for content_id, title, text_content, embedding, routing_decision, routing_score in rows:
            vec = embedding.to_numpy().astype(np.float32)
            scores = {code: float((vec @ proto_vecs.T).max()) for code, proto_vecs in prototype_vectors.items()}
            results.append(
                {
                    "content_id": content_id,
                    "title": title,
                    "text_content": text_content,
                    "scores": scores,
                    "routing_decision": routing_decision if routing_decision is not None else "NO_ROUTING_DECISION",
                    "routing_score": routing_score,
                }
            )

        routing_counts = {}
        for r in results:
            routing_counts[r["routing_decision"]] = routing_counts.get(r["routing_decision"], 0) + 1
        print("\n=== ROUTING DECISION BREAKDOWN (routing_version=1) ===")
        for decision, count in sorted(routing_counts.items(), key=lambda kv: -kv[1]):
            print(f"{decision}: {count} ({100 * count / len(results):.1f}%)")

        high_cut = {}
        for code in prototype_vectors:
            vals = sorted(r["scores"][code] for r in results)
            high_cut[code] = percentile(vals, 0.75)
        print(
            "\nEXPLORATORY high thresholds (p75, full corpus): "
            f"{{{', '.join(f'{k}={v:.3f}' for k, v in high_cut.items())}}}"
        )

        print("\n=== CROSSTAB: routing_decision x contour (score >= p75 = high) ===")
        decisions_order = ["skip", "maybe", "analyze", "NO_ROUTING_DECISION"]
        for code in prototype_vectors:
            print(f"\n-- {code} (high >= {high_cut[code]:.3f}) --")
            for decision in decisions_order:
                subset = [r for r in results if r["routing_decision"] == decision]
                if not subset:
                    continue
                high = sum(1 for r in subset if r["scores"][code] >= high_cut[code])
                print(f"  {decision}: n={len(subset)} high={high} ({100 * high / len(subset):.1f}%)")

        print("\n=== LEAKAGE CASES: routing=skip AND contour score high ===")
        print("(dshv_objects навмисно виключено з цього аудиту -- prototype-набір відомо зашумлений)")
        for code in LEAKAGE_CONTOURS:
            leakage = [
                r for r in results
                if r["routing_decision"] == "skip" and r["scores"][code] >= high_cut[code]
            ]
            leakage.sort(key=lambda r: r["scores"][code], reverse=True)
            print(f"\n--- skip AND {code} high: {len(leakage)} total (showing top {TOP_N_PER_LEAKAGE}) ---")
            for r in leakage[:TOP_N_PER_LEAKAGE]:
                rs = f"{r['routing_score']:.3f}" if r["routing_score"] is not None else "n/a"
                print(
                    f"[{r['content_id']}] {code}={r['scores'][code]:.3f} routing_score={rs} "
                    f"title={r['title'] or '(no title)'}"
                )
                print(f"    {preview(r['text_content'])}")

        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Relevance Gate v2 -- routing x contour leakage audit (read-only)")
    parser.parse_args()
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
