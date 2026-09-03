#!/usr/bin/env python3
"""
Relation Gold Set v1 — аналіз людської розмітки.

Вхід:
  --dataset    gold_set_v1_dataset.jsonl   (з семплера)
  --manifest   gold_set_v1_manifest.json   (з семплера; дає populations для ваг)
  --annotations gold_set_v1_annotations.jsonl (експорт з labeler'а)

Нічого не пише в БД, не викликає LLM, не тренує моделей. Мета — відповісти
на питання "цього сигналу достатньо / недостатньо", а не побудувати класифікатор.

ГОЛОВНЕ МЕТОДОЛОГІЧНЕ ПРАВИЛО ЦЬОГО ФАЙЛУ:
стратифікована вибірка НЕ відображає природну поширеність. Будь-яке число,
що претендує на "у популяції X% пар такі", рахується через ваги
w_s = N_s/n_s. Числа без ваг звітуються лише як per-cell (усередині страти),
і саме так і підписані. Exploratory frame (same-source-group) ніколи не
змішується з main population.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.claim_relations.gold_set import gold_set_common as common  # noqa: E402

CLAIM_BINS = ((0.50, 0.65), (0.65, 0.80), (0.80, 0.90), (0.90, 0.97), (0.97, 1.01))
CONTENT_BINS = ((-1.01, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 1.01))
CONTENT_THRESHOLDS = (0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75)
CERTIFY_BOUND = 0.90


# ---------------------------------------------------------------------------
# Завантаження та перевірка контракту
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}: invalid JSON on line {line_no}: {exc}")
    return rows


def join_annotations(dataset: list[dict], annotations: list[dict]) -> tuple[list[dict], dict]:
    """Зшиває dataset з розміткою за interaction_id і валідує контракт."""
    by_interaction = {row["interaction_id"]: row for row in dataset}
    ann_by_interaction: dict[str, dict] = {}
    problems = {"unknown_interaction": [], "duplicate_annotation": [], "contract_errors": []}

    for ann in annotations:
        iid = ann.get("interaction_id")
        if iid not in by_interaction:
            problems["unknown_interaction"].append(iid)
            continue
        if iid in ann_by_interaction:
            problems["duplicate_annotation"].append(iid)
            continue
        errs = common.validate_annotation(ann)
        if errs:
            problems["contract_errors"].append({"interaction_id": iid, "errors": errs})
            continue
        ann_by_interaction[iid] = ann

    joined = []
    for iid, ann in ann_by_interaction.items():
        row = by_interaction[iid]
        d = row["diagnostics"]
        joined.append({
            "interaction_id": iid,
            "pair_key": row["pair_key"],
            "is_repeat": row["is_repeat"],
            "repeat_of": row["repeat_of"],
            "presented_swapped": row["presented_swapped"],
            "stratum": d["stratum"],
            "frame": d["frame"],
            "claim_score": d["claim_score"],
            "content_score": d["content_score"],
            "hub_a": d["hub_a"],
            "hub_b": d["hub_b"],
            "same_group": d["same_group"],
            "claim_id_a": d["claim_id_a"],
            "claim_id_b": d["claim_id_b"],
            "status": ann.get("status", "labeled"),
            "same_referent": ann.get("same_referent"),
            "relation_label": ann.get("relation_label"),
            "conflict_type": ann.get("conflict_type"),
            "confidence": ann.get("confidence"),
            "note": ann.get("note"),
        })
    joined.sort(key=lambda r: r["interaction_id"])
    return joined, problems


# ---------------------------------------------------------------------------
# Приховані повтори та self-consistency
# ---------------------------------------------------------------------------

def split_repeats(joined: list[dict]) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """Повертає (primary_rows, repeat_pairs).

    primary_rows — рівно один рядок на унікальну пару (той, що НЕ повтор).
    Це і є gold set. repeat_pairs — (original, repeat) для оцінки
    self-consistency. Повтори НІКОЛИ не входять у primary_rows, інакше
    унікальна популяція була б порахована двічі.
    """
    primary = [r for r in joined if not r["is_repeat"]]
    by_key_primary = {r["pair_key"]: r for r in primary}
    pairs = []
    for row in joined:
        if not row["is_repeat"]:
            continue
        original = by_key_primary.get(row["pair_key"])
        if original is not None:
            pairs.append((original, row))
    return primary, pairs


def agreement_stats(repeat_pairs: list[tuple[dict, dict]]) -> dict:
    """Intra-rater agreement + Cohen's kappa на same_referent і relation_label."""
    def _agree(field: str) -> dict:
        usable = [(a, b) for a, b in repeat_pairs
                  if a["status"] == "labeled" and b["status"] == "labeled"]
        n = len(usable)
        if n == 0:
            return {"n": 0, "raw": float("nan"), "kappa": float("nan"), "disagreements": []}
        hits = sum(1 for a, b in usable if a[field] == b[field])
        raw = hits / n
        # Cohen's kappa з маргіналами тієї самої людини у двох "проходах".
        labels = sorted({a[field] for a, _ in usable} | {b[field] for _, b in usable}, key=str)
        pa = Counter(a[field] for a, _ in usable)
        pb = Counter(b[field] for _, b in usable)
        expected = sum((pa[l] / n) * (pb[l] / n) for l in labels)
        kappa = (raw - expected) / (1 - expected) if expected < 1 else float("nan")
        disagreements = [
            {"pair_key": a["pair_key"], "stratum": a["stratum"],
             "first": a[field], "second": b[field]}
            for a, b in usable if a[field] != b[field]
        ]
        return {"n": n, "raw": raw, "kappa": kappa, "disagreements": disagreements}

    per_stratum = defaultdict(lambda: {"n": 0, "hits": 0})
    for a, b in repeat_pairs:
        if a["status"] != "labeled" or b["status"] != "labeled":
            continue
        cell = per_stratum[a["stratum"]]
        cell["n"] += 1
        if a["same_referent"] == b["same_referent"]:
            cell["hits"] += 1

    return {
        "same_referent": _agree("same_referent"),
        "relation_label": _agree("relation_label"),
        "per_stratum_same_referent": {k: dict(v) for k, v in sorted(per_stratum.items())},
    }


