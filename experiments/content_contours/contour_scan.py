#!/usr/bin/env python3
"""
experiments/content_contours/contour_scan.py -- Content Contours v1, READ-ONLY
calibration scan. НЕ пише в content_contour_assignments (таблиця не обов'язкова
на момент прогону -- залежність лише від content_items + embeddings +
item_occurrences + sources).

Score (max-cosine-similarity content-вектора до prototype-набору контуру)
рахується по ВСЬОМУ поточному корпусу з BGE-M3 embedding, не по random
вибірці -- розподіл і TOP-15 мають відображати реальний корпус, а не
випадкову підмножину. Random sample використовується ОКРЕМО, лише як частина
ручного review (додаткова перевірка на несистемних прикладах, не тільки
top-scored).

top1-top2 < 0.03 -- ДІАГНОСТИКА, не майбутнє правило: max-cosine між
prototype-наборами різного розміру не гарантовано напряму зіставний
(dshv_objects/national_context/enemy_media мають різну кількість прототипів).

has_source_group_4 -- діагностичний provenance-ознака per content, порахована
через item_occurrences -> sources із ЛЕГАСІ sources.contour_id = 4. Це НЕ
результат Content Contour Classification і не використовується як
substitute для semantic score -- лише для порівняння semantic vs. legacy
джерело-походження на контурі 4 (enemy_media), де ТЗ явно каже, що
походження джерела може бути частиною семантики самого контуру.

Порогів include/maybe/exclude тут НЕМАЄ -- вони обираються ПІСЛЯ ручного
аудиту результатів цього скану. DDL (monitoring_contours/
content_contour_assignments) НЕ застосовується цим скриптом і не потрібна
для його роботи.
"""
from __future__ import annotations

import argparse
import json
import random
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
SNIPPET_LEN = 200
ENEMY_MEDIA_CODE = "enemy_media"
SOURCE_GROUP_4_LEGACY_CONTOUR_ID = 4


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
    data = json.loads(PROTOTYPES_FILE.read_text(encoding="utf-8"))
    return {code: np.asarray(model.encode(texts, normalize_embeddings=True)) for code, texts in data.items()}


def fetch_full_corpus(conn) -> list[tuple]:
    """Весь content_items з BGE-M3 embedding. Без фільтра по
    content_routing_decisions (ASSUMPTION з попереднього кроку, прийнято)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ci.content_id, ci.title, ci.text_content, e.embedding
            FROM content_items ci
            JOIN embeddings e ON e.content_id = ci.content_id AND e.embedding_model_id = %s
            ORDER BY ci.content_id
            """,
            (EMBEDDING_MODEL_ID,),
        )
        return cur.fetchall()


def fetch_source_group_4(conn) -> set:
    """content_id, які мають хоча б одне occurrence через джерело з легасі
    sources.contour_id = 4. ЧИСТО діагностика, не пишеться нікуди."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT io.content_id
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            WHERE s.contour_id = %s
            """,
            (SOURCE_GROUP_4_LEGACY_CONTOUR_ID,),
        )
        return {row[0] for row in cur.fetchall()}


def preview(text: str, length: int = SNIPPET_LEN) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[:length].rstrip() + "…"


