#!/usr/bin/env python3
"""
experiments/content_routing/routing_v2_shadow_run.py -- Relevance Gate v2,
CONSOLIDATED design shadow-run. READ-ONLY. Does NOT write to
content_routing_decisions, does NOT touch retention/deletion, does NOT modify
routing_version=1 or experiments/content_routing/prototypes_v1.json (тільки
READ, для довідки не використовується -- v1 decision/score беруться з БД).

Мета (за ТЗ користувача, після phase 1 false-negative audit і phase 2
noise-leakage audit): зібрати ОДНУ узгоджену версію Routing v2:

  relevant_score_v2   -- max cos до prototypes_v2.json["relevant"] (15
                          прикладів: 12 з v1 + 3 нових за підтвердженими
                          дірками -- прогноз майбутніх ударів РФ, зимова/
                          енергетична стійкість України, аналітичні прогнози
                          розвитку воєнної ситуації)
  irrelevant_score_v2 -- max cos до prototypes_v2.json["irrelevant"] (10
                          прикладів: 9 з v1 без змін + 1 замінений --
                          "Прогноз погоди на найближчі дні" замінено на
                          "Погодні умови зараз: ...", щоб прибрати
                          перетин з зимово-воєнною лексикою, підтверджений
                          у phase 2 audit і в false-negative кейсі 69c81c45)
  margin_v2 = relevant_score_v2 - irrelevant_score_v2  -- той самий
                          production-патерн, що v1 (routing_worker.py:
                          score = max(relevant) - max(irrelevant))

  decision_v2 -- ПРОВІЗОРНО застосовується ТОЙ САМИЙ поріг, що вже живе в
                 проді v1 (routing_worker.py: T_SKIP=-0.07, T_ANALYZE=0.10),
                 щоб transition matrix v1->v2 показувала ЛИШЕ ефект зміни
                 prototype-наборів, а не одночасно ще й довільно обраного
                 нового порогу. Це НЕ вибір фінального Routing v2 threshold
                 (п.9 ТЗ: thresholds v2 обираються тільки після цього
                 shadow-run, окремим кроком).

  generic_irrelevant_score з phase 2 (irrelevant_prototypes_v2.json,
  14 категорій) тут НЕ використовується в decision logic -- за ТЗ (п.4)
  залишається діагностикою і в цьому скрипті взагалі не рахується.

Виводить:
  1. crosscheck: скільки рядків мають routing_version=1 decision (sanity)
  2. TRANSITION MATRIX v1 decision x v2 decision (повна 3x3 + NO_ROUTING_DECISION)
  3. skip_v1 -> maybe_v2   (усі, для перевірки що v2 витягує підтверджені FN)
  4. skip_v1 -> analyze_v2 (усі)
  5. analyze_v1 -> maybe_v2 (усі)
  6. analyze_v1 -> skip_v2  (усі -- найчутливіший напрямок, критично важливо
                             вручну перевірити кожен)
  7. maybe_v1 -> skip_v2   (випадкові 75 з N, seed=42 -- відповідає на
                             головне питання: скільки з 5811 maybe можна
                             безболісно викинути)
  8. явна перевірка 2 підтверджених false-negative кейсів
     (69c81c45-c166-4dcc-b65a-116f0f426810, 62e9fda4-bed0-490e-a753-323cfbc6c7f3)
  9. підсумкові числа + явна відмова обирати фінальний threshold
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

ROUTING_VERSION = 1  # читаємо v1 decision/score з БД для порівняння, не переписуємо
PROTOTYPES_V2_FILE = Path(__file__).resolve().parent / "prototypes_v2.json"

# ПРОВІЗОРНО -- ті самі пороги, що вже в проді v1 (routing_worker.py).
# НЕ фінальний вибір для Routing v2, лише щоб ізолювати ефект зміни
# prototype-наборів від ефекту зміни порогу (п.9 ТЗ).
T_SKIP = -0.07
T_ANALYZE = 0.10

FALSE_NEGATIVE_CASES = [
    ("69c81c45-c166-4dcc-b65a-116f0f426810", "прогноз ударів РФ восени/взимку"),
    ("62e9fda4-bed0-490e-a753-323cfbc6c7f3", "прогноз складної зими/енергодефіциту для України"),
]

SNIPPET_LEN = 200
MAYBE_TO_SKIP_SAMPLE_N = 75
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


def load_prototypes_v2_vectors(model: SentenceTransformer) -> dict[str, np.ndarray]:
    data = json.loads(PROTOTYPES_V2_FILE.read_text(encoding="utf-8"))
    if "relevant" not in data or "irrelevant" not in data:
        raise RuntimeError(
            f"{PROTOTYPES_V2_FILE} має ключі {list(data.keys())}, "
            "очікувались 'relevant' та 'irrelevant'. Перевір формат вручну перед запуском."
        )
    return {code: np.asarray(model.encode(texts, normalize_embeddings=True)) for code, texts in data.items()}


def decide(score: float) -> str:
    """Той самий провізорний поріг, що вже в проді v1 (routing_worker.py)."""
    if score >= T_ANALYZE:
        return "analyze"
    if score <= T_SKIP:
        return "skip"
    return "maybe"


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


def fmt_transition(r: dict) -> str:
    v1_score = f"{r['routing_score_v1']:.3f}" if r["routing_score_v1"] is not None else "n/a"
    return (
        f"[{r['content_id']}] v1={r['decision_v1']}(score={v1_score}) -> "
        f"v2={r['decision_v2']}(rel={r['relevant_score_v2']:.3f} irr={r['irrelevant_score_v2']:.3f} "
        f"margin={r['margin_v2']:.3f}) title={r['title'] or '(no title)'}"
    )


def print_group(label: str, rows: list[dict], limit: int | None = None) -> None:
    shown = rows if limit is None else rows[:limit]
    print(f"\n--- {label}: total={len(rows)}{f', showing {len(shown)}' if limit and len(rows) > limit else ''} ---")
    for r in shown:
        print(fmt_transition(r))
        print(f"    {preview(r['text_content'])}")


def run() -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)
        print("loading BGE-M3 model...")
        model = load_model()

        proto_v2 = load_prototypes_v2_vectors(model)
        print(
            f"prototypes_v2: relevant={len(proto_v2['relevant'])} "
            f"irrelevant={len(proto_v2['irrelevant'])}"
        )
        print(f"provisional thresholds (same as v1 prod): T_SKIP={T_SKIP} T_ANALYZE={T_ANALYZE}")

        print(f"\nfetching full corpus with routing_version={ROUTING_VERSION} decisions (LEFT JOIN)...")
        rows = fetch_corpus_with_routing(conn)
        print(f"corpus size: {len(rows)}")

        results = []
        for content_id, title, text_content, embedding, routing_decision, routing_score in rows:
            vec = embedding.to_numpy().astype(np.float32)

            relevant_score_v2 = float((vec @ proto_v2["relevant"].T).max())
            irrelevant_score_v2 = float((vec @ proto_v2["irrelevant"].T).max())
            margin_v2 = relevant_score_v2 - irrelevant_score_v2
            decision_v2 = decide(margin_v2)

            results.append(
                {
                    "content_id": content_id,
                    "title": title,
                    "text_content": text_content,
                    "decision_v1": routing_decision if routing_decision is not None else "NO_ROUTING_DECISION",
                    "routing_score_v1": routing_score,
                    "relevant_score_v2": relevant_score_v2,
                    "irrelevant_score_v2": irrelevant_score_v2,
                    "margin_v2": margin_v2,
                    "decision_v2": decision_v2,
                }
            )

        n_with_v1 = sum(1 for r in results if r["decision_v1"] != "NO_ROUTING_DECISION")
        print(f"\nrows з routing_version=1 decision: {n_with_v1} з {len(results)}")

        # --- TRANSITION MATRIX v1 x v2 ---
        v1_labels = ["skip", "maybe", "analyze", "NO_ROUTING_DECISION"]
        v2_labels = ["skip", "maybe", "analyze"]
        matrix: dict[str, dict[str, int]] = {v1: {v2: 0 for v2 in v2_labels} for v1 in v1_labels}
        for r in results:
            matrix[r["decision_v1"]][r["decision_v2"]] += 1

        print("\n\n=== TRANSITION MATRIX: v1 decision -> v2 decision (provisional threshold) ===")
        header = "v1 \\ v2".ljust(20) + "".join(v2.ljust(12) for v2 in v2_labels) + "total"
        print(header)
        for v1 in v1_labels:
            row_total = sum(matrix[v1].values())
            if row_total == 0:
                continue
            line = v1.ljust(20) + "".join(str(matrix[v1][v2]).ljust(12) for v2 in v2_labels) + str(row_total)
            print(line)

        by_pair: dict[tuple[str, str], list[dict]] = {}
        for r in results:
            by_pair.setdefault((r["decision_v1"], r["decision_v2"]), []).append(r)

        def pair(v1: str, v2: str) -> list[dict]:
            return sorted(by_pair.get((v1, v2), []), key=lambda r: r["margin_v2"], reverse=True)

        # --- п.3-6: явно перелічені напрямки з ТЗ (усі рядки) ---
        print("\n\n=== 3. skip_v1 -> maybe_v2 (усі) ===")
        print_group("skip -> maybe", pair("skip", "maybe"))

        print("\n\n=== 4. skip_v1 -> analyze_v2 (усі) ===")
        print_group("skip -> analyze", pair("skip", "analyze"))

        print("\n\n=== 5. analyze_v1 -> maybe_v2 (усі) ===")
        print_group("analyze -> maybe", pair("analyze", "maybe"))

        print("\n\n=== 6. analyze_v1 -> skip_v2 (усі -- найчутливіший напрямок) ===")
        print_group("analyze -> skip", pair("analyze", "skip"))

        # --- п.7: maybe_v1 -> skip_v2, випадкові 75 ---
        maybe_to_skip = pair("maybe", "skip")
        maybe_to_maybe = pair("maybe", "maybe")
        maybe_to_analyze = pair("maybe", "analyze")
        maybe_total = len(maybe_to_skip) + len(maybe_to_maybe) + len(maybe_to_analyze)

        print(f"\n\n=== 7. maybe_v1 -> skip_v2 (випадкові {MAYBE_TO_SKIP_SAMPLE_N}, seed={RANDOM_SEED}) ===")
        rng = random.Random(RANDOM_SEED)
        sample = rng.sample(maybe_to_skip, min(MAYBE_TO_SKIP_SAMPLE_N, len(maybe_to_skip)))
        print(f"total maybe -> skip: {len(maybe_to_skip)} з {maybe_total} maybe")
        for r in sample:
            print(fmt_transition(r))
            print(f"    {preview(r['text_content'])}")

        # довідково: інші maybe-напрямки, без повного друку
        print(
            f"\n(довідково: maybe_v1 total={maybe_total}, "
            f"-> skip={len(maybe_to_skip)} "
            f"-> maybe={len(maybe_to_maybe)} "
            f"-> analyze={len(maybe_to_analyze)})"
        )

        # --- п.7B: maybe_v1 -> analyze_v2, випадкові -- НАЙБІЛЬШИЙ перехід (661),
        # раніше пропущений у цьому скрипті. Це головний драйвер росту analyze-
        # бакета в v2, тому має бути перевірений вручну нарівні з іншими напрямками.
        print(
            f"\n\n=== 7B. maybe_v1 -> analyze_v2 (випадкові {MAYBE_TO_SKIP_SAMPLE_N}, "
            f"seed={RANDOM_SEED} -- НАЙБІЛЬШИЙ перехід, раніше не перевірявся) ==="
        )
        sample_to_analyze = rng.sample(maybe_to_analyze, min(MAYBE_TO_SKIP_SAMPLE_N, len(maybe_to_analyze)))
        print(f"total maybe -> analyze: {len(maybe_to_analyze)} з {maybe_total} maybe")
        for r in sample_to_analyze:
            print(fmt_transition(r))
            print(f"    {preview(r['text_content'])}")

        # --- п.8: підтверджені false-negative кейси ---
        print("\n\n=== 8. ПЕРЕВІРКА ПІДТВЕРДЖЕНИХ FALSE NEGATIVE КЕЙСІВ (Routing v1) ===")
        by_id = {str(r["content_id"]): r for r in results}
        for content_id, note in FALSE_NEGATIVE_CASES:
            r = by_id.get(content_id)
            if r is None:
                print(f"\n[{content_id}] ({note}) -- НЕ ЗНАЙДЕНО в корпусі, перевір content_id вручну")
                continue
            print(f"\n[{content_id}] ({note})")
            print(f"    {fmt_transition(r)}")
            print(f"    {preview(r['text_content'])}")
            fixed = r["decision_v1"] == "skip" and r["decision_v2"] != "skip"
            print(f"    v2 виправляє цей false negative: {'ТАК' if fixed else 'НІ -- потребує додаткової уваги'}")

        # --- п.9: підсумок + явна відмова від фінального порогу ---
        print(
            "\n\n=== 9. ПІДСУМОК ===\n"
            f"maybe_v1 (n={maybe_total}) -> skip_v2: {len(pair('maybe', 'skip'))} "
            f"({100 * len(pair('maybe', 'skip')) / maybe_total:.1f}% з maybe) -- "
            "кандидати на 'безболісно викинути', АЛЕ це число отримане ПРОВІЗОРНИМ порогом "
            "(той самий T_SKIP/T_ANALYZE, що в проді v1, застосований до НОВИХ "
            "prototype-наборів). Це НЕ остаточне число і НЕ рішення про поріг.\n"
            "analyze_v1 -> skip_v2 (найчутливіший напрямок, втрата вже класифікованого "
            f"релевантного): {len(pair('analyze', 'skip'))} -- перевір секцію 6 вручну, "
            "кожен рядок, перед будь-яким рішенням.\n"
            "Жоден фінальний threshold для Routing v2 в цьому скрипті НЕ обирається. "
            "Вибір -- окремий крок після ручного review секцій 3-8. "
            "routing_version=1 не змінено."
        )

        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Relevance Gate v2 -- consolidated design shadow-run (read-only)")
    parser.parse_args()
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
