#!/usr/bin/env python3
"""
Relation Gold Set v1 — детермінований стратифікований семплер.

READ-ONLY. Нічого не пише в БД (з'єднання відкривається з
conn.read_only = True, тобто заборона працює на рівні PostgreSQL, а не
"ми обіцяємо"). Не викликає Mamay. Не чіпає candidate_pairs /
relation_judgments.

Що робить:
  1. Читає claims + claim embeddings + occurrences (як candidate_pair_profile).
  2. Читає content embeddings (як candidate_pair_dual_profile).
  3. Прохід 1 (blockwise): per-claim degree у cross-group просторі -> hub-прапорці.
  4. Прохід 2 (blockwise): точні розміри страт (population) + детермінований
     відбір top-n за стабільним хешем.
  5. Дотягує той самий provenance-контекст, що бачить Mamay
     (relation_judgment_worker.fetch_claims_meta).
  6. Будує prefix-balanced порядок анотації + приховані повтори.
  7. Пише JSONL (canonical) + CSV (для аналізу) + manifest.json (reproducibility).

Час НЕ використовується як eligibility/stratification/ranking signal. first_seen
експортується лише як diagnostic і в UI анотатора не показується.

Приклад:
    python3 -m experiments.claim_relations.gold_set.relation_gold_set_sampler \\
        --out-dir /tmp/gold_v1 --dry-run
    python3 -m experiments.claim_relations.gold_set.relation_gold_set_sampler \\
        --out-dir /tmp/gold_v1
"""
from __future__ import annotations

import argparse
import json
import csv
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.claim_relations import candidate_pair_profile as base  # noqa: E402
from experiments.claim_relations import candidate_pair_dual_profile as dual  # noqa: E402
from experiments.claim_relations import relation_judgment_worker as judge  # noqa: E402
from experiments.claim_relations.gold_set import gold_set_common as common  # noqa: E402

DEFAULT_SEED = 20260903
OVERSAMPLE = 8              # запас кандидатів на страту (проти капу claim-use)
MAX_SELECTOR_CAPACITY = 4000  # межа пам'яті на страту


# ---------------------------------------------------------------------------
# Завантаження (read-only)
# ---------------------------------------------------------------------------

def _connect_read_only():
    """psycopg3-з'єднання з увімкненим read-only на рівні сесії.

    Це не косметика: навіть якщо десь у коді з'явиться помилковий INSERT,
    PostgreSQL відхилить його. Дешевша і надійніша гарантія, ніж code review.
    """
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(base.DB_DSN)
    conn.read_only = True  # має бути виставлено ДО першої транзакції
    register_vector(conn)
    return conn


def load_corpus(verbose: bool = True) -> tuple[dict, np.ndarray, dict]:
    """Повертає (corpus, content_vectors, load_stats).

    corpus — те саме, що candidate_pair_profile.build_corpus, але звужене до
    claims, для яких Є content embedding (інакше content_score неможливий).
    """
    with _connect_read_only() as conn:
        base.verify_registered_model(conn)
        claim_rows = base.fetch_claims(conn)
        content_ids = sorted({row[3] for row in claim_rows}, key=str)
        occ_by_content = base.fetch_occurrences(conn, content_ids)
        claim_ids_all = [row[0] for row in claim_rows]
        content_by_claim = dual.fetch_content_vector_by_claim(conn, claim_ids_all)

    corpus = base.build_corpus(claim_rows, occ_by_content)
    # build_corpus зберігає порядок fetch_claims (ORDER BY claim_id) і лише
    # пропускає claims без occurrence/source-group. Перевіряємо саме це:
    # результат має бути підпослідовністю вхідного порядку.
    if not _is_subsequence(corpus["claim_ids"], claim_ids_all):
        raise RuntimeError("claim ordering changed while building corpus")

    keep_idx = [i for i, cid in enumerate(corpus["claim_ids"]) if cid in content_by_claim]
    missing_content = len(corpus["claim_ids"]) - len(keep_idx)
    corpus = _subset_corpus(corpus, keep_idx)

    content_vectors = np.vstack(
        [np.asarray(content_by_claim[cid], dtype=np.float32) for cid in corpus["claim_ids"]]
    ) if corpus["claim_ids"] else np.empty((0, base.DIMENSION), dtype=np.float32)

    stats = {
        "claims_fetched": len(claim_rows),
        "skipped_no_occurrence": corpus["skipped_no_occurrence"],
        "skipped_no_source_group": corpus["skipped_no_group"],
        "skipped_no_content_embedding": missing_content,
        "claims_usable": len(corpus["claim_ids"]),
    }
    if verbose:
        for key, value in stats.items():
            print(f"  {key}: {value}")
    return corpus, content_vectors, stats