# ---------------------------------------------------------------------------
# Допоміжне
# ---------------------------------------------------------------------------

def bin_index(value: float, bins) -> int:
    for i, (lo, hi) in enumerate(bins):
        if lo <= value < hi:
            return i
    return len(bins) - 1


def bin_label(bins, i: int) -> str:
    lo, hi = bins[i]
    lo_s = "<" if lo <= -1.0 else f"[{lo:.2f}"
    if lo <= -1.0:
        return f"<{hi:.2f}"
    return f"{lo_s},{hi:.2f})"


def fmt_pct(value: float) -> str:
    return "  n/a " if value != value else f"{100.0 * value:5.1f}%"


def pct_ci(successes: int, total: int) -> str:
    if total == 0:
        return "n=0"
    lo, hi = common.wilson_interval(successes, total)
    return f"{successes}/{total} = {100.0*successes/total:5.1f}% [{100*lo:.1f},{100*hi:.1f}]"


# ---------------------------------------------------------------------------
# Секції звіту
# ---------------------------------------------------------------------------

def report_completeness(dataset, joined, problems, primary, repeat_pairs) -> None:
    print("=" * 78)
    print("1. COMPLETENESS / CONTRACT")
    print("=" * 78)
    total_interactions = len(dataset)
    expected_unique = sum(1 for r in dataset if not r["is_repeat"])
    expected_repeats = total_interactions - expected_unique
    print(f"dataset interactions      : {total_interactions} "
          f"(unique={expected_unique}, hidden repeats={expected_repeats})")
    print(f"annotations joined        : {len(joined)}")
    print(f"  unique pairs labeled    : {len(primary)} / {expected_unique} "
          f"({100.0*len(primary)/expected_unique if expected_unique else 0:.1f}%)")
    print(f"  repeat pairs usable     : {len(repeat_pairs)} / {expected_repeats}")
    unusable = [r for r in primary if r["status"] == "unusable"]
    print(f"  marked unusable         : {len(unusable)}")
    for key, values in problems.items():
        if values:
            print(f"  PROBLEM {key}: {len(values)}")
            for v in values[:5]:
                print(f"    {v}")
    if not any(problems.values()):
        print("  contract: OK (no unknown ids, no duplicates, no schema violations)")

    # Псевдореплікація: скільки насправді унікальних claims стоїть за мітками.
    labeled = [r for r in primary if r["status"] == "labeled"]
    claims = Counter()
    for r in labeled:
        claims[r["claim_id_a"]] += 1
        claims[r["claim_id_b"]] += 1
    if labeled:
        worst = claims.most_common(5)
        print(f"\n  distinct claims behind {len(labeled)} labeled pairs: {len(claims)}")
        print(f"  most reused claims: " + ", ".join(f"{c[:8]}…x{n}" for c, n in worst))
        if worst and worst[0][1] >= 6:
            print("  WARNING: a single claim appears in >=6 labeled pairs. The claim-use")
            print("  cap exempts S8_HUB_X_HUB by design (hub reuse is the thing being")
            print("  measured there), so treat S8 confidence intervals as optimistic:")
            print("  those rows are not independent observations.")

    n_unusable = len(unusable)
    n_labeled = len(labeled)
    if n_labeled and n_unusable / (n_labeled + n_unusable) > 0.10:
        print(f"\n  WARNING: {n_unusable} unusable of {n_labeled + n_unusable} "
              f"({100.0 * n_unusable / (n_labeled + n_unusable):.0f}%). Weights use the")
        print("  LABELED count per stratum, so a high unusable rate inflates weights in")
        print("  the affected strata and widens the real (unreported) uncertainty.")


