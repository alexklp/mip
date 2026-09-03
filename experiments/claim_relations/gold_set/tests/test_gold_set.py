#!/usr/bin/env python3
"""
Тести Relation Gold Set v1. Тільки stdlib unittest + numpy (без pytest).

Запуск:
    python3 -m unittest discover -s experiments/claim_relations/gold_set/tests -v
або
    python3 experiments/claim_relations/gold_set/tests/test_gold_set.py

Тести діляться на два рівні:

  A. Чиста логіка (gold_set_common) — стратифікація, детермінізм, порядок,
     повтори, ваги, статистика, контракт анотації. Жодних залежностей.

  B. Інтеграція семплера і аналізатора на СИНТЕТИЧНОМУ корпусі. Каталог
     tests/stubs тимчасово додається в sys.path, тому
     candidate_pair_profile / candidate_pair_dual_profile /
     relation_judgment_worker / psycopg / pgvector підміняються заглушками.
     Це дозволяє прогнати ВЕСЬ семплер end-to-end без PostgreSQL.
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[3]          # каталог, що містить experiments/
STUBS = HERE / "stubs"
for p in (str(STUBS), str(PKG_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from experiments.claim_relations.gold_set import gold_set_common as common  # noqa: E402


# ===========================================================================
# A. Чиста логіка
# ===========================================================================

class TestStratumPartition(unittest.TestCase):
    """Страти мають бути РОЗБИТТЯМ. Якби вони перетинались, ваги N_s/n_s
    рахували б одну пару двічі і всі population-оцінки поїхали б."""

    def _grid(self):
        claims = [0.40, 0.50, 0.55, 0.64, 0.65, 0.70, 0.79, 0.80, 0.85,
                  0.90, 0.96, 0.97, 1.00]
        contents = [-0.5, 0.0, 0.39, 0.40, 0.49, 0.50, 0.64, 0.65, 0.69, 0.70, 0.95]
        for c in claims:
            for d in contents:
                for ha in (False, True):
                    for hb in (False, True):
                        for sg in (False, True):
                            yield c, d, ha, hb, sg

    def test_returns_at_most_one_stratum(self):
        for c, d, ha, hb, sg in self._grid():
            s = common.assign_stratum(c, d, ha, hb, sg)
            self.assertTrue(s is None or s in common.STRATA,
                            f"unknown stratum {s!r} for {(c, d, ha, hb, sg)}")

    def test_below_floor_never_sampled(self):
        for d in (-1.0, 0.0, 0.5, 0.9, 1.0):
            self.assertIsNone(common.assign_stratum(0.49, d, False, False, False))
            self.assertIsNone(common.assign_stratum(0.49, d, True, True, True))

    def test_same_group_only_ever_exploratory(self):
        for c, d, ha, hb, _sg in self._grid():
            s = common.assign_stratum(c, d, ha, hb, same_group=True)
            if s is not None:
                self.assertEqual(s, "S9_SAME_GROUP_EXPLORATORY")
                self.assertGreaterEqual(c, common.CLAIM_HIGH)

    def test_main_frame_never_returns_exploratory(self):
        for c, d, ha, hb, _sg in self._grid():
            s = common.assign_stratum(c, d, ha, hb, same_group=False)
            if s is not None:
                self.assertEqual(common.STRATA[s]["frame"], common.MAIN_FRAME)

    def test_priority_neardup_beats_hub(self):
        # near-dup питання окреме і має пріоритет над hub x hub
        self.assertEqual(
            common.assign_stratum(0.98, 0.10, True, True, False), "S7_NEAR_DUP")
        self.assertEqual(
            common.assign_stratum(0.96, 0.10, True, True, False), "S8_HUB_X_HUB")

    def test_hub_requires_both_sides(self):
        self.assertEqual(
            common.assign_stratum(0.85, 0.10, True, False, False),
            "S1_HIGH_CLAIM_LOW_CONTENT")

    def test_boundaries_are_half_open(self):
        self.assertEqual(common.assign_stratum(0.80, 0.49, False, False, False),
                         "S1_HIGH_CLAIM_LOW_CONTENT")
        self.assertEqual(common.assign_stratum(0.80, 0.50, False, False, False),
                         "S2_HIGH_CLAIM_MID_CONTENT")
        self.assertEqual(common.assign_stratum(0.80, 0.65, False, False, False),
                         "S3_HIGH_CLAIM_HIGH_CONTENT")
        self.assertEqual(common.assign_stratum(0.79, 0.65, False, False, False),
                         "S4_MID_CLAIM_HIGH_CONTENT")
        self.assertEqual(common.assign_stratum(0.65, 0.50, False, False, False),
                         "S5_MID_CLAIM_MID_CONTENT")
        # claim 0.65-0.80 з низьким content свідомо НЕ семплюється
        self.assertIsNone(common.assign_stratum(0.70, 0.49, False, False, False))
        self.assertEqual(common.assign_stratum(0.64, 0.70, False, False, False),
                         "S6_LOW_CLAIM_HIGH_CONTENT")
        self.assertIsNone(common.assign_stratum(0.64, 0.69, False, False, False))


class TestPairKeyAndHash(unittest.TestCase):
    def test_pair_key_order_invariant(self):
        self.assertEqual(common.pair_key("bbb", "aaa"), common.pair_key("aaa", "bbb"))
        self.assertEqual(common.pair_key("aaa", "bbb"), "aaa|bbb")

    def test_stable_hash_is_process_stable(self):
        # Значення зафіксоване: якщо воно зміниться, зміниться і вся вибірка.
        self.assertEqual(common.stable_hash(1, "x"), common.stable_hash(1, "x"))
        self.assertNotEqual(common.stable_hash(1, "x"), common.stable_hash(2, "x"))


class TestStratumSelector(unittest.TestCase):
    def _items(self, n):
        return [{"pair_key": f"k{i:04d}", "stratum": "S1_HIGH_CLAIM_LOW_CONTENT",
                 "claim_id_a": f"a{i}", "claim_id_b": f"b{i}"} for i in range(n)]

    def test_population_counts_everything_not_only_selected(self):
        sel = common.StratumSelector("S1_HIGH_CLAIM_LOW_CONTENT", capacity=5, seed=1)
        for it in self._items(200):
            sel.offer(it["pair_key"], it)
        self.assertEqual(sel.population, 200)
        self.assertEqual(len(sel.selected()), 5)

    def test_selection_is_order_independent(self):
        """Це і є причина відмови від reservoir: результат не має залежати від
        порядку обходу блоків матриці (а він залежить від chunk_size)."""
        items = self._items(120)
        a = common.StratumSelector("S1_HIGH_CLAIM_LOW_CONTENT", 10, seed=42)
        for it in items:
            a.offer(it["pair_key"], it)
        shuffled = items[:]
        random.Random(9).shuffle(shuffled)
        b = common.StratumSelector("S1_HIGH_CLAIM_LOW_CONTENT", 10, seed=42)
        for it in shuffled:
            b.offer(it["pair_key"], it)
        self.assertEqual([i["pair_key"] for i in a.selected()],
                         [i["pair_key"] for i in b.selected()])

    def test_different_seed_changes_selection(self):
        items = self._items(120)
        out = []
        for seed in (1, 2):
            sel = common.StratumSelector("S1_HIGH_CLAIM_LOW_CONTENT", 10, seed=seed)
            for it in items:
                sel.offer(it["pair_key"], it)
            out.append([i["pair_key"] for i in sel.selected()])
        self.assertNotEqual(out[0], out[1])

    def test_zero_capacity_still_counts_population(self):
        sel = common.StratumSelector("S1_HIGH_CLAIM_LOW_CONTENT", 0, seed=1)
        for it in self._items(50):
            sel.offer(it["pair_key"], it)
        self.assertEqual(sel.population, 50)
        self.assertEqual(sel.selected(), [])


class TestClaimUseCap(unittest.TestCase):
    def test_caps_repeated_claims(self):
        items = [{"pair_key": f"p{i}", "stratum": "S1_HIGH_CLAIM_LOW_CONTENT",
                  "claim_id_a": "hub", "claim_id_b": f"other{i}"} for i in range(10)]
        kept, dropped = common.enforce_claim_use_cap(items, max_uses=2)
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(dropped), 8)

    def test_hub_stratum_is_exempt(self):
        items = [{"pair_key": f"p{i}", "stratum": "S8_HUB_X_HUB",
                  "claim_id_a": "hub", "claim_id_b": f"other{i}"} for i in range(10)]
        kept, dropped = common.enforce_claim_use_cap(
            items, max_uses=2, exempt_strata=("S8_HUB_X_HUB",))
        self.assertEqual(len(kept), 10)
        self.assertEqual(dropped, [])

    def test_deterministic(self):
        items = [{"pair_key": f"p{i}", "stratum": "S1_HIGH_CLAIM_LOW_CONTENT",
                  "claim_id_a": f"a{i%3}", "claim_id_b": f"b{i%4}"} for i in range(20)]
        k1, _ = common.enforce_claim_use_cap(items, 2)
        k2, _ = common.enforce_claim_use_cap(items, 2)
        self.assertEqual([i["pair_key"] for i in k1], [i["pair_key"] for i in k2])


class TestAnnotationOrder(unittest.TestCase):
    def _items(self):
        items = []
        alloc = {"S1_HIGH_CLAIM_LOW_CONTENT": 40, "S4_MID_CLAIM_HIGH_CONTENT": 40,
                 "S7_NEAR_DUP": 16, "S8_HUB_X_HUB": 20}
        for stratum, n in alloc.items():
            for i in range(n):
                items.append({"pair_key": f"{stratum}-{i}", "stratum": stratum,
                              "claim_id_a": f"a{stratum}{i}", "claim_id_b": f"b{stratum}{i}"})
        return items

    def test_all_items_present_exactly_once(self):
        items = self._items()
        ordered = common.build_annotation_order(items, seed=5)
        self.assertEqual(len(ordered), len(items))
        self.assertEqual({i["pair_key"] for i in ordered}, {i["pair_key"] for i in items})

    def test_deterministic(self):
        items = self._items()
        a = [i["pair_key"] for i in common.build_annotation_order(items, seed=5)]
        shuffled = items[:]
        random.Random(3).shuffle(shuffled)
        b = [i["pair_key"] for i in common.build_annotation_order(shuffled, seed=5)]
        self.assertEqual(a, b)

    def test_prefix_is_stratum_balanced(self):
        """Головна властивість: анотатор може зупинитись на будь-якому місці,
        і датасет лишається аналізовним. Перевіряємо, що на 50% послідовності
        кожна страта представлена приблизно наполовину."""
        items = self._items()
        ordered = common.build_annotation_order(items, seed=5)
        total = {}
        for it in items:
            total[it["stratum"]] = total.get(it["stratum"], 0) + 1
        half = ordered[: len(ordered) // 2]
        seen = {}
        for it in half:
            seen[it["stratum"]] = seen.get(it["stratum"], 0) + 1
        for stratum, n_total in total.items():
            got = seen.get(stratum, 0)
            expected = n_total * 0.5
            self.assertLess(abs(got - expected), max(3, 0.25 * expected),
                            f"{stratum}: {got} vs expected ~{expected}")

    def test_consecutive_items_usually_differ_in_stratum(self):
        """Порядок не має йти смугами — інакше анотатор відчуває режим."""
        items = self._items()
        ordered = common.build_annotation_order(items, seed=5)
        same_neighbour = sum(1 for i in range(1, len(ordered))
                             if ordered[i]["stratum"] == ordered[i - 1]["stratum"])
        self.assertLess(same_neighbour / len(ordered), 0.5)


class TestHiddenRepeats(unittest.TestCase):
    def _ordered(self, n=100):
        items = [{"pair_key": f"p{i:03d}",
                  "stratum": "S1_HIGH_CLAIM_LOW_CONTENT" if i % 2 else "S4_MID_CLAIM_HIGH_CONTENT",
                  "claim_id_a": f"a{i}", "claim_id_b": f"b{i}"} for i in range(n)]
        return common.build_annotation_order(items, seed=11)

    def test_accounting_unique_vs_repeats(self):
        """Критично для аналізу: унікальна популяція не має бути роздута
        повторами. Рівно n унікальних + rate*n повторів."""
        ordered = self._ordered(100)
        inter = common.plan_hidden_repeats(ordered, 0.12, seed=11, min_separation=30)
        uniques = [i for i in inter if not i["is_repeat"]]
        repeats = [i for i in inter if i["is_repeat"]]
        self.assertEqual(len(uniques), 100)
        self.assertEqual(len(repeats), 12)
        self.assertEqual(len(inter), 112)
        self.assertEqual(len({i["pair_key"] for i in uniques}), 100)

    def test_every_repeat_points_at_a_real_original(self):
        ordered = self._ordered(80)
        inter = common.plan_hidden_repeats(ordered, 0.15, seed=3, min_separation=25)
        by_id = {i["interaction_id"]: i for i in inter}
        for entry in inter:
            if entry["is_repeat"]:
                self.assertIn(entry["repeat_of"], by_id)
                original = by_id[entry["repeat_of"]]
                self.assertFalse(original["is_repeat"])
                self.assertEqual(original["pair_key"], entry["pair_key"])

    def test_repeats_are_separated(self):
        ordered = self._ordered(120)
        inter = common.plan_hidden_repeats(ordered, 0.12, seed=7, min_separation=40)
        seps = common.repeat_separation(inter)
        self.assertTrue(seps)
        self.assertGreaterEqual(min(seps.values()), 40)

    def test_repeats_are_presented_swapped(self):
        ordered = self._ordered(60)
        inter = common.plan_hidden_repeats(ordered, 0.2, seed=2, min_separation=20)
        for entry in inter:
            if entry["is_repeat"]:
                self.assertTrue(entry["presented_swapped"])

    def test_repeat_side_swap_is_opposite_of_original(self):
        for key in ("a|b", "x|y", "q|r", "m|n"):
            first = common.side_swap_for_presentation(99, key, is_repeat=False)
            second = common.side_swap_for_presentation(99, key, is_repeat=True)
            self.assertNotEqual(first, second)

    def test_repeats_are_interleaved_not_dumped_at_the_end(self):
        """Суцільний блок повторів наприкінці — сам по собі підказка
        ('остання сотня якась знайома'). Повтори мають бути розпорошені."""
        ordered = self._ordered(120)
        inter = common.plan_hidden_repeats(ordered, 0.12, seed=7, min_separation=30)
        n_rep = sum(1 for i in inter if i["is_repeat"])
        tail = inter[-n_rep:]
        in_tail = sum(1 for i in tail if i["is_repeat"])
        self.assertLess(in_tail, n_rep,
                        "all repeats landed in one contiguous block at the end")
        positions = [i for i, e in enumerate(inter) if e["is_repeat"]]
        self.assertGreater(max(positions) - min(positions), n_rep,
                           "repeats are not spread out")

    def test_repeat_originals_come_from_early_positions(self):
        """Наслідок вимоги min_separation: повторюватись можуть лише пари,
        для яких відокремлений повтор фізично влазить."""
        ordered = self._ordered(100)
        inter = common.plan_hidden_repeats(ordered, 0.12, seed=7, min_separation=40)
        pos = {e["interaction_id"]: i for i, e in enumerate(inter)}
        for e in inter:
            if e["is_repeat"]:
                self.assertLess(pos[e["repeat_of"]], len(inter) - 40)

    def test_repeats_spread_across_strata(self):
        """Якби повтори бралися лише з легких комірок, self-consistency була б
        оптимістично зміщена."""
        ordered = self._ordered(100)
        inter = common.plan_hidden_repeats(ordered, 0.2, seed=5, min_separation=20)
        strata = {i["stratum"] for i in inter if i["is_repeat"]}
        self.assertGreaterEqual(len(strata), 2)

    def test_zero_rate(self):
        ordered = self._ordered(20)
        inter = common.plan_hidden_repeats(ordered, 0.0, seed=1, min_separation=5)
        self.assertEqual(len(inter), 20)
        self.assertFalse(any(i["is_repeat"] for i in inter))


class TestWeighting(unittest.TestCase):
    def test_weights(self):
        w = common.stratum_weights({"S1_HIGH_CLAIM_LOW_CONTENT": 1900,
                                    "S7_NEAR_DUP": 61},
                                   {"S1_HIGH_CLAIM_LOW_CONTENT": 40, "S7_NEAR_DUP": 16})
        self.assertAlmostEqual(w["S1_HIGH_CLAIM_LOW_CONTENT"], 47.5)
        self.assertAlmostEqual(w["S7_NEAR_DUP"], 3.8125)

    def test_weighted_proportion_differs_from_naive(self):
        """Суть проблеми: near-dup смуга крихітна в популяції, але велика у
        вибірці. Наївна частка переоцінила б її у ~12 разів."""
        rows = ([{"stratum": "S1_HIGH_CLAIM_LOW_CONTENT", "good": False}] * 40 +
                [{"stratum": "S7_NEAR_DUP", "good": True}] * 16)
        weights = common.stratum_weights(
            {"S1_HIGH_CLAIM_LOW_CONTENT": 1900, "S7_NEAR_DUP": 61},
            {"S1_HIGH_CLAIM_LOW_CONTENT": 40, "S7_NEAR_DUP": 16})
        naive = sum(1 for r in rows if r["good"]) / len(rows)
        est, _n, _d = common.weighted_proportion(rows, lambda r: r["good"], weights)
        self.assertAlmostEqual(naive, 16 / 56)                  # ~0.286
        self.assertAlmostEqual(est, 61 / (1900 + 61), places=6)  # ~0.031
        self.assertLess(est, naive / 5)

    def test_exploratory_excluded_from_main_estimates(self):
        rows = ([{"stratum": "S1_HIGH_CLAIM_LOW_CONTENT", "good": False}] * 10 +
                [{"stratum": "S9_SAME_GROUP_EXPLORATORY", "good": True}] * 10)
        weights = common.stratum_weights(
            {"S1_HIGH_CLAIM_LOW_CONTENT": 100, "S9_SAME_GROUP_EXPLORATORY": 100},
            {"S1_HIGH_CLAIM_LOW_CONTENT": 10, "S9_SAME_GROUP_EXPLORATORY": 10})
        est, _n, _d = common.weighted_proportion(rows, lambda r: r["good"], weights)
        self.assertEqual(est, 0.0)

    def test_bootstrap_ci_brackets_estimate(self):
        rows = [{"stratum": "S1_HIGH_CLAIM_LOW_CONTENT", "good": i < 30} for i in range(40)]
        weights = {"S1_HIGH_CLAIM_LOW_CONTENT": 47.5}
        est, _n, _d = common.weighted_proportion(rows, lambda r: r["good"], weights)
        lo, hi = common.bootstrap_weighted_ci(rows, lambda r: r["good"], weights,
                                              seed=1, iterations=400)
        self.assertLessEqual(lo, est)
        self.assertGreaterEqual(hi, est)


class TestStatistics(unittest.TestCase):
    def test_wilson_known_values(self):
        lo, hi = common.wilson_interval(40, 40)
        self.assertAlmostEqual(lo, 0.9124, places=3)
        self.assertAlmostEqual(hi, 1.0, places=6)
        lo2, _ = common.wilson_interval(39, 40)
        self.assertAlmostEqual(lo2, 0.8712, places=3)

    def test_error_budget_matches_design_report(self):
        """Ці числа цитуються у звіті як обґрунтування розмірів страт.

        Ключовий факт, який визначив дизайн: n<35 НЕ здатне сертифікувати 90%
        навіть при нулі помилок (-1 = неможливо). Саме тому кожна
        decision_critical страта має >= 40, а не 20.
        """
        self.assertEqual(common.max_errors_for_lower_bound(20, 0.90), -1)
        self.assertEqual(common.max_errors_for_lower_bound(30, 0.90), -1)
        self.assertEqual(common.max_errors_for_lower_bound(34, 0.90), -1)
        self.assertEqual(common.max_errors_for_lower_bound(35, 0.90), 0)
        self.assertEqual(common.max_errors_for_lower_bound(40, 0.90), 0)
        self.assertEqual(common.max_errors_for_lower_bound(60, 0.90), 1)
        self.assertEqual(common.max_errors_for_lower_bound(100, 0.90), 4)

    def test_min_n_constant_is_consistent(self):
        n = common.MIN_N_TO_CERTIFY_90
        self.assertEqual(common.max_errors_for_lower_bound(n, 0.90), 0)
        self.assertEqual(common.max_errors_for_lower_bound(n - 1, 0.90), -1)

    def test_every_decision_critical_stratum_can_certify(self):
        """Захист від регресії дизайну: якщо хтось знизить allocation
        критичної страти нижче 35, тест впаде."""
        for stratum, cfg in common.STRATA.items():
            if cfg["decision_critical"]:
                n = common.DEFAULT_ALLOCATION[stratum]
                self.assertGreaterEqual(
                    n, common.MIN_N_TO_CERTIFY_90,
                    f"{stratum} is decision-critical but n={n} cannot certify 90%")

    def test_wilson_empty(self):
        lo, hi = common.wilson_interval(0, 0)
        self.assertNotEqual(lo, lo)  # NaN


class TestAnnotationContract(unittest.TestCase):
    def test_referent_gate_mirrors_production(self):
        """same_fact/same_event/contradiction без підтвердженого референта
        заборонені — так само, як у validate_relation_judgment і sql/014."""
        for label in ("same_fact", "same_event", "contradiction"):
            errs = common.validate_annotation(
                {"same_referent": "no", "relation_label": label, "confidence": "high",
                 "conflict_type": "location"})
            self.assertTrue(errs, f"{label} must be rejected when referent=no")

    def test_valid_records(self):
        self.assertEqual(common.validate_annotation(
            {"same_referent": "yes", "relation_label": "same_event",
             "confidence": "high"}), [])
        self.assertEqual(common.validate_annotation(
            {"same_referent": "no", "relation_label": "unrelated",
             "confidence": "medium", "conflict_type": "location"}), [])

    def test_conflict_required_only_for_negative(self):
        errs = common.validate_annotation(
            {"same_referent": "no", "relation_label": "unrelated", "confidence": "low"})
        self.assertTrue(any("conflict_type" in e for e in errs))
        self.assertEqual(common.validate_annotation(
            {"same_referent": "uncertain", "relation_label": "related",
             "confidence": "low"}), [])

    def test_unusable_requires_reason(self):
        self.assertTrue(common.validate_annotation({"status": "unusable"}))
        self.assertEqual(common.validate_annotation(
            {"status": "unusable", "unusable_reason": "empty context"}), [])

    def test_bad_confidence_rejected(self):
        errs = common.validate_annotation(
            {"same_referent": "yes", "relation_label": "related", "confidence": "sure"})
        self.assertTrue(any("confidence" in e for e in errs))


# ===========================================================================
# B. Інтеграція: семплер + аналізатор на синтетичному корпусі
# ===========================================================================

class TestSamplerIntegration(unittest.TestCase):
    """Прогін ВСЬОГО семплера на синтетичному корпусі через stubs."""

    @classmethod
    def setUpClass(cls):
        from experiments.claim_relations.gold_set import relation_gold_set_sampler as sampler
        cls.sampler = sampler
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "run1"
        argv = ["--out-dir", str(out), "--seed", "20260903", "--chunk-size", "7",
                "--hub-degree-threshold", "3", "--repeat-rate", "0.15",
                "--min-repeat-separation", "10",
                "--n-s1", "6", "--n-s2", "6", "--n-s3", "6", "--n-s4", "6",
                "--n-s5", "4", "--n-s6", "4", "--n-s7", "3", "--n-s8", "3",
                "--n-s9", "4"]
        rc = sampler.main(argv)
        assert rc == 0
        cls.out = out
        cls.dataset = [json.loads(l) for l in
                       (out / "gold_set_v1_dataset.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        cls.manifest = json.loads((out / "gold_set_v1_manifest.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_outputs_exist(self):
        for name in ("gold_set_v1_dataset.jsonl", "gold_set_v1_pairs.csv",
                     "gold_set_v1_manifest.json"):
            self.assertTrue((self.out / name).exists(), name)

    def test_no_db_writes_attempted(self):
        """Заглушка psycopg рахує commit(); семплер не має викликати жодного."""
        import psycopg as stub
        self.assertTrue(stub.LAST_CONNECTIONS, "sampler never connected")
        for conn in stub.LAST_CONNECTIONS:
            self.assertEqual(conn.writes_attempted, 0)

    def test_connections_are_read_only(self):
        import psycopg as stub
        for conn in stub.LAST_CONNECTIONS:
            self.assertTrue(conn.read_only, "connection must set read_only=True")

    def test_dataset_rows_have_required_shape(self):
        self.assertTrue(self.dataset)
        for row in self.dataset:
            self.assertEqual(row["schema_version"], common.SCHEMA_VERSION)
            for field in ("interaction_id", "pair_key", "is_repeat", "repeat_of",
                          "presented_swapped", "display", "diagnostics"):
                self.assertIn(field, row)
            for side in ("A", "B"):
                for field in ("claim_text", "evidence_span", "title",
                              "context_snippet", "source"):
                    self.assertIn(field, row["display"][side])

    def test_display_leaks_no_diagnostics(self):
        """Anti-leakage: у блоці display не має бути ані score, ані страти,
        ані часу, ані ідентифікаторів груп."""
        banned = ("score", "stratum", "hub", "group", "first_seen",
                  "claim_id", "frame", "same_group")
        for row in self.dataset:
            blob = json.dumps(row["display"], ensure_ascii=False).lower()
            for token in banned:
                self.assertNotIn(token, blob,
                                 f"'{token}' leaked into display of {row['interaction_id']}")

    def test_time_is_not_in_display(self):
        for row in self.dataset:
            for side in ("A", "B"):
                self.assertNotIn("first_seen", row["display"][side])

    def test_interaction_ids_unique_and_ordered(self):
        ids = [r["interaction_id"] for r in self.dataset]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))

    def test_repeat_accounting_matches_manifest(self):
        uniq = [r for r in self.dataset if not r["is_repeat"]]
        rep = [r for r in self.dataset if r["is_repeat"]]
        self.assertEqual(len(uniq), self.manifest["unique_pairs"])
        self.assertEqual(len(rep), self.manifest["hidden_repeats"])
        self.assertEqual(len(self.dataset), self.manifest["total_interactions"])
        self.assertEqual(len({r["pair_key"] for r in uniq}), len(uniq))

    def test_populations_are_recorded_for_weighting(self):
        pops = self.manifest["populations"]
        sampled = self.manifest["sampled"]
        for stratum, n in sampled.items():
            if n:
                self.assertGreaterEqual(pops[stratum], n,
                                        f"{stratum}: sampled more than population")

    def test_exploratory_kept_separate(self):
        frames = {r["diagnostics"]["frame"] for r in self.dataset}
        self.assertIn(common.MAIN_FRAME, frames)
        for row in self.dataset:
            d = row["diagnostics"]
            if d["frame"] == common.EXPLORATORY_FRAME:
                self.assertTrue(d["same_group"])
            else:
                self.assertFalse(d["same_group"])

    def test_scores_within_bounds(self):
        for row in self.dataset:
            d = row["diagnostics"]
            self.assertGreaterEqual(d["claim_score"], -1.0)
            self.assertLessEqual(d["claim_score"], 1.0)
            self.assertGreaterEqual(d["content_score"], -1.0)
            self.assertLessEqual(d["content_score"], 1.0)

    def test_stratum_assignment_is_consistent_with_scores(self):
        """Кожен рядок має лежати саме в тій страті, яку дає assign_stratum."""
        for row in self.dataset:
            d = row["diagnostics"]
            expected = common.assign_stratum(
                d["claim_score"], d["content_score"], d["hub_a"], d["hub_b"], d["same_group"])
            self.assertEqual(expected, d["stratum"], row["interaction_id"])

    def test_rerun_same_seed_is_structurally_identical(self):
        """chunk_size may change last-bit float32 GEMM scores, but it must not
        change the selected pairs, strata, ordering, repeats, or displayed data."""
        out2 = Path(self.tmp.name) / "run2"
        argv = ["--out-dir", str(out2), "--seed", "20260903", "--chunk-size", "3",
                "--hub-degree-threshold", "3", "--repeat-rate", "0.15",
                "--min-repeat-separation", "10",
                "--n-s1", "6", "--n-s2", "6", "--n-s3", "6", "--n-s4", "6",
                "--n-s5", "4", "--n-s6", "4", "--n-s7", "3", "--n-s8", "3",
                "--n-s9", "4"]
        self.assertEqual(self.sampler.main(argv), 0)

        a = [json.loads(line) for line in
             (self.out / "gold_set_v1_dataset.jsonl").read_text(
                 encoding="utf-8").splitlines() if line.strip()]
        b = [json.loads(line) for line in
             (out2 / "gold_set_v1_dataset.jsonl").read_text(
                 encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(len(a), len(b))

        for left, right in zip(a, b):
            for field in ("schema_version", "interaction_id", "pair_key",
                          "is_repeat", "repeat_of", "presented_swapped", "display"):
                self.assertEqual(left[field], right[field], field)

            dl = left["diagnostics"]
            dr = right["diagnostics"]

            for field in dl:
                if field in ("claim_score", "content_score"):
                    self.assertLessEqual(
                        abs(float(dl[field]) - float(dr[field])),
                        2e-5,
                        f"{field} drift too large for {left['interaction_id']}",
                    )
                else:
                    self.assertEqual(
                        dl[field], dr[field],
                        f"{field} changed for {left['interaction_id']}",
                    )

    def test_different_seed_changes_dataset(self):
        out3 = Path(self.tmp.name) / "run3"
        argv = ["--out-dir", str(out3), "--seed", "999", "--chunk-size", "7",
                "--hub-degree-threshold", "3", "--repeat-rate", "0.15",
                "--min-repeat-separation", "10",
                "--n-s1", "6", "--n-s2", "6", "--n-s3", "6", "--n-s4", "6",
                "--n-s5", "4", "--n-s6", "4", "--n-s7", "3", "--n-s8", "3",
                "--n-s9", "4"]
        self.assertEqual(self.sampler.main(argv), 0)
        a = (self.out / "gold_set_v1_dataset.jsonl").read_text(encoding="utf-8")
        c = (out3 / "gold_set_v1_dataset.jsonl").read_text(encoding="utf-8")
        self.assertNotEqual(a, c)

    def test_dry_run_writes_nothing(self):
        out4 = Path(self.tmp.name) / "dry"
        argv = ["--out-dir", str(out4), "--dry-run", "--chunk-size", "7",
                "--hub-degree-threshold", "3"]
        self.assertEqual(self.sampler.main(argv), 0)
        self.assertFalse(out4.exists() and any(out4.iterdir()) if out4.exists() else False)


class TestAnalyzerIntegration(unittest.TestCase):
    """Генерує синтетичну розмітку для датасету семплера і проганяє аналізатор."""

    @classmethod
    def setUpClass(cls):
        from experiments.claim_relations.gold_set import relation_gold_set_sampler as sampler
        from experiments.claim_relations.gold_set import relation_gold_set_analyze as analyze
        cls.analyze = analyze
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "run"
        argv = ["--out-dir", str(out), "--seed", "5150", "--chunk-size", "9",
                "--hub-degree-threshold", "3", "--repeat-rate", "0.15",
                "--min-repeat-separation", "8",
                "--n-s1", "6", "--n-s2", "6", "--n-s3", "6", "--n-s4", "6",
                "--n-s5", "4", "--n-s6", "4", "--n-s7", "3", "--n-s8", "3",
                "--n-s9", "4"]
        assert sampler.main(argv) == 0
        cls.out = out
        cls.dataset = [json.loads(l) for l in
                       (out / "gold_set_v1_dataset.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

        # Синтетична "людина": вирішує за content_score, з одним навмисним
        # розходженням між оригіналом і повтором, щоб agreement != 100%.
        rng = random.Random(4)
        dataset_id = analyze.dataset_fingerprint(cls.dataset)
        anns = []
        flipped = False
        for row in cls.dataset:
            d = row["diagnostics"]
            good = d["content_score"] >= 0.60
            if row["is_repeat"] and not flipped:
                good = not good
                flipped = True
            if good:
                rec = {"dataset_id": dataset_id, "interaction_id": row["interaction_id"], "pair_key": row["pair_key"], "status": "labeled",
                       "same_referent": "yes",
                       "relation_label": rng.choice(["same_fact", "same_event"]),
                       "conflict_type": None, "confidence": "high", "note": ""}
            else:
                rec = {"dataset_id": dataset_id, "interaction_id": row["interaction_id"], "pair_key": row["pair_key"], "status": "labeled",
                       "same_referent": "no", "relation_label": "unrelated",
                       "conflict_type": "location", "confidence": "medium", "note": ""}
            anns.append(rec)
        cls.ann_path = out / "gold_set_v1_annotations.jsonl"
        cls.ann_path.write_text("\n".join(json.dumps(a) for a in anns) + "\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.analyze.main([
                "--dataset", str(self.out / "gold_set_v1_dataset.jsonl"),
                "--annotations", str(self.ann_path),
                "--manifest", str(self.out / "gold_set_v1_manifest.json"),
            ])
        return rc, buf.getvalue()

    def test_analyzer_runs_clean(self):
        rc, text = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("COMPLETENESS", text)
        self.assertIn("INTRA-RATER", text)
        self.assertIn("BELOW-0.80 RECALL PROBES", text)
        self.assertIn("EXPLORATORY FRAME", text)
        self.assertNotIn("PROBLEM", text)

    def test_repeats_do_not_inflate_unique_population(self):
        dataset = self.dataset
        anns = [json.loads(l) for l in self.ann_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        joined, problems = self.analyze.join_annotations(dataset, anns)
        primary, repeat_pairs = self.analyze.split_repeats(joined)
        expected_unique = sum(1 for r in dataset if not r["is_repeat"])
        self.assertEqual(len(primary), expected_unique)
        self.assertEqual(len({r["pair_key"] for r in primary}), expected_unique)
        self.assertTrue(repeat_pairs)
        self.assertFalse(any(problems.values()))

    def test_agreement_detects_the_planted_disagreement(self):
        dataset = self.dataset
        anns = [json.loads(l) for l in self.ann_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        joined, _ = self.analyze.join_annotations(dataset, anns)
        _primary, repeat_pairs = self.analyze.split_repeats(joined)
        stats = self.analyze.agreement_stats(repeat_pairs)
        self.assertGreater(stats["same_referent"]["n"], 0)
        self.assertLess(stats["same_referent"]["raw"], 1.0)
        self.assertEqual(len(stats["same_referent"]["disagreements"]), 1)

    def test_contract_violations_are_reported_not_silently_dropped(self):
        row = self.dataset[0]
        dataset_id = self.analyze.dataset_fingerprint(self.dataset)
        bad = [{
            "dataset_id": dataset_id,
            "interaction_id": row["interaction_id"],
            "pair_key": row["pair_key"],
            "status": "labeled",
            "same_referent": "no",
            "relation_label": "same_fact",
            "conflict_type": "location",
            "confidence": "high",
        }]
        joined, problems = self.analyze.join_annotations(self.dataset, bad)
        self.assertEqual(joined, [])
        self.assertEqual(len(problems["contract_errors"]), 1)

    def test_dataset_mismatch_is_rejected(self):
        row = self.dataset[0]
        ann = [{
            "dataset_id": "wrong-dataset",
            "interaction_id": row["interaction_id"],
            "pair_key": row["pair_key"],
            "status": "labeled",
            "same_referent": "yes",
            "relation_label": "related",
            "confidence": "high",
        }]
        joined, problems = self.analyze.join_annotations(self.dataset, ann)
        self.assertEqual(joined, [])
        self.assertEqual(problems["dataset_mismatch"], [row["interaction_id"]])

    def test_pair_mismatch_is_rejected(self):
        row = self.dataset[0]
        ann = [{
            "dataset_id": self.analyze.dataset_fingerprint(self.dataset),
            "interaction_id": row["interaction_id"],
            "pair_key": "wrong|pair",
            "status": "labeled",
            "same_referent": "yes",
            "relation_label": "related",
            "confidence": "high",
        }]
        joined, problems = self.analyze.join_annotations(self.dataset, ann)
        self.assertEqual(joined, [])
        self.assertEqual(problems["pair_mismatch"], [row["interaction_id"]])

    def test_unknown_interaction_id_reported(self):
        joined, problems = self.analyze.join_annotations(
            self.dataset, [{"interaction_id": "i9999", "status": "labeled",
                            "same_referent": "yes", "relation_label": "related",
                            "confidence": "high"}])
        self.assertEqual(joined, [])
        self.assertEqual(problems["unknown_interaction"], ["i9999"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