def _is_subsequence(subset: list, full: list) -> bool:
    """Чи є subset підпослідовністю full (зі збереженням порядку)."""
    it = iter(full)
    return all(any(x is y or x == y for y in it) for x in subset)


def _subset_corpus(corpus: dict, keep_idx: list[int]) -> dict:
    idx = np.asarray(keep_idx, dtype=np.int64)
    return {
        "claim_ids": [corpus["claim_ids"][i] for i in keep_idx],
        "claim_texts": [corpus["claim_texts"][i] for i in keep_idx],
        "run_ids": [corpus["run_ids"][i] for i in keep_idx],
        "groups": [corpus["groups"][i] for i in keep_idx],
        "first_seen": corpus["first_seen"][idx] if len(keep_idx) else corpus["first_seen"][:0],
        "vectors": corpus["vectors"][idx] if len(keep_idx) else corpus["vectors"][:0],
        "skipped_no_group": corpus["skipped_no_group"],
        "skipped_no_occurrence": corpus["skipped_no_occurrence"],
    }


# ---------------------------------------------------------------------------
# Прохід 1: hub degrees
# ---------------------------------------------------------------------------

def compute_hub_degrees(corpus: dict, chunk_size: int, hub_score: float) -> np.ndarray:
    """Per-claim degree у CROSS-GROUP просторі при claim_score >= hub_score.

    Рахуємо саме в cross-group просторі, бо це і є eligible-простір
    candidate v1; hub там — це claim, який тягне на себе бюджет.
    """
    vectors = corpus["vectors"]
    groups = corpus["groups"]
    n = len(corpus["claim_ids"])
    degrees = np.zeros(n, dtype=np.int32)
    if n == 0:
        return degrees

    grouped = base.group_indices(groups)
    group_keys = sorted(grouped, key=lambda g: tuple(sorted(g)))
    for left_pos, group_a in enumerate(group_keys):
        idx_a_all = grouped[group_a]
        for group_b in group_keys[left_pos + 1:]:
            if not group_a.isdisjoint(group_b):
                continue
            idx_b = grouped[group_b]
            b_vectors = vectors[idx_b]
            for start in range(0, len(idx_a_all), chunk_size):
                idx_a = idx_a_all[start:start + chunk_size]
                scores = vectors[idx_a] @ b_vectors.T
                np.clip(scores, -1.0, 1.0, out=scores)
                rows, cols = np.nonzero(scores >= hub_score)
                if rows.size:
                    np.add.at(degrees, idx_a[rows], 1)
                    np.add.at(degrees, idx_b[cols], 1)
    return degrees


# ---------------------------------------------------------------------------
# Прохід 2: populations + відбір
# ---------------------------------------------------------------------------

def _content_scores_for(content_vectors: np.ndarray, ga: np.ndarray, gb: np.ndarray) -> np.ndarray:
    scores = np.sum(content_vectors[ga] * content_vectors[gb], axis=1, dtype=np.float32)
    np.clip(scores, -1.0, 1.0, out=scores)
    return scores


