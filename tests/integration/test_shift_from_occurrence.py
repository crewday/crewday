"""Integration tests for occurrence-driven shift open/close."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
from app.adapters.db.time.models import Shift
from app.domain.tasks.completion import complete, start
from app.domain.time.occurrence_shifts import register_occurrence_shift_subscription
from app.domain.time.shifts import open_shift
from app.events import EventBus, TaskOccurrenceStarted
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 30, 9, 0, 0, tzinfo=UTC)


def _seed(
    session: Session,
    *,
    template_auto: bool = False,
    property_auto: bool = False,
) -> tuple[WorkspaceContext, str, str, str]:
    clock = FrozenClock(_PINNED)
    owner = bootstrap_user(
        session,
        email=f"owner-{new_ulid()}@example.com",
        display_name="Owner",
        clock=clock,
    )
    worker = bootstrap_user(
        session,
        email=f"worker-{new_ulid()}@example.com",
        display_name="Worker",
        clock=clock,
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"shift-occ-{new_ulid()[-6:].lower()}",
        name="Shift Occurrence",
        owner_user_id=owner.id,
        clock=clock,
    )
    ctx = build_workspace_context(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=owner.id,
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    property_id = new_ulid()
    template_id = new_ulid()
    occurrence_id = new_ulid()
    with tenant_agnostic():
        session.add(
            Property(
                id=property_id,
                name="Shift Villa",
                kind="vacation",
                address="1 Shift Way",
                address_json={"country": "FR"},
                country="FR",
                timezone="Europe/Paris",
                lat=None,
                lon=None,
                default_currency="EUR",
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
        session.add(
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=workspace.id,
                label="Shift Villa",
                membership_role="owner_workspace",
                share_guest_identity=False,
                auto_shift_from_occurrence=property_auto,
                status="active",
                created_at=_PINNED,
            )
        )
        session.add(
            TaskTemplate(
                id=template_id,
                workspace_id=workspace.id,
                title="Turnover",
                name="Turnover",
                description_md="",
                role_id=None,
                default_duration_min=30,
                duration_minutes=30,
                required_evidence="none",
                photo_required=False,
                default_assignee_role=None,
                property_scope="one",
                listed_property_ids=[property_id],
                area_scope="any",
                listed_area_ids=[],
                checklist_template_json=[],
                photo_evidence="disabled",
                linked_instruction_ids=[],
                priority="normal",
                required_approval=False,
                auto_shift_from_occurrence=template_auto,
                inventory_effects_json=[],
                llm_hints_md=None,
                deleted_at=None,
                created_at=_PINNED,
            )
        )
        session.flush()
        session.add(
            Occurrence(
                id=occurrence_id,
                workspace_id=workspace.id,
                schedule_id=None,
                template_id=template_id,
                property_id=property_id,
                assignee_user_id=worker.id,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(minutes=30),
                scheduled_for_local="2026-04-30T11:00",
                originally_scheduled_for="2026-04-30T11:00",
                state="pending",
                cancellation_reason=None,
                title="Turnover",
                description_md="",
                priority="normal",
                photo_evidence="disabled",
                duration_minutes=30,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=owner.id,
                created_at=_PINNED,
            )
        )
    session.flush()
    return ctx, occurrence_id, property_id, worker.id


def _register(bus: EventBus, session: Session, ctx: WorkspaceContext) -> None:
    register_occurrence_shift_subscription(
        bus,
        session_provider=lambda _event: (session, ctx),
    )


def _run_with_ctx(ctx: WorkspaceContext, fn: Callable[[], None]) -> None:
    token = set_current(ctx)
    try:
        fn()
    finally:
        reset_current(token)


def _shifts(session: Session, ctx: WorkspaceContext) -> list[Shift]:
    return list(
        session.scalars(
            select(Shift)
            .where(Shift.workspace_id == ctx.workspace_id)
            .order_by(Shift.starts_at, Shift.id)
        )
    )


def test_template_flag_opens_shift_and_duplicate_event_is_idempotent(
    db_session: Session,
) -> None:
    ctx, occurrence_id, property_id, _worker_id = _seed(db_session, template_auto=True)
    event_bus = EventBus()
    _register(event_bus, db_session, ctx)

    def act() -> None:
        start(
            db_session,
            ctx,
            occurrence_id,
            clock=FrozenClock(_PINNED),
            event_bus=event_bus,
        )
        event_bus.publish(
            TaskOccurrenceStarted(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=_PINNED,
                task_id=occurrence_id,
                started_by=ctx.actor_id,
            )
        )

    _run_with_ctx(ctx, act)

    rows = _shifts(db_session, ctx)
    assert len(rows) == 1
    assert rows[0].source == "occurrence"
    assert rows[0].source_occurrence_id == occurrence_id
    assert rows[0].property_id == property_id
    assert rows[0].ends_at is None


def test_completion_closes_auto_opened_shift(db_session: Session) -> None:
    ctx, occurrence_id, _property_id, _worker_id = _seed(db_session, template_auto=True)
    event_bus = EventBus()
    _register(event_bus, db_session, ctx)

    def act() -> None:
        start(
            db_session,
            ctx,
            occurrence_id,
            clock=FrozenClock(_PINNED),
            event_bus=event_bus,
        )
        complete(
            db_session,
            ctx,
            occurrence_id,
            clock=FrozenClock(_PINNED + timedelta(minutes=45)),
            event_bus=event_bus,
        )

    _run_with_ctx(ctx, act)

    rows = _shifts(db_session, ctx)
    assert len(rows) == 1
    assert rows[0].source_occurrence_id == occurrence_id
    assert rows[0].ends_at is not None
    assert rows[0].ends_at.replace(tzinfo=UTC) == _PINNED + timedelta(minutes=45)


def test_property_flag_opens_shift_when_template_flag_is_disabled(
    db_session: Session,
) -> None:
    ctx, occurrence_id, _property_id, _worker_id = _seed(db_session, property_auto=True)
    event_bus = EventBus()
    _register(event_bus, db_session, ctx)

    def act() -> None:
        start(
            db_session,
            ctx,
            occurrence_id,
            clock=FrozenClock(_PINNED),
            event_bus=event_bus,
        )

    _run_with_ctx(ctx, act)

    rows = _shifts(db_session, ctx)
    assert len(rows) == 1
    assert rows[0].source == "occurrence"


def test_disabled_config_does_not_open_shift(db_session: Session) -> None:
    ctx, occurrence_id, _property_id, _worker_id = _seed(db_session)
    event_bus = EventBus()
    _register(event_bus, db_session, ctx)

    def act() -> None:
        start(
            db_session,
            ctx,
            occurrence_id,
            clock=FrozenClock(_PINNED),
            event_bus=event_bus,
        )

    _run_with_ctx(ctx, act)

    assert _shifts(db_session, ctx) == []


def test_manual_shift_blocks_auto_open_and_writes_audit(db_session: Session) -> None:
    ctx, occurrence_id, property_id, worker_id = _seed(db_session, template_auto=True)
    event_bus = EventBus()
    _register(event_bus, db_session, ctx)

    def act() -> None:
        open_shift(
            db_session,
            ctx,
            user_id=worker_id,
            property_id=property_id,
            clock=FrozenClock(_PINNED - timedelta(minutes=10)),
        )
        start(
            db_session,
            ctx,
            occurrence_id,
            clock=FrozenClock(_PINNED),
            event_bus=event_bus,
        )

    _run_with_ctx(ctx, act)

    rows = _shifts(db_session, ctx)
    assert len(rows) == 1
    assert rows[0].source == "manual"
    audits = db_session.scalars(
        select(AuditLog).where(
            AuditLog.workspace_id == ctx.workspace_id,
            AuditLog.action == "shift.auto_open_skipped",
        )
    ).all()
    assert len(audits) == 1
    assert audits[0].diff["occurrence_id"] == occurrence_id
    assert audits[0].diff["existing_shift_id"] == rows[0].id
