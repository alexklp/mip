"""Exact SELECT-only adapter для повного current-24h signal discovery."""

from dataclasses import asdict
from datetime import datetime, timezone
import math
import time

from reporting.signals_data import (
    Content,
    Occurrence,
    ROUTING_DECISIONS,
    SPACES,
    SignalData,
    Source,
    windows,
)
from reporting.signals_postgres import (
    COVERAGE_SQL,
    DIMENSION_SQL,
    GUARD_SQL,
    LATEST_ROUTING_SQL,
    MODEL_SQL,
    PostgresLimits,
    ROUTING_COVERAGE_SQL,
    timeout_ms,
)


EXACT_PAIR_SQL = """
WITH current_content AS MATERIALIZED (
    SELECT DISTINCT
           io.content_id,
           e.embedding::vector(1024) AS embedding
    FROM item_occurrences io
    JOIN sources s USING (source_id)
    JOIN embeddings e
      ON e.content_id = io.content_id
     AND e.embedding_model_id = %(model_id)s
    WHERE io.collected_at >= %(middle)s
      AND io.collected_at < %(as_of)s
      AND s.source_group = ANY(%(groups)s)
      AND (""" + LATEST_ROUTING_SQL + """) = 'analyze'
      AND vector_dims(e.embedding) = %(dimension)s
),
distances AS MATERIALIZED (
    SELECT
        a.content_id AS left_content_id,
        b.content_id AS right_content_id,
        a.embedding <=> b.embedding AS distance
    FROM current_content a
    JOIN current_content b
      ON a.content_id < b.content_id
)
SELECT left_content_id, right_content_id, distance
FROM distances
WHERE distance <= %(max_distance)s
ORDER BY distance, left_content_id, right_content_id
LIMIT %(pair_probe)s
"""


EXACT_ROWS_SQL = """
SELECT io.occurrence_id, io.content_id, io.source_id,
       io.collected_at, io.published_at,
       left(io.external_ref, 2048) AS external_ref,
       left(s.name, 240) AS source_name,
       s.source_type, s.source_group,
       ci.content_hash,
       left(ci.title, 240) AS title,
       left(ci.text_content, %(text_probe)s) AS text,
       vector_dims(e.embedding) AS dimension,
       e.embedding <=> e.embedding AS self_distance
FROM item_occurrences io
JOIN sources s USING (source_id)
JOIN content_items ci USING (content_id)
JOIN embeddings e
  ON e.content_id = io.content_id
 AND e.embedding_model_id = %(model_id)s
WHERE io.collected_at >= %(start)s
  AND io.collected_at < %(as_of)s
  AND s.source_group = ANY(%(groups)s)
  AND (""" + LATEST_ROUTING_SQL + """) = 'analyze'
ORDER BY io.collected_at DESC,
         ci.content_hash,
         s.name,
         io.external_ref,
         io.occurrence_id
LIMIT %(row_probe)s
"""