def collect_strata(
    corpus: dict,
    content_vectors: np.ndarray,
    hub_flags: np.ndarray,
    chunk_size: int,
    allocation: dict[str, int],
    seed: int,
    include_exploratory: bool,
) -> tuple[dict[str, common.StratumSelector], dict]:
    """Один прохід по cross-group простору (+ опційно same-group) з точним
    підрахунком розміру кожної страти і детермінованим відбором."""
    vectors = corpus["vectors"]
    claim_ids = corpus["claim_ids"]
    groups = corpus["groups"]
    run_ids = corpus["run_ids"]

    # Тримаємо надлишок кандидатів: кап на повторне використання claim
    # відсіює частину найкращих за рангом, і без запасу страта недобирає
    # розмір (а для decision_critical страти це втрата сертифікації).
    selectors = {
        stratum: common.StratumSelector(
            stratum, min(allocation.get(stratum, 0) * OVERSAMPLE, MAX_SELECTOR_CAPACITY), seed
        )
        for stratum in common.STRATA
    }
    diag = {"blocks": 0, "cross_pairs_scanned": 0, "same_group_pairs_scanned": 0}

    grouped = base.group_indices(groups)
    group_keys = sorted(grouped, key=lambda g: tuple(sorted(g)))

    # Цілочисельні коди run_id — щоб виключення same-content у межах однієї
    # групи було векторним, а не Python-циклом на мільйонах збігів.
    run_code_by_id: dict = {}
    run_codes = np.empty(len(run_ids), dtype=np.int64)
    for i, rid in enumerate(run_ids):
        run_codes[i] = run_code_by_id.setdefault(rid, len(run_code_by_id))

    def offer_block(idx_a: np.ndarray, idx_b: np.ndarray, scores: np.ndarray,
                    same_group: bool, floor: float) -> None:
        rows, cols = np.nonzero(scores >= floor)
        if rows.size == 0:
            return
        ga = idx_a[rows]
        gb = idx_b[cols]
        if same_group:
            # У межах однієї групи виключаємо той самий content (run_id) і
            # рахуємо кожну невпорядковану пару рівно один раз (ga < gb).
            keep = (run_codes[ga] != run_codes[gb]) & (ga < gb)
            if not keep.any():
                return
            ga, gb = ga[keep], gb[keep]
            rows, cols = rows[keep], cols[keep]
        claim_scores = scores[rows, cols]
        content_scores = _content_scores_for(content_vectors, ga, gb)
        for a_i, b_i, c_score, k_score in zip(
            ga.tolist(), gb.tolist(), content_scores.tolist(), claim_scores.tolist()
        ):
            stratum = common.assign_stratum(
                claim_score=float(k_score),
                content_score=float(c_score),
                hub_a=bool(hub_flags[a_i]),
                hub_b=bool(hub_flags[b_i]),
                same_group=same_group,
            )
            if stratum is None:
                continue
            key = common.pair_key(claim_ids[a_i], claim_ids[b_i])
            selectors[stratum].offer(
                key,
                {
                    "pair_key": key,
                    "stratum": stratum,
                    "claim_id_a": claim_ids[a_i],
                    "claim_id_b": claim_ids[b_i],
                    "claim_score": float(k_score),
                    "content_score": float(c_score),
                    "hub_a": bool(hub_flags[a_i]),
                    "hub_b": bool(hub_flags[b_i]),
                    "source_group_a": sorted(groups[a_i]),
                    "source_group_b": sorted(groups[b_i]),
                    "same_group": same_group,
                },
            )

    # --- cross-group (main frame) ---
    for left_pos, group_a in enumerate(group_keys):
        idx_a_all = grouped[group_a]
        for group_b in group_keys[left_pos + 1:]:
            if not group_a.isdisjoint(group_b):
                continue
            idx_b = grouped[group_b]
            overlap = {run_ids[i] for i in idx_a_all} & {run_ids[i] for i in idx_b}
            if overlap:
                raise RuntimeError(
                    "same run_id found across disjoint source_group sets; invariant violated for "
                    f"{base.group_label(group_a)} x {base.group_label(group_b)}"
                )
            b_vectors = vectors[idx_b]
            for start in range(0, len(idx_a_all), chunk_size):
                idx_a = idx_a_all[start:start + chunk_size]
                scores = vectors[idx_a] @ b_vectors.T
                np.clip(scores, -1.0, 1.0, out=scores)
                diag["blocks"] += 1
                diag["cross_pairs_scanned"] += scores.size
                offer_block(idx_a, idx_b, scores, same_group=False, floor=common.CLAIM_FLOOR)

    # --- same-group (exploratory frame, окремо) ---
    # Поріг тут CLAIM_HIGH, а не CLAIM_FLOOR: assign_stratum усе одно віддає
    # S9 лише при claim >= 0.80, тож нижчий поріг був би чистою тратою
    # (у межах однієї медіа-групи перекладів/репостів на 0.5+ дуже багато).
    if include_exploratory:
        for group in group_keys:
            idx_all = grouped[group]
            for start in range(0, len(idx_all), chunk_size):
                idx_a = idx_all[start:start + chunk_size]
                scores = vectors[idx_a] @ vectors[idx_all].T
                np.clip(scores, -1.0, 1.0, out=scores)
                diag["blocks"] += 1
                diag["same_group_pairs_scanned"] += scores.size
                offer_block(idx_a, idx_all, scores, same_group=True, floor=common.CLAIM_HIGH)

    return selectors, diag


