"""Timezone rules shared by usage logging and analytics.

``usage_turn.ts`` is a timezone-less MySQL ``DATETIME`` (or SQLite ``TEXT``),
so values are persisted as UTC wall-clock strings.  Admin analytics use Korea
Standard Time (UTC+09:00) for calendar boundaries and labels.  Keeping those
two rules explicit makes behaviour independent of the gateway, database, and
browser host timezones.
"""

from __future__ import annotations

import datetime as _dt


UTC = _dt.timezone.utc
KST = _dt.timezone(_dt.timedelta(hours=9), name="KST")


def utc_now() -> _dt.datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return _dt.datetime.now(UTC)


def db_timestamp(instant: _dt.datetime) -> str:
    """Format an aware instant for the timezone-less UTC ``ts`` column."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("usage timestamps must be timezone-aware")
    return instant.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def current_db_timestamp() -> str:
    """Return the current UTC instant in usage-database format."""
    return db_timestamp(utc_now())


def kst_day_start_utc(day: _dt.date) -> str:
    """Return a KST calendar day's start encoded as a UTC DB timestamp."""
    local_start = _dt.datetime.combine(day, _dt.time.min, tzinfo=KST)
    return db_timestamp(local_start)


def kst_today_start_utc(now: _dt.datetime | None = None) -> str:
    """Return today's KST midnight encoded as a UTC DB timestamp."""
    return kst_today_bounds_utc(now)[0]


def kst_today_bounds_utc(now: _dt.datetime | None = None) -> tuple[str, str]:
    """Return today's inclusive/exclusive KST bounds as UTC DB timestamps."""
    instant = now or utc_now()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("current time must be timezone-aware")
    today = instant.astimezone(KST).date()
    return (
        kst_day_start_utc(today),
        kst_day_start_utc(today + _dt.timedelta(days=1)),
    )


def kst_iso_timestamp(value: _dt.datetime | str) -> str:
    """Render a stored UTC timestamp as an offset-bearing KST ISO string."""
    instant = _dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.tzinfo is None or instant.utcoffset() is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(KST).isoformat(timespec="milliseconds")
