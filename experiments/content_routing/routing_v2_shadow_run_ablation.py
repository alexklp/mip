#!/usr/bin/env python3
"""
experiments/content_routing/routing_v2_shadow_run_ablation.py -- ABLATION
read-only check: чи саме широкий relevant-прототип "Аналітичні прогнози
розвитку воєнної ситуації, можливі сценарії ескалації" був джерелом
off-topic шуму (Ізраїль-Туреччина, Іран, КНДР, Китай-Японія-НАТО), знайденого
в секції 7B попереднього routing_v2_shadow_run.py прогону.

НЕ пише в БД. НЕ чіпає routing_version=1. НЕ обирає thresholds (T_SKIP/T_ANALYZE
лишаються провізорними v1-значеннями, як і в попередньому shadow-run).

Точкова правка ЛИШЕ ОДНОГО relevant-прототипу (за формулюванням користувача):
  СТАРО: "Аналітичні прогнози розвитку воєнної ситуації, можливі сценарії
          ескалації"
  НОВО:  "Аналітичні прогнози розвитку російсько-української війни та
          пов'язаних із нею воєнно-політичних сценаріїв, що безпосередньо
          стосуються України, Росії, бойових дій, безпеки України або
          міжнародної підтримки України"
Решта 14 relevant + усі 10 irrelevant (включно з weather-cleanup) -- БЕЗ ЗМІН.

Дизайн ablation: щоб не ганяти дві окремі повні БД+модель прогони і не
змушувати вручну діффати два окремих текстових логи, цей скрипт рахує ОБИДВА
варіанти relevant-набору (старий і новий) в ОДНОМУ проході по корпусу --
irrelevant_score і vec-embedding рахуються один раз, а relevant_score
рахується двічі (old-set 15 векторів, new-set 15 векторів, відрізняються
рівно одним рядком). Це і є ablation: ізолюємо ефект ЛИШЕ цього одного
рядка, все інше в пайплайні ідентичне.

Додатково рахується діагностика best_relevant_prototype_v2 (текст прототипу,
що дав max cos-sim) окремо для old-набору і new-набору -- щоб перевірити
гіпотезу напряму: чи справді "проблемні" items раніше вигравали через широкий
forecast-прототип, а тепер -- ні (або взагалі йдуть у maybe/skip).

Виводить:
  1. crosscheck n_with_v1
  2. TRANSITION MATRIX v1 decision -> v2_new decision (для порівняння з
     попереднім прогоном routing_v2_shadow_run.py)
  2B. ABLATION DELTA: v2_old decision -> v2_new decision (3x3, весь корпус)
  3. Серед maybe_v1 -> analyze_old (661 items з попереднього прогону):
     скільки лишається analyze під new, скільки падає в maybe/skip
  4. Усі 4 колишні skip_v1 -> analyze_old кейси: old vs new decision
  5. Обидва підтверджені false-negative кейси: old vs new decision
  6. 6 конкретних off-topic foreign-conflict кейсів з попередньої ручної
     перевірки секції 7B: old vs new decision + best_relevant_prototype
  7. Нові analyze_v1 -> skip_v2_new (якщо з'являться -- найчутливіший напрямок)
  8. Випадкові 50 (seed=42) з НОВОГО maybe_v1 -> analyze_new -- свіжий ручний
     перегляд
  9. Підсумок: гіпотеза підтверджена/спростована, поріг НЕ обирається
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

ROUTING_VERSION = 1  # читаємо v1 decision/score з БД лише для довідки
PROTOTYPES_V2_FILE = Path(__file__).resolve().parent / "prototypes_v2.json"

# ПРОВІЗОРНО -- ті самі пороги, що вже в проді v1 і в попередньому shadow-run.
# Thresholds v2 в цьому скрипті НЕ обираються.
T_SKIP = -0.07
T_ANALYZE = 0.10

OLD_PROTOTYPE_TEXT = "Аналітичні прогнози розвитку воєнної ситуації, можливі сценарії ескалації"
NEW_PROTOTYPE_TEXT = (
    "Аналітичні прогнози розвитку російсько-української війни та пов'язаних із "
    "нею воєнно-політичних сценаріїв, що безпосередньо стосуються України, "
    "Росії, бойових дій, безпеки України або міжнародної підтримки України"
)

FALSE_NEGATIVE_CASES = [
    ("69c81c45-c166-4dcc-b65a-116f0f426810", "прогноз ударів РФ восени/взимку"),
    ("62e9fda4-bed0-490e-a753-323cfbc6c7f3", "прогноз складної зими/енергодефіциту для України"),
]

# 6 off-topic foreign-conflict кейсів, знайдені ручним переглядом секції 7B
# попереднього прогону (усі v1=maybe -> v2_old=analyze):
OFFTOPIC_CASES = [
    ("5a5a5d42-efb3-4fb5-be3c-bf3afb60ce50", "Israel Hayom: Ізраїль-Туреччина"),
    ("dcd1d692-c37c-41cd-ad47-8670dfd1a1f9", "США про загрозу після ракет КНДР"),
    ("c468be9f-5dac-4f6d-8ec0-cc74497f0870", "Іран погрожує Ормузькою протокою"),
    ("8febc999-a0e9-44bc-8c50-c74db05f6e5d", "Китай про Японію і НАТО"),
    ("2775adfe-d190-48b8-a78d-21a679c1f5aa", "Ізраїль може вдарити по турках у Сирії"),
    ("d6cfd4a3-c227-48ea-84c5-c3c021816b5f", "виробництво Tomahawk через 'війну в Ірані'"),
]

SNIPPET_LEN = 200
SAMPLE_N = 50
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


def load_ablation_prototypes(model: SentenceTransformer):
    data = json.loads(PROTOTYPES_V2_FILE.read_text(encoding="utf-8"))
    if "relevant" not in data or "irrelevant" not in data:
        raise RuntimeError(
            f"{PROTOTYPES_V2_FILE} має ключі {list(data.keys())}, "
            "очікувались 'relevant' та 'irrelevant'."
        )
    relevant_new_texts = list(data["relevant"])
    if NEW_PROTOTYPE_TEXT not in relevant_new_texts:
        raise RuntimeError(
            f"{PROTOTYPES_V2_FILE} не містить очікуваного нового формулювання. "
            "Перевір, чи точкова правка вже застосована до файлу."
        )
    idx = relevant_new_texts.index(NEW_PROTOTYPE_TEXT)
    relevant_old_texts = list(relevant_new_texts)
    relevant_old_texts[idx] = OLD_PROTOTYPE_TEXT

    irrelevant_texts = list(data["irrelevant"])

    relevant_old_vecs = np.asarray(model.encode(relevant_old_texts, normalize_embeddings=True))
    relevant_new_vecs = np.asarray(model.encode(relevant_new_texts, normalize_embeddings=True))
    irrelevant_vecs = np.asarray(model.encode(irrelevant_texts, normalize_embeddings=True))

    return {
        "relevant_old_texts": relevant_old_texts,
        "relevant_new_texts": relevant_new_texts,
        "irrelevant_texts": irrelevant_texts,
        "relevant_old_vecs": relevant_old_vecs,
        "relevant_new_vecs": relevant_new_vecs,
        "irrelevant_vecs": irrelevant_vecs,
    }


def decide(score: float) -> str:
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


def short(text: str, length: int = 60) -> str:
    return text if len(text) <= length else text[:length].rstrip() + "…"


def fmt_row(r: dict) -> str:
    v1_score = f"{r['routing_score_v1']:.3f}" if r["routing_score_v1"] is not None else "n/a"
    return (
        f"[{r['content_id']}] v1={r['decision_v1']}(score={v1_score}) | "
        f"OLD: {r['decision_old']}(margin={r['margin_old']:.3f}, best_proto=\"{short(r['best_proto_old'])}\") -> "
        f"NEW: {r['decision_new']}(margin={r['margin_new']:.3f}, best_proto=\"{short(r['best_proto_new'])}\") "
        f"title={r['title'] or '(no title)'}"
    )


def run() -> int:
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)
        print("loading BGE-M3 model...")
        model = load_model()

        proto = load_ablation_prototypes(model)
        print(
            f"ablation prototypes: relevant_old={len(proto['relevant_old_texts'])} "
            f"relevant_new={len(proto['relevant_new_texts'])} "
            f"irrelevant={len(proto['irrelevant_texts'])} (irrelevant однаковий для old/new)"
        )
        print(f"OLD prototype (index замінено): \"{OLD_PROTOTYPE_TEXT}\"")
        print(f"NEW prototype: \"{NEW_PROTOTYPE_TEXT}\"")
        print(f"provisional thresholds (same as v1 prod, НЕ обираються тут): T_SKIP={T_SKIP} T_ANALYZE={T_ANALYZE}")

        print(f"\nfetching full corpus with routing_version={ROUTING_VERSION} decisions (LEFT JOIN)...")
        rows = fetch_corpus_with_routing(conn)
        print(f"corpus size: {len(rows)}")

        rel_old = proto["relevant_old_vecs"]
        rel_new = proto["relevant_new_vecs"]
        irr = proto["irrelevant_vecs"]
        rel_old_texts = proto["relevant_old_texts"]
        rel_new_texts = proto["relevant_new_texts"]

        results = []
        for content_id, title, text_content, embedding, routing_decision, routing_score in rows:
            vec = embedding.to_numpy().astype(np.float32)

            irrelevant_score_v2 = float((vec @ irr.T).max())

            sims_old = vec @ rel_old.T
            best_idx_old = int(sims_old.argmax())
            relevant_score_old = float(sims_old[best_idx_old])
            margin_old = relevant_score_old - irrelevant_score_v2
            decision_old = decide(margin_old)

            sims_new = vec @ rel_new.T
            best_idx_new = int(sims_new.argmax())
            relevant_score_new = float(sims_new[best_idx_new])
            margin_new = relevant_score_new - irrelevant_score_v2
            decision_new = decide(margin_new)

            results.append(
                {
                    "content_id": content_id,
                    "title": title,
                    "text_content": text_content,
                    "decision_v1": routing_decision if routing_decision is not None else "NO_ROUTING_DECISION",
                    "routing_score_v1": routing_score,
                    "irrelevant_score_v2": irrelevant_score_v2,
                    "relevant_score_old": relevant_score_old,
                    "margin_old": margin_old,
                    "decision_old": decision_old,
                    "best_proto_old": rel_old_texts[best_idx_old],
                    "relevant_score_new": relevant_score_new,
                    "margin_new": margin_new,
                    "decision_new": decision_new,
                    "best_proto_new": rel_new_texts[best_idx_new],
                }
            )

        n_with_v1 = sum(1 for r in results if r["decision_v1"] != "NO_ROUTING_DECISION")
        print(f"\nrows з routing_version=1 decision: {n_with_v1} з {len(results)}")

        # --- 2: transition matrix v1 -> v2_new (для порівняння з попереднім прогоном) ---
        v1_labels = ["skip", "maybe", "analyze", "NO_ROUTING_DECISION"]
        v2_labels = ["skip", "maybe", "analyze"]
        matrix_v1_new: dict[str, dict[str, int]] = {v1: {v2: 0 for v2 in v2_labels} for v1 in v1_labels}
        for r in results:
            matrix_v1_new[r["decision_v1"]][r["decision_new"]] += 1

        print("\n\n=== 2. TRANSITION MATRIX: v1 decision -> v2_NEW decision (для порівняння з попереднім прогоном) ===")
        header = "v1 \\ v2_new".ljust(20) + "".join(v2.ljust(12) for v2 in v2_labels) + "total"
        print(header)
        for v1 in v1_labels:
            row_total = sum(matrix_v1_new[v1].values())
            if row_total == 0:
                continue
            line = v1.ljust(20) + "".join(str(matrix_v1_new[v1][v2]).ljust(12) for v2 in v2_labels) + str(row_total)
            print(line)

        # --- 2B: ABLATION DELTA v2_old -> v2_new, увесь корпус ---
        matrix_old_new: dict[str, dict[str, int]] = {v: {v2: 0 for v2 in v2_labels} for v in v2_labels}
        for r in results:
            matrix_old_new[r["decision_old"]][r["decision_new"]] += 1

        print("\n\n=== 2B. ABLATION DELTA: v2_OLD decision -> v2_NEW decision (увесь корпус, 7736) ===")
        header = "old \\ new".ljust(20) + "".join(v2.ljust(12) for v2 in v2_labels) + "total"
        print(header)
        for v1 in v2_labels:
            row_total = sum(matrix_old_new[v1].values())
            line = v1.ljust(20) + "".join(str(matrix_old_new[v1][v2]).ljust(12) for v2 in v2_labels) + str(row_total)
            print(line)

        # --- 3: серед maybe_v1 -> analyze_old (661 items попереднього прогону) ---
        maybe_to_analyze_old = [r for r in results if r["decision_v1"] == "maybe" and r["decision_old"] == "analyze"]
        print(
            f"\n\n=== 3. Серед maybe_v1 -> analyze_OLD ({len(maybe_to_analyze_old)} items, "
            "має бути ~661 як у попередньому прогоні) -- що з ними під NEW ==="
        )
        still_analyze = [r for r in maybe_to_analyze_old if r["decision_new"] == "analyze"]
        dropped_to_maybe = [r for r in maybe_to_analyze_old if r["decision_new"] == "maybe"]
        dropped_to_skip = [r for r in maybe_to_analyze_old if r["decision_new"] == "skip"]
        print(f"лишились analyze: {len(still_analyze)}")
        print(f"впали в maybe:    {len(dropped_to_maybe)}")
        print(f"впали в skip:     {len(dropped_to_skip)}")

        # --- 4: усі 4 колишні skip_v1 -> analyze_old кейси ---
        skip_to_analyze_old = [r for r in results if r["decision_v1"] == "skip" and r["decision_old"] == "analyze"]
        print(f"\n\n=== 4. Усі skip_v1 -> analyze_OLD кейси ({len(skip_to_analyze_old)}, мало бути 4) -- old vs new ===")
        for r in skip_to_analyze_old:
            print(fmt_row(r))
            print(f"    {preview(r['text_content'])}")

        # --- 5: обидва підтверджені false-negative кейси ---
        print("\n\n=== 5. FALSE NEGATIVE КЕЙСИ -- old vs new ===")
        by_id = {str(r["content_id"]): r for r in results}
        for content_id, note in FALSE_NEGATIVE_CASES:
            r = by_id.get(content_id)
            if r is None:
                print(f"\n[{content_id}] ({note}) -- НЕ ЗНАЙДЕНО, перевір вручну")
                continue
            print(f"\n[{content_id}] ({note})")
            print(f"    {fmt_row(r)}")
            print(f"    {preview(r['text_content'])}")

        # --- 6: 6 off-topic foreign-conflict кейсів ---
        print("\n\n=== 6. OFF-TOPIC FOREIGN-CONFLICT КЕЙСИ (знайдені ручним переглядом 7B) -- old vs new ===")
        for content_id, note in OFFTOPIC_CASES:
            r = by_id.get(content_id)
            if r is None:
                print(f"\n[{content_id}] ({note}) -- НЕ ЗНАЙДЕНО, перевір вручну")
                continue
            print(f"\n[{content_id}] ({note})")
            print(f"    {fmt_row(r)}")
            print(f"    {preview(r['text_content'])}")

        # --- 7: нові analyze_v1 -> skip_v2_new, якщо з'являться ---
        new_analyze_to_skip = [r for r in results if r["decision_v1"] == "analyze" and r["decision_new"] == "skip"]
        print(
            f"\n\n=== 7. analyze_v1 -> skip_v2_NEW ({len(new_analyze_to_skip)}, "
            "найчутливіший напрямок -- має лишатись 0) ==="
        )
        for r in new_analyze_to_skip:
            print(fmt_row(r))
            print(f"    {preview(r['text_content'])}")

        # --- 8: випадкові 50 нових maybe_v1 -> analyze_NEW ---
        maybe_to_analyze_new = [r for r in results if r["decision_v1"] == "maybe" and r["decision_new"] == "analyze"]
        print(
            f"\n\n=== 8. Випадкові {SAMPLE_N} (seed={RANDOM_SEED}) з НОВОГО maybe_v1 -> analyze_NEW "
            f"(total={len(maybe_to_analyze_new)}) -- свіжий ручний перегляд ==="
        )
        rng = random.Random(RANDOM_SEED)
        sample = rng.sample(maybe_to_analyze_new, min(SAMPLE_N, len(maybe_to_analyze_new)))
        for r in sample:
            print(fmt_row(r))
            print(f"    {preview(r['text_content'])}")

        # --- 9: підсумок ---
        print(
            "\n\n=== 9. ПІДСУМОК ABLATION ===\n"
            f"maybe_v1 -> analyze_OLD: {len(maybe_to_analyze_old)} -- "
            f"з них під NEW: analyze={len(still_analyze)}, maybe={len(dropped_to_maybe)}, skip={len(dropped_to_skip)}\n"
            f"off-topic 6 кейсів (секція 6) -- перевір вручну кожен: чи всі впали з analyze\n"
            "Це ABLATION, НЕ фінальний Routing v2 threshold run. Thresholds v2 в цьому скрипті "
            "НЕ обираються. Жодних записів у БД. routing_version=1 не змінено. "
            "Наступний крок -- окреме рішення користувача за результатами цього прогону."
        )

        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Routing v2 -- ablation shadow-run для одного relevant-прототипу (read-only)")
    parser.parse_args()
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
