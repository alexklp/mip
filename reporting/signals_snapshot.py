"""Версіонований JSON без БД, сервера та автоматичного запуску."""

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

from reporting.signals_data import SignalData, require_utc, windows, validate_routing_coverage
from reporting.signals_detector import (
    RecallConfig,
    algorithm_version,
    detect,
)


SCHEMA_VERSION = "signals/4"


def build_snapshot(data: SignalData, *, as_of: datetime, config: RecallConfig, generated_at: datetime | None = None) -> dict:
    """Без прихованого годинника: generated_at за замовчуванням дорівнює as_of.

    CLI runner передає фактичний час генерації явно. Однакові вхідні
    дані й часові параметри дають однаковий JSON незалежно від порядку рядків.
    """
    require_utc(as_of)
    generated_at = require_utc(generated_at if generated_at is not None else as_of)
    recall = {
        "max_distance": config.max_distance,
        "related_distance": config.related_distance,
        "max_related_links": config.max_related_links,
        "display_limit": config.display_limit,
        "distance_bands": list(config.distance_bands),
        "max_contents": config.max_contents,
        "max_pairs": config.max_pairs,
        "max_evidence": config.max_evidence,
        "evidence_chars": config.evidence_chars,
        "band_rule": "distance <= upper_bound",
        "distance_reference": "representative",
    }
    if config.merge_distance is not None:
        recall["merge_distance"] = config.merge_distance
        recall["merge_min_cross_links"] = config.merge_min_cross_links

    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": algorithm_version(config),
        "generated_at": generated_at.isoformat(),
        "as_of": as_of.isoformat(),
        "windows": {name: {"start": start.isoformat(), "end": end.isoformat(), "membership_field": "collected_at", "bounds": "[start,end)"} for name, (start, end) in windows(as_of).items()},
        "embedding_model": data.embedding_model,
        "embedding_dimension": data.dimension,
        "recall": recall,
        **detect(data, as_of=as_of, config=config),
    }




def deterministic_json(snapshot: dict) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def write_snapshot(snapshot: dict, destination: Path) -> None:
    """Атомарна заміна в тій самій директорії; збій до replace зберігає старе.

    Батьківська директорія має вже існувати. fsync захищає вміст файла;
    гарантія переживання втрати живлення для запису директорії не надається.
    """
    payload = deterministic_json(snapshot)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, prefix=".signals-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


