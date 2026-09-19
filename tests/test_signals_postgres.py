"""Mock-перевірки SELECT-only адаптера та одноразового runner без PostgreSQL."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from reporting import signals_postgres as pg
from reporting.signals_snapshot import main, validate_snapshot
from test_signals import AS_OF, fixture, occurrence, snapshot

MODEL = 'synthetic@1'


def database_row(cid='a', oid='o', routing_decision='analyze'):
    return dict(content_id=cid, occurrence_id=oid, source_id='ru' if cid == 'a' else 'ru2',
        collected_at=AS_OF - timedelta(hours=1), published_at=None,
        external_ref='https://example.test/item', source_name='Джерело', source_type='rss',
        source_group='ru_space', routing_decision=routing_decision,
        title='Заголовок', text='Текст', dimension=1024, content_hash=cid, self_distance=0.0)


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.prepares = []
        self.responses = {
            pg.GUARD_SQL: [dict(read_only='on', isolation='repeatable read', timeout='15s')],
            pg.ANN_SETTINGS_SQL: [dict(ef_search='64', iterative_scan='relaxed_order', max_scan_tuples='100')],
            pg.ROUTING_COVERAGE_SQL: None,
            pg.MODEL_SQL: [dict(model_name='synthetic', model_revision='1', dimension=1024, metric='cosine')],
            pg.DIMENSION_SQL: [],
            pg.COVERAGE_SQL: [dict(window='current', source_group='ru_space', occurrence_count=10,
                content_count=5, source_count=1, missing_published_at=2, missing_embedding_content_count=0)],
            pg.ANCHOR_SQL: [dict(content_id='a')],
            pg.NEIGHBOUR_SQL: [dict(left_content_id='a', right_content_id='b', distance=0.05)],
            pg.ROWS_SQL: [database_row(), database_row('b', 'o2')],
        }
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None, **kwargs):
        self.prepares.append(kwargs)
        self.calls.append((sql, params))
        self.current = sql

    def fetchall(self):
        response = self.responses[self.current]
        if isinstance(response, Exception):
            raise response
        if self.current == pg.ROUTING_COVERAGE_SQL and response is None:
            return [dict(window=r['window'], source_group=r['source_group'], decision='analyze',
                         content_count=r['content_count']) for r in self.responses[pg.COVERAGE_SQL]]
        if self.current == pg.ANCHOR_SQL:
            return [{**r, 'available': r.get('available', len(response))} for r in response]
        return response

    def close(self):
        self.closed = True


class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()

    def read(self, **kwargs):
        return pg.PostgresSignalAdapter(self.conn, model_id=1, **kwargs).read(as_of=AS_OF, embedding_model=MODEL, dimension=1024)

    def test_select_only_parameters_and_full_coverage(self):
        data = self.read()
        self.assertEqual(data.coverage['current']['ru_space']['occurrence_count'], 10)
        self.assertEqual(len(data.occurrences), 2)
        self.assertEqual(data.pairs, (('a', 'b', 0.05),))
        self.assertTrue(all(c.embedding is None and c.has_embedding for c in data.contents))
        for sql, params in self.conn.calls:
            self.assertTrue(sql.strip().startswith(('SELECT', 'WITH ')))
            self.assertIsInstance(params, dict)
            self.assertNotIn(AS_OF.isoformat(), sql)
        parameters = dict(self.conn.calls)[pg.ROWS_SQL]
        self.assertEqual(parameters['as_of'], AS_OF)
        self.assertEqual(parameters['middle'] - parameters['start'], timedelta(hours=24))
        self.assertEqual(parameters['as_of'] - parameters['middle'], timedelta(hours=24))
        self.assertEqual(parameters['groups'], ['ru_space', 'ua_space'])
        self.assertEqual(parameters['row_probe'], 5001)
        self.assertEqual(parameters['text_probe'], 601)
        self.assertEqual(data.sources[0].source_name, 'Джерело')
        self.assertEqual(data.contents[0].title, 'Заголовок')
        self.assertEqual(data.occurrences[0].external_ref, 'https://example.test/item')
        self.assertIsNone(data.occurrences[0].published_at)

    def test_readonly_timeout_and_isolation_fail_before_data(self):
        for change in ({'read_only': 'off'}, {'isolation': 'read committed'}, {'timeout': '0'}, {'timeout': '1min'}, {'timeout': 'NaN'}):
            with self.subTest(change=change):
                self.conn = FakeConnection()
                self.conn.responses[pg.GUARD_SQL][0].update(change)
                with self.assertRaises(ValueError):
                    self.read()
                self.assertEqual(len(self.conn.calls), 1)

    def test_model_registry_and_dimension_fail_closed(self):
        for change in ({'model_revision': 'other'}, {'dimension': 2}, {'metric': 'l2'}):
            self.conn = FakeConnection()
            self.conn.responses[pg.MODEL_SQL][0].update(change)
            with self.assertRaises(ValueError):
                self.read()
        self.conn = FakeConnection()
        self.conn.responses[pg.DIMENSION_SQL] = [{'content_id': 'bad'}]
        with self.assertRaises(ValueError):
            self.read()

    def test_bounded_neighbours_and_pair_budget(self):
        self.conn.responses[pg.ANCHOR_SQL] = [dict(content_id='a'), dict(content_id='b'), dict(content_id='c')]
        limits = pg.PostgresLimits(anchors=2, neighbours=1, pairs=1)
        data = self.read(limits=limits)
        queries = [p for q, p in self.conn.calls if q == pg.NEIGHBOUR_SQL]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]['neighbour_limit'], 1)
        self.assertTrue(data.truncated)
        self.assertEqual(data.selection['inspected_pair_count'], 1)
        self.assertEqual(data.selection['searched_anchor_count'], 2)

    def test_rejected_distances_also_consume_budget(self):
        self.conn.responses[pg.ANCHOR_SQL] = [dict(content_id='a'), dict(content_id='b')]
        self.conn.responses[pg.NEIGHBOUR_SQL] = [dict(left_content_id='a', right_content_id='b', distance=1.5)]
        data = self.read(limits=pg.PostgresLimits(pairs=1))
        self.assertEqual(data.pairs, ())
        self.assertEqual(data.selection['searched_anchor_count'], 2)

    def test_bad_distances_and_row_dimension_rejected(self):
        for distance in (float('nan'), -0.2, 3):
            self.conn.responses[pg.NEIGHBOUR_SQL] = [dict(left_content_id='a', right_content_id='b', distance=distance)]
            with self.assertRaises(ValueError):
                self.read()
        self.conn = FakeConnection()
        self.conn.responses[pg.ROWS_SQL][0]['dimension'] = 3
        with self.assertRaises(ValueError):
            self.read()

    def test_zero_vector_self_distance_rejected(self):
        self.conn.responses[pg.ROWS_SQL][0]['self_distance'] = float('nan')
        with self.assertRaises(ValueError):
            self.read()

    def test_row_limit_and_missing_evidence_critical(self):
        data = self.read(limits=pg.PostgresLimits(rows=1))
        self.assertTrue(data.critical_incomplete)
        self.assertTrue(data.selection['row_limit_reached'])
        self.conn.responses[pg.ROWS_SQL] = []
        self.assertTrue(self.read().critical_incomplete)

    def test_no_embeddings_with_observed_flow_is_critical(self):
        self.conn.responses[pg.ANCHOR_SQL] = []
        self.assertTrue(self.read().critical_incomplete)
        self.conn.responses[pg.COVERAGE_SQL] = []
        self.assertFalse(self.read().critical_incomplete)

    def test_sql_order_and_bounds(self):
        self.assertIn(
            'ORDER BY ci.content_hash, e.content_id',
            pg.ANCHOR_SQL,
        )
        self.assertIn(
            'io.collected_at >= %(middle)s',
            pg.ANCHOR_SQL,
        )
        self.assertIn(
            'src.source_group = ANY(%(groups)s)',
            pg.ANCHOR_SQL,
        )

        self.assertIn(
            'WITH anchor_embeddings AS MATERIALIZED',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'CROSS JOIN LATERAL',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'ORDER BY e2.embedding::vector(1024) <=> a.embedding',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'LIMIT %(neighbour_limit)s',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'WHERE distance <= %(max_distance)s',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'LIMIT %(pair_probe)s',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'io.collected_at >= %(start)s',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'io.collected_at < %(as_of)s',
            pg.NEIGHBOUR_SQL,
        )
        self.assertIn(
            'src.source_group = ANY(%(groups)s)',
            pg.NEIGHBOUR_SQL,
        )

        self.assertIn(
            'left(ci.text_content, %(text_probe)s)',
            pg.ROWS_SQL,
        )
        self.assertNotIn(
            'COALESCE(io.published_at',
            pg.ROWS_SQL,
        )

    def test_invalid_limits_before_queries(self):
        invalid = (
            pg.PostgresLimits(anchors=0),
            pg.PostgresLimits(anchors=20001),
            pg.PostgresLimits(neighbours=51),
            pg.PostgresLimits(pairs=200001),
            pg.PostgresLimits(rows=100001),
            pg.PostgresLimits(ann_probe_limit=10),
            pg.PostgresLimits(ann_probe_limit=50001),
        )

        for limits in invalid:
            with self.subTest(
                limits=limits
            ), self.assertRaises(ValueError):
                self.read(limits=limits)

        self.assertEqual(self.conn.calls, [])

    def test_ann_shape_metadata_and_repeated_execution(self):
        sql = pg.NEIGHBOUR_SQL

        self.assertIn(
            'WITH anchor_embeddings AS MATERIALIZED',
            sql,
        )
        self.assertIn(
            'CROSS JOIN LATERAL',
            sql,
        )
        self.assertIn(
            'e2.embedding_model_id = 1',
            sql,
        )
        self.assertIn(
            'e2.embedding_model_id = %(model_id)s',
            sql,
        )
        self.assertIn(
            'r.content_id = e2.content_id',
            sql,
        )
        self.assertIn(
            'io.collected_at >= %(start)s',
            sql,
        )
        self.assertIn(
            'io.collected_at < %(as_of)s',
            sql,
        )
        self.assertIn(
            'src.source_group = ANY(%(groups)s)',
            sql,
        )
        self.assertIn(
            'ORDER BY e2.embedding::vector(1024) <=> a.embedding',
            sql,
        )
        self.assertIn(
            'LIMIT %(neighbour_limit)s',
            sql,
        )
        self.assertIn(
            'WHERE distance <= %(max_distance)s',
            sql,
        )

        # Fake DB має відтворювати результат transaction-local
        # set_config для конкретного scan budget.
        self.conn.responses[
            pg.ANN_SETTINGS_SQL
        ][0]['max_scan_tuples'] = '123'

        for _ in range(7):
            data = self.read(
                limits=pg.PostgresLimits(
                    ann_probe_limit=123,
                )
            )

        for (query, params), kwargs in zip(
            self.conn.calls,
            self.conn.prepares,
        ):
            if query == sql:
                self.assertEqual(
                    kwargs,
                    {'prepare': False},
                )
                self.assertEqual(
                    params['ann_probe_limit'],
                    123,
                )

        ann = data.selection['ann']

        self.assertEqual(
            ann['recall_status'],
            'UNVERIFIED',
        )
        self.assertEqual(
            ann['probe_limit'],
            123,
        )
        self.assertEqual(
            ann['max_scan_tuples'],
            123,
        )
        self.assertEqual(
            ann['ef_search'],
            64,
        )
        self.assertEqual(
            ann['iterative_scan'],
            'relaxed_order',
        )
        self.assertEqual(
            ann['filter_stage'],
            'inside_knn',
        )
        self.assertEqual(
            ann['k'],
            10,
        )
        self.assertEqual(
            snapshot(data)['selection']['ann'],
            ann,
        )
        self.assertFalse(data.truncated)

    def test_ann_settings_fail_closed(self):
        changes = (
            {'iterative_scan': 'off'},
            {'iterative_scan': 'strict_order'},
            {'ef_search': '0'},
            {'ef_search': '1001'},
            {'max_scan_tuples': '99'},
        )

        for change in changes:
            self.conn = FakeConnection()
            self.conn.responses[
                pg.ANN_SETTINGS_SQL
            ][0].update(change)

            with self.subTest(
                change=change
            ), self.assertRaises(ValueError):
                self.read()

            self.assertNotIn(
                pg.NEIGHBOUR_SQL,
                dict(self.conn.calls),
            )

    def test_ann_limits_and_unsupported_index_contract(self):
        for value in (
            True,
            0,
            10,
            50001,
            1.5,
        ):
            with self.subTest(
                value=value
            ), self.assertRaises(ValueError):
                self.read(
                    limits=pg.PostgresLimits(
                        ann_probe_limit=value,
                    )
                )

        self.assertEqual(
            self.conn.calls,
            [],
        )

        with self.assertRaises(ValueError):
            pg.PostgresSignalAdapter(
                self.conn,
                model_id=2,
            ).read(
                as_of=AS_OF,
                embedding_model=MODEL,
                dimension=1024,
            )

        self.assertNotIn(
            pg.NEIGHBOUR_SQL,
            dict(self.conn.calls),
        )

    def test_timeout_units(self):
        self.assertEqual(pg.timeout_ms('15000ms'), 15000)
        self.assertEqual(pg.timeout_ms('15s'), 15000)
        self.assertEqual(pg.timeout_ms('1min'), 60000)

    def test_determinism_of_row_permutation(self):
        one = snapshot(self.read())
        self.conn.responses[pg.ROWS_SQL].reverse()
        two = snapshot(self.read())
        self.assertEqual(one, two)
        validate_snapshot(one)


class RunnerTests(unittest.TestCase):
    def args(self, output=None):
        return ['--as-of', AS_OF.isoformat(), '--model-id', '1', '--model', MODEL, '--dimension', '1024',
            '--source-groups', 'ru_space', 'ua_space', '--anchors', '40', '--neighbours', '10',
            '--pairs', '400', '--rows', '5000', '--max-distance', '0.1'] + (['--output', str(output)] if output else ['--benchmark'])

    def driver(self, conn):
        return {'psycopg': SimpleNamespace(connect=MagicMock(return_value=conn)),
                'psycopg.rows': SimpleNamespace(dict_row=object())}

    def test_runner_atomic_publish_and_readonly_startup(self):
        conn = FakeConnection()
        modules = self.driver(conn)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'latest.json'
            with patch.dict(sys.modules, modules):
                self.assertEqual(main(self.args(path)), 0)
            self.assertTrue(path.exists())
            options = modules['psycopg'].connect.call_args.kwargs['options']
            self.assertIn('default_transaction_read_only=on', options)
            self.assertIn('statement_timeout=15000', options)
            self.assertTrue(conn.closed)

    def test_critical_failure_keeps_previous(self):
        conn = FakeConnection()
        conn.responses[pg.ROWS_SQL] = []
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'latest.json'
            path.write_text('попередній snapshot')
            with patch.dict(sys.modules, self.driver(conn)), patch('sys.stderr'):
                self.assertEqual(main(self.args(path)), 1)
            self.assertEqual(path.read_text(), 'попередній snapshot')

    def test_timeout_error_keeps_previous_and_closes(self):
        conn = FakeConnection()
        conn.responses[pg.ANCHOR_SQL] = TimeoutError('синтетичний timeout')
        with patch.dict(sys.modules, self.driver(conn)), patch('sys.stderr'):
            self.assertEqual(main(self.args()), 1)
        self.assertTrue(conn.closed)

    def test_benchmark_never_writes(self):
        with patch.dict(sys.modules, self.driver(FakeConnection())), patch('reporting.signals_snapshot.write_snapshot') as write, patch('sys.stdout'):
            self.assertEqual(main(self.args()), 0)
            write.assert_not_called()

    def test_as_of_required(self):
        args = self.args()[2:]
        with patch('sys.stderr'), self.assertRaises(SystemExit) as raised:
            main(args)
        self.assertEqual(raised.exception.code, 2)

    def test_invalid_parameters_prevent_connection(self):
        modules = self.driver(FakeConnection())
        args = self.args()
        args[args.index('--dimension') + 1] = '0'
        with patch.dict(sys.modules, modules), patch('sys.stderr'):
            self.assertEqual(main(args), 1)
        modules['psycopg'].connect.assert_not_called()
