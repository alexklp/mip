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
