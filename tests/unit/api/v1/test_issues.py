"""Unit coverage for issue reporting domain service."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.issues.models import IssueReport
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.domain.issues import IssueCreate, IssueValidationError, create_issue
from app.events.bus import EventBus
from app.events.types import IssueReported
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s
    engine.dispose()


def _ctx(
    workspace_id: str,
    actor_id: str,
    slug: str,
    *,
    role: ActorGrantRole = "manager",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=role == "manager",
        audit_correlation_id="corr_issues",
    )


def _seed_workspace(session: Session) -> tuple[WorkspaceContext, str, str]:
    owner = bootstrap_user(
        session,
        email="issues-manager@example.com",
        display_name="Issue Manager",
    )
    worker = bootstrap_user(
        session,
        email="issues-worker@example.com",
        display_name="Issue Worker",
    )
    workspace = bootstrap_workspace(
        session,
        slug="issues",
        name="Issues",
        owner_user_id=owner.id,
    )
    property_id = "prop_issues"
    session.add(
        Property(
            id=property_id,
            name="Issue Villa",
            kind="residence",
            address="1 Issue Road",
            address_json={"line1": "1 Issue Road", "country": "US"},
            country="US",
            timezone="UTC",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace.id,
            label="Issue Villa",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_NOW,
        )
    )
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace.id,
            user_id=worker.id,
            grant_role="worker",
            scope_kind="workspace",
            scope_property_id=property_id,
            created_at=_NOW,
            created_by_user_id=owner.id,
        )
    )
    session.flush()
    return (
        _ctx(workspace.id, worker.id, workspace.slug, role="worker"),
        property_id,
        owner.id,
    )


def test_create_issue_persists_audits_and_emits_manager_event(
    session: Session,
) -> None:
    ctx, property_id, _owner_id = _seed_workspace(session)
    bus = EventBus()
    events: list[IssueReported] = []

    @bus.subscribe(IssueReported)
    def collect(event: IssueReported) -> None:
        events.append(event)

    issue = create_issue(
        session,
        ctx,
        body=IssueCreate(
            title="Leak under sink",
            severity="high",
            category="damage",
            property_id=property_id,
            area="Kitchen",
            body="Water under the cabinet",
        ),
        event_bus=bus,
        clock=FrozenClock(_NOW),
    )

    row = session.get(IssueReport, issue.id)
    assert row is not None
    assert row.title == "Leak under sink"
    assert row.state == "open"
    assert row.reported_by_user_id == ctx.actor_id
    assert (
        session.scalar(select(AuditLog.action).where(AuditLog.entity_id == issue.id))
        == "issue.create"
    )
    event_rows = [
        (event.issue_id, event.property_id, event.severity) for event in events
    ]
    assert event_rows == [(issue.id, property_id, "high")]


def test_create_issue_rejects_unassigned_worker_property(session: Session) -> None:
    ctx, _property_id, _owner_id = _seed_workspace(session)

    with pytest.raises(IssueValidationError) as excinfo:
        create_issue(
            session,
            ctx,
            body=IssueCreate(
                title="Broken chair",
                property_id="prop_not_visible",
                category="broken",
            ),
            clock=FrozenClock(_NOW),
        )

    assert excinfo.value.field == "property_id"
    assert excinfo.value.error == "not_visible"