def report_agreement(agree: dict) -> None:
    print()
    print("=" * 78)
    print("2. INTRA-RATER SELF-CONSISTENCY (hidden repeats, sides swapped)")
    print("=" * 78)
    for field in ("same_referent", "relation_label"):
        a = agree[field]
        if a["n"] == 0:
            print(f"{field:<16}: no usable repeat pairs")
            continue
        print(f"{field:<16}: raw={100*a['raw']:.1f}% kappa={a['kappa']:.3f} (n={a['n']})")
    dis = agree["same_referent"]["disagreements"]
    if dis:
        print("\n  same_referent disagreements:")
        for d in dis:
            print(f"    {d['stratum']:<32} {d['first']} -> {d['second']}  ({d['pair_key'][:16]}…)")
    print("\n  per-stratum same_referent agreement:")
    for stratum, cell in agree["per_stratum_same_referent"].items():
        if cell["n"]:
            print(f"    {stratum:<32} {cell['hits']}/{cell['n']}")
    print("\n  NOTE: repeats are presented with A/B sides swapped, so this also")
    print("  measures order-invariance, not only memory consistency.")


def report_distributions(primary: list[dict], weights: dict, populations: dict) -> None:
    print()
    print("=" * 78)
    print("3. LABEL DISTRIBUTION — sample vs weighted population estimate")
    print("=" * 78)
    labeled = [r for r in primary if r["status"] == "labeled" and r["frame"] == common.MAIN_FRAME]
    if not labeled:
        print("no labeled main-frame rows")
        return

    print("\n--- same_referent ---")
    print(f"{'value':<12} {'sample':>14} {'weighted population':>24}")
    for value in common.REFERENT_VALUES:
        k = sum(1 for r in labeled if r["same_referent"] == value)
        est, _num, _den = common.weighted_proportion(
            labeled, lambda r, v=value: r["same_referent"] == v, weights)
        lo, hi = common.bootstrap_weighted_ci(
            labeled, lambda r, v=value: r["same_referent"] == v, weights, seed=1)
        print(f"{value:<12} {k:>6}/{len(labeled):<6} {fmt_pct(est)}  [{fmt_pct(lo)},{fmt_pct(hi)}]")

    print("\n--- relation_label ---")
    for value in common.RELATION_LABELS:
        k = sum(1 for r in labeled if r["relation_label"] == value)
        est, _n, _d = common.weighted_proportion(
            labeled, lambda r, v=value: r["relation_label"] == v, weights)
        print(f"{value:<16} {k:>6}/{len(labeled):<6} weighted={fmt_pct(est)}")

    print("\n--- conflict_type (only where same_referent=no) ---")
    negatives = [r for r in labeled if r["same_referent"] == "no"]
    if negatives:
        counts = Counter(r["conflict_type"] for r in negatives)
        for value, k in counts.most_common():
            print(f"{str(value):<16} {k:>4}/{len(negatives)}  ({100.0*k/len(negatives):.1f}% of negatives)")
        obvious = sum(1 for r in negatives if r["conflict_type"] in
                      ("location", "person_entity", "object_facility", "chronology"))
        print(f"\n  -> {pct_ci(obvious, len(negatives))} of different-referent pairs have an")
        print("     EXPLICIT conflict visible in the shown text. This directly sizes a")
        print("     cheap deterministic conflict rule WITHOUT building one.")
    else:
        print("  (none)")

    print("\n--- confidence ---")
    for value in common.CONFIDENCE_VALUES:
        k = sum(1 for r in labeled if r["confidence"] == value)
        print(f"{value:<10} {k:>4}/{len(labeled)}")

    print("\nNOTE: 'sample' columns are stratified counts and DO NOT reflect natural")
    print("prevalence. Only the weighted column estimates the population.")


