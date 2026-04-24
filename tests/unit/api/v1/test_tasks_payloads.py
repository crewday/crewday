"""Unit tests for :mod:`app.api.v1.tasks` payload helpers.

Covers the two derived fields on :class:`TaskPayload` — ``overdue``
and ``time_window_local`` — plus the tuple-cursor helpers for the
comments list endpoint. Pure-Python tests: no FastAPI, no DB, no
router wiring. The integration-level behaviour is exercised by
``tests/integration/api/test_tasks_routes.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.api.v1.tasks import (
    TaskPayload,
    _compute_overdue,
    _compute_time_window_local,
    _decode_comment_cursor,
    _encode_comment_cursor,
)
from app.domain.tasks.oneoff import TaskView


def _view(
    *,
    state: str = "pending",
    scheduled_for_utc: datetime | None = None,
    duration_minutes: int | None = 60,
    property_id: str | None = "prop-01",
) -> TaskView:
    """Return a :class:`TaskView` populated with deterministic defaults."""
    anchor = scheduled_for_utc or datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC)
    return TaskView(
        id="01ZZZZZZZZZZZZZZZZZZZZZZZZZ",
        workspace_id="ws-01",
        template_id=None,
        schedule_id=None,
        property_id=property_id,
        area_id=None,
        unit_id=None,
        title="Clean pool",
        description_md=None,
        priority="normal",
        state=state,  # type: ignore[arg-type]
        scheduled_for_local="2026-04-20T11:00:00",
        scheduled_for_utc=anchor,
        duration_minutes=duration_minutes,
        photo_evidence="disabled",
        linked_instruction_ids=(),
        inventory_consumption_json={},
        expected_role_id=None,
        assigned_user_id=None,
        created_by="user-01",
        is_personal=False,
        created_at=datetime(2026, 4, 19, 0, 0, 0, tzinfo=UTC),
    )


class TestOverdue:
    """``overdue`` is ``True`` iff the task is past-anchor and non-terminal."""

    def test_future_pending_is_not_overdue(self) -> None:
        view = _view(
            state="pending",
            scheduled_for_utc=datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
        )
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        assert _compute_overdue(view, now) is False

    def test_past_pending_is_overdue(self) -> None:
        view = _view(
            state="pending",
            scheduled_for_utc=datetime(2026, 4, 20, 8, 0, 0, tzinfo=UTC),
        )
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        assert _compute_overdue(view, now) is True

    def test_terminal_states_are_never_overdue(self) -> None:
        anchor = datetime(2026, 4, 20, 8, 0, 0, tzinfo=UTC)
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        for state in ("done", "skipped", "cancelled"):
            view = _view(state=state, scheduled_for_utc=anchor)
            assert _compute_overdue(view, now) is False, (
                f"{state=} should collapse overdue to False"
            )

    def test_naive_anchor_is_treated_as_utc(self) -> None:
        """A DB round-trip that strips the tz on SQLite still lands sanely."""
        view = _view(
            state="pending",
            scheduled_for_utc=datetime(2026, 4, 20, 8, 0, 0),  # naive
        )
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        assert _compute_overdue(view, now) is True


class TestTimeWindowLocal:
    """``time_window_local`` renders the wall-clock window in the property TZ."""

    def test_renders_in_property_zone(self) -> None:
        # 09:00 UTC + 60min in Europe/Paris (UTC+2 during DST → 11:00-12:00).
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
            duration_minutes=60,
        )
        assert _compute_time_window_local(view, "Europe/Paris") == "11:00-12:00"

    def test_falls_back_to_thirty_minutes_when_duration_is_null(self) -> None:
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
            duration_minutes=None,
        )
        # Europe/Paris at 09:00 UTC in April is 11:00 local; +30min → 11:30.
        assert _compute_time_window_local(view, "Europe/Paris") == "11:00-11:30"

    def test_missing_timezone_returns_none(self) -> None:
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
        )
        assert _compute_time_window_local(view, None) is None

    def test_junk_timezone_returns_none(self) -> None:
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
        )
        assert _compute_time_window_local(view, "Not/AZone") is None

    def test_antimeridian_timezone_renders_correctly(self) -> None:
        """Pacific/Auckland is UTC+12 in winter; the window should advance."""
        # 22:00 UTC on 2026-04-20 is 10:00 local NZST (UTC+12).
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 22, 0, 0, tzinfo=UTC),
            duration_minutes=90,
        )
        assert _compute_time_window_local(view, "Pacific/Auckland") == "10:00-11:30"


class TestFromViewEndToEnd:
    """The :meth:`TaskPayload.from_view` factory composes both helpers."""

    def test_from_view_populates_derived_fields(self) -> None:
        view = _view(
            scheduled_for_utc=datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC),
            duration_minutes=60,
        )
        now = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        payload = TaskPayload.from_view(
            view, property_timezone="Europe/Paris", now_utc=now
        )
        assert payload.overdue is True
        assert payload.time_window_local == "11:00-12:00"
        assert payload.title == "Clean pool"
        assert payload.id == "01ZZZZZZZZZZZZZZZZZZZZZZZZZ"

    def test_from_view_without_zone_leaves_window_null(self) -> None:
        view = _view(property_id=None)
        payload = TaskPayload.from_view(view, property_timezone=None)
        assert payload.time_window_local is None


class TestCommentCursor:
    """Tuple-cursor round-trips for the ``comments`` pagination."""

    def test_round_trip(self) -> None:
        created = datetime(2026, 4, 20, 9, 30, 0, tzinfo=UTC)
        cursor = _encode_comment_cursor(created, "01AAAA")
        decoded_ts, decoded_id = _decode_comment_cursor(cursor)
        assert decoded_ts == created
        assert decoded_id == "01AAAA"

    def test_empty_cursor_collapses_to_none_pair(self) -> None:
        assert _decode_comment_cursor(None) == (None, None)
        assert _decode_comment_cursor("") == (None, None)

    def test_tampered_cursor_raises_422(self) -> None:
        """A base64-valid blob missing the ``|`` separator collapses to 422."""
        # "no-pipe" base64 encoded.
        import base64

        bad = base64.urlsafe_b64encode(b"nopipehere").rstrip(b"=").decode("ascii")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            _decode_comment_cursor(bad)
        assert excinfo.value.status_code == 422


class TestOverdueAcrossTimezones:
    """Edge case — an anchor in Pacific/Auckland evaluated from the owner's
    Europe/Paris frame still reasons correctly because both collapse to UTC."""

    def test_cross_zone_overdue_uses_utc(self) -> None:
        # Anchor: 2026-04-20 09:00 Pacific/Auckland == 2026-04-19 21:00 UTC.
        auckland_anchor = datetime(
            2026, 4, 20, 9, 0, 0, tzinfo=ZoneInfo("Pacific/Auckland")
        ).astimezone(UTC)
        view = _view(scheduled_for_utc=auckland_anchor)
        # "Now" in Paris: 2026-04-20 00:00 CEST == 22:00 UTC.
        now = datetime(2026, 4, 19, 22, 0, 0, tzinfo=UTC)
        assert _compute_overdue(view, now) is True
