"""Файлові стани та безпечний HTTP/Jinja шлях; жодного сервера чи live DB."""

from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reporting.signals_snapshot import validate_snapshot, write_snapshot, MAX_SNAPSHOT_BYTES
from web.signals import load_signals, safe_link, fetch_signal_chronology
from test_signals import AS_OF, fixture, occurrence, snapshot, multi_source

WEB_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ('fastapi', 'psycopg', 'jinja2', 'httpx'))


class FileStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'signals.json'

    def load(self, **kwargs):
        return load_signals(self.path, now=AS_OF + timedelta(minutes=1), **kwargs)

    def test_missing(self):
        self.assertEqual(self.load()['state'], 'missing')

    def test_corrupt_wrong_schema_and_incomplete_shapes(self):
        for payload in ('{', 'null', '[]', '42', '{"schema_version":"signals/999"}', '{"schema_version":"signals/2"}'):
            self.path.write_text(payload)
            self.assertEqual(self.load()['state'], 'invalid')

    def test_empty_and_ready(self):
        write_snapshot(snapshot(fixture()), self.path)
        self.assertEqual(self.load()['state'], 'empty')
        write_snapshot(snapshot(multi_source(fixture(occurrence('o')))), self.path)
        self.assertEqual(self.load()['state'], 'ready')

    def test_stale_uses_as_of_and_configurable_threshold(self):
        data = snapshot(multi_source(fixture(occurrence('o'))))
        data['generated_at'] = (AS_OF + timedelta(hours=3)).isoformat()
        write_snapshot(data, self.path)
        self.assertEqual(load_signals(self.path, now=AS_OF + timedelta(hours=3))['state'], 'stale')
        self.assertEqual(load_signals(self.path, now=AS_OF + timedelta(hours=3), stale_seconds=14400)['state'], 'ready')

    def test_invalid_threshold_and_future(self):
        write_snapshot(snapshot(fixture()), self.path)
        for value in (0, -1, float('nan'), '7200'):
            self.assertEqual(self.load(stale_seconds=value)['state'], 'invalid')
        self.assertEqual(load_signals(self.path, now=AS_OF - timedelta(seconds=1))['state'], 'invalid')

    def test_oversize_and_invalid_utf8(self):
        for payload in (b'x' * (MAX_SNAPSHOT_BYTES + 1), b'\xff'):
            self.path.write_bytes(payload)
            self.assertEqual(self.load()['state'], 'invalid')

    def test_invalid_nested_data(self):
        original = snapshot(multi_source(fixture(occurrence('o'))))
        mutations = [lambda d: d.update(critical_incomplete=True),
            lambda d: d['windows']['current'].update(membership_field='published_at'),
            lambda d: d['candidates'][0].update(interpretation_status='confirmed'),
            lambda d: d['candidates'][0]['dynamics']['ru_space']['current'].update(share=float('nan')),
            lambda d: d['candidates'][0]['dynamics'].update(extra={}),
            lambda d: d.update(selection='bad'),
            lambda d: d['coverage']['current'].update(extra={}),
            lambda d: d['candidates'][0]['chronology'][0].update(collected_at=AS_OF.isoformat())]
        for mutate in mutations:
            data = deepcopy(original)
            mutate(data)
            self.path.write_text(json.dumps(data))
            self.assertEqual(self.load()['state'], 'invalid')

    def test_cross_field_inconsistencies_are_invalid(self):
        original = snapshot(multi_source(fixture(occurrence('o'))))
        mutations = [lambda d: d['candidates'][0].update(occurrence_count=3),
            lambda d: d['candidates'][0].update(content_ids=['unrelated']),
            lambda d: d['candidates'][0].update(evidence_omitted_count=8),
            lambda d: d['candidates'][0].update(cross_space=True),
            lambda d: d['candidates'][0]['chronology'][0].update(content_id='other'),
            lambda d: d['candidates'][0]['evidence_references'][0].update(content_id='other'),
            lambda d: d['candidates'][0]['distances_to_representative'][0].update(distance=.5),
            lambda d: d['candidates'].append(deepcopy(d['candidates'][0])),
            lambda d: d['recall'].update(evidence_chars=1),
            lambda d: d['recall'].update(max_distance=True)]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                data = deepcopy(original)
                mutate(data)
                self.path.write_text(json.dumps(data))
                self.assertEqual(self.load()['state'], 'invalid')

    def test_refresh_observes_atomic_replacement(self):
        write_snapshot(snapshot(fixture()), self.path)
        self.assertEqual(self.load()['state'], 'empty')
        write_snapshot(snapshot(multi_source(fixture(occurrence('new')))), self.path)
        self.assertEqual(self.load()['state'], 'ready')

    def test_safe_links(self):
        for value in ('javascript:alert(1)', 'data:text/html,bad', '//example.test', 'https://user:pass@example.test', 'https://example.test/\n', 'https://[bad', 'https://example.test\\bad', 'https://example.test:bad'):
            self.assertIsNone(safe_link(value))
        self.assertEqual(safe_link('https://example.test/?a=1&b=2'), 'https://example.test/?a=1&b=2')

    def test_outside_path_rejected_before_open(self):
        with patch.object(Path, 'open', side_effect=AssertionError('Не читати')):
            self.assertEqual(load_signals('/outside-signals.json')['state'], 'invalid')

    def test_permission_error_is_invalid(self):
        with patch.object(Path, 'open', side_effect=PermissionError):
            self.assertEqual(self.load()['state'], 'invalid')