def report_per_stratum(primary: list[dict], populations: dict, weights: dict) -> None:
    print()
    print("=" * 78)
    print("4. PER-STRATUM OUTCOMES (unweighted within stratum = decision-relevant)")
    print("=" * 78)
    print(f"{'stratum':<32} {'N_pop':>9} {'n':>4} {'w':>8} {'diff-referent':>22} {'positive rel':>22}")
    for stratum in common.STRATA:
        rows = [r for r in primary if r["stratum"] == stratum and r["status"] == "labeled"]
        n = len(rows)
        npop = populations.get(stratum, 0)
        w = weights.get(stratum, 0.0)
        if n == 0:
            print(f"{stratum:<32} {npop:>9} {0:>4} {'-':>8} {'-':>22} {'-':>22}")
            continue
        diff = sum(1 for r in rows if common.is_different_referent(r))
        pos = sum(1 for r in rows if common.is_positive_relation(r))
        print(f"{stratum:<32} {npop:>9} {n:>4} {w:>8.1f} {pct_ci(diff, n):>22} {pct_ci(pos, n):>22}")

    print("\n--- reject-zone certification (Wilson lower bound on P(different referent)) ---")
    print(f"target: lower bound >= {CERTIFY_BOUND:.2f}")
    for stratum in common.MAIN_STRATA:
        rows = [r for r in primary if r["stratum"] == stratum and r["status"] == "labeled"]
        n = len(rows)
        if n == 0:
            continue
        diff = sum(1 for r in rows if common.is_different_referent(r))
        lo, _hi = common.wilson_interval(diff, n)
        budget = common.max_errors_for_lower_bound(n, CERTIFY_BOUND)
        verdict = "CERTIFIED" if lo >= CERTIFY_BOUND else "not certified"
        print(f"  {stratum:<32} lower={100*lo:5.1f}%  errors={n-diff:>2}  "
              f"max_errors_allowed_at_n={budget:>2}  {verdict}")
    print("\n  'max_errors_allowed_at_n' says what this sample size can prove at all.")
    print("  If it is 0, the cell certifies 90% ONLY when flawless; one error kills it")
    print("  and you need a larger n, not a different conclusion.")


def report_weight_caveats(manifest: dict, sampled: Counter) -> None:
    """Наскільки ваги N_s/n_s можна вважати чесними.

    Ваги припускають, що n_s — випадкова підвибірка страти. Кап на повторне
    використання claim цього припущення трохи не дотримує: пари з "зайнятими"
    claims систематично пропускались. Якщо пропусків мало — ефект нехтовний;
    якщо багато — читача треба про це попередити, а не мовчати.
    """
    fill = manifest.get("fill_stats") or {}
    if not fill:
        return
    print("\n--- weight validity (claim-use cap effect) ---")
    flagged = []
    for stratum, st in sorted(fill.items()):
        taken = st.get("taken", 0)
        skipped = st.get("skipped_by_cap", 0)
        if taken == 0:
            continue
        rate = skipped / (taken + skipped) if (taken + skipped) else 0.0
        marker = ""
        if rate > 0.5:
            marker = "  <-- weights approximate"
            flagged.append(stratum)
        print(f"  {stratum:<32} taken={taken:>3} cap-skipped={skipped:>4} "
              f"({100 * rate:4.0f}%){marker}")
    if flagged:
        print("\n  In the flagged strata the cap rejected most candidates, so the")
        print("  labeled rows are a random sample CONDITIONAL ON the cap, not a plain")
        print("  random sample of the stratum. Weighted estimates there carry an extra,")
        print("  unquantified bias (pairs built from frequently-recurring claims are")
        print("  under-represented). Treat those population numbers as indicative.")
    else:
        print("  cap rejected only a minority everywhere; weights are sound")