# ---------------------------------------------------------------------------
# Збірка датасету
# ---------------------------------------------------------------------------

def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def build_dataset(
    selected: list[dict],
    meta: dict,
    seed: int,
    repeat_rate: float,
    min_separation: int,
) -> list[dict]:
    ordered = common.build_annotation_order(selected, seed)
    interactions = common.plan_hidden_repeats(ordered, repeat_rate, seed, min_separation)

    dataset: list[dict] = []
    for entry in interactions:
        swapped = common.side_swap_for_presentation(seed, entry["pair_key"], entry["is_repeat"])
        left_id, right_id = entry["claim_id_a"], entry["claim_id_b"]
        if swapped:
            left_id, right_id = right_id, left_id
        left, right = meta[left_id], meta[right_id]

        dataset.append(
            {
                "schema_version": common.SCHEMA_VERSION,
                "interaction_id": entry["interaction_id"],
                "pair_key": entry["pair_key"],
                "is_repeat": entry["is_repeat"],
                "repeat_of": entry["repeat_of"],
                "presented_swapped": swapped,
                # --- те, що БАЧИТЬ анотатор ---
                "display": {
                    "A": _display_claim(left_id, left),
                    "B": _display_claim(right_id, right),
                },
                # --- diagnostics: у файлі є, але UI ховає до збереження мітки ---
                "diagnostics": {
                    "stratum": entry["stratum"],
                    "frame": common.STRATA[entry["stratum"]]["frame"],
                    "claim_score": round(entry["claim_score"], 6),
                    "content_score": round(entry["content_score"], 6),
                    "hub_a": entry["hub_a"],
                    "hub_b": entry["hub_b"],
                    "source_group_a": entry["source_group_a"],
                    "source_group_b": entry["source_group_b"],
                    "same_group": entry["same_group"],
                    "claim_id_a": str(entry["claim_id_a"]),
                    "claim_id_b": str(entry["claim_id_b"]),
                    "first_seen_a": _iso(meta[entry["claim_id_a"]]["first_seen"]),
                    "first_seen_b": _iso(meta[entry["claim_id_b"]]["first_seen"]),
                },
            }
        )
    return dataset


