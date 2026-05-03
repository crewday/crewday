"""HTTP tests for ``/me/{schedule,leaves,availability_overrides}`` (cd-6uij).

Covers the §12 + §14 contract:

* ``GET /me/schedule`` aggregates the rich §14 calendar payload —
  window / user_id / weekly_availability / rulesets / slots /
  assignments / tasks / properties / leaves / overrides / bookings.
* The ``[from, to]`` window defaults to ``[today, today+14d]`` per §12.
* The aggregator never leaks another user's data: every per-user
  list keys on ``ctx.actor_id``.
* Approved + pending leaves / overrides land in the same list, each
  carrying its own ``approved_at`` / ``approval_required`` so the
  SPA can render the pending-banner shape.
* ``POST /me/leaves`` + ``POST /me/availability_overrides`` force
  ``user_id = ctx.actor_id``.
* Cross-workspace probes are invisible because every SELECT pins
  ``ctx.workspace_id``.
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
from app.adapters.db.payroll.models import Booking
from app.adapters.db.places.models import (
    Property,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
)
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import (
    UserWorkRole,
    WorkEngagement,
    WorkRole,
)
from app.api.v1.me_schedule import build_me_schedule_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user
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


def _seed_property(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    name: str = "Villa Sud",
) -> str:
    with factory() as s:
        prop_id = new_ulid()
        s.add(
            Property(
                id=prop_id,
                name=name,
                kind="residence",
                address=f"{name} address",
                address_json={"city": "Antibes", "country": "FR"},
                country="FR",
                locale=None,
                default_currency=None,
                timezone="Europe/Paris",
                lat=None,
                lon=None,
                client_org_id=None,
                owner_user_id=None,
                tags_json=[],
                welcome_defaults_json={},
                property_notes_md="",
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=None,
            )
        )
        s.flush()
        s.add(
            PropertyWorkspace(
                property_id=prop_id,
                workspace_id=workspace_id,
                label=name,
                membership_role="owner_workspace",
                share_guest_identity=False,
                auto_shift_from_occurrence=False,
                status="active",
                created_at=_PINNED,
            )
        )
        s.commit()
        return prop_id


def _seed_role_assignment(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    property_id: str,
    role_name: str = "Cleaner",
) -> str:
    """Seed a work_role + user_work_role + property_work_role_assignment trio."""
    with factory() as s:
        work_role = WorkRole(
            id=new_ulid(),
            workspace_id=workspace_id,
            key=f"{role_name.lower()}-{new_ulid()[-6:]}",
            name=role_name,
            description_md="",
            default_settings_json={},
            icon_name="BrushCleaning",
            created_at=_PINNED,
            deleted_at=None,
        )
        s.add(work_role)
        s.flush()
        user_work_role = UserWorkRole(
            id=new_ulid(),
            user_id=user_id,
            workspace_id=workspace_id,
            work_role_id=work_role.id,
            started_on=date(2026, 1, 1),
            ended_on=None,
            pay_rule_id=None,
            created_at=_PINNED,
            deleted_at=None,
        )
        s.add(user_work_role)
        s.flush()
        assignment = PropertyWorkRoleAssignment(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_work_role_id=user_work_role.id,
            property_id=property_id,
            schedule_ruleset_id=None,
            property_pay_rule_id=None,
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
        s.add(assignment)
        s.commit()
        return assignment.id


def _seed_occurrence(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    assignee_user_id: str | None,
    starts_at: datetime,
    scheduled_for_local: str | None,
    state: str = "pending",
    cancellation_reason: str | None = None,
    property_id: str | None = None,
) -> str:
    with factory() as s:
        occurrence_id = new_ulid()
        s.add(
            Occurrence(
                id=occurrence_id,
                workspace_id=workspace_id,
                schedule_id=None,
                template_id=None,
                property_id=property_id,
                assignee_user_id=assignee_user_id,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
                scheduled_for_local=scheduled_for_local,
                originally_scheduled_for=scheduled_for_local,
                state=state,
                completed_at=None,
                completed_by_user_id=None,
                reviewer_user_id=None,
                reviewed_at=None,
                cancellation_reason=cancellation_reason,
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


def _seed_engagement(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
) -> str:
    """Seed an active work_engagement so booking FK is satisfied."""
    with factory() as s:
        engagement_id = new_ulid()
        s.add(
            WorkEngagement(
                id=engagement_id,
                user_id=user_id,
                workspace_id=workspace_id,
                engagement_kind="payroll",
                supplier_org_id=None,
                pay_destination_id=None,
                reimbursement_destination_id=None,
                started_on=date(2026, 1, 1),
                archived_on=None,
                notes_md=None,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        s.commit()
        return engagement_id


def _seed_booking(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    work_engagement_id: str,
    property_id: str | None,
    scheduled_start: datetime,
    status: str = "scheduled",
) -> str:
    with factory() as s:
        booking_id = new_ulid()
        s.add(
            Booking(
                id=booking_id,
                workspace_id=workspace_id,
                work_engagement_id=work_engagement_id,
                user_id=user_id,
                property_id=property_id,
                client_org_id=None,
                status=status,
                kind="work",
                pay_basis="scheduled",
                scheduled_start=scheduled_start,
                scheduled_end=scheduled_start + timedelta(hours=2),
                actual_minutes=None,
                actual_minutes_paid=0,
                break_seconds=0,
                notes_md=None,
                adjusted=False,
                adjustment_reason=None,
                pending_amend_minutes=None,
                pending_amend_reason=None,
                declined_at=None,
                declined_reason=None,
                cancelled_at=None,
                cancellation_window_hours=24,
                cancellation_pay_to_worker=True,
                created_by_actor_kind=None,
                created_by_actor_id=None,
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=None,
            )
        )
        s.commit()
        return booking_id


# ---------------------------------------------------------------------------
# /me/schedule — aggregator
# ---------------------------------------------------------------------------


class TestMeSchedule:
    def test_me_schedule_returns_aggregated_window(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Caller asks for ``[from, to]`` and gets every covering row."""
        ctx, factory, ws_id, worker_id = worker_ctx
        prop_id = _seed_property(factory, workspace_id=ws_id)
        assignment_id = _seed_role_assignment(
            factory, workspace_id=ws_id, user_id=worker_id, property_id=prop_id
        )
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
        task_id = _seed_occurrence(
            factory,
            workspace_id=ws_id,
            assignee_user_id=worker_id,
            starts_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            scheduled_for_local="2026-05-05T10:00:00",
            property_id=prop_id,
        )
        engagement_id = _seed_engagement(factory, workspace_id=ws_id, user_id=worker_id)
        booking_id = _seed_booking(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            work_engagement_id=engagement_id,
            property_id=prop_id,
            scheduled_start=datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
        )

        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Window + caller id echoed.
        assert body["window"] == {"from": "2026-05-01", "to": "2026-05-15"}
        assert body["user_id"] == worker_id
        # Weekly pattern carries the seeded slot.
        assert len(body["weekly_availability"]) == 1
        assert body["weekly_availability"][0]["weekday"] == 0
        # Approved + pending merged into a single ``leaves`` list.
        leave_ids = sorted(lv["id"] for lv in body["leaves"])
        assert leave_ids == sorted([approved_leave_id, pending_leave_id])
        override_ids = sorted(ov["id"] for ov in body["overrides"])
        assert override_ids == sorted([approved_override_id, pending_override_id])
        # Tasks come from the shared scheduler resolver.
        assert [t["id"] for t in body["tasks"]] == [task_id]
        assert body["tasks"][0]["scheduled_start"] == "2026-05-05T10:00:00"
        assert body["tasks"][0]["title"] == "Clean room 12"
        assert body["tasks"][0]["property_id"] == prop_id
        # Bookings.
        assert [b["id"] for b in body["bookings"]] == [booking_id]
        assert body["bookings"][0]["work_engagement_id"] == engagement_id
        # Assignment id surfaces verbatim.
        assert any(a["id"] == assignment_id for a in body["assignments"])
        # Property in legend (touched by assignment + task + booking).
        assert [p["id"] for p in body["properties"]] == [prop_id]
        # No legacy buckets — `pending` and `holidays` are gone.
        assert "pending" not in body
        assert "holidays" not in body

    def test_me_schedule_default_window(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Omitted ``from`` / ``to`` default to ``[today, today+14d]``."""
        ctx, factory, _ws_id, _worker_id = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/me/schedule")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        from_d = date.fromisoformat(body["window"]["from"])
        to_d = date.fromisoformat(body["window"]["to"])
        assert (to_d - from_d) == timedelta(days=14)

    def test_me_schedule_excludes_other_users_data(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Another user's leaves / overrides / tasks / bookings never surface."""
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
        prop_id = _seed_property(factory, workspace_id=ws_id)
        _seed_occurrence(
            factory,
            workspace_id=ws_id,
            assignee_user_id=other_id,
            starts_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            scheduled_for_local="2026-05-05T10:00:00",
            property_id=prop_id,
        )
        other_engagement = _seed_engagement(
            factory, workspace_id=ws_id, user_id=other_id
        )
        _seed_booking(
            factory,
            workspace_id=ws_id,
            user_id=other_id,
            work_engagement_id=other_engagement,
            property_id=prop_id,
            scheduled_start=datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
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
        assert body["bookings"] == []
        for lv in body["leaves"]:
            assert lv["user_id"] == worker_id

    def test_me_schedule_merges_approved_and_pending(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Approved + pending land in the same list, each row carries its state."""
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

        leave_ids = sorted(lv["id"] for lv in body["leaves"])
        assert leave_ids == sorted([approved_leave_id, pending_leave_id])
        override_ids = sorted(ov["id"] for ov in body["overrides"])
        assert override_ids == sorted([approved_override_id, pending_override_id])
        # Each row exposes its own approval state.
        approved_leave = next(
            lv for lv in body["leaves"] if lv["id"] == approved_leave_id
        )
        pending_leave = next(
            lv for lv in body["leaves"] if lv["id"] == pending_leave_id
        )
        assert approved_leave["approved_at"] is not None
        assert pending_leave["approved_at"] is None
        approved_override = next(
            ov for ov in body["overrides"] if ov["id"] == approved_override_id
        )
        pending_override = next(
            ov for ov in body["overrides"] if ov["id"] == pending_override_id
        )
        assert approved_override["approved_at"] is not None
        assert pending_override["approved_at"] is None

    def test_me_schedule_excludes_tombstoned_rows(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Soft-deleted leaves and bookings are invisible."""
        ctx, factory, ws_id, worker_id = worker_ctx
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

        # Soft-deleted booking too.
        prop_id = _seed_property(factory, workspace_id=ws_id)
        engagement_id = _seed_engagement(factory, workspace_id=ws_id, user_id=worker_id)
        booking_id = _seed_booking(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            work_engagement_id=engagement_id,
            property_id=prop_id,
            scheduled_start=datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
        )
        with factory() as s:
            row = s.get(Booking, booking_id)
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
        assert body["bookings"] == []

    def test_me_schedule_excludes_cancelled_tasks(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Cancelled occurrences never surface in the worker's calendar feed.

        The /me/schedule route passes ``exclude_cancelled=True`` to
        the shared scheduler resolver — a cancelled task (e.g. swept
        by a schedule-delete cascade per §06) is no longer actionable
        and shouldn't clutter the worker's calendar. The manager
        ``/scheduler/calendar`` leaves cancelled rows visible because
        managers reviewing the rota need to see what got cancelled.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        prop_id = _seed_property(factory, workspace_id=ws_id)
        _seed_role_assignment(
            factory, workspace_id=ws_id, user_id=worker_id, property_id=prop_id
        )
        live_id = _seed_occurrence(
            factory,
            workspace_id=ws_id,
            assignee_user_id=worker_id,
            starts_at=datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
            scheduled_for_local="2026-05-04T10:00:00",
            state="pending",
            property_id=prop_id,
        )
        completed_id = _seed_occurrence(
            factory,
            workspace_id=ws_id,
            assignee_user_id=worker_id,
            starts_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            scheduled_for_local="2026-05-05T10:00:00",
            state="completed",
            property_id=prop_id,
        )
        cancelled_id = _seed_occurrence(
            factory,
            workspace_id=ws_id,
            assignee_user_id=worker_id,
            starts_at=datetime(2026, 5, 6, 10, 0, tzinfo=UTC),
            scheduled_for_local="2026-05-06T10:00:00",
            state="cancelled",
            cancellation_reason="schedule deleted",
            property_id=prop_id,
        )

        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        task_ids = sorted(t["id"] for t in body["tasks"])
        # Live + completed surface; cancelled is dropped.
        assert sorted(task_ids) == sorted([live_id, completed_id])
        assert cancelled_id not in task_ids

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

    def test_me_schedule_property_legend_only_touched_properties(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """The ``properties`` list narrows to assignment + task + booking footprint.

        Seeds two workspace properties; only the one the worker has an
        assignment on appears in the response. Confirms the §14
        "Property legend" only surfaces properties the worker can see
        themselves working at.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        prop_a = _seed_property(factory, workspace_id=ws_id, name="Villa Alpha")
        _seed_property(factory, workspace_id=ws_id, name="Villa Beta")
        _seed_role_assignment(
            factory, workspace_id=ws_id, user_id=worker_id, property_id=prop_a
        )

        client = _client(ctx, factory)
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        body = resp.json()
        assert [p["id"] for p in body["properties"]] == [prop_a]


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
        """Sending ``user_id`` in the body collapses to 422 ``unknown_field``."""
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
        """Spec §12: ``POST /me/leaves`` always lands pending."""
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
        """A worker self-submits and the row carries ``user_id = ctx.actor_id``."""
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
        """GET returns the caller's overrides regardless of approval state."""
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

    def test_me_availability_overrides_filters_by_date_window(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``?from=`` / ``?to=`` slice the listing to the requested window."""
        ctx, factory, ws_id, worker_id = worker_ctx
        # Two out-of-window rows + one in-window row; only the
        # in-window id should round-trip through ``?from=`` / ``?to=``.
        _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 1),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )
        inside = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 5),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )
        _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 20),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )

        client = _client(ctx, factory)
        resp = client.get(
            "/me/availability_overrides",
            params={"from": "2026-05-04", "to": "2026-05-10"},
        )
        assert resp.status_code == 200, resp.text
        ids = [ov["id"] for ov in resp.json()["data"]]
        assert ids == [inside]

    def test_me_availability_overrides_filters_by_approved(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``?approved=true|false`` narrows by approval state."""
        ctx, factory, ws_id, worker_id = worker_ctx
        approved_id = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 4),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )
        pending_id = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 5),
            available=False,
            approved=False,
            approval_required=True,
        )

        client = _client(ctx, factory)
        resp_true = client.get(
            "/me/availability_overrides", params={"approved": "true"}
        )
        assert resp_true.status_code == 200, resp_true.text
        assert [ov["id"] for ov in resp_true.json()["data"]] == [approved_id]

        resp_false = client.get(
            "/me/availability_overrides", params={"approved": "false"}
        )
        assert resp_false.status_code == 200, resp_false.text
        assert [ov["id"] for ov in resp_false.json()["data"]] == [pending_id]

    def test_me_availability_overrides_cursor_round_trip(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``next_cursor`` round-trips: page 2 picks up where page 1 stopped."""
        ctx, factory, ws_id, worker_id = worker_ctx
        ids = [
            _seed_override(
                factory,
                workspace_id=ws_id,
                user_id=worker_id,
                on_date=date(2026, 5, day),
                available=True,
                approved=True,
                starts_local=time(8, 0),
                ends_local=time(18, 0),
            )
            for day in range(1, 6)
        ]

        client = _client(ctx, factory)
        first = client.get("/me/availability_overrides", params={"limit": 2})
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert len(first_body["data"]) == 2
        assert first_body["has_more"] is True
        assert first_body["next_cursor"] is not None

        second = client.get(
            "/me/availability_overrides",
            params={"limit": 2, "cursor": first_body["next_cursor"]},
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        # No overlap between page 1 and page 2.
        page_one_ids = {ov["id"] for ov in first_body["data"]}
        page_two_ids = {ov["id"] for ov in second_body["data"]}
        assert page_one_ids.isdisjoint(page_two_ids)
        # Together with subsequent pages, the entire seeded set is reachable.
        assert page_one_ids.issubset(set(ids))
        assert page_two_ids.issubset(set(ids))

    def test_me_availability_overrides_user_id_query_is_ignored(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A bogus ``?user_id=`` query never widens the listing past ``ctx.actor_id``.

        FastAPI silently drops query params that are not declared on
        the handler, so a worker passing ``?user_id=<other>`` still
        only sees their own rows. The router does **not** treat this
        as a 422 — we deliberately mirror the parent listing's
        permissive query handling and rely on the forced
        ``user_id = ctx.actor_id`` to keep the contract honest.
        """
        ctx, factory, ws_id, worker_id = worker_ctx
        own_id = _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            on_date=date(2026, 5, 4),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )
        with factory() as s:
            other = bootstrap_user(
                s, email="other-user-id-query@example.com", display_name="Other"
            )
            s.commit()
            other_id = other.id
        _seed_override(
            factory,
            workspace_id=ws_id,
            user_id=other_id,
            on_date=date(2026, 5, 4),
            available=True,
            approved=True,
            starts_local=time(8, 0),
            ends_local=time(18, 0),
        )

        client = _client(ctx, factory)
        resp = client.get("/me/availability_overrides", params={"user_id": other_id})
        assert resp.status_code == 200, resp.text
        ids = [ov["id"] for ov in resp.json()["data"]]
        assert ids == [own_id]


# ---------------------------------------------------------------------------
# Cross-workspace isolation
# ---------------------------------------------------------------------------


class TestCrossWorkspace:
    def test_me_schedule_blocks_cross_workspace_data(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A leave in another workspace never surfaces in the caller's feed."""
        from tests.factories.identity import bootstrap_workspace

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
