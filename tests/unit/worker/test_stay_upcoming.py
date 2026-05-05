"""Unit tests for upcoming-stay notification sweep."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.messaging.models import Notification
from app.adapters.db.places.models import Property
from app.adapters.db.stays.models import Reservation
from app.adapters.db.tasks.models import Occurrence
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from app.worker.tasks.stay_upcoming import emit_upcoming_stay_notifications
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_NOW = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s
    engine.dispose()


def _ctx(workspace_id: str, slug: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
        principal_kind="system",
    )


def _seed_property(session: Session) -> str:
    property_id = new_ulid()
    session.add(
        Property(
            id=property_id,
            name="Stay Villa",
            kind="residence",
            address="1 Stay Road",
            address_json={"line1": "1 Stay Road", "country": "US"},
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
    return property_id


def _seed_upcoming_stay(
    session: Session, *, workspace_id: str, property_id: str
) -> str:
    stay_id = new_ulid()
    session.add(
        Reservation(
            id=stay_id,
            workspace_id=workspace_id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=f"manual-{stay_id}",
            check_in=_NOW + timedelta(hours=23),
            check_out=_NOW + timedelta(days=3),
            guest_name="Ada Guest",
            guest_count=2,
            status="scheduled",
            source="manual",
            raw_summary=None,
            raw_description=None,
            guest_link_id=None,
            created_at=_NOW,
        )
    )
    return stay_id


def test_emit_upcoming_stay_notifications_dedupes_per_stay_window(
    session: Session,
) -> None:
    owner = bootstrap_user(session, email="owner@example.com", display_name="Owner")
    worker = bootstrap_user(session, email="worker@example.com", display_name="Worker")
    cancelled_worker = bootstrap_user(
        session,
        email="cancelled-worker@example.com",
        display_name="Cancelled Worker",
    )
    workspace = bootstrap_workspace(
        session,
        slug="stay-upcoming",
        name="Stay Upcoming",
        owner_user_id=owner.id,
    )
    property_id = _seed_property(session)
    stay_id = _seed_upcoming_stay(
        session,
        workspace_id=workspace.id,
        property_id=property_id,
    )
    session.add(
        Occurrence(
            id=new_ulid(),
            workspace_id=workspace.id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=worker.id,
            starts_at=_NOW + timedelta(hours=22),
            ends_at=_NOW + timedelta(hours=23),
            state="pending",
            reservation_id=stay_id,
            lifecycle_rule_id="before_checkin_default",
            occurrence_key="before_checkin",
            created_at=_NOW,
        )
    )
    session.add(
        Occurrence(
            id=new_ulid(),
            workspace_id=workspace.id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=cancelled_worker.id,
            starts_at=_NOW + timedelta(hours=22),
            ends_at=_NOW + timedelta(hours=23),
            state="cancelled",
            reservation_id=stay_id,
            lifecycle_rule_id="before_checkin_cancelled",
            occurrence_key="before_checkin_cancelled",
            created_at=_NOW,
            cancellation_reason="stay rescheduled",
        )
    )
    session.flush()
    ctx = _ctx(workspace.id, workspace.slug, owner.id)

    first = emit_upcoming_stay_notifications(ctx, session=session, now=_NOW)
    second = emit_upcoming_stay_notifications(ctx, session=session, now=_NOW)

    assert first.stays_walked == 1
    assert first.notifications_sent == 2
    assert second.stays_walked == 1
    assert second.notifications_sent == 0
    with tenant_agnostic():
        notifications = session.scalars(
            select(Notification).where(Notification.kind == "stay_upcoming")
        ).all()
    assert {
        (row.recipient_user_id, row.payload_json["stay_id"]) for row in notifications
    } == {
        (owner.id, stay_id),
        (worker.id, stay_id),
    }
