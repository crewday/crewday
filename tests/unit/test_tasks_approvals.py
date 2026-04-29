"""Unit tests for :mod:`app.domain.tasks.approvals` (cd-z2py)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import (
    Evidence,
    Occurrence,
    TaskApproval,
    TaskTemplate,
)
from app.adapters.db.workspace.models import Workspace
from app.domain.tasks.approvals import (
    ApprovalNotOpen,
    ApprovalPermissionDenied,
    approve,
    list_pending,
    reject,
    request_changes,
    request_review,
)
from app.events.bus import EventBus
from app.events.types import (
    TaskApprovalRequested,
    TaskApproved,
    TaskChangesRequested,
    TaskRejected,
)
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


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


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _ctx(
    workspace_id: str,
    *,
    role: ActorGrantRole = "manager",
    owner: bool = False,
    actor_id: str | None = None,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="ws",
        actor_id=actor_id if actor_id is not None else new_ulid(),
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=owner,
        audit_correlation_id=new_ulid(),
    )


def _bootstrap_workspace(session: Session) -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug="ws",
            name="Approval House",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _bootstrap_user(session: Session) -> str:
    from app.adapters.db.identity.models import User

    user_id = new_ulid()
    session.add(
        User(
            id=user_id,
            email=f"{user_id}@example.com",
            email_lower=f"{user_id}@example.com".lower(),
            display_name=user_id,
            locale=None,
            timezone=None,
            avatar_blob_hash=None,
            created_at=_PINNED,
            last_login_at=None,
        )
    )
    session.flush()
    return user_id


def _grant_manager(session: Session, *, workspace_id: str, user_id: str) -> None:
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="manager",
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()


def _bootstrap_property(session: Session, *, name: str = "Villa Sud") -> str:
    property_id = new_ulid()
    session.add(
        Property(
            id=property_id,
            name=name,
            address="1 Villa Sud Way",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return property_id


def _bootstrap_template(session: Session, *, workspace_id: str, name: str) -> str:
    template_id = new_ulid()
    session.add(
        TaskTemplate(
            id=template_id,
            workspace_id=workspace_id,
            title=f"{name} legacy",
            name=name,
            role_id=None,
            description_md="",
            default_duration_min=60,
            duration_minutes=60,
            required_evidence="none",
            photo_required=False,
            default_assignee_role=None,
            property_scope="any",
            listed_property_ids=[],
            area_scope="any",
            listed_area_ids=[],
            checklist_template_json=[],
            photo_evidence="disabled",
            linked_instruction_ids=[],
            priority="normal",
            required_approval=True,
            inventory_effects_json=[],
            llm_hints_md=None,
            deleted_at=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return template_id


def _bootstrap_completed_task(
    session: Session,
    *,
    workspace_id: str,
    property_id: str | None,
    completed_by_user_id: str | None = None,
    template_id: str | None = None,
    title: str | None = "Deep clean",
) -> str:
    task_id = new_ulid()
    session.add(
        Occurrence(
            id=task_id,
            workspace_id=workspace_id,
            schedule_id=None,
            template_id=template_id,
            property_id=property_id,
            assignee_user_id=completed_by_user_id,
            starts_at=_PINNED - timedelta(hours=1),
            ends_at=_PINNED,
            scheduled_for_local="2026-04-29T13:00",
            originally_scheduled_for="2026-04-29T13:00",
            state="done",
            completed_at=_PINNED,
            completed_by_user_id=completed_by_user_id,
            cancellation_reason=None,
            title=title,
            description_md="",
            priority="normal",
            photo_evidence="disabled",
            duration_minutes=60,
            area_id=None,
            unit_id=None,
            expected_role_id=None,
            linked_instruction_ids=[],
            inventory_consumption_json={},
            is_personal=False,
            created_by_user_id=None,
            created_at=_PINNED - timedelta(days=1),
        )
    )
    session.flush()
    return task_id


def _bootstrap_pending_approval(
    session: Session, *, workspace_id: str, task_id: str
) -> str:
    approval_id = new_ulid()
    session.add(
        TaskApproval(
            id=approval_id,
            workspace_id=workspace_id,
            task_id=task_id,
            requested_at=_PINNED,
            requested_by_user_id=None,
            state="pending",
            decided_at=None,
            decided_by_user_id=None,
            note_md=None,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    session.flush()
    return approval_id


def test_request_review_creates_pending_event_and_audit(
    session: Session, clock: FrozenClock, bus: EventBus
) -> None:
    workspace_id = _bootstrap_workspace(session)
    property_id = _bootstrap_property(session)
    worker_id = _bootstrap_user(session)
    task_id = _bootstrap_completed_task(
        session,
        workspace_id=workspace_id,
        property_id=property_id,
        completed_by_user_id=worker_id,
    )
    ctx = _ctx(workspace_id, role="worker", actor_id=worker_id)
    events: list[TaskApprovalRequested] = []
    bus.subscribe(TaskApprovalRequested)(events.append)

    view = request_review(session, ctx, task_id, clock=clock, event_bus=bus)

    row = session.get(TaskApproval, view.approval_id)
    assert row is not None
    assert row.task_id == task_id
    assert row.state == "pending"
    assert row.requested_by_user_id == worker_id
    assert events[0].name == "task.approval_requested"
    assert events[0].task_id == task_id
    assert events[0].approval_id == view.approval_id

    audit = session.scalars(
        select(AuditLog).where(AuditLog.entity_id == view.approval_id)
    ).one()
    assert audit.action == "task.approval_requested"
    assert audit.diff["after"]["state"] == "pending"


def test_partial_unique_constraint_blocks_two_open_reviews(
    session: Session, clock: FrozenClock, bus: EventBus
) -> None:
    workspace_id = _bootstrap_workspace(session)
    requester_id = _bootstrap_user(session)
    task_id = _bootstrap_completed_task(
        session, workspace_id=workspace_id, property_id=None
    )
    ctx = _ctx(workspace_id, actor_id=requester_id)

    request_review(session, ctx, task_id, clock=clock, event_bus=bus)

    with pytest.raises(IntegrityError):
        request_review(session, ctx, task_id, clock=clock, event_bus=bus)


@pytest.mark.parametrize(
    ("verb", "expected_state", "event_type", "expected_action"),
    [
        (approve, "approved", TaskApproved, "task.approved"),
        (reject, "rejected", TaskRejected, "task.rejected"),
        (
            request_changes,
            "changes_requested",
            TaskChangesRequested,
            "task.changes_requested",
        ),
    ],
)
def test_decision_verbs_emit_event_and_audit(
    session: Session,
    clock: FrozenClock,
    bus: EventBus,
    verb,
    expected_state: str,
    event_type,
    expected_action: str,
) -> None:
    workspace_id = _bootstrap_workspace(session)
    property_id = _bootstrap_property(session)
    manager_id = _bootstrap_user(session)
    _grant_manager(session, workspace_id=workspace_id, user_id=manager_id)
    task_id = _bootstrap_completed_task(
        session, workspace_id=workspace_id, property_id=property_id
    )
    approval_id = _bootstrap_pending_approval(
        session, workspace_id=workspace_id, task_id=task_id
    )
    ctx = _ctx(workspace_id, actor_id=manager_id)
    events = []
    bus.subscribe(event_type)(events.append)

    view = verb(
        session,
        ctx,
        approval_id,
        note_md="Looks right",
        clock=clock,
        event_bus=bus,
    )

    assert view.state == expected_state
    row = session.get(TaskApproval, approval_id)
    assert row is not None
    assert row.state == expected_state
    assert row.decided_by_user_id == manager_id
    assert row.note_md == "Looks right"
    assert events[0].name == expected_action
    assert events[0].state == expected_state
    assert events[0].decided_by_user_id == manager_id
    assert events[0].note_md == "Looks right"

    audit = session.scalars(
        select(AuditLog).where(AuditLog.entity_id == approval_id)
    ).one()
    assert audit.action == expected_action
    assert audit.diff["before"]["state"] == "pending"
    assert audit.diff["after"]["state"] == expected_state


def test_terminal_approval_cannot_be_decided_again(
    session: Session, clock: FrozenClock, bus: EventBus
) -> None:
    workspace_id = _bootstrap_workspace(session)
    manager_id = _bootstrap_user(session)
    _grant_manager(session, workspace_id=workspace_id, user_id=manager_id)
    task_id = _bootstrap_completed_task(
        session, workspace_id=workspace_id, property_id=None
    )
    approval_id = _bootstrap_pending_approval(
        session, workspace_id=workspace_id, task_id=task_id
    )
    ctx = _ctx(workspace_id, actor_id=manager_id)

    approve(session, ctx, approval_id, clock=clock, event_bus=bus)

    with pytest.raises(ApprovalNotOpen):
        reject(session, ctx, approval_id, clock=clock, event_bus=bus)


def test_decision_requires_tasks_review_decide(
    session: Session, clock: FrozenClock, bus: EventBus
) -> None:
    workspace_id = _bootstrap_workspace(session)
    worker_id = _bootstrap_user(session)
    task_id = _bootstrap_completed_task(
        session, workspace_id=workspace_id, property_id=None
    )
    approval_id = _bootstrap_pending_approval(
        session, workspace_id=workspace_id, task_id=task_id
    )
    ctx = _ctx(workspace_id, role="worker", actor_id=worker_id)

    with pytest.raises(ApprovalPermissionDenied):
        approve(session, ctx, approval_id, clock=clock, event_bus=bus)


def test_list_pending_returns_manager_surface_fields(session: Session) -> None:
    workspace_id = _bootstrap_workspace(session)
    property_id = _bootstrap_property(session, name="Maison Bleue")
    worker_id = _bootstrap_user(session)
    task_id = _bootstrap_completed_task(
        session,
        workspace_id=workspace_id,
        property_id=property_id,
        completed_by_user_id=worker_id,
        title="Turnover reset",
    )
    approval_id = _bootstrap_pending_approval(
        session, workspace_id=workspace_id, task_id=task_id
    )
    session.add(
        Evidence(
            id=new_ulid(),
            workspace_id=workspace_id,
            occurrence_id=task_id,
            kind="photo",
            blob_hash="sha256:photo",
            note_md=None,
            gps_lat=None,
            gps_lon=None,
            checklist_snapshot_json=None,
            created_at=_PINNED,
            created_by_user_id=worker_id,
            deleted_at=None,
        )
    )
    session.flush()

    rows = list_pending(session, _ctx(workspace_id), property_id=property_id)

    assert len(rows) == 1
    row = rows[0]
    assert row.approval_id == approval_id
    assert row.title == "Turnover reset"
    assert row.property_id == property_id
    assert row.property_name == "Maison Bleue"
    assert row.completed_by_user_id == worker_id
    assert row.completed_at is not None
    assert row.evidence_count == 1


def test_list_pending_uses_template_title_when_task_title_missing(
    session: Session,
) -> None:
    workspace_id = _bootstrap_workspace(session)
    property_id = _bootstrap_property(session)
    template_id = _bootstrap_template(
        session, workspace_id=workspace_id, name="VIP reset"
    )
    task_id = _bootstrap_completed_task(
        session,
        workspace_id=workspace_id,
        property_id=property_id,
        template_id=template_id,
        title=None,
    )
    _bootstrap_pending_approval(session, workspace_id=workspace_id, task_id=task_id)

    rows = list_pending(session, _ctx(workspace_id))

    assert len(rows) == 1
    assert rows[0].title == "VIP reset"
