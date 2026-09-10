"""Форматування часу для Web MVP.

Усі дані зберігаються та обробляються в UTC.
У користувацькому інтерфейсі час відображається у часовій зоні Europe/Kyiv.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")

    return value


def to_kyiv(value: datetime | str | None) -> datetime | None:
    dt = _as_datetime(value)
    return dt.astimezone(KYIV_TZ) if dt else None


def fmt_kyiv(value: datetime | str | None) -> str:
    dt = to_kyiv(value)
    return dt.strftime("%d.%m.%Y · %H:%M") if dt else "—"


def fmt_kyiv_clock(value: datetime | str | None) -> str:
    dt = to_kyiv(value)
    return dt.strftime("%H:%M") if dt else "—"


def fmt_kyiv_zoned(value: datetime | str | None) -> str:
    dt = to_kyiv(value)
    if dt is None:
        return "—"
    return f"{dt.strftime('%d.%m.%Y · %H:%M')} {utc_offset_label(dt)}"


def fmt_kyiv_clock_zoned(value: datetime | str | None) -> str:
    dt = to_kyiv(value)
    if dt is None:
        return "—"
    return f"{dt.strftime('%H:%M')} {utc_offset_label(dt)}"


def utc_offset_label(value: datetime | str | None = None) -> str:
    dt = to_kyiv(value or datetime.now(timezone.utc))
    if dt is None:
        return "UTC"

    offset = dt.utcoffset()
    if offset is None:
        return "UTC"

    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)

    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"

    return f"UTC{sign}{hours}"


def kyiv_time_label(value: datetime | str | None = None) -> str:
    return f"Київ, {utc_offset_label(value)}"
