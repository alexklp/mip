"""Обмежений SELECT-only адаптер. План та швидкодія потребують live benchmark."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import time

from reporting.signals_data import Content, Occurrence, SignalData, Source, SPACES, ROUTING_DECISIONS, windows


@dataclass(frozen=True)
class PostgresLimits:
    anchors: int = 40
    neighbours: int = 10
    pairs: int = 400
    rows: int = 5000
    evidence_chars: int = 600
    statement_timeout_ms: int = 15000
    ann_probe_limit: int = 100

    def validate(self):
        for name, ceiling in (
            ("anchors", 20000),
            ("neighbours", 50),
            ("pairs", 200000),
            ("rows", 100000),
            ("evidence_chars", 2000),
            ("ann_probe_limit", 50000),
            ("statement_timeout_ms", 120000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("Некоректний ліміт " + name)
        if self.ann_probe_limit < self.neighbours + 1:
            raise ValueError("ANN scan budget має бути не меншим за k сусідів")


GUARD_SQL = """
SELECT current_setting('transaction_read_only') AS read_only,
       current_setting('transaction_isolation') AS isolation,
       current_setting('statement_timeout') AS timeout
"""
ANN_SETTINGS_SQL = """
SELECT
    set_config('hnsw.iterative_scan', 'relaxed_order', true) AS iterative_scan,
    set_config('hnsw.ef_search', '64', true) AS ef_search,
    set_config(
        'hnsw.max_scan_tuples',
        %(ann_probe_limit)s::text,
        true
    ) AS max_scan_tuples
"""
PLANNER_SORT_READ_SQL = """
SELECT current_setting('enable_sort') AS enable_sort
"""
PLANNER_SORT_SET_SQL = """
SELECT set_config(
    'enable_sort',
    %(enable_sort)s,
    true
) AS enable_sort
"""
MODEL_SQL = """
SELECT model_name, model_revision, dimension, metric
FROM embedding_models WHERE embedding_model_id = %(model_id)s
"""
LATEST_ROUTING_SQL = """
SELECT r.decision FROM content_routing_decisions r
WHERE r.content_id = io.content_id
ORDER BY r.routing_version DESC, r.created_at DESC, r.routing_id DESC
LIMIT 1
"""
WATERMARK_SQL = """
SELECT s.source_group, max(io.collected_at) AS watermark
FROM item_occurrences io JOIN sources s USING (source_id)
WHERE s.source_group = ANY(%(groups)s) AND io.collected_at <= CURRENT_TIMESTAMP
GROUP BY s.source_group
"""
COVERAGE_SQL = """
SELECT CASE WHEN io.collected_at < %(middle)s THEN 'previous' ELSE 'current' END AS window,
       s.source_group, count(*) AS occurrence_count,
       count(DISTINCT io.content_id) AS content_count,
       count(DISTINCT io.source_id) AS source_count,
       count(*) FILTER (WHERE io.published_at IS NULL) AS missing_published_at,
       count(DISTINCT io.content_id) FILTER (WHERE e.content_id IS NULL)
           AS missing_embedding_content_count
FROM item_occurrences io JOIN sources s USING (source_id)
LEFT JOIN embeddings e ON e.content_id = io.content_id AND e.embedding_model_id = %(model_id)s
WHERE io.collected_at >= %(start)s AND io.collected_at < %(as_of)s
  AND s.source_group = ANY(%(groups)s)
GROUP BY 1, 2
"""
ROUTING_COVERAGE_SQL = """
SELECT window_name AS window, source_group, COALESCE(decision, 'missing') AS decision, count(*) AS content_count
FROM (
    SELECT DISTINCT CASE WHEN io.collected_at < %(middle)s THEN 'previous' ELSE 'current' END AS window_name,
           s.source_group, io.content_id, (""" + LATEST_ROUTING_SQL + """) AS decision
    FROM item_occurrences io JOIN sources s USING (source_id)
    WHERE io.collected_at >= %(start)s AND io.collected_at < %(as_of)s
      AND s.source_group = ANY(%(groups)s)
) observed
GROUP BY 1, 2, 3
"""
ANCHOR_SQL = """
SELECT
    e.content_id,
    ci.content_hash,
    count(*) OVER () AS available
FROM embeddings e
JOIN content_items ci
  ON ci.content_id = e.content_id