def report_2d(primary: list[dict], weights: dict) -> None:
    print()
    print("=" * 78)
    print("5. 2D BINS: claim_score x content_score (main frame, labeled)")
    print("=" * 78)
    labeled = [r for r in primary if r["status"] == "labeled" and r["frame"] == common.MAIN_FRAME]

    def cell_rows(r_i: int, c_i: int) -> list[dict]:
        return [r for r in labeled
                if bin_index(r["claim_score"], CLAIM_BINS) == r_i
                and bin_index(r["content_score"], CONTENT_BINS) == c_i]

    print("\n--- sample count (raw) ---")
    corner = "claim x content"
    header = f"{corner:<18}" + "".join(
        f"{bin_label(CONTENT_BINS, c):>14}" for c in range(len(CONTENT_BINS)))
    print(header)
    for r_i in range(len(CLAIM_BINS)):
        cells = [f"{len(cell_rows(r_i, c_i)):>14}" for c_i in range(len(CONTENT_BINS))]
        print(f"{bin_label(CLAIM_BINS, r_i):<18}" + "".join(cells))

    for title, predicate in (("different referent", common.is_different_referent),
                             ("positive relation", common.is_positive_relation)):
        print(f"\n--- {title}: raw k/n  and  (weighted %) ---")
        print(header)
        for r_i in range(len(CLAIM_BINS)):
            cells = []
            for c_i in range(len(CONTENT_BINS)):
                rows = cell_rows(r_i, c_i)
                if not rows:
                    cells.append(f"{'-':>14}")
                    continue
                k = sum(1 for r in rows if predicate(r))
                est, _n, _d = common.weighted_proportion(rows, predicate, weights)
                est_s = "  n/a" if est != est else f"{100 * est:.0f}%"
                cells.append(f"{k}/{len(rows)} ({est_s})".rjust(14))
            print(f"{bin_label(CLAIM_BINS, r_i):<18}" + "".join(cells))

    # Скільки страт змішується в одній 2D-комірці — це і є міра того,
    # наскільки сирий k/n у цій комірці можна читати без ваг.
    mixed = []
    for r_i in range(len(CLAIM_BINS)):
        for c_i in range(len(CONTENT_BINS)):
            rows = cell_rows(r_i, c_i)
            strata = {r["stratum"] for r in rows}
            if len(strata) > 1:
                mixed.append((bin_label(CLAIM_BINS, r_i), bin_label(CONTENT_BINS, c_i), sorted(strata)))

    print("\nHOW TO READ THIS TABLE:")
    print("  Raw k/n is a count inside the SAMPLE, not a population rate. 2D bins do")
    print("  not coincide with strata (S7 near-dup and S8 hub x hub cut across them),")
    print("  so a cell can mix strata sampled at very different rates. The weighted %")
    print("  in parentheses corrects for that and is the number to quote.")
    if mixed:
        print(f"\n  cells mixing >1 stratum (raw k/n there is biased): {len(mixed)}")
        for claim_lab, content_lab, strata in mixed[:6]:
            print(f"    claim {claim_lab} x content {content_lab}: {', '.join(s.split('_')[0] for s in strata)}")
    else:
        print("\n  no cell mixes strata in this sample; raw and weighted agree by construction")


