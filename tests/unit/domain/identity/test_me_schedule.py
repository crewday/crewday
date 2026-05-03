"""Pure-function tests for the §12 schedule aggregator (cd-lot5).

The HTTP-tier suite at :mod:`tests.unit.api.v1.identity.test_me_schedule`
exercises the full feed end-to-end through the router + service + DB.
This module pins the aggregator's pure logic against a fake
:class:`~app.domain.identity.me_schedule_ports.MeScheduleQueryRepository`
so a future regression in the aggregator (a swapped predicate, a window
default drift, a missing seam call) fires here in milliseconds without
spinning up a TestClient or a SQLite engine.

Specifically pins:

* The window default fires when both ``from_date`` and ``to_date`` are
  ``None`` (clock-driven; injected ``Clock`` keeps the test deterministic).
* Approved + pending rows are merged into single ``leaves`` /
  ``overrides`` lists — each carries its own ``approved_at`` /
  ``approval_required`` so the SPA branches per row.
* The aggregator passes ``ctx.actor_id`` + ``ctx.workspace_id`` to
  every repo method — defence-in-depth that the seam never broadens
  to another user / workspace.
* ``window_start_utc`` / ``window_end_utc`` for the booking read
  resolve to the full-day UTC bounds of the resolved window.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from app.domain.identity.availability_ports import (
    UserAvailabilityOverrideRow,
    UserLeaveRow,
    UserWeeklyAvailabilityRow,
)
from app.domain.identity.me_schedule import (
    DEFAULT_WINDOW_DAYS,
    aggregate_schedule,
)
from app.domain.identity.me_schedule_ports import BookingRefRow
from app.tenancy import WorkspaceContext
from app.tenancy.context import ActorGrantRole
from app.util.clock import Clock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_WS_ID = "01HWWS00000000000000000000"
_USER_ID = "01HWUSER0000000000000000"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _CallRecord:
    """Records the kwargs each repo method received."""

    workspace_id: str | None = None
    user_id: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    window_start_utc: datetime | None = None
    window_end_utc: datetime | None = None


@dataclass
class _FakeRepo:
    """Hand-rolled fake of :class:`MeScheduleQueryRepository`."""

    weekly_rows: Sequence[UserWeeklyAvailabilityRow] = field(default_factory=list)
    override_rows: Sequence[UserAvailabilityOverrideRow] = field(default_factory=list)
    leave_rows: Sequence[UserLeaveRow] = field(default_factory=list)
    booking_rows: Sequence[BookingRefRow] = field(default_factory=list)
    weekly_call: _CallRecord = field(default_factory=_CallRecord)
    override_call: _CallRecord = field(default_factory=_CallRecord)
    leave_call: _CallRecord = field(default_factory=_CallRecord)
    booking_call: _CallRecord = field(default_factory=_CallRecord)

    def list_weekly_pattern(
        self,
        *,
        workspace_id: str,
        user_id: str,
    ) -> Sequence[UserWeeklyAvailabilityRow]:
        self.weekly_call = _CallRecord(workspace_id=workspace_id, user_id=user_id)
        return self.weekly_rows

    def list_overrides_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        from_date: date,
        to_date: date,
    ) -> Sequence[UserAvailabilityOverrideRow]:
        self.override_call = _CallRecord(
            workspace_id=workspace_id,
            user_id=user_id,
            from_date=from_date,
            to_date=to_date,
        )
        return self.override_rows

    def list_leaves_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        from_date: date,
        to_date: date,
    ) -> Sequence[UserLeaveRow]:
        self.leave_call = _CallRecord(
            workspace_id=workspace_id,
            user_id=user_id,
            from_date=from_date,
            to_date=to_date,
        )
        return self.leave_rows

    def list_bookings_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> Sequence[BookingRefRow]:
        self.booking_call = _CallRecord(
            workspace_id=workspace_id,
            user_id=user_id,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
        )
        return self.booking_rows


@dataclass
class _PinnedClock(Clock):
    """Clock pinned to a fixed instant for window-default tests."""

    now_value: datetime = _PINNED

    def now(self) -> datetime:
        return self.now_value


def _ctx(
    *,
    workspace_id: str = _WS_ID,
    actor_id: str = _USER_ID,
    grant_role: ActorGrantRole = "worker",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="ws-test",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _weekly(
    weekday: int,
    starts: time | None,
    ends: time | None,
) -> UserWeeklyAvailabilityRow:
    return UserWeeklyAvailabilityRow(
        id=new_ulid(),
        workspace_id=_WS_ID,
        user_id=_USER_ID,
        weekday=weekday,
        starts_local=starts,
        ends_local=ends,
        updated_at=_PINNED,
    )


def _override(
    *,
    on_date: date,
    available: bool = True,
    approval_required: bool = False,
    approved: bool = True,
) -> UserAvailabilityOverrideRow:
    return UserAvailabilityOverrideRow(
        id=new_ulid(),
        workspace_id=_WS_ID,
        user_id=_USER_ID,
        date=on_date,
        available=available,
        starts_local=time(9, 0) if available else None,
        ends_local=time(17, 0) if available else None,
        reason=None,
        approval_required=approval_required,
        approved_at=_PINNED if approved else None,
        approved_by=_USER_ID if approved else None,
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=None,
    )


def _leave(*, starts_on: date, ends_on: date, approved: bool = True) -> UserLeaveRow:
    return UserLeaveRow(
        id=new_ulid(),
        workspace_id=_WS_ID,
        user_id=_USER_ID,
        starts_on=starts_on,
        ends_on=ends_on,
        category="vacation",
        approved_at=_PINNED if approved else None,
        approved_by=_USER_ID if approved else None,
        note_md=None,
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=None,
    )


def _booking(*, scheduled_start: datetime, status: str = "scheduled") -> BookingRefRow:
    return BookingRefRow(
        id=new_ulid(),
        workspace_id=_WS_ID,
        user_id=_USER_ID,
        work_engagement_id="01HWENG0000000000000000",
        property_id=None,
        client_org_id=None,
        status=status,
        kind="work",
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_start + timedelta(hours=2),
        actual_minutes=None,
        actual_minutes_paid=0,
        break_seconds=0,
        pending_amend_minutes=None,
        pending_amend_reason=None,
        declined_at=None,
        declined_reason=None,
        notes_md=None,
        adjusted=False,
        adjustment_reason=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAggregatorPredicateForwarding:
    """Defence-in-depth: every repo call must see the caller's identifiers."""

    def test_aggregator_passes_workspace_and_user_to_every_seam(self) -> None:
        repo = _FakeRepo()
        ctx = _ctx()
        from_d = date(2026, 5, 1)
        to_d = date(2026, 5, 15)

        aggregate_schedule(repo, ctx, from_date=from_d, to_date=to_d)

        for call in (
            repo.weekly_call,
            repo.override_call,
            repo.leave_call,
            repo.booking_call,
        ):
            assert call.workspace_id == _WS_ID
            assert call.user_id == _USER_ID

        # Window edges flow through verbatim to the date-keyed reads.
        assert repo.override_call.from_date == from_d
        assert repo.override_call.to_date == to_d
        assert repo.leave_call.from_date == from_d
        assert repo.leave_call.to_date == to_d

    def test_booking_window_resolves_to_full_day_utc_bounds(self) -> None:
        """``window_start_utc`` = 00:00, ``window_end_utc`` = 23:59:59.999999."""
        repo = _FakeRepo()
        ctx = _ctx()
        from_d = date(2026, 5, 1)
        to_d = date(2026, 5, 15)

        aggregate_schedule(repo, ctx, from_date=from_d, to_date=to_d)

        assert repo.booking_call.window_start_utc == datetime(
            2026, 5, 1, 0, 0, 0, tzinfo=UTC
        )
        # ``time.max`` is 23:59:59.999999.
        assert repo.booking_call.window_end_utc == datetime(
            2026, 5, 15, 23, 59, 59, 999999, tzinfo=UTC
        )