class _ChronologyCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class _ChronologyConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _ChronologyCursor(self.rows)


class ChronologyQueryTests(unittest.TestCase):
    def kwargs(self, **overrides):
        values = dict(
            content_ids=['00000000-0000-0000-0000-000000000001'],
            source_groups=['ru_space'],
            window_start=AS_OF - timedelta(hours=48),
            window_end=AS_OF,
            expected_total=1,
            offset=0,
            limit=30,
        )
        values.update(overrides)
        return values

    def test_empty_later_page_before_expected_total_is_mismatch(self):
        conn = _ChronologyConnection([])

        with self.assertRaisesRegex(
            ValueError,
            'Snapshot chronology',
        ):
            fetch_signal_chronology(
                conn,
                **self.kwargs(
                    expected_total=10,
                    offset=5,
                    limit=5,
                ),
            )

    def test_missing_publication_time_keeps_collected_time(self):
        collected_at = AS_OF - timedelta(hours=2)
        conn = _ChronologyConnection([
            {
                'occurrence_id': 'o1',
                'content_id': '00000000-0000-0000-0000-000000000001',
                'source_id': 's1',
                'source_group': 'ru_space',
                'source_name': 'Джерело',
                'source_type': 'telegram',
                'title': 'Матеріал',
                'text': 'Текст',
                'text_truncated': False,
                'external_ref': 'https://example.test/post/1',
                'published_at': None,
                'collected_at': collected_at,
                'available': 1,
            }
        ])

        result = fetch_signal_chronology(
            conn,
            **self.kwargs(),
        )

        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]['published_at'])
        self.assertEqual(
            result[0]['collected_at'],
            collected_at.isoformat(),
        )
        self.assertEqual(
            result[0]['safe_link'],
            'https://example.test/post/1',
        )

    def test_pagination_bounds_are_rejected(self):
        conn = _ChronologyConnection([])

        for offset, limit in (
            (-1, 30),
            (0, 0),
            (0, 51),
        ):
            with self.subTest(offset=offset, limit=limit):
                with self.assertRaisesRegex(
                    ValueError,
                    'Некоректна сторінка chronology',
                ):
                    fetch_signal_chronology(
                        conn,
                        **self.kwargs(
                            offset=offset,
                            limit=limit,
                        ),
                    )


@unittest.skipUnless(WEB_AVAILABLE, 'BLOCKED: потрібен комплект FastAPI/psycopg/Jinja2/httpx; встановлення заборонено')
class HttpTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from web.app import app
        self.app = app
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'signals.json'
        app.state.signals_snapshot_path = self.path
        app.state.signals_stale_seconds = 7200
        self.addCleanup(lambda: delattr(app.state, 'signals_snapshot_path'))
        self.addCleanup(lambda: delattr(app.state, 'signals_stale_seconds'))

    def request(self):
        from datetime import datetime, timezone
        # Фіксуємо лише годинник loader, зберігаючи його справжню реалізацію.
        with patch('web.signals.datetime') as clock, patch('web.app.read_connection', side_effect=AssertionError('Заборонено БД')), patch('psycopg.connect', side_effect=AssertionError('Заборонено БД')), patch('reporting.signals_postgres.PostgresSignalAdapter.read', side_effect=AssertionError('Заборонено recall')), patch('reporting.signals_detector.detect', side_effect=AssertionError('Заборонено обчислення')), patch('socket.create_connection', side_effect=AssertionError('Заборонено мережу')):
            clock.now.return_value = AS_OF + timedelta(minutes=1)
            clock.fromisoformat = datetime.fromisoformat
            return self.client.get('/signals')

    def test_all_states_and_no_db_request(self):
        self.assertIn('data-state="missing"', self.request().text)
        self.path.write_text('{')
        self.assertIn('data-state="invalid"', self.request().text)
        write_snapshot(snapshot(fixture()), self.path)
        self.assertIn('data-state="empty"', self.request().text)
        write_snapshot(snapshot(multi_source(fixture(occurrence('o')))), self.path)
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-state="ready"', response.text)
        self.app.state.signals_stale_seconds = 1
        self.assertIn('data-state="stale"', self.request().text)

    def test_html_escaping_and_unsafe_url(self):
        data = snapshot(multi_source(fixture(occurrence('o'))))
        attack = '<script>alert("x")</script>'
        data['candidates'][0]['evidence_references'][0]['text'] = attack
        row = data['candidates'][0]['chronology'][0]
        row.update(source_name=attack, title=attack, external_ref='javascript:alert(1)')
        write_snapshot(data, self.path)
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(attack, response.text)
        self.assertIn('&lt;script&gt;', response.text)
        self.assertNotIn('href="javascript:', response.text)
        self.assertEqual(response.text.count('class="signals-dynamic-row"'), 1)

    def test_existing_route_registration_and_health(self):
        paths = {route.path for route in self.app.routes}
        self.assertTrue({
            '/', '/health', '/materials', '/sources',
            '/theses', '/theses/export.csv', '/signals',
            '/signals/{candidate_id}/chronology',
        } <= paths)
        self.assertEqual(self.client.get('/health').json(), {'status': 'ok', 'service': 'mip-web'})

    def test_safe_external_link(self):
        data = snapshot(multi_source(fixture(occurrence('o'))))
        data['candidates'][0]['chronology'][0]['external_ref'] = 'https://example.test/?a=1&b=2'
        write_snapshot(data, self.path)
        response = self.request()
        self.assertIn('href="https://example.test/?a=1&amp;b=2"', response.text)
        self.assertIn('rel="noopener noreferrer"', response.text)


