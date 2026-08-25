#!/usr/bin/env python3
"""
experiments/content_routing/routing_v2_noise_leakage_audit.py -- Relevance Gate v2
calibration, phase 2: NOISE LEAKAGE audit. READ-ONLY. Does NOT write to
content_routing_decisions, does NOT touch retention/deletion, does NOT modify
routing_version=1 or its prototypes (experiments/content_routing/prototypes_v1.json
is only READ here).

Мета (за ТЗ користувача): фаза 1 (routing_v2_calibration_audit.py) перевірила
false negatives -- чи не губить routing=skip реальний контент контурів 1-4.
Ця фаза перевіряє протилежне: false positives -- скільки нерелевантного
побутового/загальноновинного шуму зараз проходить у decision=maybe/analyze,
тобто доходить до дорогого claim extraction.

Діагностичний generic_irrelevant_score рахується як max-cosine до нового,
розширеного 14-категорійного noise prototype set
(experiments/content_routing/irrelevant_prototypes_v2.json), ОКРЕМО від
малого "irrelevant" списку в CR v1 prototypes_v1.json (10 прикладів), який
використовується лише для relevance_margin recompute/cross-check.

Для кожного content_id рахується:
  relevance_score     -- max cos до CR v1 "relevant" списку (10 прикладів)
  relevance_irrel_v1   -- max cos до CR v1 "irrelevant" списку (10 прикладів,
                           той самий баланс, що вже застосовано в routing v1)
  relevance_margin     -- relevance_score - relevance_irrel_v1
                           (crosscheck: має збігатись з БД-вим routing_score,
                           бо routing v1 рахує margin так само)
  generic_irrelevant_score -- max cos до ВСІХ 34 прикладів 14-категорійного
                           noise-набору v2 (ширший, "побутовий шум" baseline)
  best_noise_category   -- яка з 14 категорій дала максимум (argmax)
  contour scores x4      -- dshv_objects/world_context/national_context/
                           enemy_media (experiments/content_contours/
                           prototypes_v1.json) -- лише для п.7 (conflict cases)
  routing_decision, routing_score -- з content_routing_decisions
                           (LEFT JOIN routing_version=1, як у phase 1)

Усі "high"-пороги в цьому скрипті -- EXPLORATORY p75/tercile split,
рахуються заново з живого корпусу, друкуються явно і НЕ є рішенням
про майбутній threshold (п.8 ТЗ: жодного threshold поки не обирати).

Виводить 7 секцій (п.1-7 ТЗ), плюс явна відмова обирати threshold (п.8):
  1. розподіл generic_irrelevant_score по skip/maybe/analyze
  2. top-50 за generic_irrelevant_score всередині maybe
  3. top-30 за generic_irrelevant_score всередині analyze
  4. випадкові по 30 з maybe у кількох score bands (tercile split всередині maybe)
  5. generic_irrelevant_score high, але routing=analyze
  6. generic_irrelevant_score high І routing=maybe
  7. конфлікти: high contour score (будь-який з 4) І high generic_irrelevant_score
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

ROUTING_VERSION = 1
ROUTING_PROTOTYPES_FILE = Path(__file__).resolve().parent / "prototypes_v1.json"
IRRELEVANT_V2_FILE = Path(__file__).resolve().parent / "irrelevant_prototypes_v2.json"
CONTOUR_PROTOTYPES_FILE = Path(__file__).resolve().parents[1] / "content_contours" / "prototypes_v1.json"

SNIPPET_LEN = 200
TOP_MAYBE_N = 50
TOP_ANALYZE_N = 30
BAND_SAMPLE_N = 30
CONFLICT_PRINT_N = 40
RANDOM_SEED = 42


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


def load_routing_prototype_vectors(model: SentenceTransformer) -> dict[str, np.ndarray]:
    """CR v1 prototypes_v1.json -- очікує ключі 'relevant' та 'irrelevant'."""
    data = json.loads(ROUTING_PROTOTYPES_FILE.read_text(encoding="utf-8"))
    if "relevant" not in data or "irrelevant" not in data:
        raise RuntimeError(
            f"{ROUTING_PROTOTYPES_FILE} має ключі {list(data.keys())}, "
            "очікувались 'relevant' та 'irrelevant'. Перевір формат вручну перед запуском."
        )
    return {code: np.asarray(model.encode(texts, normalize_embeddings=True)) for code, texts in data.items()}


def load_irrelevant_v2_vectors(model: SentenceTransformer) -> tuple[np.ndarray, list[str]]:
    """Плоский масив усіх прикладів 14 категорій + паралельний список назв
    категорій (для argmax -> best_noise_category)."""
    data = json.loads(IRRELEVANT_V2_FILE.read_text(encoding="utf-8"))
    texts: list[str] = []
    categories: list[str] = []
    for category, examples in data.items():
        for ex in examples:
            texts.append(ex)
            categories.append(category)
    vectors = np.asarray(model.encode(texts, normalize_embeddings=True))
    return vectors, categories


def load_contour_prototype_vectors(model: SentenceTransformer) -> dict[str, np.ndarray]:
    data = json.loads(CONTOUR_PROTOTYPES_FILE.read_text(encoding="utf-8"))
    return {code: np.asarray(model.encode(texts, normalize_embeddings=True)) for code, texts in data.items()}


def fetch_corpus_with_routing(conn) -> list[tuple]:
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


def print_dist(label: str, vals: list[float]) -> None:
    if not vals:
        print(f"  {label}: n=0")
        return
    s = sorted(vals)
    print(
        f"  {label}: n={len(s)} min={s[0]:.3f} p25={percentile(s, 0.25):.3f} "
        f"p50={percentile(s, 0.50):.3f} p75={percentile(s, 0.75):.3f} "
        f"p90={percentile(s, 0.90):.3f} max={s[-1]:.3f}"
    )


def fmt_row(r: dict, extra: str = "") -> str:
    rs = f"{r['routing_score']:.3f}" if r["routing_score"] is not None else "n/a"
    return (
        f"[{r['content_id']}] decision={r['routing_decision']} routing_score={rs} "
        f"generic_irrelevant={r['generic_irrelevant_score']:.3f} (best={r['best_noise_category']}) "
        f"relevance_score={r['relevance_score']:.3f} relevance_margin={r['relevance_margin']:.3f}"
        f"{extra} title={r['title'] or '(no title)'}"
    )


def run() -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)
        print("loading BGE-M3 model...")
        model = load_model()

        routing_vecs = load_routing_prototype_vectors(model)
        print(f"CR v1 prototypes: relevant={len(routing_vecs['relevant'])} irrelevant={len(routing_vecs['irrelevant'])}")

        irrelevant_v2_vecs, irrelevant_v2_categories = load_irrelevant_v2_vectors(model)
        print(f"irrelevant_prototypes_v2: {len(irrelevant_v2_vecs)} прикладів у 14 категоріях")

        contour_vecs = load_contour_prototype_vectors(model)
        print(f"content_contours prototypes: {[(k, len(v)) for k, v in contour_vecs.items()]}")

        print(f"\nfetching full corpus with routing_version={ROUTING_VERSION} decisions (LEFT JOIN)...")
        rows = fetch_corpus_with_routing(conn)
        print(f"corpus size: {len(rows)}")

        results = []
        for content_id, title, text_content, embedding, routing_decision, routing_score in rows:
            vec = embedding.to_numpy().astype(np.float32)

            relevance_score = float((vec @ routing_vecs["relevant"].T).max())
            relevance_irrel_v1 = float((vec @ routing_vecs["irrelevant"].T).max())
            relevance_margin = relevance_score - relevance_irrel_v1

            sims_v2 = vec @ irrelevant_v2_vecs.T
            best_idx = int(sims_v2.argmax())
            generic_irrelevant_score = float(sims_v2[best_idx])
            best_noise_category = irrelevant_v2_categories[best_idx]

            contour_scores = {code: float((vec @ pv.T).max()) for code, pv in contour_vecs.items()}

            results.append(
                {
                    "content_id": content_id,
                    "title": title,
                    "text_content": text_content,
                    "relevance_score": relevance_score,
                    "relevance_irrel_v1": relevance_irrel_v1,
                    "relevance_margin": relevance_margin,
                    "generic_irrelevant_score": generic_irrelevant_score,
                    "best_noise_category": best_noise_category,
                    "contour_scores": contour_scores,
                    "routing_decision": routing_decision if routing_decision is not None else "NO_ROUTING_DECISION",
                    "routing_score": routing_score,
                }
            )

        # crosscheck: relevance_margin (recomputed) vs routing_score (stored) -- мають збігатись
        # для рядків, де routing_score не NULL (routing v1 рахує margin так само).
        deltas = [
            abs(r["relevance_margin"] - r["routing_score"])
            for r in results
            if r["routing_score"] is not None
        ]
        if deltas:
            print(
                f"\ncrosscheck relevance_margin (recomputed) vs routing_score (DB): "
                f"n={len(deltas)} mean_abs_delta={sum(deltas) / len(deltas):.5f} max_abs_delta={max(deltas):.5f}"
            )
            if max(deltas) > 0.01:
                print(
                    "  УВАГА: розбіжність > 0.01 -- можлива різниця в prototype-наборі/моделі між "
                    "цим скриптом і routing_worker.py. Перевір вручну перед подальшими висновками."
                )

        by_decision: dict[str, list[dict]] = {}
        for r in results:
            by_decision.setdefault(r["routing_decision"], []).append(r)

        print("\n=== ROUTING DECISION BREAKDOWN (routing_version=1) ===")
        for decision, subset in sorted(by_decision.items(), key=lambda kv: -len(kv[1])):
            print(f"{decision}: {len(subset)} ({100 * len(subset) / len(results):.1f}%)")

        # EXPLORATORY high threshold для generic_irrelevant_score, повний корпус
        all_irrel_sorted = sorted(r["generic_irrelevant_score"] for r in results)
        irrel_high_cut = percentile(all_irrel_sorted, 0.75)
        print(
            f"\nEXPLORATORY generic_irrelevant_score high threshold (p75, full corpus): "
            f"{irrel_high_cut:.3f} -- діагностика, НЕ рішення про майбутній threshold"
        )

        # EXPLORATORY high thresholds для 4 контурів, повний корпус (для п.7)
        contour_high_cut = {}
        for code in contour_vecs:
            vals = sorted(r["contour_scores"][code] for r in results)
            contour_high_cut[code] = percentile(vals, 0.75)
        print(
            "EXPLORATORY contour high thresholds (p75, full corpus): "
            f"{{{', '.join(f'{k}={v:.3f}' for k, v in contour_high_cut.items())}}}"
        )

        # --- п.1: розподіл generic_irrelevant_score по skip/maybe/analyze ---
        print("\n\n=== 1. РОЗПОДІЛ generic_irrelevant_score ПО ROUTING DECISION ===")
        for decision in ["skip", "maybe", "analyze", "NO_ROUTING_DECISION"]:
            subset = by_decision.get(decision, [])
            print_dist(decision, [r["generic_irrelevant_score"] for r in subset])

        maybe = by_decision.get("maybe", [])
        analyze = by_decision.get("analyze", [])

        # --- п.2: top-50 за generic_irrelevant_score всередині maybe ---
        print(f"\n\n=== 2. TOP-{TOP_MAYBE_N} за generic_irrelevant_score ВСЕРЕДИНІ maybe ===")
        print(f"(maybe total n={len(maybe)})")
        top_maybe = sorted(maybe, key=lambda r: r["generic_irrelevant_score"], reverse=True)[:TOP_MAYBE_N]
        for r in top_maybe:
            print(fmt_row(r))
            print(f"    {preview(r['text_content'])}")

        # --- п.3: top-30 за generic_irrelevant_score всередині analyze ---
        print(f"\n\n=== 3. TOP-{TOP_ANALYZE_N} за generic_irrelevant_score ВСЕРЕДИНІ analyze ===")
        print(f"(analyze total n={len(analyze)})")
        top_analyze = sorted(analyze, key=lambda r: r["generic_irrelevant_score"], reverse=True)[:TOP_ANALYZE_N]
        for r in top_analyze:
            print(fmt_row(r))
            print(f"    {preview(r['text_content'])}")

        # --- п.4: випадкові по 30 з maybe у кількох score bands (tercile split всередині maybe) ---
        print(f"\n\n=== 4. ВИПАДКОВІ ПО {BAND_SAMPLE_N} З maybe, TERCILE BANDS (за generic_irrelevant_score всередині maybe) ===")
        rng = random.Random(RANDOM_SEED)
        if maybe:
            maybe_irrel_sorted = sorted(r["generic_irrelevant_score"] for r in maybe)
            t1 = percentile(maybe_irrel_sorted, 0.333)
            t2 = percentile(maybe_irrel_sorted, 0.667)
            bands = [
                ("LOW", [r for r in maybe if r["generic_irrelevant_score"] < t1]),
                ("MID", [r for r in maybe if t1 <= r["generic_irrelevant_score"] < t2]),
                ("HIGH", [r for r in maybe if r["generic_irrelevant_score"] >= t2]),
            ]
            print(f"(tercile cuts всередині maybe: t1={t1:.3f} t2={t2:.3f}, EXPLORATORY, не рішення)")
            for band_name, band_items in bands:
                sample = rng.sample(band_items, min(BAND_SAMPLE_N, len(band_items)))
                print(f"\n--- band {band_name}: n={len(band_items)}, showing random {len(sample)} (seed={RANDOM_SEED}) ---")
                for r in sample:
                    print(fmt_row(r))
                    print(f"    {preview(r['text_content'])}")
        else:
            print("maybe порожній -- немає що семплувати.")

        # --- п.5: generic_irrelevant_score high, але routing=analyze ---
        print(f"\n\n=== 5. generic_irrelevant_score HIGH (>= {irrel_high_cut:.3f}), АЛЕ routing=analyze ===")
        high_in_analyze = sorted(
            (r for r in analyze if r["generic_irrelevant_score"] >= irrel_high_cut),
            key=lambda r: r["generic_irrelevant_score"],
            reverse=True,
        )
        print(f"total: {len(high_in_analyze)} з {len(analyze)} у analyze")
        for r in high_in_analyze:
            print(fmt_row(r))
            print(f"    {preview(r['text_content'])}")

        # --- п.6: generic_irrelevant_score high І routing=maybe ---
        print(f"\n\n=== 6. generic_irrelevant_score HIGH (>= {irrel_high_cut:.3f}) І routing=maybe ===")
        high_in_maybe = sorted(
            (r for r in maybe if r["generic_irrelevant_score"] >= irrel_high_cut),
            key=lambda r: r["generic_irrelevant_score"],
            reverse=True,
        )
        print(f"total: {len(high_in_maybe)} з {len(maybe)} у maybe")
        for r in high_in_maybe:
            print(fmt_row(r))
            print(f"    {preview(r['text_content'])}")

        # --- п.7: конфлікти -- high contour score (будь-який з 4) І high generic_irrelevant_score ---
        print(
            "\n\n=== 7. КОНФЛІКТИ: high contour score (будь-який з 4, p75) "
            f"І high generic_irrelevant_score (>= {irrel_high_cut:.3f}) ==="
        )
        print("(dshv_objects включено, але його prototype-набір відомо зашумлений -- враховуй це при читанні)")
        conflicts = []
        for r in results:
            triggered = [code for code, cut in contour_high_cut.items() if r["contour_scores"][code] >= cut]
            if triggered and r["generic_irrelevant_score"] >= irrel_high_cut:
                conflicts.append((r, triggered))
        conflicts.sort(key=lambda t: t[0]["generic_irrelevant_score"], reverse=True)
        print(f"total conflicts: {len(conflicts)} (showing top {CONFLICT_PRINT_N})")
        for r, triggered in conflicts[:CONFLICT_PRINT_N]:
            contour_str = ", ".join(f"{c}={r['contour_scores'][c]:.3f}" for c in triggered)
            print(fmt_row(r, extra=f" contours=[{contour_str}]"))
            print(f"    {preview(r['text_content'])}")

        # --- п.8: явно НЕ обираємо жодного порогу рішення ---
        print(
            "\n\n=== 8. ПОРІГ РІШЕННЯ ===\n"
            "Жоден threshold для generic_irrelevant_score НЕ обирається в цьому скрипті. "
            "Усі 'high' пороги вище -- EXPLORATORY p75/tercile, лише для сортування виводу "
            "й ручного review. Рішення по Routing v2 -- окремий крок після ручного аналізу "
            "цього виводу."
        )

        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Relevance Gate v2, phase 2 -- noise leakage audit (read-only)")
    parser.parse_args()
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
