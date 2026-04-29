"""Unit coverage for asset action and document services."""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import AssetAction, AssetType
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.domain.assets.actions import (
    AssetActionValidationError,
    next_due,
    record_action,
)
from app.domain.assets.assets import AssetCreate, create_asset
from app.events.bus import EventBus
from app.events.types import AssetActionPerformed
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from tests._fakes.storage import InMemoryStorage
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


def _ctx(workspace_id: str, actor_id: str, slug: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_asset_actions",
    )


def _seed_workspace(session: Session) -> tuple[WorkspaceContext, str]:
    owner = bootstrap_user(
        session,
        email="asset-actions@example.com",
        display_name="Asset Action Manager",
    )
    workspace = bootstrap_workspace(
        session,
        slug="asset-actions",
        name="Asset Actions",
        owner_user_id=owner.id,
    )
    property_id = "prop_asset_actions"
    session.add(
        Property(
            id=property_id,
            name="Asset Action Villa",
            kind="residence",
            address="1 Action Way",
            address_json={"line1": "1 Action Way", "country": "US"},
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
            label="Asset Action Villa",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_NOW,
        )
    )
    session.flush()
    return _ctx(workspace.id, owner.id, workspace.slug), property_id


def test_record_action_stores_meter_reading_audits_and_publishes(
    session: Session,
) -> None:
    ctx, property_id = _seed_workspace(session)
    storage = InMemoryStorage()
    evidence = "e" * 64
    storage.put(evidence, io.BytesIO(b"photo"), content_type="image/jpeg")
    asset = create_asset(
        session,
        ctx,
        property_id=property_id,
        label="Pool meter",
        token_factory=lambda: "ACT100000001",
        clock=FrozenClock(_NOW),
    )
    bus = EventBus()
    events: list[AssetActionPerformed] = []

    @bus.subscribe(AssetActionPerformed)
    def collect(event: AssetActionPerformed) -> None:
        events.append(event)

    action = record_action(
        session,
        ctx,
        asset.id,
        kind="read",
        meter_reading=Decimal("123.4567"),
        evidence_blob_hash=evidence,
        notes_md="  checked after service  ",
        storage=storage,
        event_bus=bus,
        clock=FrozenClock(_NOW),
    )

    row = session.get(AssetAction, action.id)
    assert row is not None
    assert row.meter_reading == Decimal("123.4567")
    assert row.notes_md == "checked after service"
    assert row.evidence_blob_hash == evidence
    assert events == []

    session.commit()
    assert [(event.asset_id, event.action_id, event.kind) for event in events] == [
        (asset.id, action.id, "read")
    ]
    assert (
        session.scalar(select(AuditLog.action).where(AuditLog.entity_id == action.id))
        == "asset_action.performed"
    )


def test_record_action_rejects_missing_evidence_blob(session: Session) -> None:
    ctx, property_id = _seed_workspace(session)
    asset = create_asset(
        session,
        ctx,
        property_id=property_id,
        label="Boiler",
        token_factory=lambda: "ACT100000002",
        clock=FrozenClock(_NOW),
    )

    with pytest.raises(AssetActionValidationError) as excinfo:
        record_action(
            session,
            ctx,
            asset.id,
            kind="inspect",
            evidence_blob_hash="f" * 64,
            storage=InMemoryStorage(),
            clock=FrozenClock(_NOW),
        )

    assert excinfo.value.field == "evidence_blob_hash"
    assert excinfo.value.error == "not_found"


def test_next_due_uses_default_action_catalog_and_history(session: Session) -> None:
    ctx, property_id = _seed_workspace(session)
    asset_type = AssetType(
        id="asset_type_next_due",
        workspace_id=ctx.workspace_id,
        key="pool-pump",
        name="Pool pump",
        category="pool",
        icon_name=None,
        description_md=None,
        default_lifespan_years=None,
        default_actions_json=[
            {
                "key": "inspect",
                "kind": "inspect",
                "label": "Inspect",
                "interval_days": 30,
            },
            {
                "key": "service",
                "kind": "service",
                "label": "Service",
                "interval_days": 90,
            },
        ],
        created_at=_NOW,
        updated_at=_NOW,
        deleted_at=None,
    )
    session.add(asset_type)
    session.flush()
    asset = create_asset(
        session,
        ctx,
        body=AssetCreate(
            property_id=property_id,
            asset_type_id=asset_type.id,
            name="North pool pump",
            installed_on=date(2026, 1, 1),
        ),
        token_factory=lambda: "ACT100000003",
        clock=FrozenClock(_NOW),
    )
    record_action(
        session,
        ctx,
        asset.id,
        kind="inspect",
        performed_at=datetime(2026, 1, 20, 9, 0, tzinfo=UTC),
        clock=FrozenClock(_NOW),
    )

    due = next_due(session, ctx, asset.id)

    assert due is not None
    assert due.key == "inspect"
    assert due.due_at == datetime(2026, 2, 19, 9, 0, tzinfo=UTC)