def report_rules(primary: list[dict], weights: dict) -> None:
    print()
    print("=" * 78)
    print("6. SIMPLE DETERMINISTIC RULES (weighted precision / recall)")
    print("=" * 78)
    labeled = [r for r in primary if r["status"] == "labeled" and r["frame"] == common.MAIN_FRAME]
    if not labeled:
        print("no labeled rows")
        return

    total_pos_weight = sum(weights.get(r["stratum"], 0.0) for r in labeled if common.is_positive_relation(r))
    if total_pos_weight <= 0:
        print("no positive relations in the labeled set — cannot compute recall")

    def evaluate(name: str, accept) -> None:
        acc = [r for r in labeled if accept(r)]
        if not acc:
            print(f"{name:<44} accepts nothing")
            return
        w_acc = sum(weights.get(r["stratum"], 0.0) for r in acc)
        w_acc_pos = sum(weights.get(r["stratum"], 0.0) for r in acc if common.is_positive_relation(r))
        precision = w_acc_pos / w_acc if w_acc else float("nan")
        recall = w_acc_pos / total_pos_weight if total_pos_weight else float("nan")
        n_pos = sum(1 for r in acc if common.is_positive_relation(r))
        print(f"{name:<44} P={fmt_pct(precision)} R={fmt_pct(recall)} "
              f"(n={len(acc)}, pos={n_pos}, est_pairs={w_acc:,.0f})")

    print("\n--- baseline: current retrieval ---")
    evaluate("claim>=0.80 (candidate v1 retrieval)", lambda r: r["claim_score"] >= 0.80)

    print("\n--- claim>=0.80 + content threshold ---")
    for t in CONTENT_THRESHOLDS:
        evaluate(f"claim>=0.80 AND content>={t:.2f}",
                 lambda r, t=t: r["claim_score"] >= 0.80 and r["content_score"] >= t)

    print("\n--- hub suppression ---")
    evaluate("claim>=0.80 AND NOT(hub_a AND hub_b)",
             lambda r: r["claim_score"] >= 0.80 and not (r["hub_a"] and r["hub_b"]))
    evaluate("claim>=0.80 AND NOT(hub_a AND hub_b) AND content>=0.50",
             lambda r: r["claim_score"] >= 0.80 and not (r["hub_a"] and r["hub_b"])
             and r["content_score"] >= 0.50)

    print("\n--- content-led retrieval (tests the 0.80 floor itself) ---")
    for t in (0.60, 0.65, 0.70):
        evaluate(f"content>={t:.2f} AND claim>=0.65",
                 lambda r, t=t: r["content_score"] >= t and r["claim_score"] >= 0.65)
    evaluate("claim>=0.65 (lower retrieval floor)", lambda r: r["claim_score"] >= 0.65)

    print("\nRecall is measured RELATIVE TO the sampled frame (cross-source-group,")
    print("claim>=0.50). Pairs below claim 0.50 were never sampled, so absolute")
    print("recall over the whole corpus is NOT estimated here — see report section 7.")


def report_recall_loss(primary: list[dict], weights: dict) -> None:
    print()
    print("=" * 78)
    print("7. WHAT THE 0.80 RETRIEVAL FLOOR MISSES")
    print("=" * 78)
    labeled = [r for r in primary if r["status"] == "labeled" and r["frame"] == common.MAIN_FRAME]
    below = [r for r in labeled if r["claim_score"] < 0.80]
    if not below:
        print("no labeled pairs below claim 0.80")
        return
    pos_below = [r for r in below if common.is_positive_relation(r)]
    print(f"labeled pairs with claim<0.80 : {len(below)}")
    print(f"  of which positive relation  : {pct_ci(len(pos_below), len(below))}")

    w_pos_below = sum(weights.get(r["stratum"], 0.0) for r in pos_below)
    w_pos_all = sum(weights.get(r["stratum"], 0.0) for r in labeled if common.is_positive_relation(r))
    share = w_pos_below / w_pos_all if w_pos_all else float("nan")
    print(f"\n  weighted share of ALL positive relations that sit below claim 0.80:")
    print(f"    {fmt_pct(share)}  (estimated {w_pos_below:,.0f} of {w_pos_all:,.0f} pairs)")
    print("\n  This is the headline recall answer: if it is small, the 0.80 floor is")
    print("  defensible; if it is large, claim cosine is the wrong retrieval signal.")

    by_stratum = defaultdict(list)
    for r in below:
        by_stratum[r["stratum"]].append(r)
    print("\n  breakdown by stratum:")
    for stratum, rows in sorted(by_stratum.items()):
        pos = sum(1 for r in rows if common.is_positive_relation(r))
        print(f"    {stratum:<32} {pct_ci(pos, len(rows))}")

    print("\n  LIMITATION (must be stated in any writeup): pairs with claim<0.50 were")
    print("  never sampled. Positive relations there are invisible to this instrument,")
    print("  so the number above is a LOWER BOUND on what the 0.80 floor misses.")