class TestWindowResolution:
    """The §12 ``[today, today+14d]`` default fires when edges are unset."""

    def test_default_window_when_both_edges_unset(self) -> None:
        repo = _FakeRepo()
        ctx = _ctx()
        clock = _PinnedClock(now_value=_PINNED)

        payload = aggregate_schedule(repo, ctx, clock=clock)

        assert payload.from_date == _PINNED.date()
        assert payload.to_date == _PINNED.date() + timedelta(days=DEFAULT_WINDOW_DAYS)

    def test_default_window_only_to_unset(self) -> None:
        """``from_date`` set, ``to_date`` unset → ``to_date = today + 14d``."""
        repo = _FakeRepo()
        ctx = _ctx()
        clock = _PinnedClock(now_value=_PINNED)
        explicit_from = date(2026, 4, 1)

        payload = aggregate_schedule(
            repo,
            ctx,
            from_date=explicit_from,
            clock=clock,
        )

        assert payload.from_date == explicit_from
        assert payload.to_date == _PINNED.date() + timedelta(days=DEFAULT_WINDOW_DAYS)

    def test_default_window_only_from_unset(self) -> None:
        """``to_date`` set, ``from_date`` unset → ``from_date = today``."""
        repo = _FakeRepo()
        ctx = _ctx()
        clock = _PinnedClock(now_value=_PINNED)
        explicit_to = date(2026, 6, 1)

        payload = aggregate_schedule(repo, ctx, to_date=explicit_to, clock=clock)

        assert payload.from_date == _PINNED.date()
        assert payload.to_date == explicit_to