@unittest.skipUnless(importlib.util.find_spec('jinja2') is not None, 'BLOCKED: Jinja2 відсутня')
class TemplateTests(unittest.TestCase):
    def setUp(self):
        from web.app import templates
        self.template = templates.get_template('signals.html')
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'signals.json'

    def render(self, **kwargs):
        return self.template.render(active_page='signals', **load_signals(self.path, now=AS_OF + timedelta(minutes=1), **kwargs))

    def test_real_template_all_states(self):
        self.assertIn('data-state="missing"', self.render())
        self.path.write_text('{')
        self.assertIn('data-state="invalid"', self.render())
        write_snapshot(snapshot(fixture()), self.path)
        self.assertIn('data-state="empty"', self.render())
        write_snapshot(snapshot(multi_source(fixture(occurrence('o')))), self.path)
        self.assertIn('data-state="ready"', self.render())
        self.assertIn('data-state="stale"', self.render(stale_seconds=1))

    def test_real_template_escapes_evidence_and_metadata(self):
        data = snapshot(multi_source(fixture(occurrence('o'))))
        attack = '<script>alert("x")</script>'
        data['candidates'][0]['evidence_references'][0]['text'] = attack
        data['candidates'][0]['chronology'][0].update(source_name=attack, title=attack, external_ref='javascript:alert(1)')
        write_snapshot(data, self.path)
        html = self.render()
        self.assertNotIn(attack, html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('href="javascript:', html)
        self.assertEqual(html.count('class="signals-dynamic-row"'), 1)
        self.assertIn('aria-current="page">Сигнали', html)

    def test_real_template_safe_link_and_publication_time(self):
        data = snapshot(multi_source(fixture(occurrence('o'))))
        chronology = data['candidates'][0]['chronology']

        for index, row in enumerate(chronology):
            row['published_at'] = (
                AS_OF
                - timedelta(days=4)
                + timedelta(minutes=index)
            ).isoformat()

        row = chronology[0]
        row['external_ref'] = 'https://example.test/?a=1&b=2'

        write_snapshot(data, self.path)
        html = self.render()

        self.assertIn(
            'href="https://example.test/?a=1&amp;b=2"',
            html,
        )
        self.assertIn('rel="noopener noreferrer"', html)

        from web.timefmt import fmt_kyiv_zoned

        self.assertIn(
            fmt_kyiv_zoned(row['published_at']),
            html,
        )
        self.assertNotIn(
            fmt_kyiv_zoned(row['collected_at']),
            html,
        )

    def test_real_template_labels_collected_time_when_publication_missing(self):
        data = snapshot(
            multi_source(
                fixture(
                    occurrence('o')
                )
            )
        )

        row = data['candidates'][0]['chronology'][0]

        self.assertIsNone(row['published_at'])

        write_snapshot(data, self.path)
        html = self.render()

        from web.timefmt import fmt_kyiv_zoned

        self.assertIn('Зафіксовано', html)
        self.assertIn(
            fmt_kyiv_zoned(row['collected_at']),
            html,
        )

    def test_unselected_group_not_presented_as_measured_absence(self):
        from test_signals_postgres import FakeConnection, MODEL
        from reporting.signals_postgres import PostgresSignalAdapter
        data = snapshot(PostgresSignalAdapter(FakeConnection(), model_id=1, source_groups=['ru_space']).read(
            as_of=AS_OF, embedding_model=MODEL, dimension=1024))
        write_snapshot(data, self.path)
        html = self.render()
        self.assertEqual(html.count('class="signals-dynamic-row"'), 1)
        self.assertNotIn('ua_space: простір не включено у вибірку', html)