def report_signal_comparison(primary: list[dict]) -> None:
    print()
    print("=" * 78)
    print("8. IS content_score AN INDEPENDENT SIGNAL?")
    print("=" * 78)
    labeled = [r for r in primary if r["status"] == "labeled" and r["frame"] == common.MAIN_FRAME]
    pos = [r for r in labeled if common.is_positive_relation(r)]
    neg = [r for r in labeled if common.is_different_referent(r)]
    if not pos or not neg:
        print("need both positive and different-referent rows")
        return

    def summarize(name: str, rows: list[dict], field: str) -> None:
        values = sorted(r[field] for r in rows)
        n = len(values)
        med = values[n // 2]
        p10 = values[max(0, int(0.10 * (n - 1)))]
        p90 = values[min(n - 1, int(0.90 * (n - 1)))]
        print(f"  {name:<28} n={n:<4} p10={p10:.3f} median={med:.3f} p90={p90:.3f}")

    print("\nclaim_score:")
    summarize("positive relation", pos, "claim_score")
    summarize("different referent", neg, "claim_score")
    print("\ncontent_score:")
    summarize("positive relation", pos, "content_score")
    summarize("different referent", neg, "content_score")

    # AUC (Mann-Whitney) для кожного сигналу окремо: наскільки він взагалі
    # відділяє позитив від різного референта. Без навчання моделі.
    def auc(field: str) -> float:
        p = [r[field] for r in pos]
        q = [r[field] for r in neg]
        wins = sum(1 for x in p for y in q if x > y)
        ties = sum(1 for x in p for y in q if x == y)
        return (wins + 0.5 * ties) / (len(p) * len(q))

    print(f"\nseparation AUC (positive vs different-referent), SAMPLE-LEVEL:")
    print(f"  claim_score   AUC = {auc('claim_score'):.3f}")
    print(f"  content_score AUC = {auc('content_score'):.3f}")
    print("\n  AUC 0.5 = no signal. CAVEAT: this AUC is computed on the stratified")
    print("  sample without weights, so its MAGNITUDE is not a population estimate")
    print("  (strata are sampled at very different rates by design). What is robust")
    print("  is the DIRECTION and the gap: if content_score clearly beats claim_score")
    print("  here, the dual-score hypothesis is supported by labels, not anecdotes.")
    print("  The per-stratum tables in section 4 are the unbiased view.")


def report_exploratory(primary: list[dict]) -> None:
    print()
    print("=" * 78)
    print("9. EXPLORATORY FRAME (same source-group) — REPORTED SEPARATELY")
    print("=" * 78)
    rows = [r for r in primary if r["frame"] == common.EXPLORATORY_FRAME and r["status"] == "labeled"]
    if not rows:
        print("not sampled or not labeled")
        return
    pos = sum(1 for r in rows if common.is_positive_relation(r))
    diff = sum(1 for r in rows if common.is_different_referent(r))
    print(f"labeled same-group pairs : {len(rows)}")
    print(f"  positive relation      : {pct_ci(pos, len(rows))}")
    print(f"  different referent     : {pct_ci(diff, len(rows))}")
    print("\n  These rows are NOT part of the main population and were excluded from")
    print("  every weighted estimate above. They answer one question only: does")
    print("  cross-source-only eligibility discard usable within-group relations?")
    print("  A high positive rate here means the pilot scope is costing real events.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Relation Gold Set v1 human labels")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)

    dataset = load_jsonl(args.dataset)
    annotations = load_jsonl(args.annotations)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))

    if dataset and dataset[0].get("schema_version") != common.SCHEMA_VERSION:
        print(f"WARNING: dataset schema_version={dataset[0].get('schema_version')} "
              f"expected {common.SCHEMA_VERSION}")

    joined, problems = join_annotations(dataset, annotations)
    primary, repeat_pairs = split_repeats(joined)

    populations = manifest.get("populations", {})
    sampled = Counter(r["stratum"] for r in primary if r["status"] == "labeled")
    weights = common.stratum_weights(populations, sampled)

    print(f"Relation Gold Set v1 analysis")
    print(f"seed={manifest.get('seed')} code_revision={manifest.get('code_revision')}")
    print(f"embedding_model_id={manifest.get('embedding_model_id')}")
    print(f"time_signal: {manifest.get('time_signal')}")

    report_completeness(dataset, joined, problems, primary, repeat_pairs)
    report_agreement(agreement_stats(repeat_pairs))
    report_distributions(primary, weights, populations)
    report_per_stratum(primary, populations, weights)
    report_weight_caveats(manifest, sampled)
    report_2d(primary, weights)
    report_rules(primary, weights)
    report_recall_loss(primary, weights)
    report_signal_comparison(primary)
    report_exploratory(primary)

    print()
    print("=" * 78)
    print("DONE. No DB writes, no LLM calls, no model training.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
