"""Local-day snapping helpers."""

from __future__ import annotations

from datetime import UTC, datetime, time, tzinfo
from zoneinfo import ZoneInfo

__all__ = ["snap_to_day"]


def snap_to_day(ts: datetime, tz: str | tzinfo) -> datetime:
    """Return the UTC instant for ``ts``'s local-day midnight in ``tz``."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("snap_to_day requires a timezone-aware datetime")

    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local_day = ts.astimezone(zone).date()
    local_midnight = datetime.combine(local_day, time.min, tzinfo=zone)
    return local_midnight.astimezone(UTC)