def percentile(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    return sorted_vals[int(p * (n - 1))]


def run(sample_size: int, seed: int) -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)
        print("loading BGE-M3 model...")
        model = load_model()
        prototype_vectors = load_prototype_vectors(model)
        print(f"prototype sets: {[(k, len(v)) for k, v in prototype_vectors.items()]}")

        print("fetching full corpus (content_items x embeddings, model_id=1)...")
        corpus = fetch_full_corpus(conn)
        print(f"corpus size: {len(corpus)}")

        source_group_4 = fetch_source_group_4(conn)
        print(f"content_ids with >=1 occurrence via legacy sources.contour_id=4: {len(source_group_4)}")

        results = []
        for content_id, title, text_content, embedding in corpus:
            vec = embedding.to_numpy().astype(np.float32)
            scores = {code: float((vec @ proto_vecs.T).max()) for code, proto_vecs in prototype_vectors.items()}
            results.append(
                {
                    "content_id": content_id,
                    "title": title,
                    "text_content": text_content,
                    "scores": scores,
                    "has_source_group_4": content_id in source_group_4,
                }
            )

        # 1) Розподіл score по контурах -- по всьому корпусу.
        print("\n=== SCORE DISTRIBUTION (per contour, full corpus) ===")
        contour_sorted_vals = {}
        for code in prototype_vectors:
            vals = sorted(r["scores"][code] for r in results)
            contour_sorted_vals[code] = vals
            print(
                f"{code}: n={len(vals)} min={vals[0]:.3f} p25={percentile(vals, 0.25):.3f} "
                f"median={percentile(vals, 0.5):.3f} p75={percentile(vals, 0.75):.3f} "
                f"p90={percentile(vals, 0.90):.3f} max={vals[-1]:.3f}"
            )

        # 2) TOP-15 по кожному контуру -- з усього корпусу.
        for code in prototype_vectors:
            print(f"\n=== TOP-15 for {code} (full corpus) ===")
            top = sorted(results, key=lambda r: r["scores"][code], reverse=True)[:15]
            for r in top:
                flag = " [source_group_4]" if r["has_source_group_4"] else ""
                print(f"[{r['content_id']}] score={r['scores'][code]:.3f}{flag} title={r['title'] or '(no title)'}")
                print(f"    {preview(r['text_content'])}")

        # 3) ДІАГНОСТИКА (не правило): top1-top2 < 0.03.
        print(
            "\n=== DIAGNOSTIC ONLY: top1-top2 < 0.03 ===\n"
            "НЕ є майбутнім decision rule -- prototype-набори різного розміру, "
            "max-cosine не гарантовано напряму зіставний між контурами."
        )
        amb_count = 0
        for r in results:
            ranked = sorted(r["scores"].items(), key=lambda kv: kv[1], reverse=True)
            if ranked[0][1] - ranked[1][1] < 0.03:
                amb_count += 1
        print(f"count: {amb_count}/{len(results)}")
        print("приклади (перші 15):")
        shown = 0
        for r in results:
            ranked = sorted(r["scores"].items(), key=lambda kv: kv[1], reverse=True)
            if ranked[0][1] - ranked[1][1] < 0.03:
                flag = " [source_group_4]" if r["has_source_group_4"] else ""
                print(
                    f"[{r['content_id']}]{flag} {ranked[0][0]}={ranked[0][1]:.3f} vs "
                    f"{ranked[1][0]}={ranked[1][1]:.3f} title={r['title'] or '(no title)'}"
                )
                print(f"    {preview(r['text_content'])}")
                shown += 1
                if shown >= 15:
                    break

        # 4) Контур 4 (enemy_media): semantic vs. source-group-4 provenance breakdown.
        # EXPLORATORY split (не threshold-рішення): high = score >= p75, low = score < median,
        # пороги взяті з розподілу самого enemy_media на цьому корпусі, лише для цього звіту.
        em_vals = contour_sorted_vals[ENEMY_MEDIA_CODE]
        high_cut = percentile(em_vals, 0.75)
        low_cut = percentile(em_vals, 0.5)
        print(
            f"\n=== {ENEMY_MEDIA_CODE}: semantic vs. source_group_4 breakdown "
            f"(EXPLORATORY split: high >= p75={high_cut:.3f}, low < median={low_cut:.3f}) ==="
        )
        groups = {
            "semantic_high_AND_source_group_4": [],
            "semantic_high_AND_NOT_source_group_4": [],
            "semantic_low_AND_source_group_4": [],
        }
        for r in results:
            score = r["scores"][ENEMY_MEDIA_CODE]
            if score >= high_cut and r["has_source_group_4"]:
                groups["semantic_high_AND_source_group_4"].append(r)
            elif score >= high_cut and not r["has_source_group_4"]:
                groups["semantic_high_AND_NOT_source_group_4"].append(r)
            elif score < low_cut and r["has_source_group_4"]:
                groups["semantic_low_AND_source_group_4"].append(r)

        for group_name, items in groups.items():
            print(f"\n-- {group_name}: {len(items)} --")
            for r in sorted(items, key=lambda r: r["scores"][ENEMY_MEDIA_CODE], reverse=True)[:10]:
                print(f"[{r['content_id']}] score={r['scores'][ENEMY_MEDIA_CODE]:.3f} title={r['title'] or '(no title)'}")
                print(f"    {preview(r['text_content'])}")

        # 5) Random sample -- ОКРЕМА частина ручного review, не для score distribution/top-15.
        print(f"\n=== RANDOM SAMPLE for manual review (n={sample_size}, seed={seed}) ===")
        sample = list(results)
        random.Random(seed).shuffle(sample)
        sample = sample[:sample_size]
        for r in sample:
            flag = " [source_group_4]" if r["has_source_group_4"] else ""
            score_str = " ".join(f"{code}={r['scores'][code]:.3f}" for code in prototype_vectors)
            print(f"[{r['content_id']}]{flag} {score_str} title={r['title'] or '(no title)'}")
            print(f"    {preview(r['text_content'])}")

        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Content Contours v1 -- read-only calibration scan")
    parser.add_argument("--sample-size", type=int, default=150, help="розмір random sample для ручного review")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    return run(args.sample_size, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
