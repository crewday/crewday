"""HTTP tests for ``/me/{schedule,leaves,availability_overrides}`` (cd-6uij).

Covers the §12 "Self-service shortcuts" contract:

* ``GET /me/schedule`` aggregates rota / tasks / approved leaves /
  approved overrides / holidays / pending for a date window.
* The ``[from, to]`` window defaults to ``[today, today+14d]`` per §12.
* The aggregator never leaks another user's data: rota / tasks /
  leaves / overrides / pending all key on ``ctx.actor_id``.
* Approved rows land in ``leaves`` / ``overrides``; pending rows
  land under ``pending`` so the UI does not promote them into the
  live precedence stack.
* ``POST /me/leaves`` + ``POST /me/availability_overrides`` force
  ``user_id = ctx.actor_id``: the request body forbids the field at
  the schema layer, so a worker passing ``user_id: <other-id>``
  collapses to a 422 from Pydantic ``extra="forbid"``.
* Cross-workspace probes are invisible because the aggregator's
  every SELECT keys on ``ctx.workspace_id``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.availability.models import (
    UserAvailabilityOverride,
    UserLeave,
    UserWeeklyAvailability,
)
from app.adapters.db.holidays.models import PublicHoliday
from app.adapters.db.tasks.models import Occurrence
from app.api.v1.me_schedule import build_me_schedule_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_me_schedule_router())], factory, ctx)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_weekly(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    weekday: int,
    starts_local: time | None,
    ends_local: time | None,
) -> None:
    with factory() as s:
        s.add(
            UserWeeklyAvailability(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                weekday=weekday,
                starts_local=starts_local,
                ends_local=ends_local,
                updated_at=_PINNED,
            )
        )
        s.commit()


def _seed_leave(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    starts_on: date,
    ends_on: date,
    approved: bool,
    category: str = "personal",
) -> str:
    with factory() as s:
        leave_id = new_ulid()
        s.add(
            UserLeave(
                id=leave_id,
                workspace_id=workspace_id,
                user_id=user_id,
                starts_on=starts_on,
                ends_on=ends_on,
                category=category,
                approved_at=_PINNED if approved else None,
                approved_by=user_id if approved else None,
                note_md=None,
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=None,
            )
        )
        s.commit()
        return leave_id


def _seed_override(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    on_date: date,
    available: bool,
    approved: bool,
    starts_local: time | None = None,
    ends_local: time | None = None,
    approval_required: bool = False,
) -> str:
    with factory() as s:
        override_id = new_ulid()
        s.add(
            UserAvailabilityOverride(
                id=override_id,
                workspace_id=workspace_id,
                user_id=user_id,
                date=on_date,
                available=available,
                starts_local=starts_local,
                ends_local=ends_local,
                reason=None,
                approval_required=approval_required,
                approved_at=_PINNED if approved else None,
                approved_by=user_id if approved else None,
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=None,
            )
        )
        s.commit()
        return override_id


def _seed_holiday(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    name: str,
    on_date: date,
    country: str | None = None,
) -> str:
    with factory() as s:
        holiday_id = new_ulid()
        s.add(
            PublicHoliday(
                id=holiday_id,
                workspace_id=workspace_id,
                name=name,
                date=on_date,
                country=country,
                scheduling_effect="block",
                reduced_starts_local=None,
                reduced_ends_local=None,
                payroll_multiplier=None,
                recurrence=None,
                notes_md=None,
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=None,
            )
        )
        s.commit()
        return holiday_id


def _seed_occurrence(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    assignee_user_id: str | None,
    starts_at: datetime,
    scheduled_for_local: str | None,
) -> str:
    with factory() as s:
        occurrence_id = new_ulid()
        s.add(
            Occurrence(
                id=occurrence_id,
                workspace_id=workspace_id,
                schedule_id=None,
                template_id=None,
                property_id=None,
                assignee_user_id=assignee_user_id,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
                scheduled_for_local=scheduled_for_local,
                originally_scheduled_for=scheduled_for_local,
                state="pending",
                completed_at=None,
                completed_by_user_id=None,
                reviewer_user_id=None,
                reviewed_at=None,
                cancellation_reason=None,
                title="Clean room 12",
                description_md=None,
                priority="normal",
                photo_evidence="disabled",
                duration_minutes=None,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=None,
                created_at=_PINNED,
            )
        )
        s.commit()
        return occurrence_id


# ---------------------------------------------------------------------------
# /me/schedule — aggregator
# ---------------------------------------------------------------------------


class TestMeSchedule:
    def test_me_schedule_returns_aggregated_window(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Caller asks for ``[from, to]`` and gets every covering row.

        Seeds one rota slot, one approved leave, one approved override,
        one pending leave, one pending override, one holiday, and one
        assigned task. The response carries each in the right bucket.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        approved_leave_id = _seed_leave(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            starts_on=date(2026, 5, 4),
            ends_on=date(2026, 5, 5),
            approved=True,
        )
        pending_leave_id = _seed_leave(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            starts_on=date(2026, 5, 10),
            ends_on=date(2026, 5, 12),
            approved=False,
            category="vacation",
        )
        approved_override_id = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 6),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )
        pending_override_id = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 7),
            available=False,
            approved=False,
            approval_required=True,
        )
        holiday_id = _seed_holiday(
            factory,
            workspace_id=ws_id,
            name="Labour Day",
            on_date=date(2026, 5, 1),
        )
        task_id = _seed_occurrence(
            factory,
            workspace_id=ws_id,
            assignee_user_id=worker_id,
            starts_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            scheduled_for_local="2026-05-05T10:00:00",
        )

        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["from"] == "2026-05-01"
        assert body["to"] == "2026-05-15"
        assert len(body["rota"]) == 1
        assert body["rota"][0]["weekday"] == 0
        assert body["rota"][0]["starts_local"] == "09:00:00"
        leave_ids = [lv["id"] for lv in body["leaves"]]
        assert leave_ids == [approved_leave_id]
        override_ids = [ov["id"] for ov in body["overrides"]]
        assert override_ids == [approved_override_id]
        assert [h["id"] for h in body["holidays"]] == [holiday_id]
        assert [t["id"] for t in body["tasks"]] == [task_id]
        assert body["tasks"][0]["scheduled_for_local"] == "2026-05-05T10:00:00"
        pending_leave_ids = [lv["id"] for lv in body["pending"]["leaves"]]
        pending_override_ids = [ov["id"] for ov in body["pending"]["overrides"]]
        assert pending_leave_ids == [pending_leave_id]
        assert pending_override_ids == [pending_override_id]

    def test_me_schedule_default_window(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Omitted ``from`` / ``to`` default to ``[today, today+14d]``.

        Asserts both edges are echoed in the response and span 14
        days. We don't pin a specific date here because the system
        clock walks; the test just checks the relative spread.
        """
        ctx, factory, _ws_id, _worker_id = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/me/schedule")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        from_d = date.fromisoformat(body["from"])
        to_d = date.fromisoformat(body["to"])
        assert (to_d - from_d) == timedelta(days=14)

    def test_me_schedule_excludes_other_users_data(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Another user's leaves / overrides / tasks never surface to the caller.

        Seeds a sibling user's row inside the same workspace and the
        same window; the caller's feed must not see it.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        # Sibling user in the same workspace.
        with factory() as s:
            other = bootstrap_user(
                s, email="other-me@example.com", display_name="Other"
            )
            s.commit()
            other_id = other.id

        _seed_leave(
            factory,
            workspace_id=ws_id,
            user_id=other_id,
            starts_on=date(2026, 5, 4),
            ends_on=date(2026, 5, 5),
            approved=True,
        )
        _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=other_id,
            on_date=date(2026, 5, 6),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )
        _seed_occurrence(
            factory,
            workspace_id=ws_id,
            assignee_user_id=other_id,
            starts_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            scheduled_for_local="2026-05-05T10:00:00",
        )

        # Caller's own minimal seed.
        own_leave_id = _seed_leave(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            starts_on=date(2026, 5, 4),
            ends_on=date(2026, 5, 4),
            approved=True,
        )

        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [lv["id"] for lv in body["leaves"]] == [own_leave_id]
        assert body["overrides"] == []
        assert body["tasks"] == []
        for lv in body["leaves"]:
            assert lv["user_id"] == worker_id

    def test_me_schedule_separates_approved_from_pending(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Approved → top-level ``leaves``/``overrides``; pending → ``pending``.

        Seeds two leaves (one approved, one pending) and two
        overrides on the same dates. Asserts the partitioning is
        exact: a row appears in exactly one bucket, never both.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        approved_leave_id = _seed_leave(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            starts_on=date(2026, 5, 4),
            ends_on=date(2026, 5, 4),
            approved=True,
        )
        pending_leave_id = _seed_leave(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            starts_on=date(2026, 5, 5),
            ends_on=date(2026, 5, 5),
            approved=False,
        )
        approved_override_id = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 6),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )
        pending_override_id = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 7),
            available=False,
            approved=False,
            approval_required=True,
        )

        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        body = resp.json()

        assert [lv["id"] for lv in body["leaves"]] == [approved_leave_id]
        assert [ov["id"] for ov in body["overrides"]] == [approved_override_id]
        assert [lv["id"] for lv in body["pending"]["leaves"]] == [pending_leave_id]
        assert [ov["id"] for ov in body["pending"]["overrides"]] == [
            pending_override_id
        ]

        # Disjoint partitioning: an id never lands in both buckets.
        approved_ids = {lv["id"] for lv in body["leaves"]} | {
            ov["id"] for ov in body["overrides"]
        }
        pending_ids = {lv["id"] for lv in body["pending"]["leaves"]} | {
            ov["id"] for ov in body["pending"]["overrides"]
        }
        assert approved_ids.isdisjoint(pending_ids)

    def test_me_schedule_excludes_tombstoned_rows(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Soft-deleted leaves / overrides + tombstoned holidays are invisible.

        The aggregator filters ``deleted_at IS NULL`` on every
        soft-delete-aware table so a withdrawn request doesn't
        resurface in the worker's feed.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        # Soft-delete a leave by stamping deleted_at after seed.
        leave_id = _seed_leave(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            starts_on=date(2026, 5, 4),
            ends_on=date(2026, 5, 4),
            approved=True,
        )
        with factory() as s:
            row = s.get(UserLeave, leave_id)
            assert row is not None
            row.deleted_at = _PINNED
            s.commit()

        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        body = resp.json()
        assert body["leaves"] == []
        assert body["pending"]["leaves"] == []

    def test_me_schedule_window_validation(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``to < from`` collapses to 422 ``invalid_field``."""
        ctx, factory, _ws_id, _worker_id = worker_ctx
        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-15", "to": "2026-05-01"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"] == "invalid_field"
        assert detail["field"] == "to"


# ---------------------------------------------------------------------------
# /me/leaves — POST
# ---------------------------------------------------------------------------


class TestMeLeavesCreate:
    def test_me_leaves_forces_user_id_to_caller(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A worker self-submits and the row carries ``user_id = ctx.actor_id``."""
        ctx, factory, _ws_id, worker_id = worker_ctx
        client = _client(ctx, factory)

        resp = client.post(
            "/me/leaves",
            json={
                "starts_on": "2026-07-01",
                "ends_on": "2026-07-02",
                "category": "sick",
                "note_md": "Flu",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == worker_id
        assert body["approved_at"] is None  # worker self-submit lands pending

    def test_me_leaves_rejects_explicit_user_id(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Sending ``user_id`` in the body collapses to 422 ``unknown_field``.

        Pydantic ``extra="forbid"`` rejects the field at the schema
        layer — the cleanest way to enforce "self only" at the wire
        without a route-level 403 check that would race the schema
        validator.
        """
        ctx, factory, _ws_id, worker_id = worker_ctx
        # Mint a sibling whose id we can pass.
        with factory() as s:
            other = bootstrap_user(
                s, email="other-leave@example.com", display_name="Other"
            )
            s.commit()
            other_id = other.id
        # ``other_id`` differs from worker_id by construction.
        assert other_id != worker_id

        client = _client(ctx, factory)
        resp = client.post(
            "/me/leaves",
            json={
                "user_id": other_id,
                "starts_on": "2026-07-01",
                "ends_on": "2026-07-02",
                "category": "sick",
            },
        )
        assert resp.status_code == 422, resp.text

    def test_me_leaves_validation_window(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Backwards window (``ends < starts``) collapses to 422."""
        ctx, factory, _ws_id, _worker_id = worker_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/me/leaves",
            json={
                "starts_on": "2026-07-05",
                "ends_on": "2026-07-01",
                "category": "personal",
            },
        )
        assert resp.status_code == 422

    def test_me_leaves_always_pending_even_for_manager(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Spec §12: ``POST /me/leaves`` always lands pending.

        Even a manager / owner self-submitting through this surface
        lands the row pending — ``approved_at`` stays null. A manager
        wanting to retroactively self-log + auto-approve uses the
        generic ``POST /user_leaves`` endpoint instead.
        """
        ctx, factory, _ws_id = owner_ctx
        client = _client(ctx, factory)

        resp = client.post(
            "/me/leaves",
            json={
                "starts_on": "2026-07-01",
                "ends_on": "2026-07-02",
                "category": "personal",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approved_at"] is None
        assert body["approved_by"] is None


# ---------------------------------------------------------------------------
# /me/availability_overrides — POST
# ---------------------------------------------------------------------------


class TestMeAvailabilityOverridesCreate:
    def test_me_availability_overrides_forces_user_id_to_caller(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A worker self-submits and the row carries ``user_id = ctx.actor_id``.

        Seeds a working pattern for the relevant weekday so the §06
        approval matrix produces a deterministic ``approval_required``;
        a workspace with no weekly pattern would auto-approve every
        override (pattern off → adding work → approved), which is
        already covered separately.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        # 2026-05-04 is a Monday → weekday=0.
        _seed_weekly(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        client = _client(ctx, factory)

        resp = client.post(
            "/me/availability_overrides",
            json={
                "date": "2026-05-04",
                "available": False,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == worker_id
        assert body["approval_required"] is True
        assert body["approved_at"] is None

    def test_me_availability_overrides_rejects_explicit_user_id(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Sending ``user_id`` in the body collapses to 422 ``unknown_field``."""
        ctx, factory, _ws_id, worker_id = worker_ctx
        with factory() as s:
            other = bootstrap_user(
                s, email="other-ovr@example.com", display_name="Other"
            )
            s.commit()
            other_id = other.id
        assert other_id != worker_id

        client = _client(ctx, factory)
        resp = client.post(
            "/me/availability_overrides",
            json={
                "user_id": other_id,
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "13:00:00",
            },
        )
        assert resp.status_code == 422, resp.text

    def test_me_availability_overrides_invalid_hours_pairing(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Half-set ``starts_local`` without ``ends_local`` → 422."""
        ctx, factory, _ws_id, _worker_id = worker_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/me/availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /me/availability_overrides — GET
# ---------------------------------------------------------------------------


class TestMeAvailabilityOverridesList:
    def test_me_availability_overrides_lists_only_caller_rows(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """GET returns the caller's overrides regardless of approval state.

        Seeds two of the caller's overrides (one approved, one
        pending) and one sibling user's row in the same workspace —
        only the caller's two surface, in id-ascending order.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        own_approved = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 4),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )
        own_pending = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 5),
            available=False,
            approved=False,
            approval_required=True,
        )
        # Sibling in the same workspace — must not surface.
        with factory() as s:
            other = bootstrap_user(
                s, email="other-list@example.com", display_name="Other"
            )
            s.commit()
            other_id = other.id
        _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=other_id,
            on_date=date(2026, 5, 6),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )

        client = _client(ctx, factory)
        resp = client.get("/me/availability_overrides")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids = sorted(ov["id"] for ov in body["data"])
        assert ids == sorted([own_approved, own_pending])
        for ov in body["data"]:
            assert ov["user_id"] == worker_id

    def test_me_availability_overrides_empty(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """No rows → ``{data: [], next_cursor: null, has_more: false}``."""
        ctx, factory, _ws_id, _worker_id = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/me/availability_overrides")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"data": [], "next_cursor": None, "has_more": False}


# ---------------------------------------------------------------------------
# Cross-workspace isolation
# ---------------------------------------------------------------------------


class TestCrossWorkspace:
    def test_me_schedule_blocks_cross_workspace_data(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A leave in another workspace never surfaces in the caller's feed.

        The caller's :class:`WorkspaceContext` pins the SELECT predicate;
        even though the row sits in the same DB and the same user id
        repeats, the workspace_id partition rejects it.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        # Mint a second workspace with the same user id paired in.
        with factory() as s:
            other_ws = bootstrap_workspace(
                s,
                slug="ws-other-me",
                name="Other WS",
                owner_user_id=worker_id,
            )
            s.commit()
            other_ws_id = other_ws.id
        assert other_ws_id != ws_id
        # Same worker id, different workspace.
        _seed_leave(
            factory,
            workspace_id=other_ws_id,
            user_id=worker_id,
            starts_on=date(2026, 5, 4),
            ends_on=date(2026, 5, 5),
            approved=True,
        )

        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["leaves"] == []
        assert body["pending"]["leaves"] == []