WHERE e.embedding_model_id = 1
  AND e.embedding_model_id = %(model_id)s
  AND vector_dims(e.embedding) = %(dimension)s
  AND (""" + LATEST_ROUTING_SQL.replace(
      "io.content_id", "e.content_id"
  ) + """) IN ('analyze', 'maybe')
  AND EXISTS (
      SELECT 1
      FROM item_occurrences io
      JOIN sources src USING (source_id)
      WHERE io.content_id = e.content_id
        AND io.collected_at >= %(middle)s
        AND io.collected_at < %(as_of)s
        AND src.source_group = ANY(%(groups)s)
  )
ORDER BY ci.content_hash, e.content_id
LIMIT %(anchor_probe)s
"""

# Усі current-24h anchors обробляються одним batch query.
# Eligibility сусідів знаходиться ВСЕРЕДИНІ KNN scan, тому iterative HNSW
# може продовжувати пошук після routing/time/source filters.
NEIGHBOUR_SQL = """
WITH anchor_embeddings AS MATERIALIZED (
    SELECT
        e.content_id,
        e.embedding::vector(1024) AS embedding
    FROM embeddings e
    WHERE e.embedding_model_id = 1
      AND e.embedding_model_id = %(model_id)s
      AND e.content_id = ANY(%(anchor_ids)s::uuid[])
),
knn AS MATERIALIZED (
    SELECT
        a.content_id AS anchor_id,
        n.content_id AS neighbour_id,
        n.distance
    FROM anchor_embeddings a
    CROSS JOIN LATERAL (
        SELECT
            e2.content_id,
            e2.embedding::vector(1024) <=> a.embedding AS distance
        FROM embeddings e2
        WHERE e2.embedding_model_id = 1
          AND e2.embedding_model_id = %(model_id)s
          AND e2.content_id <> a.content_id
          AND (""" + LATEST_ROUTING_SQL.replace(
              "io.content_id", "e2.content_id"
          ) + """) IN ('analyze', 'maybe')
          AND EXISTS (
              SELECT 1
              FROM item_occurrences io
              JOIN sources src USING (source_id)
              WHERE io.content_id = e2.content_id
                AND io.collected_at >= %(start)s
                AND io.collected_at < %(as_of)s
                AND src.source_group = ANY(%(groups)s)
          )
        ORDER BY e2.embedding::vector(1024) <=> a.embedding
        LIMIT %(neighbour_limit)s
    ) n
),
dedup AS (
    SELECT
        LEAST(anchor_id, neighbour_id) AS left_content_id,
        GREATEST(anchor_id, neighbour_id) AS right_content_id,
        min(distance) AS distance
    FROM knn
    WHERE distance <= %(max_distance)s
    GROUP BY 1, 2
)
SELECT
    left_content_id,
    right_content_id,
    distance,
    count(*) OVER () AS available
FROM dedup
ORDER BY distance, left_content_id, right_content_id
LIMIT %(pair_probe)s
"""
ROWS_SQL = """
SELECT io.occurrence_id, io.content_id, io.source_id, io.collected_at, io.published_at,
       left(io.external_ref, 2048) AS external_ref,
       left(s.name, 240) AS source_name, s.source_type, s.source_group,
       ci.content_hash,
       (""" + LATEST_ROUTING_SQL + """) AS routing_decision,
       left(ci.title, 240) AS title, left(ci.text_content, %(text_probe)s) AS text,
       vector_dims(e.embedding) AS dimension, e.embedding <=> e.embedding AS self_distance
FROM item_occurrences io JOIN sources s USING (source_id)
JOIN content_items ci USING (content_id)
JOIN embeddings e ON e.content_id = io.content_id AND e.embedding_model_id = %(model_id)s
WHERE io.content_id = ANY(%(ids)s::uuid[])
  AND io.collected_at >= %(start)s AND io.collected_at < %(as_of)s
  AND s.source_group = ANY(%(groups)s)
  AND (""" + LATEST_ROUTING_SQL + """) IN ('analyze', 'maybe')