class TestApprovedPendingMerged:
    """Approved + pending rows now share one list each — SPA branches per row."""

    def test_overrides_merged_into_single_list_with_state_per_row(self) -> None:
        approved = _override(on_date=date(2026, 5, 4), available=True, approved=True)
        pending = _override(
            on_date=date(2026, 5, 5),
            available=False,
            approval_required=True,
            approved=False,
        )
        repo = _FakeRepo(override_rows=[approved, pending])
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 1), to_date=date(2026, 5, 15)
        )

        ids = {v.id for v in payload.overrides}
        assert ids == {approved.id, pending.id}
        approved_view = next(v for v in payload.overrides if v.id == approved.id)
        pending_view = next(v for v in payload.overrides if v.id == pending.id)
        assert approved_view.approved_at is not None
        assert pending_view.approved_at is None
        assert pending_view.approval_required is True

    def test_leaves_merged_into_single_list_with_state_per_row(self) -> None:
        approved = _leave(
            starts_on=date(2026, 5, 4), ends_on=date(2026, 5, 5), approved=True
        )
        pending = _leave(
            starts_on=date(2026, 5, 6), ends_on=date(2026, 5, 7), approved=False
        )
        repo = _FakeRepo(leave_rows=[approved, pending])
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 1), to_date=date(2026, 5, 15)
        )

        ids = {v.id for v in payload.leaves}
        assert ids == {approved.id, pending.id}


class TestProjections:
    def test_weekly_projection_carries_off_pattern(self) -> None:
        repo = _FakeRepo(
            weekly_rows=[
                _weekly(0, time(9, 0), time(17, 0)),
                _weekly(1, None, None),
            ]
        )
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 1), to_date=date(2026, 5, 15)
        )

        assert [s.weekday for s in payload.weekly_availability] == [0, 1]
        assert payload.weekly_availability[1].starts_local is None
        assert payload.weekly_availability[1].ends_local is None

    def test_bookings_pass_through(self) -> None:
        booking = _booking(
            scheduled_start=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
            status="pending_approval",
        )
        repo = _FakeRepo(booking_rows=[booking])
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 1), to_date=date(2026, 5, 15)
        )

        assert [b.id for b in payload.bookings] == [booking.id]
        assert payload.bookings[0].status == "pending_approval"

    def test_payload_echoes_caller_id(self) -> None:
        repo = _FakeRepo()
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 1), to_date=date(2026, 5, 15)
        )

        assert payload.user_id == _USER_ID