class ExactPostgresSignalAdapter:
    """Повний exact discovery для content, активного у current 24h.

    Previous 24h читається лише як контекст для dynamics.
    Snapshot та БД не змінюються цим adapter.
    """

    def __init__(
        self,
        connection,
        *,
        model_id,
        source_groups=SPACES,
        limits=PostgresLimits(),
        max_distance=0.36,
        critical_distance=0.18,
    ):
        limits.validate()
        if type(model_id) is not int or model_id <= 0:
            raise ValueError("Потрібен додатний model id")
        if (
            not source_groups
            or len(set(source_groups)) != len(source_groups)
            or set(source_groups) - set(SPACES)
        ):
            raise ValueError("Некоректні групи джерел")
        if not math.isfinite(max_distance) or not 0 <= max_distance <= 2:
            raise ValueError("Некоректний поріг відстані")
        if (
            not math.isfinite(critical_distance)
            or not 0 <= critical_distance <= max_distance
        ):
            raise ValueError("Некоректний критичний поріг відстані")

        self.connection = connection
        self.model_id = model_id
        self.groups = tuple(sorted(source_groups))
        self.limits = limits
        self.max_distance = max_distance
        self.critical_distance = critical_distance
        self.timings = []

    def _query(self, name, sql, params):
        started = time.monotonic()
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        self.timings.append(
            {
                "query": name,
                "seconds": time.monotonic() - started,
                "rows": len(rows),
            }
        )
        return rows

    def read(
        self,
        *,
        as_of: datetime,
        embedding_model: str,
        dimension: int,
    ) -> SignalData:
        bounds = windows(as_of)

        if type(dimension) is not int or dimension <= 0 or not embedding_model:
            raise ValueError("Потрібні модель та розмірність")

        self.timings = []

        p = dict(
            start=bounds["previous"][0],
            middle=bounds["current"][0],
            as_of=as_of,
            groups=list(self.groups),
            model_id=self.model_id,
            dimension=dimension,
            row_probe=self.limits.rows + 1,
            text_probe=self.limits.evidence_chars + 1,
            invalid_limit=1,
            max_distance=self.max_distance,
            pair_probe=self.limits.pairs + 1,
        )

        guard = self._query("guard", GUARD_SQL, {})[0]
        timeout = timeout_ms(guard["timeout"])
        if (
            guard["read_only"] != "on"
            or guard["isolation"] != "repeatable read"
            or not 0 < timeout <= self.limits.statement_timeout_ms
        ):
            raise ValueError(
                "Потрібні read-only, repeatable read та обмежений statement_timeout"
            )

        models = self._query("model", MODEL_SQL, p)
        if (
            len(models) != 1
            or models[0]["model_name"] + "@" + models[0]["model_revision"]
            != embedding_model
            or models[0]["dimension"] != dimension
            or models[0]["metric"] != "cosine"
        ):
            raise ValueError("Реєстр моделі не відповідає очікуваному контракту")

        if self.model_id != 1 or dimension != 1024:
            raise ValueError(
                "Exact path зараз підтримує model_id=1 та dimension=1024"
            )

        if self._query("dimension", DIMENSION_SQL, p):
            raise ValueError(
                "Некоректна розмірність embedding у спостережуваному потоці"
            )

        keys = (
            "occurrence_count",
            "content_count",
            "source_count",
            "missing_published_at",
            "missing_embedding_content_count",
        )

        coverage = {
            w: {
                g: dict.fromkeys(keys, 0)
                for g in (*SPACES, "excluded")
            }
            for w in bounds
        }

        for row in self._query("coverage", COVERAGE_SQL, p):
            coverage[row["window"]][row["source_group"]] = {
                k: int(row[k]) for k in keys
            }

        routing_coverage = {
            w: {
                g: dict.fromkeys(ROUTING_DECISIONS, 0)
                for g in SPACES
            }
            for w in bounds
        }

        for row in self._query(
            "routing_coverage",
            ROUTING_COVERAGE_SQL,
            p,
        ):
            routing_coverage[row["window"]][row["source_group"]][
                row["decision"]
            ] = int(row["content_count"])

        raw_pairs = self._query(
            "exact_pairs",
            EXACT_PAIR_SQL,
            p,
        )
        pair_limited = len(raw_pairs) > self.limits.pairs
        critical_pair_limited = False
        if pair_limited:
            first_omitted_distance = float(
                raw_pairs[self.limits.pairs]["distance"]
            )
            if (
                not math.isfinite(first_omitted_distance)
                or not 0 <= first_omitted_distance <= 2
            ):
                raise ValueError("Некоректна exact cosine distance")
            critical_pair_limited = (
                first_omitted_distance <= self.critical_distance
            )

        raw_pairs = raw_pairs[: self.limits.pairs]

        pairs = []
        for row in raw_pairs:
            distance = float(row["distance"])
            if not math.isfinite(distance) or not 0 <= distance <= 2:
                raise ValueError("Некоректна exact cosine distance")
            pairs.append(
                (
                    str(row["left_content_id"]),
                    str(row["right_content_id"]),
                    distance,
                )
            )

        raw_rows = self._query(
            "rows",
            EXACT_ROWS_SQL,
            p,
        )
        row_limited = len(raw_rows) > self.limits.rows
        raw_rows = raw_rows[: self.limits.rows]

        sources = {}
        contents = {}
        occurrences = []

        for row in raw_rows:
            self_distance = float(row["self_distance"])
            if (
                row["dimension"] != dimension
                or not math.isfinite(self_distance)
                or abs(self_distance) > 1e-6
            ):
                raise ValueError(
                    "Некоректна розмірність або норма embedding evidence"
                )

            sid = str(row["source_id"])
            cid = str(row["content_id"])

            sources[sid] = Source(
                sid,
                row["source_group"],
                row["source_name"],
                row["source_type"],
            )
            contents[cid] = Content(
                cid,
                row["text"],
                title=row["title"] or "",
                has_embedding=True,
                selection_key=row["content_hash"],
                routing_decision="analyze",
            )
            occurrences.append(
                Occurrence(
                    str(row["occurrence_id"]),
                    cid,
                    sid,
                    row["collected_at"].astimezone(timezone.utc),
                    row["published_at"].astimezone(timezone.utc)
                    if row["published_at"]
                    else None,
                    row["external_ref"],
                )
            )

        current_start, current_end = bounds["current"]
        current_ids = {
            row.content_id
            for row in occurrences
            if current_start <= row.collected_at < current_end
        }

        current_analyze = sum(
            routing_coverage["current"][group]["analyze"]
            for group in self.groups
        )

        critical = (
            critical_pair_limited
            or row_limited
            or (not current_ids and current_analyze > 0)
        )

        result = SignalData(
            tuple(sources.values()),
            tuple(contents.values()),
            tuple(occurrences),
            embedding_model,
            dimension,
            truncated=pair_limited or row_limited,
            pairs=tuple(pairs),
            coverage=coverage,
            critical_incomplete=critical,
            routing_coverage=routing_coverage,
            selection={
                "strategy": "exact_current_24h",
                "distance_method": "exact_pgvector_cosine",
                "limits": asdict(self.limits),
                "source_groups": list(self.groups),
                "routing_policy": "latest_analyze_only",
                "model_id": self.model_id,
                "current_content_count": len(current_ids),
                "selected_content_count": len(contents),
                "selected_occurrence_count": len(raw_rows),
                "pair_count": len(pairs),
                "pair_limit_reached": pair_limited,
                "row_limit_reached": row_limited,
                "coverage_scope": "observed_source_groups_48h",
                "query_plan_status": "UNVERIFIED",
            },
        )

        result.validate()
        return result
