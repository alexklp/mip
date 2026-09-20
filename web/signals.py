"""Читання обмеженого JSON snapshot без БД, моделі та мережі."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from reporting.signals_snapshot import MAX_SNAPSHOT_BYTES, validate_snapshot
from reporting.signals_data import require_utc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / 'reporting' / 'signals.latest.json'


def safe_link(value: str) -> str | None:
    """Дозволяємо лише абсолютні HTTP(S) посилання без userinfo."""
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` forces urlsplit to reject malformed numeric ports.
        _ = parsed.port
        if parsed.scheme.lower() in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password and not any(ord(c) < 33 for c in value) and '\\' not in value:
            return value
    except ValueError:
        pass
    return None


def fetch_signal_chronology(
    conn,
    *,
    content_ids,
    source_groups,
    window_start,
    window_end,
    expected_total,
    offset=0,
    limit=30,
):
    """Bounded read-only chronology page for one snapshot candidate."""
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 50
    ):
        raise ValueError("Некоректна сторінка chronology")

    if (
        not isinstance(content_ids, list)
        or not content_ids
        or not isinstance(source_groups, list)
        or not source_groups
        or type(expected_total) is not int
        or expected_total < 1
    ):
        raise ValueError("Некоректний candidate chronology contract")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                io.occurrence_id::text AS occurrence_id,
                io.content_id::text AS content_id,
                io.source_id::text AS source_id,
                s.source_group::text AS source_group,
                left(s.name, 240) AS source_name,
                s.source_type::text AS source_type,
                left(ci.title, 240) AS title,
                left(ci.text_content, 600) AS text,
                char_length(coalesce(ci.text_content, '')) > 600
                    AS text_truncated,
                left(io.external_ref, 2048) AS external_ref,
                io.published_at,
                count(*) OVER () AS available
            FROM item_occurrences io
            JOIN sources s USING (source_id)
            JOIN content_items ci USING (content_id)
            WHERE io.content_id = ANY(%s::uuid[])
              AND s.source_group::text = ANY(%s::text[])
              AND io.collected_at >= %s
              AND io.collected_at < %s
            ORDER BY
                coalesce(io.published_at, io.collected_at),
                io.collected_at,
                io.occurrence_id
            LIMIT %s OFFSET %s
            """,
            (
                content_ids,
                source_groups,
                window_start,
                window_end,
                limit,
                offset,
            ),
        )
        rows = cur.fetchall()

    if rows:
        available = int(rows[0]["available"])
        if available != expected_total:
            raise ValueError(
                "Snapshot chronology не відповідає поточним даним"
            )
    elif offset == 0 and expected_total:
        raise ValueError(
            "Snapshot chronology не відповідає поточним даним"
        )

    result = []

    for row in rows:
        published_at = row["published_at"]

        result.append(
            {
                "occurrence_id": row["occurrence_id"],
                "content_id": row["content_id"],
                "source_id": row["source_id"],
                "source_group": row["source_group"],
                "source_name": row["source_name"],
                "source_type": row["source_type"],
                "title": row["title"] or "",
                "text": row["text"] or "",
                "text_truncated": bool(row["text_truncated"]),
                "published_at": (
                    published_at.isoformat()
                    if published_at is not None
                    else None
                ),
                "safe_link": safe_link(row["external_ref"] or ""),
            }
        )

    return result


def load_signals(path=DEFAULT_SNAPSHOT, *, now=None, stale_seconds=7200) -> dict:
    """Стани missing/invalid/stale/empty/ready; файл перечитується на кожен запит."""
    now = require_utc(now or datetime.now(timezone.utc))
    result = {'state': 'invalid', 'snapshot': None}
    try:
        if type(stale_seconds) is not int or not 1 <= stale_seconds <= 604800:
            return result
        path = Path(path)
        if not path.resolve().is_relative_to(ROOT):
            return result
        with path.open('rb') as handle:
            payload = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(payload) > MAX_SNAPSHOT_BYTES:
            return result
        snapshot = json.loads(payload)
        validate_snapshot(snapshot)
        as_of = datetime.fromisoformat(snapshot['as_of'])
        generated = datetime.fromisoformat(snapshot['generated_at'])
        if as_of > now or generated > now or snapshot['critical_incomplete']:
            return result
        for candidate in snapshot['candidates']:
            for row in candidate['chronology']:
                row['safe_link'] = safe_link(row['external_ref'])
        # Новий generated_at не робить старий as_of свіжим.
        stale = now - min(as_of, generated) > timedelta(seconds=stale_seconds)
        state = 'stale' if stale else 'ready' if snapshot['candidates'] else 'empty'
        return {'state': state, 'snapshot': snapshot}
    except FileNotFoundError:
        return {'state': 'missing', 'snapshot': None}
    except (OSError, ValueError, TypeError, RecursionError):
        return result
