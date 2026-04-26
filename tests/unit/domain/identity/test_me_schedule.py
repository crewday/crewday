"""Pure-function tests for the §12 schedule aggregator (cd-lot5).

The HTTP-tier suite at :mod:`tests.unit.api.v1.identity.test_me_schedule`
exercises the full feed end-to-end through the router + service + DB.
This module pins the aggregator's pure logic against a fake
:class:`~app.domain.identity.me_schedule_ports.MeScheduleQueryRepository`
so a future regression in the aggregator (a swapped predicate, a
miscategorised pending row, a window default drift) fires here in
milliseconds without spinning up a TestClient or a SQLite engine.

Specifically pins:

* The window default fires when both ``from_date`` and ``to_date`` are
  ``None`` (clock-driven; injected ``Clock`` keeps the test
  deterministic).
* Approved overrides + leaves land in the top-level buckets; pending
  rows land under :attr:`SchedulePayload.pending`.
* The defensive "approval_required=False without approved_at" branch
  is dropped from both buckets (neither approved nor pending).
* A backwards window (``to_date < from_date``) returns empty calendar
  buckets but keeps the rota (the rota is window-independent).
* The aggregator passes ``ctx.actor_id`` + ``ctx.workspace_id`` to
  every repo method — defence-in-depth that the seam never broadens
  to another user / workspace.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.domain.identity.availability_ports import (
    UserAvailabilityOverrideRow,
    UserLeaveRow,
    UserWeeklyAvailabilityRow,
)
from app.domain.identity.me_schedule import (
    DEFAULT_WINDOW_DAYS,
    aggregate_schedule,
)
from app.domain.identity.me_schedule_ports import (
    OccurrenceRefRow,
    PublicHolidayRow,
)
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
    """Records the kwargs each repo method received.

    The aggregator must hand every read the caller's
    ``workspace_id`` + ``user_id``; this struct lets the tests assert
    the predicate flowed through verbatim.
    """

    workspace_id: str | None = None
    user_id: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    window_start_utc: datetime | None = None
    window_end_utc: datetime | None = None


@dataclass
class _FakeRepo:
    """Hand-rolled fake of :class:`MeScheduleQueryRepository`.

    Returns exactly the rows the test seeds; every call records its
    kwargs so the aggregator's defence-in-depth predicates can be
    asserted on directly.
    """

    weekly_rows: Sequence[UserWeeklyAvailabilityRow] = field(default_factory=list)
    override_rows: Sequence[UserAvailabilityOverrideRow] = field(default_factory=list)
    leave_rows: Sequence[UserLeaveRow] = field(default_factory=list)
    holiday_rows: Sequence[PublicHolidayRow] = field(default_factory=list)
    occurrence_rows: Sequence[OccurrenceRefRow] = field(default_factory=list)
    weekly_call: _CallRecord = field(default_factory=_CallRecord)
    override_call: _CallRecord = field(default_factory=_CallRecord)
    leave_call: _CallRecord = field(default_factory=_CallRecord)
    holiday_call: _CallRecord = field(default_factory=_CallRecord)
    occurrence_call: _CallRecord = field(default_factory=_CallRecord)

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

    def list_holidays_in_window(
        self,
        *,
        workspace_id: str,
        from_date: date,
        to_date: date,
    ) -> Sequence[PublicHolidayRow]:
        self.holiday_call = _CallRecord(
            workspace_id=workspace_id,
            from_date=from_date,
            to_date=to_date,
        )
        return self.holiday_rows

    def list_assigned_occurrences_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> Sequence[OccurrenceRefRow]:
        self.occurrence_call = _CallRecord(
            workspace_id=workspace_id,
            user_id=user_id,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
        )
        return self.occurrence_rows


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


def _holiday(*, on_date: date, name: str = "Bank Holiday") -> PublicHolidayRow:
    return PublicHolidayRow(
        id=new_ulid(),
        name=name,
        date=on_date,
        country=None,
        scheduling_effect="block",
        reduced_starts_local=None,
        reduced_ends_local=None,
        payroll_multiplier=Decimal("1.50"),
    )


def _occurrence_ref(*, scheduled_for_local: str) -> OccurrenceRefRow:
    return OccurrenceRefRow(
        id=new_ulid(),
        scheduled_for_local=scheduled_for_local,
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

        # Workspace + user pinned on every call that takes a user.
        for call in (
            repo.weekly_call,
            repo.override_call,
            repo.leave_call,
            repo.occurrence_call,
        ):
            assert call.workspace_id == _WS_ID
            assert call.user_id == _USER_ID
        # Holiday is workspace-scoped only (no per-user filter — see
        # the seam docstring on country-narrowing being v1-deferred).
        assert repo.holiday_call.workspace_id == _WS_ID
        assert repo.holiday_call.user_id is None

        # Window edges flow through verbatim to the date-keyed reads.
        assert repo.override_call.from_date == from_d
        assert repo.override_call.to_date == to_d
        assert repo.leave_call.from_date == from_d
        assert repo.leave_call.to_date == to_d
        assert repo.holiday_call.from_date == from_d
        assert repo.holiday_call.to_date == to_d

    def test_occurrence_window_resolves_to_full_day_utc_bounds(self) -> None:
        """``window_start_utc`` = 00:00, ``window_end_utc`` = 23:59:59.999999."""
        repo = _FakeRepo()
        ctx = _ctx()
        from_d = date(2026, 5, 1)
        to_d = date(2026, 5, 15)

        aggregate_schedule(repo, ctx, from_date=from_d, to_date=to_d)

        assert repo.occurrence_call.window_start_utc == datetime(
            2026, 5, 1, 0, 0, 0, tzinfo=UTC
        )
        # ``time.max`` is 23:59:59.999999.
        assert repo.occurrence_call.window_end_utc == datetime(
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


class TestApprovedVsPending:
    """Approved rows top-level; pending rows under :attr:`SchedulePayload.pending`."""

    def test_partitioning_is_disjoint_for_overrides(self) -> None:
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

        assert [v.id for v in payload.overrides] == [approved.id]
        assert [v.id for v in payload.pending.overrides] == [pending.id]

    def test_partitioning_is_disjoint_for_leaves(self) -> None:
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

        assert [v.id for v in payload.leaves] == [approved.id]
        assert [v.id for v in payload.pending.leaves] == [pending.id]

    def test_override_without_approval_required_and_unapproved_drops(self) -> None:
        """Defensive branch: ``approval_required=False`` + ``approved_at=None`` drops.

        Per the §06 hybrid-approval matrix this state is unreachable
        in production (an auto-approved row stamps ``approved_at`` at
        insert time). The aggregator drops the row from **both**
        buckets so a future bug in the approval calculator surfaces
        as a missing row in the worker feed (the bug becomes visible)
        rather than as an "approved" misclassification (silently
        allowing assignment).
        """
        rogue = _override(
            on_date=date(2026, 5, 4),
            available=True,
            approval_required=False,
            approved=False,
        )
        repo = _FakeRepo(override_rows=[rogue])
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 1), to_date=date(2026, 5, 15)
        )

        assert payload.overrides == []
        assert payload.pending.overrides == []


class TestRotaIsWindowIndependent:
    def test_backwards_window_keeps_rota_drops_calendar(self) -> None:
        """``to_date < from_date`` returns empty calendar buckets, but rota survives.

        The rota is the standing weekly pattern — it has no calendar
        edges to respect, so a malformed window must not erase it.
        """
        repo = _FakeRepo(
            weekly_rows=[_weekly(0, time(9, 0), time(17, 0))],
            # The seams may still return matching rows even on a
            # backwards window; the aggregator is permissive (see the
            # docstring on :func:`aggregate_schedule`). We pin empty
            # collections to keep the test focused.
        )
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 15), to_date=date(2026, 5, 1)
        )

        assert payload.from_date == date(2026, 5, 15)
        assert payload.to_date == date(2026, 5, 1)
        assert len(payload.rota) == 1
        assert payload.rota[0].weekday == 0
        assert payload.tasks == []
        assert payload.leaves == []
        assert payload.overrides == []
        assert payload.holidays == []
        assert payload.pending.leaves == []
        assert payload.pending.overrides == []


class TestProjections:
    def test_holiday_projection_carries_payroll_decimal(self) -> None:
        row = _holiday(on_date=date(2026, 5, 1))
        repo = _FakeRepo(holiday_rows=[row])
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 1), to_date=date(2026, 5, 15)
        )

        assert len(payload.holidays) == 1
        view = payload.holidays[0]
        assert view.id == row.id
        assert view.name == row.name
        assert view.payroll_multiplier == Decimal("1.50")
        assert view.scheduling_effect == "block"

    def test_occurrence_ref_projection_passes_local_through(self) -> None:
        row = _occurrence_ref(scheduled_for_local="2026-05-05T10:00:00")
        repo = _FakeRepo(occurrence_rows=[row])
        ctx = _ctx()

        payload = aggregate_schedule(
            repo, ctx, from_date=date(2026, 5, 1), to_date=date(2026, 5, 15)
        )

        assert len(payload.tasks) == 1
        assert payload.tasks[0].id == row.id
        assert payload.tasks[0].scheduled_for_local == "2026-05-05T10:00:00"