ORDER BY io.collected_at DESC, ci.content_hash, s.name, io.external_ref, io.occurrence_id
LIMIT %(row_probe)s
"""
DIMENSION_SQL = """
SELECT e.content_id FROM embeddings e
WHERE e.embedding_model_id = %(model_id)s AND vector_dims(e.embedding) <> %(dimension)s
  AND EXISTS (SELECT 1 FROM item_occurrences io JOIN sources s USING (source_id)
      WHERE io.content_id = e.content_id AND io.collected_at >= %(start)s
      AND io.collected_at < %(as_of)s AND s.source_group = ANY(%(groups)s))
LIMIT %(invalid_limit)s
"""


def round_robin_anchors(group_rows, limit):
    """Чергуємо відсортовані групи, пропускаючи вже відібрані content IDs.

    Черги SQL впорядковані за свіжістю, джерелами та content hash.
    Кожний ID належить лише одному ходу; порожня група не витрачає квоту.
    """
    from collections import deque
    queues = {group: deque(rows) for group, rows in sorted(group_rows.items())}
    result, seen = [], set()
    selected = dict.fromkeys(queues, 0)
    while len(result) < limit:
        advanced = False
        for group, queue in queues.items():
            while queue and str(queue[0]['content_id']) in seen:
                queue.popleft()
            if queue and len(result) < limit:
                row = queue.popleft()
                result.append(row)
                seen.add(str(row['content_id']))
                selected[group] += 1
                advanced = True
        if not advanced:
            break
    metadata = {}
    for group, rows in sorted(group_rows.items()):
        available = int(rows[0]['available']) if rows else 0
        covered = sum(str(row['content_id']) in seen for row in rows)
        metadata[group] = dict(selected=selected[group], available=available,
                               limit_reached=available > covered)
    return result, metadata


def timeout_ms(value):
    """PostgreSQL виводить одиниці залежно від точності значення."""
    value = str(value).strip()
    for suffix, multiplier in (("ms", 1), ("min", 60000), ("s", 1000), ("h", 3600000)):
        if value.endswith(suffix):
            return float(value[:-len(suffix)]) * multiplier
    return float(value)


class PostgresSignalAdapter:
    """Викликаючий код надає dict-row connection у read-only repeatable-read.

    Жодного SET, DDL або DML. Усі запити мають параметри. Benchmark також
    виконує лише SELECT; EXPLAIN/ANALYZE автоматично не запускається.
    """

    def __init__(self, connection, *, model_id, source_groups=SPACES,
                 limits=PostgresLimits(), max_distance=0.36):
        limits.validate()
        if type(model_id) is not int or model_id <= 0:
            raise ValueError("Потрібен додатний model id")
        if not source_groups or len(set(source_groups)) != len(source_groups) or set(source_groups) - set(SPACES):
            raise ValueError("Некоректні групи джерел")
        if not math.isfinite(max_distance) or not 0 <= max_distance <= 2:
            raise ValueError("Некоректний поріг відстані")
        self.connection, self.model_id = connection, model_id
        self.groups, self.limits, self.max_distance = tuple(sorted(source_groups)), limits, max_distance
        self.timings = []

    def _query(self, name, sql, params):
        started = time.monotonic()
        with self.connection.cursor() as cursor:
            if name == "neighbours":
                # Для filtered kNN planner інколи обирає повний explicit
                # sort замість HNSW. Тимчасово забороняємо цей шлях лише
                # для одного neighbour query та відновлюємо попередній
                # transaction-local стан одразу після успішного SELECT.
                cursor.execute(
                    PLANNER_SORT_READ_SQL,
                    {},
                )
                setting_rows = cursor.fetchall()

                if (
                    len(setting_rows) != 1
                    or setting_rows[0]["enable_sort"]
                    not in ("on", "off")
                ):
                    raise ValueError(
                        "Не вдалося прочитати planner enable_sort"
                    )

                previous_enable_sort = (
                    setting_rows[0]["enable_sort"]
                )

                cursor.execute(
                    PLANNER_SORT_SET_SQL,
                    {"enable_sort": "off"},
                )
                disabled_rows = cursor.fetchall()

                if (
                    len(disabled_rows) != 1
                    or disabled_rows[0]["enable_sort"] != "off"
                ):
                    raise ValueError(
                        "Не вдалося вимкнути planner sort для HNSW"
                    )

                # Не переходимо на generic plan після
                # psycopg prepare_threshold.
                cursor.execute(
                    sql,
                    params,
                    prepare=False,
                )
                rows = cursor.fetchall()

                cursor.execute(
                    PLANNER_SORT_SET_SQL,
                    {
                        "enable_sort":
                            previous_enable_sort,
                    },
                )
                restored_rows = cursor.fetchall()

                if (
                    len(restored_rows) != 1
                    or restored_rows[0]["enable_sort"]
                    != previous_enable_sort
                ):
                    raise ValueError(
                        "Не вдалося відновити planner enable_sort"
                    )
            else:
                cursor.execute(
                    sql,
                    params,
                )
                rows = cursor.fetchall()

        self.timings.append({
            "query": name,
            "seconds": time.monotonic() - started,
            "rows": len(rows),
        })
        return rows

    def read(self, *, as_of: datetime, embedding_model: str, dimension: int) -> SignalData:
        bounds = windows(as_of)
        if type(dimension) is not int or dimension <= 0 or not embedding_model:
            raise ValueError("Потрібні модель та розмірність")
        self.timings = []
        p = dict(start=bounds['previous'][0], middle=bounds['current'][0], as_of=as_of,
                 groups=list(self.groups), model_id=self.model_id, dimension=dimension,
                 anchor_probe=self.limits.anchors + 1,
                 pair_probe=self.limits.pairs + 1,
                 row_probe=self.limits.rows + 1,
                 text_probe=self.limits.evidence_chars + 1,
                 invalid_limit=1,
                 neighbour_limit=self.limits.neighbours,
                 max_distance=self.max_distance,
                 ann_probe_limit=self.limits.ann_probe_limit)
        guard = self._query("guard", GUARD_SQL, {})[0]
        timeout = timeout_ms(guard['timeout'])
        if guard['read_only'] != 'on' or guard['isolation'] != 'repeatable read' or not 0 < timeout <= self.limits.statement_timeout_ms:
            raise ValueError("Потрібні read-only, repeatable read та обмежений statement_timeout")
        models = self._query("model", MODEL_SQL, p)
        if len(models) != 1 or models[0]['model_name'] + '@' + models[0]['model_revision'] != embedding_model or models[0]['dimension'] != dimension or models[0]['metric'] != 'cosine':
            raise ValueError("Реєстр моделі не відповідає очікуваному контракту")
        if self.model_id != 1 or dimension != 1024:
            raise ValueError("Наявний HNSW підтримує лише model_id=1 та dimension=1024")
        if self._query("dimension", DIMENSION_SQL, p):
            raise ValueError("Некоректна розмірність embedding у спостережуваному потоці")
        ann_settings = self._query(
            "ann_settings",
            ANN_SETTINGS_SQL,
            p,
        )[0]
        if ann_settings['iterative_scan'] != 'relaxed_order':
            raise ValueError("Потрібен hnsw.iterative_scan=relaxed_order")
        ef_search = int(ann_settings['ef_search'])
        max_scan_tuples = int(ann_settings['max_scan_tuples'])
        if not 1 <= ef_search <= 1000:
            raise ValueError("Некоректний hnsw.ef_search")
        if max_scan_tuples != self.limits.ann_probe_limit:
            raise ValueError("Некоректний hnsw.max_scan_tuples")
        keys = ('occurrence_count', 'content_count', 'source_count', 'missing_published_at', 'missing_embedding_content_count')
        coverage = {w: {g: dict.fromkeys(keys, 0) for g in (*SPACES, 'excluded')} for w in bounds}
        for row in self._query("coverage", COVERAGE_SQL, p):
            coverage[row['window']][row['source_group']] = {k: int(row[k]) for k in keys}
        routing_coverage = {w: {g: dict.fromkeys(ROUTING_DECISIONS, 0) for g in SPACES} for w in bounds}
        for row in self._query("routing_coverage", ROUTING_COVERAGE_SQL, p):
            routing_coverage[row['window']][row['source_group']][row['decision']] = int(row['content_count'])
        for w in bounds:
            for g in SPACES:
                if sum(routing_coverage[w][g].values()) != coverage[w][g]['content_count']:
                    raise ValueError("Routing coverage не відповідає observed content")
        anchors = self._query("anchors", ANCHOR_SQL, p)
        anchor_available = (
            int(anchors[0].get('available', len(anchors)))
            if anchors else 0
        )
        anchor_limited = anchor_available > self.limits.anchors
        anchors = anchors[:self.limits.anchors]

        ids = {
            str(row['content_id'])
            for row in anchors
        }

        pair_rows = self._query(
            "neighbours",
            NEIGHBOUR_SQL,
            {
                **p,
                'anchor_ids': sorted(ids),
            },
        ) if ids else []

        pair_available = (
            int(pair_rows[0].get('available', len(pair_rows)))
            if pair_rows else 0
        )
        pair_limited = pair_available > self.limits.pairs
        pair_rows = pair_rows[:self.limits.pairs]

        pairs = {}
        for row in pair_rows:
            left = str(row['left_content_id'])
            right = str(row['right_content_id'])
            distance = float(row['distance'])

            if (
                not math.isfinite(distance)
                or not 0 <= distance <= 2
            ):
                raise ValueError("Некоректна SQL-відстань")

            if distance > self.max_distance:
                continue

            ids.add(left)
            ids.add(right)
            key = tuple(sorted((left, right)))
            pairs[key] = min(
                pairs.get(key, distance),
                distance,
            )

        rows = self._query("rows", ROWS_SQL, {**p, 'ids': sorted(ids)}) if ids else []
        row_limited = len(rows) > self.limits.rows
        rows = rows[:self.limits.rows]
        sources, contents, occurrences = {}, {}, []
        for row in rows:
            self_distance = float(row['self_distance'])
            if row['dimension'] != dimension or not math.isfinite(self_distance) or abs(self_distance) > 1e-6:
                raise ValueError("Некоректна розмірність або норма embedding evidence")
            sid, cid = str(row['source_id']), str(row['content_id'])
            sources[sid] = Source(sid, row['source_group'], row['source_name'], row['source_type'])
            contents[cid] = Content(cid, row['text'], title=row['title'] or '', has_embedding=True, selection_key=row['content_hash'], routing_decision=row['routing_decision'])
            occurrences.append(Occurrence(str(row['occurrence_id']), cid, sid,
                row['collected_at'].astimezone(timezone.utc),
                row['published_at'].astimezone(timezone.utc) if row['published_at'] else None, row['external_ref']))
        critical = (
            anchor_limited
            or pair_limited
            or row_limited
            or bool(ids - contents.keys())
            or (
                not contents
                and any(
                    routing_coverage[w][g]["analyze"]
                    + routing_coverage[w][g]["maybe"]
                    for w in bounds
                    for g in self.groups
                )
            )
        )

        result = SignalData(
            tuple(sources.values()),
            tuple(contents.values()),
            tuple(occurrences),
            embedding_model,
            dimension,
            truncated=(
                anchor_limited
                or pair_limited
                or row_limited
                or critical
            ),
            pairs=tuple(
                (a, b, d)
                for (a, b), d in sorted(pairs.items())
                if a in contents and b in contents
            ),
            coverage=coverage,
            critical_incomplete=critical,
            routing_coverage=routing_coverage,
            selection={
                "strategy": "sparse_hnsw_current_24h",
                "limits": asdict(self.limits),
                "source_groups": list(self.groups),
                "routing_policy": "latest_analyze_or_maybe",
                "model_id": self.model_id,
                "current_content_count": anchor_available,
                "anchor_count": len(anchors),
                "searched_anchor_count": len(anchors),
                "pair_count": len(pairs),
                "inspected_pair_count": pair_available,
                "selected_content_count": len(contents),
                "selected_occurrence_count": len(rows),
                "anchor_limit_reached": anchor_limited,
                "pair_limit_reached": pair_limited,
                "row_limit_reached": row_limited,
                "ann": {
                    "method": "hnsw",
                    "index": "idx_embeddings_hnsw_bge_m3",
                    "probe_limit": self.limits.ann_probe_limit,
                    "max_scan_tuples": max_scan_tuples,
                    "k": self.limits.neighbours,
                    "filter_stage": "inside_knn",
                    "ef_search": ef_search,
                    "iterative_scan": ann_settings["iterative_scan"],
                    "recall_status": "UNVERIFIED",
                    "prepared": False,
                },
                "coverage_scope": "observed_source_groups_48h",
                "query_plan_status": "UNVERIFIED",
            },
        )
        result.validate()
        return result
