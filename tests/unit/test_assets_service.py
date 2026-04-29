"""Unit coverage for the tracked asset domain service."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import AssetType
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.domain.assets.assets import (
    AssetNotFound,
    create_asset,
    get_asset_by_qr_token,
    move_asset,
    regenerate_qr,
    update_asset,
)
from app.events.bus import EventBus
from app.events.types import AssetChanged
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
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
        audit_correlation_id="corr_assets",
    )


def _seed_workspace(session: Session) -> tuple[WorkspaceContext, str, str, str]:
    owner = bootstrap_user(
        session,
        email="assets-service@example.com",
        display_name="Asset Manager",
    )
    workspace = bootstrap_workspace(
        session,
        slug="assets-service",
        name="Assets Service",
        owner_user_id=owner.id,
    )
    ctx = _ctx(workspace.id, owner.id, workspace.slug)
    property_id = "prop_assets_service"
    second_property_id = "prop_assets_service_second"
    area_id = "area_assets_service_kitchen"
    for prop_id, label in (
        (property_id, "Asset Villa"),
        (second_property_id, "Second Asset Villa"),
    ):
        session.add(
            Property(
                id=prop_id,
                name=label,
                kind="residence",
                address=f"{label} Road",
                address_json={"line1": f"{label} Road", "country": "US"},
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
                property_id=prop_id,
                workspace_id=workspace.id,
                label=label,
                membership_role="owner_workspace",
                share_guest_identity=False,
                status="active",
                created_at=_NOW,
            )
        )
    session.add(
        Area(
            id=area_id,
            property_id=property_id,
            unit_id=None,
            name="Kitchen",
            label="Kitchen",
            kind="indoor_room",
            icon=None,
            ordering=10,
            parent_area_id=None,
            notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    session.flush()
    return ctx, property_id, second_property_id, area_id


def _asset_type_id(session: Session, ctx: WorkspaceContext) -> str:
    type_id = session.scalar(
        select(AssetType.id)
        .where(AssetType.workspace_id == ctx.workspace_id)
        .order_by(AssetType.key)
        .limit(1)
    )
    assert type_id is not None
    return type_id


def _audit_actions(session: Session, asset_id: str) -> list[str]:
    return list(
        session.scalars(
            select(AuditLog.action)
            .where(AuditLog.entity_id == asset_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


def test_asset_crud_and_qr_regeneration_audit(session: Session) -> None:
    ctx, property_id, _second_property_id, area_id = _seed_workspace(session)
    clock = FrozenClock(_NOW)
    asset_type_id = _asset_type_id(session, ctx)
    tokens = iter(["ABCDEFGHJKM1", "ABCDEFGHJKM2", "ABCDEFGHJKM3"])

    first = create_asset(
        session,
        ctx,
        asset_type_id=asset_type_id,
        property_id=property_id,
        area_id=area_id,
        label="Kitchen fridge",
        token_factory=lambda: "ABCDEFGHJKM1",
        clock=clock,
    )
    second = create_asset(
        session,
        ctx,
        asset_type_id=asset_type_id,
        property_id=property_id,
        name="Wine fridge",
        token_factory=lambda: next(tokens),
        clock=clock,
    )

    assert first.qr_token == "ABCDEFGHJKM1"
    assert second.qr_token == "ABCDEFGHJKM2"

    updated = update_asset(
        session,
        ctx,
        second.id,
        name="Cellar fridge",
        status="in_repair",
        clock=clock,
    )
    assert updated.name == "Cellar fridge"
    assert updated.status == "in_repair"

    old_token = second.qr_token
    regenerated = regenerate_qr(
        session,
        ctx,
        second.id,
        token_factory=lambda: "ABCDEFGHJKM3",
        clock=clock,
    )
    assert regenerated.qr_token == "ABCDEFGHJKM3"
    with pytest.raises(AssetNotFound):
        get_asset_by_qr_token(session, ctx, qr_token=old_token)
    assert get_asset_by_qr_token(session, ctx, qr_token=regenerated.qr_token).id == (
        second.id
    )

    assert _audit_actions(session, second.id) == [
        "asset.create",
        "asset.update",
        "asset.qr_regenerate",
    ]


def test_move_audit_records_before_and_after_placement(session: Session) -> None:
    ctx, property_id, second_property_id, area_id = _seed_workspace(session)
    clock = FrozenClock(_NOW)
    asset = create_asset(
        session,
        ctx,
        property_id=property_id,
        area_id=area_id,
        label="Pool pump",
        token_factory=lambda: "P00PMP000001",
        clock=clock,
    )

    moved = move_asset(
        session,
        ctx,
        asset.id,
        property_id=second_property_id,
        area_id=None,
        clock=clock,
    )
    assert moved.property_id == second_property_id
    assert moved.area_id is None

    audit = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == asset.id,
            AuditLog.action == "asset.move",
        )
    ).one()
    assert audit.diff == {
        "before": {"property_id": property_id, "area_id": area_id},
        "after": {"property_id": second_property_id, "area_id": None},
    }


def test_update_kwargs_preserve_explicit_null_clears(session: Session) -> None:
    ctx, property_id, _second_property_id, area_id = _seed_workspace(session)
    asset = create_asset(
        session,
        ctx,
        property_id=property_id,
        area_id=area_id,
        label="Utility boiler",
        token_factory=lambda: "B01ER0000001",
        clock=FrozenClock(_NOW),
    )

    cleared = update_asset(
        session,
        ctx,
        asset.id,
        area_id=None,
        clock=FrozenClock(_NOW),
    )

    assert cleared.area_id is None


def test_asset_changed_event_publishes_after_commit(session: Session) -> None:
    ctx, property_id, _second_property_id, _area_id = _seed_workspace(session)
    bus = EventBus()
    events: list[AssetChanged] = []

    @bus.subscribe(AssetChanged)
    def collect(event: AssetChanged) -> None:
        events.append(event)

    asset = create_asset(
        session,
        ctx,
        property_id=property_id,
        label="Generator",
        token_factory=lambda: "GEN000000001",
        event_bus=bus,
        clock=FrozenClock(_NOW),
    )

    assert events == []
    session.commit()
    assert [(event.asset_id, event.action) for event in events] == [
        (asset.id, "create")
    ]
