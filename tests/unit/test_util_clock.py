"""Tests for :mod:`app.util.clock`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from app.util.clock import Clock, FrozenClock, SystemClock, aware_utc


class TestAwareUtc:
    def test_treats_naive_datetime_as_utc(self) -> None:
        value = datetime(2026, 4, 19, 12, 0)
        assert aware_utc(value) == datetime(2026, 4, 19, 12, 0, tzinfo=UTC)

    def test_preserves_utc_aware_instant(self) -> None:
        value = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
        assert aware_utc(value) == value

    def test_converts_non_utc_aware_datetime(self) -> None:
        eastern = timezone(-timedelta(hours=4))
        value = datetime(2026, 4, 19, 8, 0, tzinfo=eastern)
        assert aware_utc(value) == datetime(2026, 4, 19, 12, 0, tzinfo=UTC)

    def test_rejects_invalid_input(self) -> None:
        with pytest.raises(TypeError, match="requires a datetime"):
            aware_utc(cast(datetime, "2026-04-19T12:00:00Z"))


class TestSystemClock:
    def test_returns_aware_utc(self) -> None:
        clock = SystemClock()
        now = clock.now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_two_calls_are_monotonic(self) -> None:
        clock = SystemClock()
        first = clock.now()
        second = clock.now()
        assert second >= first

    def test_satisfies_clock_protocol(self) -> None:
        clock: Clock = SystemClock()
        assert isinstance(clock, Clock)


class TestFrozenClock:
    def test_freezes_at_given_instant(self) -> None:
        fixed = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
        clock = FrozenClock(fixed)
        assert clock.now() == fixed
        assert clock.now() == fixed  # still the same on repeated calls

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="aware datetime"):
            FrozenClock(datetime(2026, 4, 19, 12, 0))

    def test_converts_non_utc_to_utc(self) -> None:
        paris = timezone(timedelta(hours=2))
        at_paris = datetime(2026, 4, 19, 14, 0, tzinfo=paris)
        clock = FrozenClock(at_paris)
        assert clock.now() == datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
        assert clock.now().utcoffset() == timedelta(0)

    def test_advance_moves_forward(self) -> None:
        clock = FrozenClock(datetime(2026, 4, 19, 12, 0, tzinfo=UTC))
        clock.advance(timedelta(hours=1, minutes=30))
        assert clock.now() == datetime(2026, 4, 19, 13, 30, tzinfo=UTC)

    def test_advance_accepts_negative_delta(self) -> None:
        clock = FrozenClock(datetime(2026, 4, 19, 12, 0, tzinfo=UTC))
        clock.advance(-timedelta(minutes=30))
        assert clock.now() == datetime(2026, 4, 19, 11, 30, tzinfo=UTC)

    def test_set_resets_instant(self) -> None:
        clock = FrozenClock(datetime(2026, 4, 19, 12, 0, tzinfo=UTC))
        target = datetime(2030, 1, 1, tzinfo=UTC)
        clock.set(target)
        assert clock.now() == target

    def test_set_rejects_naive(self) -> None:
        clock = FrozenClock(datetime(2026, 4, 19, 12, 0, tzinfo=UTC))
        with pytest.raises(ValueError, match="aware datetime"):
            clock.set(datetime(2026, 4, 20, 12, 0))

    def test_set_normalises_to_utc(self) -> None:
        clock = FrozenClock(datetime(2026, 4, 19, 12, 0, tzinfo=UTC))
        sydney = timezone(timedelta(hours=10))
        clock.set(datetime(2026, 4, 19, 22, 0, tzinfo=sydney))
        assert clock.now() == datetime(2026, 4, 19, 12, 0, tzinfo=UTC)

    def test_satisfies_clock_protocol(self) -> None:
        clock: Clock = FrozenClock(datetime(2026, 4, 19, tzinfo=UTC))
        assert isinstance(clock, Clock)