def _display_claim(claim_id, m: dict) -> dict:
    """Рівно ті поля, які relation_judgment_worker передає Mamay.

    Свідомо БЕЗ first_seen, contour_set і будь-яких score — див. anti-leakage
    правила в ANNOTATION_HANDBOOK.md. Людина і модель мають судити з однакового
    інформаційного набору, інакше пізніше порівняння human-vs-Mamay нечесне.
    """
    return {
        "claim_text": m["claim_text"],
        "evidence_span": m["evidence_span"],
        "title": m["title"],
        "context_snippet": m["context_snippet"],
        "source": m["display_source"],
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only deterministic stratified sampler for Relation Gold Set v1"
    )
    parser.add_argument("--out-dir", type=Path, required=True, help="каталог для артефактів")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--hub-score-threshold", type=float, default=0.85)
    parser.add_argument("--hub-degree-threshold", type=int, default=5)
    parser.add_argument("--max-claim-uses", type=int, default=2,
                        help="скільки разів один claim може зустрітися у вибірці (S8 звільнений)")
    parser.add_argument("--repeat-rate", type=float, default=0.12)
    parser.add_argument("--min-repeat-separation", type=int, default=40)
    parser.add_argument("--no-exploratory", action="store_true",
                        help="не семплювати same-source-group діагностичну страту")
    parser.add_argument("--dry-run", action="store_true",
                        help="порахувати розміри страт і вийти, нічого не пишучи")
    for stratum in common.STRATA:
        parser.add_argument(
            f"--n-{stratum.split('_')[0].lower()}",
            type=int,
            default=common.DEFAULT_ALLOCATION[stratum],
            dest=f"n_{stratum}",
            help=f"розмір вибірки для {stratum} ({common.STRATA[stratum]['label']})",
        )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size must be > 0")
    if not (0.0 <= args.repeat_rate < 1.0):
        raise SystemExit("--repeat-rate must be in [0,1)")

    allocation = {stratum: getattr(args, f"n_{stratum}") for stratum in common.STRATA}
    if args.no_exploratory:
        for stratum in common.EXPLORATORY_STRATA:
            allocation[stratum] = 0

    started = time.perf_counter()
    print("=== Relation Gold Set v1 sampler (READ-ONLY) ===")
    print(f"seed={args.seed} chunk_size={args.chunk_size}")
    print("loading corpus...")
    corpus, content_vectors, load_stats = load_corpus()

    n_claims = len(corpus["claim_ids"])
    if n_claims == 0:
        raise SystemExit("no usable claims; nothing to sample")

    print(f"pass 1: hub degrees at claim_score >= {args.hub_score_threshold}")
    degrees = compute_hub_degrees(corpus, args.chunk_size, args.hub_score_threshold)
    hub_flags = degrees >= args.hub_degree_threshold
    print(f"  hubs: {int(hub_flags.sum())}/{n_claims} "
          f"(degree >= {args.hub_degree_threshold}); max degree = {int(degrees.max()) if n_claims else 0}")

    print("pass 2: stratum populations + deterministic selection")
    selectors, diag = collect_strata(
        corpus, content_vectors, hub_flags, args.chunk_size, allocation,
        args.seed, include_exploratory=not args.no_exploratory,
    )

    populations = {s: sel.population for s, sel in selectors.items()}
    print("\n--- stratum populations / allocation ---")
    print(f"{'stratum':<32} {'population':>12} {'requested':>10} {'selected':>9}")
    for stratum in common.STRATA:
        sel = selectors[stratum]
        print(f"{stratum:<32} {sel.population:>12} {allocation[stratum]:>10} {len(sel.selected()):>9}")

    if args.dry_run:
        print("\n--dry-run: nothing written. Adjust --n-* allocations if a stratum is short.")
        print(f"elapsed={time.perf_counter() - started:.2f}s")
        return 0

    ranked_by_stratum = {stratum: selectors[stratum].selected() for stratum in common.STRATA}
    kept, fill_stats = common.fill_allocation(
        ranked_by_stratum, allocation, args.max_claim_uses, exempt_strata=("S8_HUB_X_HUB",)
    )

    print(f"\n--- allocation fill (claim-use cap = {args.max_claim_uses}) ---")
    print(f"{'stratum':<32} {'want':>5} {'got':>5} {'cap-skips':>10} {'short':>6}")
    short_critical = []
    for stratum in common.STRATA:
        st = fill_stats.get(stratum)
        if not st or st["requested"] == 0:
            continue
        print(f"{stratum:<32} {st['requested']:>5} {st['taken']:>5} "
              f"{st['skipped_by_cap']:>10} {st['short_by']:>6}")
        if st["short_by"] and common.STRATA[stratum]["decision_critical"]:
            short_critical.append((stratum, st["taken"]))

    for stratum, taken in short_critical:
        if taken < common.MIN_N_TO_CERTIFY_90:
            print(f"  WARNING: {stratum} is decision-critical but got only n={taken}; "
                  f"n>={common.MIN_N_TO_CERTIFY_90} is required to certify a 90% "
                  f"lower bound at all. It can screen, not certify.")

    # Дедуплікація на випадок, якщо та сама пара якимось чином потрапила двічі.
    seen_keys: set[str] = set()
    unique: list[dict] = []
    for item in kept:
        if item["pair_key"] in seen_keys:
            continue
        seen_keys.add(item["pair_key"])
        unique.append(item)
    if len(unique) != len(kept):
        print(f"deduplicated {len(kept) - len(unique)} duplicate pair_keys")

    claim_ids_needed = sorted({item["claim_id_a"] for item in unique} | {item["claim_id_b"] for item in unique}, key=str)
    print(f"\nfetching provenance context for {len(claim_ids_needed)} claims...")
    with _connect_read_only() as conn:
        meta = judge.fetch_claims_meta(conn, claim_ids_needed)
    missing_meta = [cid for cid in claim_ids_needed if cid not in meta]
    if missing_meta:
        raise RuntimeError(f"claim metadata missing for {len(missing_meta)} claims: {missing_meta[:5]}")

    dataset = build_dataset(unique, meta, args.seed, args.repeat_rate, args.min_repeat_separation)
    n_unique = sum(1 for row in dataset if not row["is_repeat"])
    n_repeats = sum(1 for row in dataset if row["is_repeat"])
    separations = common.repeat_separation(
        [{"interaction_id": r["interaction_id"], "is_repeat": r["is_repeat"],
          "repeat_of": r["repeat_of"], "pair_key": r["pair_key"]} for r in dataset]
    )
    min_sep = min(separations.values()) if separations else None

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = out_dir / "gold_set_v1_dataset.jsonl"
    with dataset_path.open("w", encoding="utf-8") as fh:
        for row in dataset:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    csv_path = out_dir / "gold_set_v1_pairs.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "interaction_id", "pair_key", "is_repeat", "repeat_of", "presented_swapped",
            "stratum", "frame", "claim_score", "content_score", "hub_a", "hub_b",
            "source_group_a", "source_group_b", "claim_id_a", "claim_id_b",
        ])
        for row in dataset:
            d = row["diagnostics"]
            writer.writerow([
                row["interaction_id"], row["pair_key"], row["is_repeat"], row["repeat_of"],
                row["presented_swapped"], d["stratum"], d["frame"], d["claim_score"],
                d["content_score"], d["hub_a"], d["hub_b"],
                "|".join(str(g) for g in d["source_group_a"]),
                "|".join(str(g) for g in d["source_group_b"]),
                d["claim_id_a"], d["claim_id_b"],
            ])

    manifest = {
        "schema_version": common.SCHEMA_VERSION,
        "generated_by": "relation_gold_set_sampler.py",
        "code_revision": _safe_code_revision(),
        "embedding_model_id": base.EMBEDDING_MODEL_ID,
        "seed": args.seed,
        "chunk_size": args.chunk_size,
        "hub_score_threshold": args.hub_score_threshold,
        "hub_degree_threshold": args.hub_degree_threshold,
        "max_claim_uses": args.max_claim_uses,
        "repeat_rate": args.repeat_rate,
        "min_repeat_separation_requested": args.min_repeat_separation,
        "min_repeat_separation_actual": min_sep,
        "allocation": allocation,
        "populations": populations,
        "fill_stats": fill_stats,
        "sampled": {s: sum(1 for r in dataset if (not r["is_repeat"]) and r["diagnostics"]["stratum"] == s)
                    for s in common.STRATA},
        "unique_pairs": n_unique,
        "hidden_repeats": n_repeats,
        "total_interactions": len(dataset),
        "load_stats": load_stats,
        "scan_diagnostics": diag,
        "hub_count": int(hub_flags.sum()),
        "time_signal": "NOT USED for eligibility/stratification/ranking; first_seen exported as diagnostic only",
        "strata": {s: common.STRATA[s] for s in common.STRATA},
    }
    manifest_path = out_dir / "gold_set_v1_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nunique pairs        : {n_unique}")
    print(f"hidden repeats      : {n_repeats}")
    print(f"total interactions  : {len(dataset)}")
    print(f"min repeat separation: {min_sep}")
    print(f"\nwrote:\n  {dataset_path}\n  {csv_path}\n  {manifest_path}")
    print(f"elapsed={time.perf_counter() - started:.2f}s")
    return 0


def _safe_code_revision() -> str:
    try:
        return base.get_code_revision()
    except Exception as exc:  # pragma: no cover - залежить від наявності git
        return f"<unavailable: {type(exc).__name__}>"


if __name__ == "__main__":
    raise SystemExit(main())
