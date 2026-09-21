"""Лише синтетичні unit-тести; тимчасові файли залишаються в репозиторії."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reporting.signals_data import Content, Occurrence, SignalData, Source
from reporting.signals_detector import (
    ALGORITHM_VERSION,
    MERGE_ALGORITHM_VERSION,
    RecallConfig,
    _merge_supported_groups,
    cosine_distance,
)
from reporting.signals_snapshot import (
    build_snapshot,
    deterministic_json,
    validate_snapshot,
    write_snapshot,
)


AS_OF = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
CONFIG = RecallConfig(max_distance=0.1, distance_bands=(0.05, 0.1, 0.3))


def occurrence(oid, cid="a", source="ru", hours=1, published_at=None):
    return Occurrence(oid, cid, source, AS_OF - timedelta(hours=hours), published_at)


def fixture(*rows, contents=None, sources=None):
    return SignalData(
        sources=tuple(sources if sources is not None else (Source("ru", "ru_space"), Source("ua", "ua_space"), Source("ru2", "ru_space"), Source("x", "unknown"))),
        contents=tuple(replace(c, routing_decision="analyze") for c in (contents if contents is not None else (Content("a", "Синтетичний матеріал А", (1.0, 0.0)),))),
        occurrences=tuple(rows),
        embedding_model="synthetic@1",
        dimension=2,
    )


def multi_source(data):
    """Явно додаємо друге джерело лише fixtures, яким потрібен published core."""
    extra = []
    for cid in sorted({r.content_id for r in data.occurrences}):
        rows = [r for r in data.occurrences if r.content_id == cid]
        if len({r.source_id for r in rows}) == 1:
            extra.append(replace(rows[0], occurrence_id=rows[0].occurrence_id + '-second', source_id='ru2'))
    sources = data.sources if any(s.source_id == 'ru2' for s in data.sources) else (*data.sources, Source('ru2', 'ru_space'))
    return replace(data, sources=sources, occurrences=(*data.occurrences, *extra))


def snapshot(data, config=CONFIG):
    return build_snapshot(data, as_of=AS_OF, config=config)


class SignalTests(unittest.TestCase):
    def test_utc_required(self):
        for invalid in (AS_OF.replace(tzinfo=None), AS_OF.astimezone(timezone(timedelta(hours=2)))):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                build_snapshot(fixture(), as_of=invalid, config=CONFIG)
        with self.assertRaises(ValueError):
            snapshot(fixture(replace(occurrence("o"), collected_at=AS_OF.replace(tzinfo=None))))
        with self.assertRaises(ValueError):
            build_snapshot(fixture(), as_of=AS_OF, config=CONFIG, generated_at=AS_OF.replace(tzinfo=None))

    def test_half_open_windows(self):
        data = fixture(occurrence("before", hours=49), occurrence("left", hours=48), occurrence("middle", hours=24), occurrence("inside", hours=0.001), occurrence("right", hours=0))
        result = snapshot(data)
        self.assertEqual(result["coverage"]["previous"]["ru_space"]["occurrence_count"], 1)
        self.assertEqual(result["coverage"]["current"]["ru_space"]["occurrence_count"], 2)
        self.assertEqual(result["outside_window_occurrence_count"], 2)
        self.assertEqual(result["windows"]["previous"]["start"], (AS_OF - timedelta(hours=48)).isoformat())
        self.assertEqual(result["windows"]["previous"]["end"], result["windows"]["current"]["start"])
        self.assertEqual(result["windows"]["current"]["end"], AS_OF.isoformat())

    def test_publication_time_is_separate_and_nullable(self):
        old_publication = AS_OF - timedelta(days=30)
        result = snapshot(fixture(occurrence("known", published_at=old_publication), occurrence("missing", source="ru2")))
        coverage = result["coverage"]["current"]["ru_space"]
        self.assertEqual(coverage["occurrence_count"], 2)
        self.assertEqual(coverage["missing_published_at"], 1)
        chronology = result["candidates"][0]["chronology"]
        self.assertEqual(chronology[0]["published_at"], old_publication.isoformat())
        self.assertIsNone(chronology[1]["published_at"])

    def test_republications_counts_and_cross_space(self):
        result = snapshot(fixture(occurrence("r1"), occurrence("r2"), occurrence("u1", source="ua")))
        candidate = result["candidates"][0]
        self.assertEqual((candidate["occurrence_count"], candidate["content_count"], candidate["source_count"]), (3, 1, 2))
        self.assertTrue(candidate["cross_space"])
        self.assertEqual(candidate["exact_republication_content_ids"], ["a"])
        self.assertEqual(result["coverage"]["current"]["ru_space"]["occurrence_count"], 2)
        self.assertEqual(result["coverage"]["current"]["ua_space"]["occurrence_count"], 1)

    def test_same_vector_different_content_is_not_exact_republication(self):
        data = fixture(occurrence("a"), occurrence("b", "b", source="ru2"), contents=(Content("a", "А", (1.0, 0.0)), Content("b", "Б", (2.0, 0.0))))
        candidate = snapshot(data)["candidates"][0]
        self.assertEqual(candidate["content_count"], 2)
        self.assertEqual(candidate["exact_republication_content_ids"], [])
        self.assertFalse(candidate["cross_space"])

    def test_unknown_group_is_excluded(self):
        result = snapshot(fixture(occurrence("known"), occurrence("unknown", source="x")))
        self.assertEqual(result["coverage"]["current"]["excluded"]["occurrence_count"], 1)
        self.assertEqual(result["coverage"]["current"]["ru_space"]["occurrence_count"], 1)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["incomplete"])

    def test_zero_previous_is_new_without_infinity(self):
        dynamics = snapshot(fixture(occurrence("new"), occurrence("new2", source="ru2")))["candidates"][0]["dynamics"]
        self.assertEqual(dynamics["ru_space"]["change_status"], "new_in_observed_window")
        self.assertIsNone(dynamics["ru_space"]["growth_ratio"])
        self.assertIsNone(dynamics["ru_space"]["previous"]["share"])
        self.assertEqual(dynamics["ua_space"]["change_status"], "not_observed")
        self.assertNotIn("Infinity", deterministic_json(dynamics))

    def test_dynamics_normalize_within_each_space(self):
        rows = [occurrence("old-a", hours=25), occurrence("old-b", "b", hours=25), occurrence("new-a1"), occurrence("new-a2"), occurrence("ua-a", source="ua")]
        rows.extend(occurrence(f"new-b{i}", "b") for i in range(6))
        data = fixture(*rows, contents=(Content("a", "А", (1.0, 0.0)), Content("b", "Б", (0.0, 1.0))))
        dynamics = snapshot(data)["candidates"][0]["dynamics"]
        self.assertEqual(dynamics["ru_space"]["growth_ratio"], 2)
        self.assertEqual(dynamics["ru_space"]["previous"]["share"], 0.5)
        self.assertEqual(dynamics["ru_space"]["current"]["share"], 0.25)
        self.assertEqual(dynamics["ru_space"]["share_delta"], -0.25)
        self.assertEqual(dynamics["ua_space"]["current"]["share"], 1)

    def test_bridge_chain_does_not_merge(self):
        # A–B та B–C близькі, A–C віддалені. B має перший ID, щоб
        # перевірити також випадок representative у центрі ланцюжка.
        vector = lambda degrees: (math.cos(math.radians(degrees)), math.sin(math.radians(degrees)))
        a, b, c = vector(0), vector(20), vector(40)
        self.assertLess(cosine_distance(a, b), CONFIG.max_distance)
        self.assertLess(cosine_distance(b, c), CONFIG.max_distance)
        self.assertGreater(cosine_distance(a, c), CONFIG.max_distance)
        data = fixture(occurrence("a", "1"), occurrence("b", "0"), occurrence("c", "2"), contents=(Content("1", "А", a), Content("0", "Б", b), Content("2", "В", c)))
        self.assertEqual([row["content_ids"] for row in snapshot(multi_source(data))["candidates"]], [["0", "1"], ["2"]])

    def test_supported_core_merge_requires_two_cross_links(self):
        groups = [
            ["a", "b"],
            ["c", "d"],
            ["e"],
        ]
        selected = ["a", "b", "c", "d", "e"]
        pairs = {
            ("a", "c"): 0.18,
            ("b", "c"): 0.19,
            ("a", "d"): 0.19,
            ("d", "e"): 0.10,
        }

        # На 0.18 між першими cores є лише один link:
        # жодного merge.
        self.assertEqual(
            _merge_supported_groups(
                groups,
                pairs,
                selected,
                merge_distance=0.18,
                min_cross_links=2,
            ),
            groups,
        )

        # На 0.19 є два незалежні cross-links:
        # перші cores зливаються.
        # Один дуже близький d-e link НЕ тягне за собою e.
        self.assertEqual(
            _merge_supported_groups(
                groups,
                pairs,
                selected,
                merge_distance=0.19,
                min_cross_links=2,
            ),
            [
                ["a", "b", "c", "d"],
                ["e"],
            ],
        )

    def test_supported_merge_rejects_transitive_chain(self):
        groups = [
            ["a1", "a2"],
            ["b1", "b2"],
            ["c1", "c2"],
        ]
        selected = [
            "a1", "a2",
            "b1", "b2",
            "c1", "c2",
        ]
        pairs = {
            ("a1", "b1"): 0.18,
            ("a2", "b2"): 0.18,
            ("b1", "c1"): 0.18,
            ("b2", "c2"): 0.18,
        }

        self.assertEqual(
            _merge_supported_groups(
                groups,
                pairs,
                selected,
                merge_distance=0.19,
                min_cross_links=2,
            ),
            [
                ["a1", "a2", "b1", "b2"],
                ["c1", "c2"],
            ],
        )

    def test_supported_merge_requires_member_coverage(self):
        left = [f"a{i}" for i in range(10)]
        right = [f"b{i}" for i in range(10)]
        groups = [left, right]
        selected = left + right

        pairs = {
            ("a0", "b0"): 0.10,
            ("a1", "b1"): 0.10,
        }

        self.assertEqual(
            _merge_supported_groups(
                groups,
                pairs,
                selected,
                merge_distance=0.19,
                min_cross_links=2,
            ),
            groups,
        )

    def test_merge_algorithm_version_and_config_contract(self):
        strict = snapshot(
            multi_source(fixture(occurrence("strict"))),
            replace(
                CONFIG,
                max_distance=0.18,
                related_distance=0.36,
            ),
        )
        self.assertEqual(strict["algorithm_version"], ALGORITHM_VERSION)
        self.assertNotIn("merge_distance", strict["recall"])

        merged = snapshot(
            multi_source(fixture(occurrence("merged"))),
            replace(
                CONFIG,
                max_distance=0.18,
                related_distance=0.36,
                merge_distance=0.19,
                merge_min_cross_links=2,
            ),
        )
        self.assertEqual(
            merged["algorithm_version"],
            MERGE_ALGORITHM_VERSION,
        )
        self.assertEqual(merged["recall"]["merge_distance"], 0.19)
        self.assertEqual(
            merged["recall"]["merge_min_cross_links"],
            2,
        )
        validate_snapshot(merged)

        for bad in (
            replace(
                CONFIG,
                max_distance=0.18,
                related_distance=0.36,
                merge_distance=0.17,
            ),
            replace(
                CONFIG,
                max_distance=0.18,
                related_distance=0.36,
                merge_distance=0.37,
            ),
            replace(
                CONFIG,
                max_distance=0.18,
                related_distance=0.36,
                merge_distance=0.19,
                merge_min_cross_links=1,
            ),
        ):
            with self.subTest(config=bad), self.assertRaises(ValueError):
                snapshot(fixture(), bad)

    def test_supported_core_merge_is_opt_in_and_rejects_single_bridge(self):
        contents = tuple(
            Content(cid, cid, has_embedding=True)
            for cid in "abcdef"
        )
        data = multi_source(
            fixture(
                *(occurrence(cid, cid) for cid in "abcdef"),
                contents=contents,
            )
        )
        data = replace(
            data,
            pairs=(
                ("a", "b", 0.10),
                ("c", "d", 0.10),
                ("e", "f", 0.10),
                ("a", "c", 0.185),
                ("b", "c", 0.190),
                ("a", "d", 0.190),
                ("d", "e", 0.05),
            ),
        )

        base = replace(
            CONFIG,
            max_distance=0.18,
            related_distance=0.36,
        )

        strict = snapshot(data, base)
        self.assertEqual(
            [c["content_ids"] for c in strict["candidates"]],
            [["a", "b"], ["c", "d"], ["e", "f"]],
        )
        self.assertNotIn(
            "strict_core_group_count",
            strict["presentation"],
        )

        merged = snapshot(
            data,
            replace(
                base,
                merge_distance=0.19,
                merge_min_cross_links=2,
            ),
        )

        self.assertEqual(
            [c["content_ids"] for c in merged["candidates"]],
            [["a", "b", "c", "d"], ["e", "f"]],
        )
        self.assertEqual(
            merged["presentation"]["strict_core_group_count"],
            3,
        )
        self.assertEqual(
            merged["presentation"]["core_group_count"],
            2,
        )

        # a-c / b-c стали внутрішніми merge-links;
        # d-e — один bridge і не повинен об'єднати третій core.
        membership = {
            cid: candidate["candidate_id"]
            for candidate in merged["candidates"]
            for cid in candidate["content_ids"]
        }
        self.assertNotEqual(membership["d"], membership["e"])

        self.assertFalse(
            any(
                membership.get(link["left_content_id"])
                == membership.get(link["right_content_id"])
                for link in merged["related_links"]
            )
        )

        validate_snapshot(merged)

    def test_unverified_neutral_distances(self):
        candidate = snapshot(multi_source(fixture(occurrence("one"))))["candidates"][0]
        self.assertEqual(candidate["interpretation_status"], "unverified")
        self.assertEqual(candidate["distances_to_representative"][0]["distance"], 0)
        self.assertEqual(candidate["distances_to_representative"][0]["distance_band"], "band_0")
        for forbidden in ("semantic_verdict", "same_thesis", "borrowing", "independent_confirmation", "relation_label"):
            self.assertNotIn(forbidden, deterministic_json(candidate))

    def test_determinism_under_input_permutation(self):
        data = fixture(occurrence("b", "b", source="ua"), occurrence("a", hours=25), occurrence("c", "c"), contents=(Content("a", "А", (1.0, 0.0)), Content("b", "Б", (1.0, 0.1)), Content("c", "В", (0.0, 1.0))))
        permuted = replace(data, sources=tuple(reversed(data.sources)), contents=tuple(reversed(data.contents)), occurrences=tuple(reversed(data.occurrences)))
        self.assertEqual(deterministic_json(snapshot(data)), deterministic_json(snapshot(permuted)))
        self.assertEqual(snapshot(data)["generated_at"], AS_OF.isoformat())

    def test_recall_configuration_changes_packages_not_verdicts(self):
        data = fixture(occurrence("a"), occurrence("b", "b"), contents=(Content("a", "А", (1.0, 0.0)), Content("b", "Б", (0.0, 1.0))))
        data = multi_source(data)
        strict = snapshot(data)
        broad = snapshot(data, replace(CONFIG, max_distance=1.0, related_distance=1.0))
        self.assertEqual(len(strict["candidates"]), 2)
        self.assertEqual(len(broad["candidates"]), 1)
        self.assertEqual(broad["candidates"][0]["interpretation_status"], "unverified")
        self.assertEqual(broad["candidates"][0]["distances_to_representative"][1]["distance"], 1.0)
        self.assertEqual(broad["candidates"][0]["distances_to_representative"][1]["distance_band"], "band_3")
        self.assertEqual(broad["embedding_model"], "synthetic@1")
        self.assertEqual(broad["embedding_dimension"], 2)

    def test_missing_embeddings_are_separate(self):
        data = fixture(occurrence("a"), occurrence("b", "b"), contents=(Content("a", "А"), Content("b", "Б")))
        result = snapshot(multi_source(data))
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(result["incomplete"])
        self.assertIsNone(result["candidates"][0]["distances_to_representative"][0]["distance"])
        self.assertEqual(result["coverage"]["current"]["ru_space"]["missing_embedding_content_count"], 2)

    def test_truncation_preserves_coverage_denominator(self):
        data = fixture(occurrence("a"), occurrence("b", "b"), contents=(Content("a", "А", (1.0, 0.0)), Content("b", "Б", (0.0, 1.0))))
        result = snapshot(multi_source(data), replace(CONFIG, max_contents=1))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["omitted_content_count"], 1)
        self.assertEqual(result["candidates"][0]["dynamics"]["ru_space"]["current"]["share"], 0.5)
        self.assertTrue(snapshot(replace(data, truncated=True))["incomplete"])

    def test_invalid_input_is_rejected(self):
        for vector in ((1.0,), (0.0, 0.0), (float("nan"), 0.0)):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                snapshot(fixture(contents=(Content("a", "А", vector),)))
        for data in (fixture(occurrence("o", "missing")), fixture(occurrence("o"), occurrence("o")), replace(fixture(), dimension=0)):
            with self.assertRaises(ValueError):
                snapshot(data)
        for config in (RecallConfig(-1), RecallConfig(0.1, (0.3, 0.1)), RecallConfig(0.1, max_contents=0)):
            with self.assertRaises(ValueError):
                snapshot(fixture(), config)

    def test_empty_input(self):
        result = snapshot(fixture())
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["coverage"]["current"]["ua_space"]["occurrence_count"], 0)

    def test_atomic_write_and_failures_preserve_previous_snapshot(self):
        payload = snapshot(fixture(occurrence("o")))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            destination = Path(directory) / "latest.json"
            write_snapshot(payload, destination)
            original = destination.read_bytes()
            self.assertEqual(json.loads(original), payload)
            changed = {**payload, "generated_at": (AS_OF + timedelta(minutes=1)).isoformat()}
            for target in ("reporting.signals_snapshot.os.replace", "reporting.signals_snapshot.os.fsync"):
                with self.subTest(target=target), patch(target, side_effect=OSError("Синтетичний збій")):
                    with self.assertRaises(OSError):
                        write_snapshot(changed, destination)
                self.assertEqual(destination.read_bytes(), original)
                self.assertEqual(list(Path(directory).iterdir()), [destination])
            with self.assertRaises(ValueError):
                write_snapshot({"invalid": float("nan")}, destination)
            self.assertEqual(destination.read_bytes(), original)
            write_snapshot(changed, destination)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), changed)


class BoundedSignalTests(unittest.TestCase):
    def test_recent_material_beats_uuid_order(self):
        data = fixture(occurrence('old', 'a', hours=25), occurrence('new', 'z'),
            contents=(Content('a', 'старе', (1., 0.)), Content('z', 'нове', (0., 1.))))
        self.assertEqual(snapshot(multi_source(data), replace(CONFIG, max_contents=1))['candidates'][0]['candidate_id'], 'z')

    def test_observed_sources_break_recency_tie(self):
        data = fixture(occurrence('a'), occurrence('z', 'z'), occurrence('u', 'z', source='ua'),
            contents=(Content('a', 'А', (1., 0.)), Content('z', 'З', (0., 1.))))
        self.assertEqual(snapshot(data, replace(CONFIG, max_contents=1))['candidates'][0]['candidate_id'], 'z')

    def test_pair_budget_bounds_python_comparison(self):
        data = fixture(*(occurrence(str(i), str(i)) for i in range(1000)),
            contents=tuple(Content(str(i), 'тест', (1., 0.)) for i in range(1000)))
        with patch('reporting.signals_detector.cosine_distance', return_value=1.) as compare:
            result = snapshot(data, replace(CONFIG, max_pairs=7))
        self.assertEqual(compare.call_count, 7)
        self.assertTrue(result['truncated'])

    def test_sql_pairs_never_use_python_vectors_and_no_bridge(self):
        data = fixture(*(occurrence(cid, cid) for cid in ('a', 'b', 'c')),
            contents=tuple(Content(cid, cid, has_embedding=True) for cid in ('a', 'b', 'c')))
        data = replace(data, pairs=(('a', 'b', .05), ('b', 'c', .05)))
        with patch('reporting.signals_detector.cosine_distance', side_effect=AssertionError('Заборонено')):
            result = snapshot(multi_source(data))
        self.assertEqual([c['content_ids'] for c in result['candidates']], [['a', 'b'], ['c']])

    def test_evidence_limits_metadata_and_counts(self):
        data = fixture(*(replace(occurrence(str(i), source='ru' if i % 2 == 0 else 'ru2'), external_ref='https://example.test') for i in range(20)),
            contents=(Content('a', 'я' * 1000, (1., 0.), title='Заголовок'),),
            sources=(Source('ru', 'ru_space', 'Джерело', 'rss'), Source('ru2', 'ru_space', 'Друге', 'rss')))
        result = snapshot(data, replace(CONFIG, max_evidence=3, evidence_chars=17))['candidates'][0]
        self.assertEqual(result['occurrence_count'], 20)
        self.assertEqual(len(result['chronology']), 3)
        self.assertEqual(result['evidence_omitted_count'], 17)
        self.assertEqual(len(result['evidence_references'][0]['text']), 17)
        self.assertTrue(result['evidence_references'][0]['text_truncated'])
        row = result['chronology'][0]
        self.assertEqual((row['source_name'], row['source_type'], row['title'], row['external_ref']),
            ('Джерело', 'rss', 'Заголовок', 'https://example.test'))

    def test_production_content_hash_breaks_ties_without_uuid(self):
        data = fixture(occurrence('a'), occurrence('z', 'z'), contents=(
            Content('a', 'А', (1., 0.), selection_key='zzz'),
            Content('z', 'З', (0., 1.), selection_key='aaa')))
        self.assertEqual(snapshot(multi_source(data), replace(CONFIG, max_contents=1))['candidates'][0]['candidate_id'], 'z')


class ContractTests(unittest.TestCase):
    def test_duplicate_or_self_pairs_are_rejected(self):
        data = fixture(occurrence('a'), occurrence('b', 'b'), contents=(
            Content('a', 'А', has_embedding=True), Content('b', 'Б', has_embedding=True)))
        for pairs in ((('a', 'b', .05), ('b', 'a', .09)), (('a', 'a', .0),), (('a', 'b', True),)):
            with self.subTest(pairs=pairs), self.assertRaises(ValueError):
                snapshot(replace(data, pairs=pairs))

    def test_pair_order_does_not_change_snapshot(self):
        data = fixture(*(occurrence(cid, cid) for cid in ('a', 'b', 'c')),
            contents=tuple(Content(cid, cid, has_embedding=True) for cid in ('a', 'b', 'c')))
        data = replace(data, pairs=(('a', 'b', .05), ('b', 'c', .06), ('a', 'c', .07)))
        self.assertEqual(snapshot(data), snapshot(replace(data, pairs=tuple(reversed(data.pairs)))))

    def test_invalid_metadata_is_rejected(self):
        for data in (fixture(contents=(Content('a', None),)),
                     fixture(sources=(Source('ru', 'ru_space', None),)),
                     fixture(replace(occurrence('o'), external_ref=None))):
            with self.assertRaises(ValueError):
                snapshot(data)

    def test_forbidden_verdict_fields_are_rejected_by_snapshot_contract(self):
        data = snapshot(multi_source(fixture(occurrence('o'))))
        data['candidates'][0]['semantic_verdict'] = 'confirmed'
        with self.assertRaises(ValueError):
            validate_snapshot(data)


if __name__ == "__main__":
    unittest.main()
