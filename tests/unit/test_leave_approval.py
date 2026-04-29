"""Unit tests for manager leave approval flow (cd-8pi)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.time.models import Leave, Shift
from app.adapters.db.workspace.models import Workspace
from app.events.bus import EventBus
from app.events.types import LeaveDecided
from app.services.leave import (
    LeaveDecisionRequest,
    LeavePermissionDenied,
    LeaveTransitionForbidden,
    decide_leave,
    get_conflicts,
)
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_START = _PINNED + timedelta(days=7)
_END = _START + timedelta(days=2)


def _load_all_models() -> None:
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug="leave-approval",
            name="Leave Approval",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _user(s: Session, email: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=email,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _grant(s: Session, *, workspace_id: str, user_id: str, grant_role: str) -> None:
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()


def _ctx(
    *, workspace_id: str, actor_id: str, grant_role: ActorGrantRole
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="leave-approval",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _leave(s: Session, *, workspace_id: str, user_id: str) -> str:
    leave_id = new_ulid()
    s.add(
        Leave(
            id=leave_id,
            workspace_id=workspace_id,
            user_id=user_id,
            kind="vacation",
            starts_at=_START,
            ends_at=_END,
            status="pending",
            reason_md="family",
            decided_by=None,
            decided_at=None,
            created_at=_PINNED,
        )
    )
    s.flush()
    return leave_id


def _seed_env(session: Session) -> tuple[WorkspaceContext, WorkspaceContext, str, str]:
    workspace_id = _workspace(session)
    manager_id = _user(session, "manager@example.com")
    worker_id = _user(session, "worker@example.com")
    _grant(session, workspace_id=workspace_id, user_id=manager_id, grant_role="manager")
    _grant(session, workspace_id=workspace_id, user_id=worker_id, grant_role="worker")
    leave_id = _leave(session, workspace_id=workspace_id, user_id=worker_id)
    session.commit()
    return (
        _ctx(workspace_id=workspace_id, actor_id=manager_id, grant_role="manager"),
        _ctx(workspace_id=workspace_id, actor_id=worker_id, grant_role="worker"),
        leave_id,
        worker_id,
    )


def _recorded(bus: EventBus) -> list[LeaveDecided]:
    events: list[LeaveDecided] = []
    bus.subscribe(LeaveDecided)(events.append)
    return events


def test_conflicts_empty_for_pending_leave(session: Session) -> None:
    manager_ctx, _worker_ctx, leave_id, _worker_id = _seed_env(session)

    conflicts = get_conflicts(session, manager_ctx, leave_id=leave_id)

    assert conflicts.shift_ids == ()
    assert conflicts.occurrence_ids == ()


def test_conflicts_return_overlapping_work_ids(session: Session) -> None:
    manager_ctx, _worker_ctx, leave_id, worker_id = _seed_env(session)
    overlapping_shift = Shift(
        id=new_ulid(),
        workspace_id=manager_ctx.workspace_id,
        user_id=worker_id,
        starts_at=_START + timedelta(hours=2),
        ends_at=_START + timedelta(hours=6),
        property_id=None,
        source="manual",
        notes_md=None,
        approved_by=None,
        approved_at=None,
    )
    adjacent_shift = Shift(
        id=new_ulid(),
        workspace_id=manager_ctx.workspace_id,
        user_id=worker_id,
        starts_at=_END,
        ends_at=_END + timedelta(hours=2),
        property_id=None,
        source="manual",
        notes_md=None,
        approved_by=None,
        approved_at=None,
    )
    overlapping_occurrence = Occurrence(
        id=new_ulid(),
        workspace_id=manager_ctx.workspace_id,
        schedule_id=None,
        template_id=None,
        property_id=None,
        assignee_user_id=worker_id,
        starts_at=_START + timedelta(hours=4),
        ends_at=_START + timedelta(hours=5),
        scheduled_for_local="2026-04-26T16:00:00",
        originally_scheduled_for="2026-04-26T16:00:00",
        state="scheduled",
        title="linen reset",
        created_at=_PINNED,
    )
    cancelled_occurrence = Occurrence(
        id=new_ulid(),
        workspace_id=manager_ctx.workspace_id,
        schedule_id=None,
        template_id=None,
        property_id=None,
        assignee_user_id=worker_id,
        starts_at=_START + timedelta(hours=5),
        ends_at=_START + timedelta(hours=6),
        scheduled_for_local="2026-04-26T17:00:00",
        originally_scheduled_for="2026-04-26T17:00:00",
        state="cancelled",
        title="cancelled",
        created_at=_PINNED,
    )
    session.add_all(
        [
            overlapping_shift,
            adjacent_shift,
            overlapping_occurrence,
            cancelled_occurrence,
        ]
    )
    session.flush()

    conflicts = get_conflicts(session, manager_ctx, leave_id=leave_id)

    assert conflicts.shift_ids == (overlapping_shift.id,)
    assert conflicts.occurrence_ids == (overlapping_occurrence.id,)


def test_decision_flips_status_audits_and_publishes_once(session: Session) -> None:
    manager_ctx, _worker_ctx, leave_id, _worker_id = _seed_env(session)
    bus = EventBus()
    events = _recorded(bus)
    clock = FrozenClock(_PINNED + timedelta(minutes=5))

    view = decide_leave(
        session,
        manager_ctx,
        leave_id=leave_id,
        body=LeaveDecisionRequest(decision="approved", rationale_md="covered"),
        clock=clock,
        event_bus=bus,
    )
    replay = decide_leave(
        session,
        manager_ctx,
        leave_id=leave_id,
        body=LeaveDecisionRequest(decision="approved", rationale_md="covered"),
        clock=FrozenClock(_PINNED + timedelta(minutes=10)),
        event_bus=bus,
    )

    assert view.status == "approved"
    assert view.decided_by == manager_ctx.actor_id
    assert view.decided_at == _PINNED + timedelta(minutes=5)
    assert replay.decided_at == view.decided_at
    assert len(events) == 1
    assert events[0].name == "leave.decided"
    assert events[0].leave_id == leave_id
    assert events[0].decision == "approved"

    audits = session.scalars(
        select(AuditLog).where(
            AuditLog.workspace_id == manager_ctx.workspace_id,
            AuditLog.entity_kind == "leave",
            AuditLog.entity_id == leave_id,
        )
    ).all()
    assert [row.action for row in audits] == ["leave.decided"]
    diff = audits[0].diff
    assert diff["before"]["status"] == "pending"
    assert diff["after"]["status"] == "approved"
    assert diff["rationale_md"] == "covered"


def test_terminal_decision_cannot_change(session: Session) -> None:
    manager_ctx, _worker_ctx, leave_id, _worker_id = _seed_env(session)
    decide_leave(
        session,
        manager_ctx,
        leave_id=leave_id,
        body=LeaveDecisionRequest(decision="approved", rationale_md=None),
        event_bus=EventBus(),
    )

    with pytest.raises(LeaveTransitionForbidden):
        decide_leave(
            session,
            manager_ctx,
            leave_id=leave_id,
            body=LeaveDecisionRequest(decision="rejected", rationale_md="changed"),
            event_bus=EventBus(),
        )


def test_worker_cannot_decide_or_read_conflicts(session: Session) -> None:
    _manager_ctx, worker_ctx, leave_id, _worker_id = _seed_env(session)

    with pytest.raises(LeavePermissionDenied):
        decide_leave(
            session,
            worker_ctx,
            leave_id=leave_id,
            body=LeaveDecisionRequest(decision="approved", rationale_md=None),
            event_bus=EventBus(),
        )

    with pytest.raises(LeavePermissionDenied):
        get_conflicts(session, worker_ctx, leave_id=leave_id)
