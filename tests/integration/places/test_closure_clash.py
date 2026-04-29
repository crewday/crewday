"""Integration tests for property-closure clash detection."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.adapters.db.stays.models import Reservation
from app.adapters.db.tasks.models import Schedule, TaskTemplate
from app.domain.places.closure_service import detect_clashes
from app.domain.places.property_service import PropertyCreate, create_property
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_SLUG_COUNTER = 0


def _next_slug() -> str:
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"clash-{_SLUG_COUNTER:05d}"


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    registry.register("property_workspace")
    registry.register("audit_log")
    registry.register("reservation")
    registry.register("task_template")
    registry.register("schedule")


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CLSH",
    )


@pytest.fixture
def env(db_session: Session) -> Iterator[tuple[Session, WorkspaceContext]]:
    install_tenant_filter(db_session)
    slug = _next_slug()
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(
        db_session,
        email=f"{slug}@example.com",
        display_name=f"User {slug}",
        clock=clock,
    )
    workspace = bootstrap_workspace(
        db_session,
        slug=slug,
        name=f"WS {slug}",
        owner_user_id=user.id,
        clock=clock,
    )
    ctx = _ctx_for(workspace.id, workspace.slug, user.id)
    token = set_current(ctx)
    try:
        yield db_session, ctx
    finally:
        reset_current(token)


def _create_property(session: Session, ctx: WorkspaceContext) -> str:
    view = create_property(
        session,
        ctx,
        body=PropertyCreate.model_validate(
            {
                "name": "Villa Sud",
                "kind": "str",
                "address": "12 Chemin des Oliviers, Antibes",
                "address_json": {
                    "line1": "12 Chemin des Oliviers",
                    "city": "Antibes",
                    "country": "FR",
                },
                "country": "FR",
                "timezone": "Europe/Paris",
            }
        ),
        clock=FrozenClock(_PINNED),
    )
    return view.id


def _seed_reservation(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    check_in: datetime,
    check_out: datetime,
    status: str = "scheduled",
) -> str:
    row_id = new_ulid()
    session.add(
        Reservation(
            id=row_id,
            workspace_id=ctx.workspace_id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=row_id,
            check_in=check_in,
            check_out=check_out,
            guest_name=None,
            guest_count=None,
            status=status,
            source="manual",
            raw_summary=None,
            raw_description=None,
            guest_link_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return row_id


def _seed_schedule(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None,
    active_from: str,
    active_until: str | None,
    enabled: bool = True,
    deleted_at: datetime | None = None,
    paused_at: datetime | None = None,
) -> str:
    template_id = new_ulid()
    session.add(
        TaskTemplate(
            id=template_id,
            workspace_id=ctx.workspace_id,
            title="Pool service",
            name="Pool service",
            role_id=None,
            description_md="",
            default_duration_min=30,
            duration_minutes=30,
            required_evidence="none",
            photo_required=False,
            default_assignee_role=None,
            property_scope="one" if property_id is not None else "any",
            listed_property_ids=[property_id] if property_id is not None else [],
            area_scope="any",
            listed_area_ids=[],
            checklist_template_json=[],
            photo_evidence="disabled",
            linked_instruction_ids=[],
            priority="normal",
            required_approval=False,
            inventory_effects_json=[],
            llm_hints_md=None,
            deleted_at=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    schedule_id = new_ulid()
    session.add(
        Schedule(
            id=schedule_id,
            workspace_id=ctx.workspace_id,
            template_id=template_id,
            property_id=property_id,
            name="Pool service",
            area_id=None,
            rrule_text="FREQ=DAILY",
            dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            dtstart_local="2026-05-01T09:00",
            until=None,
            duration_minutes=30,
            rdate_local="",
            exdate_local="",
            active_from=active_from,
            active_until=active_until,
            paused_at=paused_at,
            deleted_at=deleted_at,
            assignee_user_id=None,
            backup_assignee_user_ids=[],
            assignee_role=None,
            enabled=enabled,
            next_generation_at=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return schedule_id


def test_detect_clashes_returns_overlapping_stays_and_live_schedules(
    env: tuple[Session, WorkspaceContext],
) -> None:
    session, ctx = env
    property_id = _create_property(session, ctx)
    overlapping_stay = _seed_reservation(
        session,
        ctx,
        property_id=property_id,
        check_in=datetime(2026, 5, 2, 15, 0, tzinfo=UTC),
        check_out=datetime(2026, 5, 6, 10, 0, tzinfo=UTC),
    )
    _seed_reservation(
        session,
        ctx,
        property_id=property_id,
        check_in=datetime(2026, 5, 8, 15, 0, tzinfo=UTC),
        check_out=datetime(2026, 5, 9, 10, 0, tzinfo=UTC),
    )
    overlapping_schedule = _seed_schedule(
        session,
        ctx,
        property_id=property_id,
        active_from="2026-05-01",
        active_until="2026-05-31",
    )
    workspace_wide_schedule = _seed_schedule(
        session,
        ctx,
        property_id=None,
        active_from="2026-05-01",
        active_until="2026-05-31",
    )
    _seed_schedule(
        session,
        ctx,
        property_id=property_id,
        active_from="2026-06-01",
        active_until=None,
    )
    _seed_schedule(
        session,
        ctx,
        property_id=property_id,
        active_from="2026-05-01",
        active_until="2026-05-31",
        enabled=False,
    )
    _seed_schedule(
        session,
        ctx,
        property_id=property_id,
        active_from="2026-05-01",
        active_until="2026-05-31",
        paused_at=_PINNED,
    )

    clashes = detect_clashes(
        session,
        ctx,
        property_id=property_id,
        starts_at=datetime(2026, 5, 3, 0, 0, tzinfo=UTC),
        ends_at=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
    )

    assert [stay.id for stay in clashes.stays] == [overlapping_stay]
    assert [schedule.id for schedule in clashes.schedules] == [
        overlapping_schedule,
        workspace_wide_schedule,
    ]


def test_detect_clashes_compares_schedule_dates_in_property_timezone(
    env: tuple[Session, WorkspaceContext],
) -> None:
    session, ctx = env
    property_id = _create_property(session, ctx)
    schedule_id = _seed_schedule(
        session,
        ctx,
        property_id=property_id,
        active_from="2026-06-01",
        active_until="2026-06-01",
    )

    clashes = detect_clashes(
        session,
        ctx,
        property_id=property_id,
        starts_at=datetime(2026, 5, 31, 22, 30, tzinfo=UTC),
        ends_at=datetime(2026, 5, 31, 23, 30, tzinfo=UTC),
    )

    assert [schedule.id for schedule in clashes.schedules] == [schedule_id]
