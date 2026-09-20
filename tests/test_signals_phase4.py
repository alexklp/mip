"""Регресії relevance gate, core/review та bounded presentation без live БД."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import io
import json
import unittest
import sqlite3
import test_signals_postgres as postgres_tests
from unittest.mock import patch

from reporting import signals_postgres as pg
from reporting.signals_data import Content, Source
from reporting.signals_detector import MERGE_ALGORITHM_VERSION, RecallConfig
from reporting.signals_snapshot import main, validate_snapshot
from test_signals import AS_OF, fixture, occurrence, multi_source, snapshot
from test_signals_postgres import FakeConnection


class Phase4Tests(unittest.TestCase):
    def test_pair_budget_operational_ceiling(self):
        RecallConfig(max_pairs=200000).validate()
        pg.PostgresLimits(pairs=200000).validate()

        with self.assertRaises(ValueError):
            RecallConfig(max_pairs=200001).validate()

        with self.assertRaises(ValueError):
            pg.PostgresLimits(pairs=200001).validate()

    def data(self):
        data = multi_source(fixture(*(occurrence(c, c) for c in 'abc'),
            contents=tuple(Content(c, c, has_embedding=True) for c in 'abc')))
        return replace(data, pairs=(('a', 'b', .18), ('b', 'c', .2), ('a', 'c', .36)))

    def test_latest_routing_is_inside_sparse_eligible_predicate(self):
        latest = pg.LATEST_ROUTING_SQL

        self.assertIn(
            'ORDER BY r.routing_version DESC, r.created_at DESC, r.routing_id DESC\nLIMIT 1',
            latest,
        )
        self.assertNotIn("decision = 'analyze'", latest)
        self.assertNotIn('routing_version =', latest)
        self.assertNotIn('embedding_model_id =', latest)

        for query in (
            pg.ANCHOR_SQL,
            pg.NEIGHBOUR_SQL,
            pg.ROWS_SQL,
        ):
            self.assertIn(
                "LIMIT 1\n) IN ('analyze', 'maybe')",
                query,
            )
            self.assertNotIn(
                'content_contour_assignments',
                query,
            )
            self.assertNotIn("COALESCE", query)

        self.assertIn(
            'WITH anchor_embeddings AS MATERIALIZED',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'r.content_id = e2.content_id',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'io.collected_at >= %(start)s',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'src.source_group = ANY(%(groups)s)',
            pg.NEIGHBOUR_SQL,
        )

        # Routing/time/source eligibility має бути всередині самого
        # KNN LATERAL scan, до ORDER BY distance.
        self.assertLess(
            pg.NEIGHBOUR_SQL.index(
                'r.content_id = e2.content_id'
            ),
            pg.NEIGHBOUR_SQL.index(
                'ORDER BY e2.embedding::vector(1024)'
            ),
        )

        self.assertNotIn(
            'WITH ann AS MATERIALIZED',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            "COALESCE(decision, 'missing')",
            pg.ROUTING_COVERAGE_SQL,
        )

    def test_latest_decision_executes_readonly_select_on_versions_and_ties(self):
        # Виконуємо той самий scalar SQL над CTE VALUES; без DDL/DML та live БД.
        cases = [
            ([('a', 1, '2026-09-08', 'z', 'analyze'), ('a', 2, '2026-09-01', 'a', 'skip')], 'skip'),
            ([('a', 2, '2026-09-01', 'z', 'skip'), ('a', 2, '2026-09-02', 'a', 'maybe')], 'maybe'),
            ([('a', 2, '2026-09-02', 'a', 'skip'), ('a', 2, '2026-09-02', 'z', 'analyze')], 'analyze'),
            ([('other', 1, '2026-09-01', 'a', 'analyze')], None),
        ]
        with sqlite3.connect(':memory:') as conn:
            for rows, expected in cases:
                values = ','.join('(?,?,?,?,?)' for _ in rows)
                query = ('WITH content_routing_decisions(content_id,routing_version,created_at,routing_id,decision) AS (VALUES '
                         + values + "), io(content_id) AS (VALUES ('a')) SELECT (" + pg.LATEST_ROUTING_SQL + ') FROM io')
                params = tuple(value for row in rows for value in row)
                self.assertEqual(conn.execute(query, params).fetchone()[0], expected)

    def test_maybe_is_eligible_but_missing_and_skip_are_excluded(self):
        data = multi_source(fixture(occurrence('a')))
        data = replace(
            data,
            contents=(
                replace(data.contents[0], routing_decision='maybe'),
            ),
        )
        result = snapshot(data, RecallConfig())
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['candidate_id'], 'a')
        self.assertEqual(
            result['routing_coverage']['current']['ru_space']['maybe'],
            1,
        )
        self.assertFalse(result['incomplete'])
        validate_snapshot(result)

        for decision in (None, 'skip'):
            data = multi_source(fixture(occurrence('a')))
            data = replace(
                data,
                contents=(
                    replace(
                        data.contents[0],
                        routing_decision=decision,
                    ),
                ),
            )
            result = snapshot(data, RecallConfig())
            self.assertEqual(result['candidates'], [])
            self.assertEqual(result['related_links'], [])
            self.assertEqual(
                result['routing_coverage']['current']['ru_space'][
                    decision or 'missing'
                ],
                1,
            )
            self.assertEqual(result['incomplete'], decision is None)
            validate_snapshot(result)

    def test_exact_current_signal_keeps_previous_window_propagation(self):
        data = fixture(
            occurrence('now', 'a', source='ru', hours=1),
            occurrence('prev-1', 'a', source='ru2', hours=25),
            occurrence('prev-2', 'a', source='third', hours=30),
            contents=(Content('a', 'Матеріал', has_embedding=True),),
            sources=(
                Source('ru', 'ru_space'),
                Source('ru2', 'ru_space'),
                Source('third', 'ru_space'),
            ),
        )
        data = replace(
            data,
            contents=(
                replace(data.contents[0], routing_decision='maybe'),
            ),
            selection={'strategy': 'exact_current_24h'},
        )

        result = snapshot(data, RecallConfig())

        self.assertEqual(len(result['candidates']), 1)
        candidate = result['candidates'][0]

        self.assertEqual(candidate['candidate_id'], 'a')
        self.assertEqual(candidate['source_count'], 3)
        self.assertEqual(candidate['occurrence_count'], 3)
        self.assertEqual(
            candidate['exact_republication_content_ids'],
            ['a'],
        )
        self.assertEqual(
            candidate['dynamics']['ru_space']['current']['occurrence_count'],
            1,
        )
        self.assertEqual(
            candidate['dynamics']['ru_space']['previous']['occurrence_count'],
            2,
        )
        self.assertEqual(
            candidate['last_observed'],
            (AS_OF - timedelta(hours=1)).isoformat(),
        )

    def test_previous_only_candidate_is_suppressed_from_current_signals(self):
        data = fixture(
            occurrence(
                'old-1',
                'a',
                source='ru',
                hours=25,
            ),
            occurrence(
                'old-2',
                'a',
                source='ru2',
                hours=26,
            ),
            contents=(
                Content(
                    'a',
                    'Матеріал',
                    has_embedding=True,
                ),
            ),
            sources=(
                Source('ru', 'ru_space'),
                Source('ru2', 'ru_space'),
            ),
        )

        result = snapshot(
            data,
            RecallConfig(),
        )

        self.assertEqual(
            result['candidates'],
            [],
        )
        self.assertEqual(
            result['presentation']['suppressed']['previous_only'],
            1,
        )
        self.assertEqual(
            result['presentation']['eligible_candidate_count'],
            0,
        )

        validate_snapshot(result)

    def test_core_related_boundaries_do_not_inflate_aggregates(self):
        data = self.data()
        result = snapshot(data, RecallConfig())
        core = result['candidates'][0]
        self.assertEqual(core['content_ids'], ['a', 'b'])
        self.assertEqual((core['content_count'], core['source_count'], core['occurrence_count']), (2, 2, 4))
        self.assertEqual(core['dynamics']['ru_space']['current']['occurrence_count'], 4)
        self.assertEqual([r['distance'] for r in result['related_links']], [.2, .36])
        without_review = snapshot(replace(data, pairs=(('a', 'b', .18),)), RecallConfig())
        self.assertEqual(result['candidates'], without_review['candidates'])
        self.assertEqual(result, snapshot(replace(data, pairs=tuple(reversed(data.pairs))), RecallConfig()))
        validate_snapshot(result)

    def test_related_links_bounded_and_upper_threshold(self):
        data = replace(self.data(), pairs=(('a', 'b', .181), ('a', 'c', .36), ('b', 'c', .361)))
        result = snapshot(data, RecallConfig(max_related_links=1))
        self.assertEqual(len(result['related_links']), 1)
        self.assertEqual(result['presentation']['related_links_available'], 2)
        self.assertTrue(result['presentation']['related_limit_reached'])
        self.assertTrue(all(c['content_count'] == 1 for c in result['candidates']))
        validate_snapshot(result)

    def test_suppression_reasons(self):
        data = fixture(occurrence('a', 'a'), occurrence('b1', 'b'), occurrence('b2', 'b'),
            occurrence('c', 'c'), occurrence('d', 'd'),
            contents=tuple(Content(c, c, has_embedding=True) for c in 'abcd'))
        result = snapshot(replace(data, pairs=(('c', 'd', .1),)), RecallConfig())
        self.assertEqual(result['candidates'], [])
        self.assertEqual(
            result['presentation']['suppressed'],
            dict(
                singleton_single_source=1,
                repeated_content_single_source=1,
                core_single_source=1,
                previous_only=0,
            ),
        )
        validate_snapshot(result)

    def test_same_space_exact_republication_requires_distinct_sources(self):
        result = snapshot(multi_source(fixture(occurrence('a'))), RecallConfig())
        candidate = result['candidates'][0]
        self.assertFalse(candidate['cross_space'])
        self.assertEqual(candidate['exact_republication_content_ids'], ['a'])
        self.assertEqual(candidate['source_count'], 2)
        self.assertEqual(candidate['content_count'], 1)
        validate_snapshot(result)

    def test_generic_round_robin_deduplicates_and_redistributes(self):
        def rows(ids):
            return [dict(content_id=cid, available=len(ids)) for cid in ids]
        queues = {'z_group': rows(['shared', 'z1', 'z2']), 'a_group': rows(['shared', 'a1']), 'empty': []}
        selected, groups = pg.round_robin_anchors(queues, 4)
        self.assertEqual([r['content_id'] for r in selected], ['shared', 'z1', 'a1', 'z2'])
        self.assertEqual(sum(r['selected'] for r in groups.values()), 4)
        self.assertFalse(any(r['limit_reached'] for r in groups.values()))
        self.assertEqual(pg.round_robin_anchors(dict(reversed(list(queues.items()))), 4), (selected, groups))
        selected, groups = pg.round_robin_anchors(queues, 2)
        self.assertEqual([r['content_id'] for r in selected], ['shared', 'z1'])
        self.assertEqual(groups['empty'], dict(selected=0, available=0, limit_reached=False))
        self.assertTrue(groups['z_group']['limit_reached'])

    def test_rank_prioritizes_propagation_breadth_before_recency(self):
        data = multi_source(fixture(occurrence('a'), occurrence('b', 'b'), occurrence('c', 'c', hours=25),
            occurrence('d', 'd', hours=25), occurrence('e', 'e', hours=23), occurrence('e3', 'e', source='third', hours=26),
            contents=tuple(Content(c, c, has_embedding=True) for c in 'abcde'),
            sources=(Source('ru', 'ru_space'), Source('ru2', 'ru_space'), Source('third', 'ru_space'))))
        # e вже має два джерела; третє додаємо явно, щоб перевірити пріоритет джерел.
        data = replace(data, occurrences=(*data.occurrences, occurrence('e2', 'e', source='ru2', hours=26)),
                       pairs=(('c', 'd', .1),))
        result = snapshot(data, RecallConfig())

        # e старіший, але поширений трьома distinct sources.
        # Він має бути вище свіжіших двохджерельних кандидатів.
        self.assertEqual(result['candidates'][0]['candidate_id'], 'e')
        self.assertEqual(result['candidates'][0]['source_count'], 3)

        validate_snapshot(result)

    def test_missing_and_skip_do_not_make_false_critical(self):
        for decision in ('missing', 'skip'):
            conn = FakeConnection()
            conn.responses[pg.ROUTING_COVERAGE_SQL] = [
                dict(
                    window='current',
                    source_group='ru_space',
                    decision=decision,
                    content_count=5,
                )
            ]
            conn.responses[pg.ANCHOR_SQL] = []
            data = pg.PostgresSignalAdapter(
                conn,
                model_id=1,
            ).read(
                as_of=AS_OF,
                embedding_model='synthetic@1',
                dimension=1024,
            )
            self.assertFalse(data.critical_incomplete)
            self.assertEqual(
                snapshot(data, RecallConfig())['candidates'],
                [],
            )

    def test_missing_maybe_evidence_is_critical(self):
        conn = FakeConnection()
        conn.responses[pg.ROUTING_COVERAGE_SQL] = [
            dict(
                window='current',
                source_group='ru_space',
                decision='maybe',
                content_count=5,
            )
        ]
        conn.responses[pg.ANCHOR_SQL] = []
        data = pg.PostgresSignalAdapter(
            conn,
            model_id=1,
        ).read(
            as_of=AS_OF,
            embedding_model='synthetic@1',
            dimension=1024,
        )
        self.assertTrue(data.critical_incomplete)
        self.assertEqual(
            snapshot(data, RecallConfig())['candidates'],
            [],
        )

    def test_rank_and_display_cap(self):
        data = multi_source(fixture(occurrence('a'), occurrence('b', 'b'), occurrence('c', 'c', source='ua'),
            contents=tuple(Content(c, c, has_embedding=True) for c in 'abc')))
        data = replace(data, pairs=())
        result = snapshot(data, RecallConfig(display_limit=2))
        # За однакової ширини cross-space має пріоритет над freshness.
        self.assertEqual(
            [r['candidate_id'] for r in result['candidates']],
            ['c', 'a'],
        )
        self.assertTrue(result['presentation']['display_limit_reached'])
        self.assertEqual(result['presentation']['eligible_candidate_count'], 3)
        self.assertEqual(result, snapshot(replace(data, occurrences=tuple(reversed(data.occurrences))), RecallConfig(display_limit=2)))
        validate_snapshot(result)

    def test_routing_coverage_all_windows_and_groups(self):
        data = fixture(occurrence('a'), occurrence('b', 'a', hours=25), occurrence('c', 'a', source='ua'),
                       occurrence('d', 'a', source='ua', hours=25))
        result = snapshot(data, RecallConfig())
        for window in ('current', 'previous'):
            for group in ('ru_space', 'ua_space'):
                self.assertEqual(result['routing_coverage'][window][group], dict(analyze=1, maybe=0, skip=0, missing=0))
        validate_snapshot(result)

    def test_schema_rejects_metadata_corruption(self):
        original = snapshot(self.data(), RecallConfig())
        mutations = [
            lambda d: d['routing_coverage']['current']['ru_space'].update(analyze=999),
            lambda d: d['routing_coverage']['current']['ru_space'].update(missing=-1),
            lambda d: d['routing_coverage']['current']['ru_space'].update(other=0),
            lambda d: d['routing_coverage'].pop('previous'),
            lambda d: d['presentation']['suppressed'].update(core_single_source=True),
            lambda d: d['presentation'].update(core_group_count=999),
            lambda d: d['presentation'].update(display_limit_reached=True),
            lambda d: d['presentation'].update(related_link_count=999),
            lambda d: d['candidates'][0].update(source_count=1),
            lambda d: d['candidates'].reverse(),
            lambda d: d['related_links'][0].update(left_content_id='unknown'),
            lambda d: d['presentation'].update(selected_content_ids=[]),
            lambda d: d['related_links'][0].update(distance=.18),
            lambda d: d['related_links'][0].update(distance=.361),
            lambda d: d['related_links'][0].update(interpretation_status='confirmed'),
            lambda d: d['related_links'].reverse(),
        ]
        for mutate in mutations:
            damaged = deepcopy(original)
            mutate(damaged)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validate_snapshot(damaged)

    def test_postgres_metadata_validation(self):
        conn = FakeConnection()

        data = pg.PostgresSignalAdapter(
            conn,
            model_id=1,
        ).read(
            as_of=AS_OF,
            embedding_model='synthetic@1',
            dimension=1024,
        )

        original = snapshot(data, RecallConfig())
        validate_snapshot(original)

        mutations = (
            lambda selection: selection['ann'].update(
                probe_limit=1001,
            ),
            lambda selection: selection.update(
                searched_anchor_count=(
                    selection['anchor_count'] + 1
                ),
            ),
            lambda selection: selection.update(
                routing_policy='maybe',
            ),
            lambda selection: selection['limits'].update(
                ann_probe_limit=10,
            ),
            lambda selection: selection['ann'].update(
                filter_stage='after_probe',
            ),
        )

        for mutate in mutations:
            damaged = deepcopy(original)
            mutate(damaged['selection'])

            with self.subTest(mutate=mutate), self.assertRaises(
                ValueError
            ):
                validate_snapshot(damaged)

    def test_cli_merge_contract_is_opt_in(self):
        helper = postgres_tests.RunnerTests()
        conn = FakeConnection()
        out = io.StringIO()

        with (
            patch.dict('sys.modules', helper.driver(conn)),
            patch('sys.stdout', out),
        ):
            self.assertEqual(
                main(
                    helper.args()
                    + [
                        '--merge-distance', '0.19',
                        '--merge-min-cross-links', '2',
                    ]
                ),
                0,
            )

        result = json.loads(out.getvalue())

        self.assertEqual(
            result['algorithm_version'],
            MERGE_ALGORITHM_VERSION,
        )
        self.assertEqual(
            result['recall']['merge_distance'],
            0.19,
        )
        self.assertEqual(
            result['recall']['merge_min_cross_links'],
            2,
        )
        self.assertEqual(
            result['presentation']['strict_core_group_count'],
            1,
        )

    def test_cli_ann_probe_and_no_snapshot(self):
        helper = postgres_tests.RunnerTests()

        for limit in ('100', '123'):
            conn = FakeConnection()

            # Fake DB emulates SELECT set_config(... ann_probe_limit ...).
            conn.responses[pg.ANN_SETTINGS_SQL][0][
                'max_scan_tuples'
            ] = limit

            out = io.StringIO()

            with (
                patch.dict(
                    'sys.modules',
                    helper.driver(conn),
                ),
                patch('sys.stdout', out),
                patch(
                    'reporting.signals_snapshot.write_snapshot'
                ) as write,
            ):
                self.assertEqual(
                    main(
                        helper.args()
                        + ['--ann-probe-limit', limit]
                    ),
                    0,
                )
                write.assert_not_called()

            result = json.loads(out.getvalue())

            self.assertEqual(
                result['selection']['ann']['probe_limit'],
                int(limit),
            )
            self.assertEqual(
                result['selection']['ann']['max_scan_tuples'],
                int(limit),
            )
            self.assertEqual(
                result['selection']['limits'][
                    'ann_probe_limit'
                ],
                int(limit),
            )
            self.assertEqual(
                result['selection']['ann'][
                    'iterative_scan'
                ],
                'relaxed_order',
            )
            self.assertEqual(
                result['selection']['ann'][
                    'filter_stage'
                ],
                'inside_knn',
            )
            self.assertIn(
                'routing_coverage',
                result,
            )
            self.assertIn(
                'presentation',
                result,
            )

        for limit in ('0', '10', '50001'):
            modules = helper.driver(FakeConnection())

            with (
                patch.dict('sys.modules', modules),
                patch('sys.stderr'),
            ):
                self.assertEqual(
                    main(
                        helper.args()
                        + ['--ann-probe-limit', limit]
                    ),
                    1,
                )
                modules[
                    'psycopg'
                ].connect.assert_not_called()

    def test_common_watermark_and_explicit_readonly_transaction(self):
        helper = postgres_tests.RunnerTests()
        conn = FakeConnection()
        conn.responses[pg.WATERMARK_SQL] = [dict(source_group='ru_space', watermark=AS_OF),
                                          dict(source_group='ua_space', watermark=AS_OF-timedelta(minutes=1))]
        args = helper.args()
        args[1] = 'common'
        out = io.StringIO()
        with patch.dict('sys.modules', helper.driver(conn)), patch('sys.stdout', out):
            self.assertEqual(main(args), 0)
        self.assertEqual(conn.calls[0][0], 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        self.assertEqual(json.loads(out.getvalue())['as_of'], (AS_OF-timedelta(minutes=1)).isoformat())
        conn.responses[pg.WATERMARK_SQL] = []
        with patch.dict('sys.modules', helper.driver(conn)), patch('sys.stderr'):
            self.assertEqual(main(args), 1)
