"""Unit tests for :mod:`app.domain.places.closure_service`."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import PropertyClosure, Unit
from app.adapters.db.session import make_engine
from app.adapters.db.stays.models import IcalFeed
from app.adapters.db.workspace.models import Workspace
from app.domain.places.closure_service import (
    ClosureNotFound,
    PropertyClosureCreate,
    PropertyClosureUpdate,
    create_closure,
    delete_closure,
    list_closures,
    update_closure,
)
from app.domain.places.property_service import PropertyCreate, create_property
from app.events.bus import EventBus
from app.events.types import PropertyClosureCreated, PropertyClosureUpdated
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
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


@pytest.fixture(name="engine_closure")
def fixture_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_closure")
def fixture_session(engine_closure: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_closure, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _ctx(*, workspace_id: str, slug: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id="system",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLC",
        principal_kind="system",
    )


def _make_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


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


def _first_unit_id(session: Session, *, property_id: str) -> str:
    unit_id = session.scalars(
        select(Unit.id).where(Unit.property_id == property_id)
    ).one()
    return unit_id


def _create_feed(session: Session, ctx: WorkspaceContext, *, property_id: str) -> str:
    feed_id = new_ulid()
    session.add(
        IcalFeed(
            id=feed_id,
            workspace_id=ctx.workspace_id,
            property_id=property_id,
            unit_id=None,
            url="https://calendar.example.test/feed.ics",
            provider="airbnb",
            poll_cadence="*/15 * * * *",
            last_polled_at=None,
            last_etag=None,
            last_error=None,
            enabled=True,
            created_at=_PINNED,
        )
    )
    session.flush()
    return feed_id


def _body(
    *,
    starts_at: datetime = datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    ends_at: datetime = datetime(2026, 5, 4, 0, 0, tzinfo=UTC),
    reason: str = "renovation",
    unit_id: str | None = None,
    source_ical_feed_id: str | None = None,
) -> PropertyClosureCreate:
    return PropertyClosureCreate.model_validate(
        {
            "starts_at": starts_at,
            "ends_at": ends_at,
            "reason": reason,
            "unit_id": unit_id,
            "source_ical_feed_id": source_ical_feed_id,
        }
    )


def _update_body(
    *,
    starts_at: datetime = datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
    ends_at: datetime = datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
    reason: str = "seasonal",
    unit_id: str | None = None,
    source_ical_feed_id: str | None = None,
) -> PropertyClosureUpdate:
    return PropertyClosureUpdate.model_validate(
        {
            "starts_at": starts_at,
            "ends_at": ends_at,
            "reason": reason,
            "unit_id": unit_id,
            "source_ical_feed_id": source_ical_feed_id,
        }
    )


def test_create_and_update_publish_events_once_with_payload(
    session_closure: Session, frozen_clock: FrozenClock
) -> None:
    ws = _make_workspace(session_closure, slug="closure-events")
    ctx = _ctx(workspace_id=ws, slug="closure-events")
    property_id = _create_property(session_closure, ctx)
    bus = EventBus()
    created_events: list[PropertyClosureCreated] = []
    updated_events: list[PropertyClosureUpdated] = []
    bus.subscribe(PropertyClosureCreated)(created_events.append)
    bus.subscribe(PropertyClosureUpdated)(updated_events.append)

    created = create_closure(
        session_closure,
        ctx,
        property_id=property_id,
        body=_body(),
        clock=frozen_clock,
        event_bus=bus,
    )
    updated = update_closure(
        session_closure,
        ctx,
        closure_id=created.id,
        body=_update_body(),
        clock=frozen_clock,
        event_bus=bus,
    )

    assert updated.reason == "seasonal"
    assert len(created_events) == 1
    assert created_events[0].closure_id == created.id
    assert created_events[0].property_id == property_id
    assert created_events[0].starts_at == datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    assert created_events[0].reason == "renovation"
    assert len(updated_events) == 1
    assert updated_events[0].closure_id == created.id
    assert updated_events[0].ends_at == datetime(2026, 5, 5, 0, 0, tzinfo=UTC)

    audits = session_closure.scalars(
        select(AuditLog).where(AuditLog.entity_id == created.id)
    ).all()
    assert [audit.action for audit in audits] == ["create", "update"]


def test_ical_lineage_is_persisted_and_listed(
    session_closure: Session, frozen_clock: FrozenClock
) -> None:
    ws = _make_workspace(session_closure, slug="closure-lineage")
    ctx = _ctx(workspace_id=ws, slug="closure-lineage")
    property_id = _create_property(session_closure, ctx)
    feed_id = _create_feed(session_closure, ctx, property_id=property_id)

    created = create_closure(
        session_closure,
        ctx,
        property_id=property_id,
        body=_body(reason="ical_unavailable", source_ical_feed_id=feed_id),
        clock=frozen_clock,
        event_bus=EventBus(),
    )

    rows = list_closures(session_closure, ctx, property_id=property_id)
    assert [row.id for row in rows] == [created.id]
    assert rows[0].source_ical_feed_id == feed_id
    db_row = session_closure.get(PropertyClosure, created.id)
    assert db_row is not None
    assert db_row.source_ical_feed_id == feed_id


def test_unit_scope_is_persisted_listed_and_updated(
    session_closure: Session, frozen_clock: FrozenClock
) -> None:
    ws = _make_workspace(session_closure, slug="closure-unit")
    ctx = _ctx(workspace_id=ws, slug="closure-unit")
    property_id = _create_property(session_closure, ctx)
    unit_id = _first_unit_id(session_closure, property_id=property_id)

    created = create_closure(
        session_closure,
        ctx,
        property_id=property_id,
        body=_body(unit_id=unit_id),
        clock=frozen_clock,
        event_bus=EventBus(),
    )

    rows = list_closures(session_closure, ctx, property_id=property_id, unit_id=unit_id)
    assert [row.id for row in rows] == [created.id]
    assert created.unit_id == unit_id
    db_row = session_closure.get(PropertyClosure, created.id)
    assert db_row is not None
    assert db_row.unit_id == unit_id

    updated = update_closure(
        session_closure,
        ctx,
        closure_id=created.id,
        body=_update_body(unit_id=None),
        clock=frozen_clock,
        event_bus=EventBus(),
    )

    assert updated.unit_id is None
    assert session_closure.get(PropertyClosure, created.id).unit_id is None


def test_cross_property_unit_collapses_to_not_found(
    session_closure: Session, frozen_clock: FrozenClock
) -> None:
    ws = _make_workspace(session_closure, slug="closure-cross-unit")
    ctx = _ctx(workspace_id=ws, slug="closure-cross-unit")
    property_id = _create_property(session_closure, ctx)
    other_property_id = _create_property(session_closure, ctx)
    other_unit_id = _first_unit_id(session_closure, property_id=other_property_id)

    with pytest.raises(ClosureNotFound):
        create_closure(
            session_closure,
            ctx,
            property_id=property_id,
            body=_body(unit_id=other_unit_id),
            clock=frozen_clock,
            event_bus=EventBus(),
        )

    created = create_closure(
        session_closure,
        ctx,
        property_id=property_id,
        body=_body(),
        clock=frozen_clock,
        event_bus=EventBus(),
    )
    with pytest.raises(ClosureNotFound):
        update_closure(
            session_closure,
            ctx,
            closure_id=created.id,
            body=_update_body(unit_id=other_unit_id),
            clock=frozen_clock,
            event_bus=EventBus(),
        )


def test_workspace_denial_collapses_to_not_found(
    session_closure: Session, frozen_clock: FrozenClock
) -> None:
    ws_a = _make_workspace(session_closure, slug="closure-deny-a")
    ctx_a = _ctx(workspace_id=ws_a, slug="closure-deny-a")
    property_id = _create_property(session_closure, ctx_a)
    created = create_closure(
        session_closure,
        ctx_a,
        property_id=property_id,
        body=_body(),
        clock=frozen_clock,
        event_bus=EventBus(),
    )

    ws_b = _make_workspace(session_closure, slug="closure-deny-b")
    ctx_b = _ctx(workspace_id=ws_b, slug="closure-deny-b")

    with pytest.raises(ClosureNotFound):
        list_closures(session_closure, ctx_b, property_id=property_id)
    with pytest.raises(ClosureNotFound):
        update_closure(
            session_closure,
            ctx_b,
            closure_id=created.id,
            body=_update_body(),
            clock=frozen_clock,
            event_bus=EventBus(),
        )


def test_delete_removes_row_and_audits(
    session_closure: Session, frozen_clock: FrozenClock
) -> None:
    ws = _make_workspace(session_closure, slug="closure-delete")
    ctx = _ctx(workspace_id=ws, slug="closure-delete")
    property_id = _create_property(session_closure, ctx)
    created = create_closure(
        session_closure,
        ctx,
        property_id=property_id,
        body=_body(),
        clock=frozen_clock,
        event_bus=EventBus(),
    )

    deleted = delete_closure(
        session_closure,
        ctx,
        closure_id=created.id,
        clock=frozen_clock,
    )

    assert deleted.id == created.id
    assert deleted.deleted_at == _PINNED
    db_row = session_closure.get(PropertyClosure, created.id)
    assert db_row is not None
    assert db_row.deleted_at is not None
    assert db_row.deleted_at.replace(tzinfo=UTC) == _PINNED
    assert list_closures(session_closure, ctx, property_id=property_id) == []
    audits = session_closure.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == created.id,
            AuditLog.entity_kind == "property_closure",
        )
    ).all()
    assert [audit.action for audit in audits] == ["create", "delete"]
    assert audits[-1].diff["after"]["deleted_at"] == _PINNED.isoformat()
