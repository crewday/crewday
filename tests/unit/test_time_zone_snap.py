from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.util.time_zone import snap_to_day


def test_snap_to_day_handles_pacific_auckland_date_line() -> None:
    zone = ZoneInfo("Pacific/Auckland")
    snapped = snap_to_day(datetime(2026, 4, 12, 18, 30, tzinfo=UTC), zone)

    assert snapped == datetime(2026, 4, 12, 12, 0, tzinfo=UTC)


def test_snap_to_day_handles_spring_dst_boundary() -> None:
    zone = ZoneInfo("America/New_York")
    starts = snap_to_day(datetime(2026, 3, 8, 12, 0, tzinfo=UTC), zone)
    ends = snap_to_day(datetime(2026, 3, 9, 12, 0, tzinfo=UTC), zone)

    assert starts == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert ends == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)


def test_snap_to_day_handles_fall_dst_boundary() -> None:
    zone = ZoneInfo("America/New_York")
    starts = snap_to_day(datetime(2026, 11, 1, 12, 0, tzinfo=UTC), zone)
    ends = snap_to_day(datetime(2026, 11, 2, 12, 0, tzinfo=UTC), zone)

    assert starts == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert ends == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)


def test_snap_to_day_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        snap_to_day(datetime(2026, 4, 12, 12, 0), "UTC")
