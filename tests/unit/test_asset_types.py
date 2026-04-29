"""Unit coverage for the asset-type domain service."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.bootstrap import (
    BASE_ASSET_TYPE_CATALOG,
    seed_asset_type_catalog,
)
from app.adapters.db.assets.models import Asset, AssetType
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.domain.assets.types import (
    AssetTypeKeyConflict,
    DefaultAssetAction,
    create_type,
    delete_type,
    list_types,
    update_type,
    validate_default_actions,
)
from app.tenancy import WorkspaceContext


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s
    engine.dispose()


def _ctx(workspace_id: str = "ws_asset_types") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_id,
        actor_id="user_asset_types",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_asset_types",
    )


def _default_action() -> dict[str, object]:
    return {
        "kind": "service",
        "label": "Annual service",
        "interval_days": 365,
        "warn_before_days": 30,
    }


def test_default_actions_validate_required_shape() -> None:
    assert validate_default_actions([_default_action()]) == [_default_action()]

    with pytest.raises(ValidationError):
        validate_default_actions(
            [
                {
                    "kind": "service",
                    "label": "Annual service",
                    "interval_days": 365,
                    "warn_before_days": 30,
                    "extra": "nope",
                }
            ]
        )

    with pytest.raises(ValidationError):
        DefaultAssetAction.model_validate(
            {
                "kind": "service",
                "label": "Annual service",
                "interval_days": 30,
                "warn_before_days": 31,
            }
        )


def test_bootstrap_seeds_base_catalog_once_per_workspace(session: Session) -> None:
    ctx = _ctx()
    first = seed_asset_type_catalog(session, ctx)
    second = seed_asset_type_catalog(session, ctx)

    assert len(first) == len(BASE_ASSET_TYPE_CATALOG)
    assert second == []
    rows = session.scalars(
        select(AssetType)
        .where(AssetType.workspace_id == ctx.workspace_id)
        .order_by(AssetType.key)
    ).all()
    assert [row.key for row in rows] == sorted(
        item.key for item in BASE_ASSET_TYPE_CATALOG
    )


def test_crud_writes_audit_and_archives_referenced_type(session: Session) -> None:
    ctx = _ctx()
    view = create_type(
        session,
        ctx,
        slug="linen_set",
        name="Linen set",
        category="other",
        icon="Package",
        default_actions=[_default_action()],
    )

    updated = update_type(
        session,
        ctx,
        type_id=view.id,
        name="Linen bundle",
        default_actions=[
            {
                "kind": "inspect",
                "label": "Count linens",
                "interval_days": 30,
                "warn_before_days": 3,
            }
        ],
    )
    assert updated.name == "Linen bundle"
    assert updated.default_actions[0]["kind"] == "inspect"

    asset = Asset(
        id="asset_for_type",
        workspace_id=ctx.workspace_id,
        property_id="property_for_type",
        asset_type_id=view.id,
        name="Linen bundle A",
        condition="good",
        status="active",
        qr_token="LINEN0000001"[:12],
        created_at=datetime(2026, 4, 29, tzinfo=UTC),
        updated_at=datetime(2026, 4, 29, tzinfo=UTC),
    )
    session.add(asset)
    session.flush()

    archived = delete_type(session, ctx, type_id=view.id)
    assert archived is not None
    assert archived.deleted_at is not None
    assert list_types(session, ctx, workspace_only=True) == []
    assert [row.id for row in list_types(session, ctx, include_archived=True)] == [
        view.id
    ]

    actions = session.scalars(
        select(AuditLog.action)
        .where(AuditLog.entity_id == view.id)
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    assert actions == [
        "asset_type.create",
        "asset_type.update",
        "asset_type.delete",
    ]


def test_delete_unused_type_hard_deletes_and_conflicts_by_key(
    session: Session,
) -> None:
    ctx = _ctx()
    view = create_type(
        session,
        ctx,
        key="bbq",
        name="BBQ",
        category="outdoor",
        default_actions=[],
    )
    with pytest.raises(AssetTypeKeyConflict):
        create_type(
            session,
            ctx,
            key="bbq",
            name="Duplicate BBQ",
            category="outdoor",
            default_actions=[],
        )

    assert delete_type(session, ctx, type_id=view.id) is None
    assert session.get(AssetType, view.id) is None