def validate_snapshot(value: dict) -> None:
    """Перевіряємо контракт, який читає шаблон; довільний JSON не є snapshot."""
    def nonnegative(number):
        if type(number) is not int or number < 0:
            raise ValueError("Некоректний лічильник")

    def timestamp(text):
        if not isinstance(text, str):
            raise ValueError("Некоректний час")
        return require_utc(datetime.fromisoformat(text))

    def string(text, limit=2048):
        if not isinstance(text, str) or len(text) > limit:
            raise ValueError("Некоректний текст")

    def metrics(row):
        for key in ('occurrence_count', 'content_count', 'source_count'):
            nonnegative(row[key])
        if not row['source_count'] <= row['occurrence_count'] or not row['content_count'] <= row['occurrence_count']:
            raise ValueError("Неузгоджені лічильники")

    try:
        if value['schema_version'] != SCHEMA_VERSION:
            raise ValueError("Непідтримувана версія snapshot")
        recall = value['recall']
        config = RecallConfig(
            recall['max_distance'],
            tuple(recall['distance_bands']),
            recall['max_contents'],
            recall['max_pairs'],
            recall['max_evidence'],
            recall['evidence_chars'],
            recall['related_distance'],
            recall['max_related_links'],
            recall['display_limit'],
            merge_distance=recall.get('merge_distance'),
            merge_min_cross_links=recall.get('merge_min_cross_links', 2),
        )
        config.validate()
        expected_algorithm = algorithm_version(config)
        if (
            value['algorithm_version'] != expected_algorithm
            or recall['distance_reference'] != 'representative'
            or recall['band_rule'] != 'distance <= upper_bound'
        ):
            raise ValueError("Некоректний контракт алгоритму")
        as_of, generated = timestamp(value['as_of']), timestamp(value['generated_at'])
        if generated < as_of:
            raise ValueError("Генерація раніше as_of")
        string(value['embedding_model'])
        if not value['embedding_model'] or type(value['embedding_dimension']) is not int or value['embedding_dimension'] <= 0:
            raise ValueError("Некоректний контракт моделі")
        if set(value['windows']) != {'previous', 'current'} or set(value['coverage']) != {'previous', 'current'}:
            raise ValueError("Некоректні групи вікон")
        for name, (start, end) in windows(as_of).items():
            window = value['windows'][name]
            if timestamp(window['start']) != start or timestamp(window['end']) != end or window['membership_field'] != 'collected_at' or window['bounds'] != '[start,end)':
                raise ValueError("Некоректні вікна")
            if set(value['coverage'][name]) != {'ru_space', 'ua_space', 'excluded'}:
                raise ValueError("Некоректні групи джерел")
            for group in ('ru_space', 'ua_space', 'excluded'):
                row = value['coverage'][name][group]
                metrics(row)
                nonnegative(row['missing_published_at'])
                nonnegative(row['missing_embedding_content_count'])
                if row['missing_published_at'] > row['occurrence_count'] or row['missing_embedding_content_count'] > row['content_count']:
                    raise ValueError("Неузгоджена повнота потоку")
        validate_routing_coverage(value['routing_coverage'], value['coverage'])
        for flag in ('truncated', 'incomplete', 'critical_incomplete'):
            if type(value[flag]) is not bool:
                raise ValueError("Некоректний прапорець")
        if not isinstance(value['warnings'], list) or len(value['warnings']) > 30:
            raise ValueError("Некоректні попередження")
        for warning in value['warnings']:
            string(warning)
        if not isinstance(value['candidates'], list) or len(value['candidates']) > config.display_limit:
            raise ValueError("Забагато кандидатів")
        selection = value['selection']
        exact_strategy = (
            isinstance(selection, dict)
            and selection.get('strategy') == 'exact_current_24h'
        )
        if exact_strategy:
            from reporting.signals_postgres import PostgresLimits
            limits = PostgresLimits(**selection['limits'])
            limits.validate()

            if selection['source_groups'] != sorted(set(selection['source_groups'])):
                raise ValueError("Недетермінований scope груп")
            if set(selection['source_groups']) - {'ru_space', 'ua_space'}:
                raise ValueError("Некоректний scope груп")
            if selection['routing_policy'] != 'latest_analyze_or_maybe':
                raise ValueError("Некоректна routing policy")
            if selection['distance_method'] != 'exact_pgvector_cosine':
                raise ValueError("Некоректний exact distance contract")
            if selection['coverage_scope'] not in {
                'observed_source_groups_48h',
                'analytical_contour_source_groups_48h',
            }:
                raise ValueError("Некоректний coverage scope")
            if selection['query_plan_status'] != 'UNVERIFIED':
                raise ValueError("Некоректний query plan status")

            contour_id = selection.get('monitoring_contour_id')
            object_id = selection.get('object_id')

            if contour_id not in (None, 1):
                raise ValueError("Некоректний contour scope")

            if object_id is not None and (
                contour_id != 1
                or type(object_id) is not int
                or object_id <= 0
            ):
                raise ValueError("Некоректний object scope")

            expected_coverage_scope = (
                'observed_source_groups_48h'
                if contour_id is None
                else 'analytical_contour_source_groups_48h'
            )

            if selection['coverage_scope'] != expected_coverage_scope:
                raise ValueError("Coverage scope не відповідає analytical scope")

            for key in (
                'current_content_count',
                'selected_content_count',
                'selected_occurrence_count',
                'pair_count',
            ):
                nonnegative(selection[key])

            if selection['pair_count'] > limits.pairs:
                raise ValueError("Перевищено exact pair budget")
            if type(selection['pair_limit_reached']) is not bool:
                raise ValueError("Некоректний pair limit flag")
            if type(selection['row_limit_reached']) is not bool:
                raise ValueError("Некоректний row limit flag")

            selection = None

        if selection is not None:
            if selection.get('strategy') != 'sparse_hnsw_current_24h':
                raise ValueError("Некоректна ANN strategy")

            for key in (
                'current_content_count',
                'anchor_count',
                'searched_anchor_count',
                'pair_count',
                'inspected_pair_count',
                'selected_content_count',
                'selected_occurrence_count',
            ):
                nonnegative(selection[key])

            string(selection['query_plan_status'])

            if (
                not isinstance(selection['source_groups'], list)
                or not selection['source_groups']
                or set(selection['source_groups'])
                - {'ru_space', 'ua_space'}
            ):
                raise ValueError("Некоректний scope джерел")

            if (
                selection['source_groups']
                != sorted(set(selection['source_groups']))
            ):
                raise ValueError("Недетермінований scope груп")

            from reporting.signals_postgres import PostgresLimits

            limits = PostgresLimits(**selection['limits'])
            limits.validate()

            if selection['routing_policy'] != 'latest_analyze_or_maybe':
                raise ValueError("Некоректна routing policy")

            if (
                selection['coverage_scope']
                != 'observed_source_groups_48h'
            ):
                raise ValueError("Некоректний coverage scope")

            if selection['current_content_count'] < selection['anchor_count']:
                raise ValueError("Некоректний anchor count")

            if selection['anchor_count'] > limits.anchors:
                raise ValueError("Перевищено sparse anchor budget")

            if (
                selection['searched_anchor_count']
                != selection['anchor_count']
            ):
                raise ValueError("Не всі sparse anchors оброблено")

            if selection['pair_count'] > limits.pairs:
                raise ValueError("Перевищено sparse pair budget")

            for flag in (
                'anchor_limit_reached',
                'pair_limit_reached',
                'row_limit_reached',
            ):
                if type(selection[flag]) is not bool:
                    raise ValueError("Некоректний sparse limit flag")

            ann = selection['ann']
            if (
                ann['method'] != 'hnsw'
                or ann['index'] != 'idx_embeddings_hnsw_bge_m3'
                or ann['iterative_scan'] != 'relaxed_order'
                or ann['filter_stage'] != 'inside_knn'
                or ann['recall_status'] != 'UNVERIFIED'
                or ann['prepared'] is not False
                or ann['probe_limit'] != limits.ann_probe_limit
                or ann['max_scan_tuples'] != limits.ann_probe_limit
                or ann['k'] != limits.neighbours
                or type(ann['ef_search']) is not int
                or not 1 <= ann['ef_search'] <= 1000
            ):
                raise ValueError("Некоректні sparse ANN metadata")

        presentation = value['presentation']
        selected_ids = presentation['selected_content_ids']
        if (not isinstance(selected_ids, list) or len(selected_ids) > config.max_contents
                or any(not isinstance(cid, str) or not cid for cid in selected_ids)
                or selected_ids != sorted(set(selected_ids))):
            raise ValueError("Некоректні відібрані content IDs")
        for key in ('core_group_count', 'eligible_candidate_count', 'displayed_candidate_count',
                    'related_links_available', 'related_link_count', 'display_limit'):
            nonnegative(presentation[key])
        if config.merge_distance is not None:
            nonnegative(presentation['strict_core_group_count'])
            if presentation['strict_core_group_count'] < presentation['core_group_count']:
                raise ValueError("Merge не може збільшувати кількість strict cores")
        elif 'strict_core_group_count' in presentation:
            raise ValueError("Неочікувана merge metadata")

        suppressed = presentation['suppressed']
        if set(suppressed) != {'singleton_single_source', 'repeated_content_single_source', 'core_single_source'}:
            raise ValueError("Некоректні причини suppression")
        for n in suppressed.values():
            nonnegative(n)
        if (presentation['core_group_count'] > len(selected_ids)
                or presentation['core_group_count'] != presentation['eligible_candidate_count'] + sum(suppressed.values())
                or presentation['display_limit'] != config.display_limit
                or presentation['displayed_candidate_count'] != len(value['candidates'])
                or len(value['candidates']) != min(config.display_limit, presentation['eligible_candidate_count'])):
            raise ValueError("Неузгоджені presentation counts")
        for key, expected in (
                ('display_limit_reached', presentation['eligible_candidate_count'] > len(value['candidates'])),
                ('related_limit_reached', presentation['related_links_available'] > presentation['related_link_count'])):
            if type(presentation[key]) is not bool or presentation[key] != expected:
                raise ValueError("Неузгоджені presentation flags")
        links = value['related_links']
        if (not isinstance(links, list) or len(links) != presentation['related_link_count']
                or len(links) != min(config.max_related_links, presentation['related_links_available'])
                or presentation['related_links_available'] > config.max_pairs):
            raise ValueError("Неузгоджений ліміт related links")
        seen_links = set()
        for link in links:
            a, b, d = link['left_content_id'], link['right_content_id'], link['distance']
            string(a)
            string(b)
            if (a not in selected_ids or b not in selected_ids or a >= b or (a, b) in seen_links or type(d) not in (int, float)
                    or not config.max_distance < d <= config.related_distance
                    or link['interpretation_status'] != 'unverified'):
                raise ValueError("Некоректний related review link")
            seen_links.add((a, b))
        if links != sorted(links, key=lambda r: (r['distance'], r['left_content_id'], r['right_content_id'])):
            raise ValueError("Недетермінований порядок related links")
        seen_candidates, seen_contents, seen_occurrences = set(), set(), set()
        for candidate in value['candidates']:
            forbidden = {'semantic_verdict', 'relation_label', 'same_thesis', 'borrowing', 'independent_confirmation'}
            if forbidden.intersection(candidate):
                raise ValueError("Snapshot містить заборонене semantic verdict поле")
            string(candidate['candidate_id'])
            if candidate['interpretation_status'] != 'unverified':
                raise ValueError("Snapshot не може містити семантичний вердикт")
            metrics(candidate)
            if candidate["source_count"] < 2:
                raise ValueError("Core candidate потребує двох distinct sources")
            candidate_last = timestamp(candidate["last_observed"])
            if exact_strategy:
                freshness_start, freshness_end = windows(as_of)["current"]
            else:
                freshness_start = windows(as_of)["previous"][0]
                freshness_end = as_of
            if not freshness_start <= candidate_last < freshness_end:
                raise ValueError("Некоректна свіжість candidate")
            cid = candidate['candidate_id']
            members = candidate['content_ids']
            if not isinstance(members, list) or not members or len(members) > config.max_contents:
                raise ValueError("Некоректні учасники пакета")
            for member in members:
                string(member)
            if cid in seen_candidates or len(set(members)) != len(members) or set(members) & seen_contents or candidate['representative_content_id'] != cid or members[0] != cid:
                raise ValueError("Неузгоджені ідентифікатори пакетів")
            seen_candidates.add(cid)
            seen_contents.update(members)
            if candidate['content_count'] != len(members) or candidate['occurrence_count'] == 0:
                raise ValueError("Неузгоджений розмір пакета")
            distances = candidate['distances_to_representative']
            if not isinstance(distances, list) or len(distances) != len(members) or {row['content_id'] for row in distances} != set(members):
                raise ValueError("Неузгоджені відстані пакета")
            for row in distances:
                distance = row['distance']
                if distance is None:
                    if (
                        row['distance_band'] != 'unavailable'
                        or (config.merge_distance is None and len(members) != 1)
                    ):
                        raise ValueError("Невідома відстань у семантичній групі")
                elif (
                    type(distance) not in (float, int)
                    or not 0 <= distance <= (
                        2 if config.merge_distance is not None else config.max_distance
                    )
                    or row['distance_band'] != config.band(distance)
                    or (row['content_id'] == cid and distance != 0)
                ):
                    raise ValueError("Некоректна відстань пакета")
            nonnegative(candidate['evidence_omitted_count'])
            if set(candidate['dynamics']) != {'ru_space', 'ua_space'}:
                raise ValueError("Некоректні групи динаміки")
            for group in ('ru_space', 'ua_space'):
                dynamics = candidate['dynamics'][group]
                for name in ('current', 'previous'):
                    metrics(dynamics[name])
                    nonnegative(dynamics[name]['observed_flow_count'])
                    denominator = value['coverage'][name][group]['occurrence_count']
                    if dynamics[name]['observed_flow_count'] != denominator or dynamics[name]['occurrence_count'] > denominator:
                        raise ValueError("Неузгоджений знаменник")
                    share = dynamics[name]['share']
                    if share is not None and (type(share) not in (int, float) or not 0 <= share <= 1):
                        raise ValueError("Некоректна частка")
                    expected_share = dynamics[name]['occurrence_count'] / denominator if denominator else None
                    if share != expected_share:
                        raise ValueError("Неузгоджена частка")
                if type(dynamics['delta']) is not int or dynamics['delta'] != dynamics['current']['occurrence_count'] - dynamics['previous']['occurrence_count']:
                    raise ValueError("Некоректна динаміка")
            total = sum(
                candidate['dynamics'][group][name]['occurrence_count']
                for group in ('ru_space', 'ua_space')
                for name in ('current', 'previous')
            )
            groups = sorted(
                group
                for group in ('ru_space', 'ua_space')
                if any(
                    candidate['dynamics'][group][name]['occurrence_count']
                    for name in ('current', 'previous')
                )
            )
            if total != candidate['occurrence_count']:
                raise ValueError("Динаміка не відповідає розміру пакета")
            if candidate['source_groups'] != groups or type(candidate['cross_space']) is not bool or candidate['cross_space'] != (len(groups) == 2):
                raise ValueError("Неузгоджені простори пакета")
            for key in ('chronology', 'evidence_references'):
                if not isinstance(candidate[key], list) or not candidate[key] or len(candidate[key]) > config.max_evidence:
                    raise ValueError("Забагато evidence")
            if len(candidate['chronology']) + candidate['evidence_omitted_count'] != candidate['occurrence_count']:
                raise ValueError("Неузгоджений ліміт evidence")
            for row in candidate['chronology']:
                for key in ('source_id', 'source_group', 'source_name', 'source_type', 'title', 'external_ref', 'content_id', 'occurrence_id'):
                    string(row[key])
                if row['content_id'] not in members or row['source_group'] not in groups or row['occurrence_id'] in seen_occurrences:
                    raise ValueError("Evidence не відповідає пакету")
                seen_occurrences.add(row['occurrence_id'])
                collected = timestamp(row['collected_at'])
                evidence_start = windows(as_of)['previous'][0]
                evidence_end = as_of
                if not evidence_start <= collected < evidence_end:
                    raise ValueError("Evidence поза дозволеним вікном")
                if row['published_at'] is not None:
                    timestamp(row['published_at'])
            exact = candidate['exact_republication_content_ids']
            if not isinstance(exact, list) or len(set(exact)) != len(exact) or set(exact) - set(members):
                raise ValueError("Некоректні exact republications")
            if candidate['evidence_omitted_count'] == 0:
                chronology = candidate['chronology']
                if (len({r['source_id'] for r in chronology}) != candidate['source_count']
                        or {r['content_id'] for r in chronology} != set(members)
                        or max(timestamp(r['collected_at']) for r in chronology) != timestamp(candidate['last_observed'])):
                    raise ValueError("Core aggregates не відповідають повній chronology")
                expected_exact = [cid for cid in members if len({r['source_id'] for r in chronology if r['content_id'] == cid}) >= 2]
                if exact != expected_exact:
                    raise ValueError("Exact republication потребує distinct sources")
            reference_ids = [row['content_id'] for row in candidate['evidence_references']]
            if len(set(reference_ids)) != len(reference_ids) or set(reference_ids) - set(members):
                raise ValueError("Некоректні посилання evidence")
            for row in candidate['evidence_references']:
                string(row['content_id'])
                string(row['text'], config.evidence_chars)
                if type(row['text_truncated']) is not bool:
                    raise ValueError("Некоректний прапорець evidence")

        ranked = sorted(value['candidates'], key=lambda c: (
            -c['source_count'],
            -c['cross_space'],
            -len(c['exact_republication_content_ids']),
            -timestamp(c['last_observed']).timestamp(),
            -c['content_count'],
            c['candidate_id'],
        ))
        if ranked != value['candidates']:
            raise ValueError("Некоректне ранжування candidates")
        if seen_contents - set(selected_ids) or len(seen_contents) > config.max_contents:
            raise ValueError("Перевищено ліміт матеріалів snapshot")
        membership = {cid: c['candidate_id'] for c in value['candidates'] for cid in c['content_ids']}
        if any(link['left_content_id'] in membership and
               membership[link['left_content_id']] == membership.get(link['right_content_id']) for link in links):
            raise ValueError("Related link не може належати одному core")
        if ((value['truncated'] or any(value['routing_coverage'][w][g]['missing']
                for w in ('previous', 'current') for g in ('ru_space', 'ua_space'))) and not value['incomplete']):
            raise ValueError("Неповноту не можна приховувати")
        # Відхиляємо NaN/Infinity також у полях, які не показує шаблон.
        if len(deterministic_json(value).encode('utf-8')) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Snapshot перевищує ліміт розміру")
    except (KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Пошкоджений контракт snapshot") from exc


def main(argv=None) -> int:
    """Одноразовий запуск; періодичність задає окремий дозволений оператором таймер."""
    import argparse
    from datetime import timezone
    import sys
    from reporting.signals_postgres import PostgresLimits, PostgresSignalAdapter, WATERMARK_SQL
    from reporting.signals_exact import ExactPostgresSignalAdapter

    parser = argparse.ArgumentParser(description="Read-only генерація signals snapshot")
    parser.add_argument('--as-of', required=True, help='UTC timestamp або common для спільного watermark груп')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--database', default='mip_dev')
    parser.add_argument('--model-id', required=True, type=int)
    parser.add_argument('--model', required=True, help='model_name@model_revision')
    parser.add_argument('--dimension', required=True, type=int)
    parser.add_argument('--source-groups', nargs='+', required=True, choices=('ru_space', 'ua_space'))
    parser.add_argument(
        '--contour-id',
        type=int,
        default=None,
        help='Аналітичний scope контуру; наразі підтримується лише C1',
    )
    parser.add_argument(
        '--object-id',
        type=int,
        default=None,
        help='Обмежити C1 Signals одним об\'єктом',
    )
    parser.add_argument(
        '--strategy',
        choices=('bounded_ann', 'exact_current_24h'),
        default='bounded_ann',
    )
    parser.add_argument('--anchors', type=int, required=True)
    parser.add_argument('--neighbours', type=int, required=True)
    parser.add_argument('--pairs', type=int, required=True)
    parser.add_argument('--rows', type=int, required=True)
    parser.add_argument('--max-distance', '--core-distance', type=float, default=0.18)
    parser.add_argument('--related-distance', type=float, default=0.36)
    parser.add_argument(
        '--merge-distance',
        type=float,
        default=None,
        help='Другий етап merge strict cores; за замовчуванням вимкнений',
    )
    parser.add_argument(
        '--merge-min-cross-links',
        type=int,
        default=2,
        help='Мінімальна кількість content-pairs між strict cores для merge',
    )
    parser.add_argument('--max-related-links', type=int, default=100)
    parser.add_argument('--display-limit', type=int, default=20)
    parser.add_argument('--ann-probe-limit', type=int, default=100)
    parser.add_argument('--distance-bands', type=float, nargs='*', default=[])
    parser.add_argument('--evidence-chars', type=int, default=600)
    parser.add_argument('--max-evidence', type=int, default=12)
    parser.add_argument('--statement-timeout-ms', type=int, default=15000)
    parser.add_argument('--benchmark', action='store_true', help='Лише SELECT та час виконання; без запису snapshot')
    args = parser.parse_args(argv)
    connection = None
    stage = 'перевірка аргументів'
    try:
        as_of = None if args.as_of == 'common' else require_utc(datetime.fromisoformat(args.as_of))
        now = datetime.now(timezone.utc)
        if as_of is not None and as_of > now:
            raise ValueError("as_of не може бути в майбутньому")
        if args.benchmark == (args.output is not None):
            raise ValueError("Потрібен або --output, або --benchmark")
        if args.output is not None:
            root = Path(__file__).resolve().parents[1]
            if not args.output.resolve().is_relative_to(root):
                raise ValueError("Output має бути всередині репозиторію")
        if args.dimension <= 0 or args.model_id <= 0 or not all(args.model.split("@")) or "@" not in args.model:
            raise ValueError("Некоректний контракт моделі")
        if len(set(args.source_groups)) != len(args.source_groups):
            raise ValueError("Повторені групи джерел")

        if args.object_id is not None and args.contour_id is None:
            raise ValueError("object-id потребує contour-id")

        if args.contour_id is not None:
            if args.contour_id != 1:
                raise ValueError(
                    "Scoped Signals наразі підтримує лише C1"
                )
            if args.strategy != 'exact_current_24h':
                raise ValueError(
                    "Scoped Signals потребує exact_current_24h"
                )

        if args.object_id is not None and args.object_id <= 0:
            raise ValueError("Некоректний object-id")
        limits = PostgresLimits(args.anchors, args.neighbours, args.pairs, args.rows, args.evidence_chars, args.statement_timeout_ms, args.ann_probe_limit)
        limits.validate()
        max_contents = min(args.rows, 50000)
        config = RecallConfig(args.max_distance, tuple(args.distance_bands),
            max_contents=max_contents, max_pairs=args.pairs,
            max_evidence=args.max_evidence, evidence_chars=args.evidence_chars,
            related_distance=args.related_distance,
            max_related_links=args.max_related_links,
            display_limit=args.display_limit,
            merge_distance=args.merge_distance,
            merge_min_cross_links=args.merge_min_cross_links)
        config.validate()

        # Не читаємо .env та конфігурацію застосунку. Libpq відкриває лише
        # явно запитане оператором підключення; секрети не друкуються.
        stage = 'доступність драйвера'
        import psycopg
        from psycopg.rows import dict_row
        stage = 'read-only підключення'
        connection = psycopg.connect(dbname=args.database, row_factory=dict_row,
            connect_timeout=5, autocommit=True, options=f'-c default_transaction_read_only=on -c default_transaction_isolation=repeatable\\ read -c statement_timeout={limits.statement_timeout_ms}')
        with connection.cursor() as cursor:
            cursor.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            if as_of is None:
                cursor.execute(WATERMARK_SQL, {'groups': sorted(args.source_groups)})
                watermarks = {r['source_group']: require_utc(r['watermark']) for r in cursor.fetchall()}
                if set(watermarks) != set(args.source_groups):
                    raise ValueError("Немає watermark для кожної вибраної групи")
                as_of = min(watermarks.values())
        if args.strategy == 'exact_current_24h':
            critical_distance = (
                args.merge_distance
                if args.merge_distance is not None
                else args.core_distance
            )
            adapter = ExactPostgresSignalAdapter(
                connection,
                model_id=args.model_id,
                source_groups=args.source_groups,
                limits=limits,
                max_distance=args.related_distance,
                critical_distance=critical_distance,
                contour_id=args.contour_id,
                object_id=args.object_id,
            )
        else:
            adapter = PostgresSignalAdapter(
                connection,
                model_id=args.model_id,
                source_groups=args.source_groups,
                limits=limits,
                max_distance=args.related_distance,
            )
        stage = 'SELECT та контракт даних'
        data = adapter.read(as_of=as_of, embedding_model=args.model, dimension=args.dimension)
        stage = 'валідація snapshot'
        result = build_snapshot(
            data,
            as_of=as_of,
            config=config,
            generated_at=datetime.now(timezone.utc),
        )
        validate_snapshot(result)
        if args.benchmark:
            print(deterministic_json({
                'status': 'UNVERIFIED',
                'algorithm_version': result['algorithm_version'],
                'recall': result['recall'],
                'timings': adapter.timings,
                'as_of': result['as_of'],
                'routing_coverage': result['routing_coverage'],
                'presentation': result['presentation'],
                'selection': result['selection'],
                'critical_incomplete': result['critical_incomplete'],
            }), end='')
        if result['critical_incomplete']:
            stage = 'критично неповні evidence'
            raise ValueError("Критично неповні evidence; попередній snapshot збережено")
        if not args.benchmark:
            stage = 'атомарний запис'
            write_snapshot(result, args.output)
        return 0
    except Exception as exc:
        # Текст винятку драйвера може містити деталі підключення.
        print('Signals: ' + stage + '; snapshot не оновлено (' + type(exc).__name__ + ').', file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
