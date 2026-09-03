#!/usr/bin/env python3
"""
Relation Gold Set v1 — аналіз людської розмітки.

Вхід:
  --dataset     gold_set_v1_dataset.jsonl
  --manifest    gold_set_v1_manifest.json
  --annotations gold_set_v1_annotations.jsonl

Нічого не пише в БД, не викликає LLM, не тренує моделей.

Методологічна рамка:
- стратифікована вибірка не відображає natural prevalence;
- population-like estimates використовують stratum weights N_s/n_s;
- sampled envelope НЕ є всім cross-group простором і НЕ дає absolute recall;
- S4/S5/S6 — recall probes нижче поточного claim>=0.80 floor;
- exploratory frame ніколи не змішується з main frame.
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
# Dataset identity / loading / contract validation
# ---------------------------------------------------------------------------

def dataset_fingerprint(dataset: list[dict]) -> str:
    """Same FNV-1a64 identity as the standalone HTML labeler.

    Identity is not a security primitive. It scopes browser progress and prevents
    annotations for i0000 from silently attaching to a different generated set.
    pair_key is validated separately, so a hash collision cannot silently misjoin.
    """
    text = "\x1e".join(
        f"{row.get('schema_version', '')}\x1f{row.get('interaction_id', '')}\x1f{row.get('pair_key', '')}"
        for row in dataset
    )
    h = 14695981039346656037
    mask = (1 << 64) - 1
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 1099511628211) & mask
    return f"fnv1a64-{h:016x}"


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
    """Join by interaction_id only after dataset_id AND pair_key match."""
    by_interaction = {row["interaction_id"]: row for row in dataset}
    expected_dataset_id = dataset_fingerprint(dataset)
    ann_by_interaction: dict[str, dict] = {}
    problems = {
        "unknown_interaction": [],
        "duplicate_annotation": [],
        "dataset_mismatch": [],
        "pair_mismatch": [],
        "contract_errors": [],
    }

    for ann in annotations:
        iid = ann.get("interaction_id")
        if iid not in by_interaction:
            problems["unknown_interaction"].append(iid)
            continue
        if iid in ann_by_interaction:
            problems["duplicate_annotation"].append(iid)
            continue
        if ann.get("dataset_id") != expected_dataset_id:
            problems["dataset_mismatch"].append(iid)
            continue
        row = by_interaction[iid]
        if ann.get("pair_key") != row.get("pair_key"):
            problems["pair_mismatch"].append(iid)
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
# Hidden repeats / self-consistency
# ---------------------------------------------------------------------------

def split_repeats(joined: list[dict]) -> tuple[list[dict], list[tuple[dict, dict]]]:
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
    def _agree(field: str) -> dict:
        usable = [(a, b) for a, b in repeat_pairs
                  if a["status"] == "labeled" and b["status"] == "labeled"]
        n = len(usable)
        if n == 0:
            return {"n": 0, "raw": float("nan"), "kappa": float("nan"), "disagreements": []}
        hits = sum(1 for a, b in usable if a[field] == b[field])
        raw = hits / n
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
# Helpers
# ---------------------------------------------------------------------------

def bin_index(value: float, bins) -> int:
    for i, (lo, hi) in enumerate(bins):
        if lo <= value < hi:
            return i
    return len(bins) - 1


def bin_label(bins, i: int) -> str:
    lo, hi = bins[i]
    if lo <= -1.0:
        return f"<{hi:.2f}"
    return f"[{lo:.2f},{hi:.2f})"


def fmt_pct(value: float) -> str:
    return "  n/a " if value != value else f"{100.0 * value:5.1f}%"


def pct_ci(successes: int, total: int) -> str:
    if total == 0:
        return "n=0"
    lo, hi = common.wilson_interval(successes, total)
    return f"{successes}/{total} = {100.0*successes/total:5.1f}% [{100*lo:.1f},{100*hi:.1f}]"


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def report_completeness(dataset, joined, problems, primary, repeat_pairs) -> None:
    print("=" * 78)
    print("1. COMPLETENESS / CONTRACT")
    print("=" * 78)
    total_interactions = len(dataset)
    expected_unique = sum(1 for r in dataset if not r["is_repeat"])
    expected_repeats = total_interactions - expected_unique
    print(f"dataset_id                : {dataset_fingerprint(dataset)}")
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
        print("  contract: OK (dataset_id/pair_key/schema joins are consistent)")

    labeled = [r for r in primary if r["status"] == "labeled"]
    claims = Counter()
    for r in labeled:
        claims[r["claim_id_a"]] += 1
        claims[r["claim_id_b"]] += 1
    if labeled:
        worst = claims.most_common(5)
        print(f"\n  distinct claims behind {len(labeled)} labeled pairs: {len(claims)}")
        print("  most reused claims: " + ", ".join(f"{c[:8]}…x{n}" for c, n in worst))
        if worst and worst[0][1] >= 6:
            print("  WARNING: repeated claims induce clustered observations; plain pair-level")
            print("  Wilson intervals may be optimistic in affected strata.")

    n_unusable = len(unusable)
    n_labeled = len(labeled)
    if n_labeled and n_unusable / (n_labeled + n_unusable) > 0.10:
        print(f"\n  WARNING: {n_unusable} unusable of {n_labeled + n_unusable} "
              f"({100.0 * n_unusable / (n_labeled + n_unusable):.0f}%).")


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
    print("\n  NOTE: repeats have A/B swapped, so this also tests order-invariance.")


def report_distributions(primary: list[dict], weights: dict, populations: dict) -> None:
    print()
    print("=" * 78)
    print("3. LABEL DISTRIBUTION — sample vs weighted sampled-envelope estimate")
    print("=" * 78)
    labeled = [r for r in primary if r["status"] == "labeled" and r["frame"] == common.MAIN_FRAME]
    if not labeled:
        print("no labeled main-frame rows")
        return

    print("\n--- same_referent ---")
    print(f"{'value':<12} {'sample':>14} {'weighted envelope':>24}")
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
        print(f"\n  -> {pct_ci(obvious, len(negatives))} of sampled different-referent pairs")
        print("     have an explicit visible conflict — candidate evidence for a cheap")
        print("     deterministic negative rule, not yet a production threshold.")
    else:
        print("  (none)")

    print("\n--- confidence ---")
    for value in common.CONFIDENCE_VALUES:
        k = sum(1 for r in labeled if r["confidence"] == value)
        print(f"{value:<10} {k:>4}/{len(labeled)}")

    print("\nNOTE: weighted numbers estimate only the explicitly sampled measurement")
    print("envelope. They are not prevalence over all 188.9M cross-group pairs.")


def report_per_stratum(primary: list[dict], populations: dict, weights: dict) -> None:
    print()
    print("=" * 78)
    print("4. PER-STRATUM OUTCOMES")
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

    print("\n--- reject-zone evidence (Wilson lower bound on P(different referent)) ---")
    print(f"target for independent-pair certification: lower bound >= {CERTIFY_BOUND:.2f}")
    for stratum in common.MAIN_STRATA:
        rows = [r for r in primary if r["stratum"] == stratum and r["status"] == "labeled"]
        n = len(rows)
        if n == 0:
            continue
        diff = sum(1 for r in rows if common.is_different_referent(r))
        lo, _hi = common.wilson_interval(diff, n)
        budget = common.max_errors_for_lower_bound(n, CERTIFY_BOUND)
        if stratum == "S8_HUB_X_HUB":
            verdict = "SCREENING ONLY (hub-clustered pairs)"
        else:
            verdict = "CERTIFIED" if lo >= CERTIFY_BOUND else "not certified"
        print(f"  {stratum:<32} lower={100*lo:5.1f}%  errors={n-diff:>2}  "
              f"max_errors_allowed_at_n={budget:>2}  {verdict}")
    print("\n  S8 is never called CERTIFIED by this analyzer: hub pairs are clustered by")
    print("  construction, so a plain Wilson interval over rows overstates independence.")


def report_weight_caveats(manifest: dict, sampled: Counter) -> None:
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
        if skipped:
            marker = "  <-- cap changed inclusion probabilities"
            flagged.append(stratum)
        print(f"  {stratum:<32} taken={taken:>3} cap-skipped={skipped:>4} "
              f"({100 * rate:4.0f}%){marker}")
    if flagged:
        print("\n  Any cap-skips make N_s/n_s weights approximate because inclusion is")
        print("  conditional on claim reuse. Prefer an uncapped inferential sample.")
    else:
        print("  no cap-skips: simple within-stratum inclusion weights are appropriate")


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
        print(f"\n--- {title}: raw k/n and weighted sampled-envelope % ---")
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

    print("\nHOW TO READ THIS TABLE:")
    print("  The sample is stratified on these signals. Raw k/n is descriptive only;")
    print("  weighted values correct stratum sampling rates within the sampled envelope.")


def report_rules(primary: list[dict], weights: dict) -> None:
    print()
    print("=" * 78)
    print("6. SIMPLE DETERMINISTIC RULE PROBES")
    print("=" * 78)
    labeled = [r for r in primary if r["status"] == "labeled" and r["frame"] == common.MAIN_FRAME]
    if not labeled:
        print("no labeled rows")
        return

    total_pos_weight = sum(weights.get(r["stratum"], 0.0) for r in labeled if common.is_positive_relation(r))

    def evaluate(name: str, accept) -> None:
        acc = [r for r in labeled if accept(r)]
        if not acc:
            print(f"{name:<44} accepts nothing")
            return
        w_acc = sum(weights.get(r["stratum"], 0.0) for r in acc)
        w_acc_pos = sum(weights.get(r["stratum"], 0.0) for r in acc if common.is_positive_relation(r))
        precision = w_acc_pos / w_acc if w_acc else float("nan")
        recall_probe = w_acc_pos / total_pos_weight if total_pos_weight else float("nan")
        n_pos = sum(1 for r in acc if common.is_positive_relation(r))
        print(f"{name:<44} P={fmt_pct(precision)} envelope-R={fmt_pct(recall_probe)} "
              f"(n={len(acc)}, pos={n_pos}, est_pairs={w_acc:,.0f})")

    print("\n--- current retrieval region ---")
    evaluate("claim>=0.80", lambda r: r["claim_score"] >= 0.80)

    print("\n--- claim>=0.80 + content threshold ---")
    for t in CONTENT_THRESHOLDS:
        evaluate(f"claim>=0.80 AND content>={t:.2f}",
                 lambda r, t=t: r["claim_score"] >= 0.80 and r["content_score"] >= t)

    print("\n--- hub suppression ---")
    evaluate("claim>=0.80 AND NOT(hub_a AND hub_b)",
             lambda r: r["claim_score"] >= 0.80 and not (r["hub_a"] and r["hub_b"]))

    print("\n--- below-floor retrieval probes ---")
    for t in (0.60, 0.65, 0.70):
        evaluate(f"content>={t:.2f} AND claim>=0.65",
                 lambda r, t=t: r["content_score"] >= t and r["claim_score"] >= 0.65)

    print("\n'envelope-R' is recall only relative to this sampled measurement envelope.")
    print("It is NOT absolute corpus recall and must not be quoted as such.")


def report_recall_loss(primary: list[dict], weights: dict) -> None:
    print()
    print("=" * 78)
    print("7. BELOW-0.80 RECALL PROBES")
    print("=" * 78)
    labeled = [r for r in primary if r["status"] == "labeled" and r["frame"] == common.MAIN_FRAME]
    below = [r for r in labeled if r["claim_score"] < 0.80]
    if not below:
        print("no labeled probe pairs below claim 0.80")
        return
    pos_below = [r for r in below if common.is_positive_relation(r)]
    print(f"labeled probe pairs with claim<0.80 : {len(below)}")
    print(f"  positive relation in probes        : {pct_ci(len(pos_below), len(below))}")

    w_pos_below = sum(weights.get(r["stratum"], 0.0) for r in pos_below)
    w_pos_envelope = sum(weights.get(r["stratum"], 0.0) for r in labeled if common.is_positive_relation(r))
    share = w_pos_below / w_pos_envelope if w_pos_envelope else float("nan")
    print("\n  weighted share of positive relations INSIDE THE SAMPLED ENVELOPE")
    print(f"  contributed by below-0.80 probe strata: {fmt_pct(share)}")
    print(f"  (estimated {w_pos_below:,.0f} of {w_pos_envelope:,.0f} envelope pairs)")

    by_stratum = defaultdict(list)
    for r in below:
        by_stratum[r["stratum"]].append(r)
    print("\n  breakdown by recall-probe stratum:")
    for stratum, rows in sorted(by_stratum.items()):
        pos = sum(1 for r in rows if common.is_positive_relation(r))
        print(f"    {stratum:<32} {pct_ci(pos, len(rows))}")

    print("\n  INTERPRETATION:")
    print("  * any credible positive in S4/S5/S6 proves that claim>=0.80 misses some")
    print("    usable relations and tells us where to investigate retrieval expansion;")
    print("  * absence of positives in these probes does NOT prove high absolute recall;")
    print("  * unsampled regions include claim 0.65-0.80 with content<0.50,")
    print("    claim 0.50-0.65 with content<0.70, and every pair with claim<0.50.")
    print("  Therefore this instrument cannot estimate absolute corpus recall.")


def report_signal_comparison(primary: list[dict]) -> None:
    print()
    print("=" * 78)
    print("8. CLAIM_SCORE vs CONTENT_SCORE — DIAGNOSTIC ONLY")
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

    def auc(field: str) -> float:
        p = [r[field] for r in pos]
        q = [r[field] for r in neg]
        wins = sum(1 for x in p for y in q if x > y)
        ties = sum(1 for x in p for y in q if x == y)
        return (wins + 0.5 * ties) / (len(p) * len(q))

    print("\nseparation AUC on the STRATIFIED SAMPLE (descriptive, not inferential):")
    print(f"  claim_score   AUC = {auc('claim_score'):.3f}")
    print(f"  content_score AUC = {auc('content_score'):.3f}")
    print("\n  IMPORTANT: the sample was deliberately stratified using claim/content")
    print("  scores, so neither the AUC magnitude nor the gap proves that content is an")
    print("  independent discriminator. Incremental value must be judged within narrow")
    print("  claim-score regions / strata and by the labeled error patterns.")


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
    print("\n  These rows are not part of the main envelope and are never mixed into")
    print("  weighted main-frame estimates.")


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

    print("Relation Gold Set v1 analysis")
    print(f"dataset_id={dataset_fingerprint(dataset)}")
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
