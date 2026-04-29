"""Integration tests for the asset schema against a migrated DB."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import Asset, AssetAction, AssetDocument, AssetType
from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_LATER = _PINNED + timedelta(hours=1)
_ASSET_TABLES = ("asset_type", "asset", "asset_action", "asset_document")


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_assets_registered() -> None:
    for table in _ASSET_TABLES:
        registry.register(table)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )


def _bootstrap(
    session: Session, *, email: str, display: str, slug: str, name: str
) -> tuple[Workspace, User]:
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(session, email=email, display_name=display, clock=clock)
    workspace = bootstrap_workspace(
        session, slug=slug, name=name, owner_user_id=user.id, clock=clock
    )
    return workspace, user


def _seed_property_workspace(
    session: Session,
    *,
    workspace: Workspace,
    property_id: str,
    label: str,
) -> str:
    with tenant_agnostic():
        session.add(
            Property(
                id=property_id,
                address=f"{label} address",
                timezone="UTC",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=workspace.id,
                label=label,
                membership_role="owner_workspace",
                status="active",
                created_at=_PINNED,
            )
        )
        session.flush()
    return property_id


def _seed_asset_type(session: Session, *, workspace_id: str, suffix: str) -> AssetType:
    asset_type = AssetType(
        id=f"01HWA00000000000000000AT{suffix}",
        workspace_id=workspace_id,
        key=f"pool_pump_{suffix.lower()}",
        name=f"Pool pump {suffix}",
        category="pool",
        default_lifespan_years=8,
        default_actions_json=[{"key": "basket_clean", "label": "Clean basket"}],
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(asset_type)
    session.flush()
    return asset_type


def _seed_asset(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    asset_type_id: str,
    asset_id: str,
    qr_token: str,
) -> Asset:
    asset = Asset(
        id=asset_id,
        workspace_id=workspace_id,
        property_id=property_id,
        asset_type_id=asset_type_id,
        name=f"Asset {asset_id[-2:]}",
        condition="good",
        status="active",
        installed_on=date(2025, 1, 1),
        purchased_on=date(2024, 12, 20),
        purchase_price_cents=120000,
        purchase_currency="USD",
        qr_token=qr_token,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(asset)
    session.flush()
    return asset


class TestMigrationShape:
    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _ASSET_TABLES:
            assert table in tables

    def test_asset_columns_and_qr_unique(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("asset")}
        expected = {
            "id",
            "workspace_id",
            "property_id",
            "area_id",
            "asset_type_id",
            "name",
            "make",
            "model",
            "serial_number",
            "condition",
            "status",
            "installed_on",
            "purchased_on",
            "purchase_price_cents",
            "purchase_currency",
            "purchase_vendor",
            "warranty_expires_on",
            "expected_lifespan_years",
            "estimated_replacement_on",
            "cover_photo_file_id",
            "qr_token",
            "guest_visible",
            "guest_instructions_md",
            "notes_md",
            "settings_override_json",
            "created_at",
            "updated_at",
            "deleted_at",
        }
        assert set(cols) == expected
        assert cols["workspace_id"]["nullable"] is False
        assert cols["property_id"]["nullable"] is False
        assert cols["qr_token"]["nullable"] is False
        uniques = {
            u["name"]: u for u in inspect(engine).get_unique_constraints("asset")
        }
        assert uniques["uq_asset_workspace_qr_token"]["column_names"] == [
            "workspace_id",
            "qr_token",
        ]

    def test_asset_type_indexes(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("asset_type")}
        assert indexes["uq_asset_type_workspace_key"]["column_names"] == [
            "workspace_id",
            "key",
        ]
        assert indexes["uq_asset_type_workspace_key"]["unique"] == 1
        assert indexes["uq_asset_type_system_key"]["column_names"] == ["key"]
        assert indexes["uq_asset_type_system_key"]["unique"] == 1

    def test_asset_action_history_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("asset_action")}
        assert "ix_asset_action_asset_history" in indexes
        columns = indexes["ix_asset_action_asset_history"]["column_names"]
        assert columns[0] == "asset_id"
        assert "last_performed_at" in columns

    def test_document_one_parent_check(self, engine: Engine) -> None:
        checks = {
            c["name"]: c
            for c in inspect(engine).get_check_constraints("asset_document")
        }
        assert "ck_asset_document_asset_document_one_parent" in checks
        sqltext = checks["ck_asset_document_asset_document_one_parent"]["sqltext"]
        assert "asset_id IS NOT NULL" in sqltext
        assert "property_id IS NOT NULL" in sqltext


class TestCrudAndConstraints:
    def test_full_graph_round_trip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="asset-crud@example.com",
            display="AssetCrud",
            slug="asset-crud-ws",
            name="AssetCrudWS",
        )
        property_id = _seed_property_workspace(
            db_session,
            workspace=workspace,
            property_id="01HWA00000000000000000PRAA",
            label="Asset property",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            asset_type = _seed_asset_type(
                db_session, workspace_id=workspace.id, suffix="AA"
            )
            asset = _seed_asset(
                db_session,
                workspace_id=workspace.id,
                property_id=property_id,
                asset_type_id=asset_type.id,
                asset_id="01HWA00000000000000000ASAA",
                qr_token="ASSET0000001",
            )
            action = AssetAction(
                id="01HWA00000000000000000ACAA",
                workspace_id=workspace.id,
                asset_id=asset.id,
                key="basket_clean",
                kind="service",
                label="Clean basket",
                interval_days=7,
                estimated_duration_minutes=15,
                inventory_effects_json=[],
                last_performed_at=_LATER,
                performed_by=user.id,
                notes_md="No debris.",
                meter_reading=Decimal("12.5000"),
                evidence_blob_hash="blob-action",
                created_at=_PINNED,
                updated_at=_PINNED,
            )
            asset_doc = AssetDocument(
                id="01HWA00000000000000000DOAA",
                workspace_id=workspace.id,
                file_id="01HWA00000000000000000FLAA",
                blob_hash="blob-doc",
                filename="manual.pdf",
                asset_id=asset.id,
                kind="manual",
                title="Pump manual",
                created_at=_PINNED,
                updated_at=_PINNED,
            )
            property_doc = AssetDocument(
                id="01HWA00000000000000000DOAB",
                workspace_id=workspace.id,
                file_id="01HWA00000000000000000FLAB",
                property_id=property_id,
                kind="insurance",
                title="Property insurance",
                amount_cents=10000,
                amount_currency="USD",
                created_at=_PINNED,
                updated_at=_PINNED,
            )
            db_session.add_all([action, asset_doc, property_doc])
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(Asset, asset.id)
            assert reloaded is not None
            assert reloaded.qr_token == "ASSET0000001"
            assert reloaded.purchase_currency == "USD"

            actions = db_session.scalars(
                select(AssetAction)
                .where(AssetAction.asset_id == asset.id)
                .order_by(AssetAction.last_performed_at.desc())
            ).all()
            assert [row.id for row in actions] == [action.id]
            assert actions[0].meter_reading == Decimal("12.5000")

            docs = db_session.scalars(
                select(AssetDocument).where(AssetDocument.workspace_id == workspace.id)
            ).all()
            assert {doc.kind for doc in docs} == {"manual", "insurance"}
        finally:
            reset_current(token)

    def test_qr_token_unique_inside_workspace(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="asset-qr-dup@example.com",
            display="AssetQrDup",
            slug="asset-qr-dup-ws",
            name="AssetQrDupWS",
        )
        property_id = _seed_property_workspace(
            db_session,
            workspace=workspace,
            property_id="01HWA00000000000000000PRQA",
            label="QR property",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            asset_type = _seed_asset_type(
                db_session, workspace_id=workspace.id, suffix="QA"
            )
            _seed_asset(
                db_session,
                workspace_id=workspace.id,
                property_id=property_id,
                asset_type_id=asset_type.id,
                asset_id="01HWA00000000000000000ASQA",
                qr_token="DUPQR0000001",
            )
            db_session.add(
                Asset(
                    id="01HWA00000000000000000ASQB",
                    workspace_id=workspace.id,
                    property_id=property_id,
                    asset_type_id=asset_type.id,
                    name="Duplicate QR",
                    condition="good",
                    status="active",
                    qr_token="DUPQR0000001",
                    created_at=_PINNED,
                    updated_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_same_qr_token_different_workspaces_allowed(
        self, db_session: Session
    ) -> None:
        workspace_a, user_a = _bootstrap(
            db_session,
            email="asset-qr-a@example.com",
            display="AssetQrA",
            slug="asset-qr-a-ws",
            name="AssetQrAWS",
        )
        property_a = _seed_property_workspace(
            db_session,
            workspace=workspace_a,
            property_id="01HWA00000000000000000PRRA",
            label="QR A",
        )
        workspace_b, _user_b = _bootstrap(
            db_session,
            email="asset-qr-b@example.com",
            display="AssetQrB",
            slug="asset-qr-b-ws",
            name="AssetQrBWS",
        )
        property_b = _seed_property_workspace(
            db_session,
            workspace=workspace_b,
            property_id="01HWA00000000000000000PRRB",
            label="QR B",
        )
        token = set_current(_ctx_for(workspace_a, user_a.id))
        try:
            type_a = _seed_asset_type(
                db_session, workspace_id=workspace_a.id, suffix="RA"
            )
            type_b = _seed_asset_type(
                db_session, workspace_id=workspace_b.id, suffix="RB"
            )
            _seed_asset(
                db_session,
                workspace_id=workspace_a.id,
                property_id=property_a,
                asset_type_id=type_a.id,
                asset_id="01HWA00000000000000000ASRA",
                qr_token="SHARED000001",
            )
            _seed_asset(
                db_session,
                workspace_id=workspace_b.id,
                property_id=property_b,
                asset_type_id=type_b.id,
                asset_id="01HWA00000000000000000ASRB",
                qr_token="SHARED000001",
            )
            # justification: this constraint assertion must inspect both
            # workspaces to prove the QR token is not globally unique.
            with tenant_agnostic():
                rows = db_session.scalars(
                    select(Asset).where(Asset.qr_token == "SHARED000001")
                ).all()
            assert {row.workspace_id for row in rows} == {
                workspace_a.id,
                workspace_b.id,
            }
        finally:
            reset_current(token)

    def test_asset_document_requires_exactly_one_parent(
        self, db_session: Session
    ) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="asset-doc-check@example.com",
            display="AssetDocCheck",
            slug="asset-doc-check-ws",
            name="AssetDocCheckWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                AssetDocument(
                    id="01HWA00000000000000000DOCX",
                    workspace_id=workspace.id,
                    kind="manual",
                    title="Orphan",
                    created_at=_PINNED,
                    updated_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestTenantRegistry:
    def test_asset_tables_raise_without_context(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        for table in _ASSET_TABLES:
            registry.register(table)
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing),
        ):
            session.scalars(select(Asset)).all()

    def test_filtered_queries_scope_assets_and_custom_asset_types(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        with filtered_factory() as session:
            workspace_a, user_a = _bootstrap(
                session,
                email="asset-filter-a@example.com",
                display="AssetFilterA",
                slug="asset-filter-a-ws",
                name="AssetFilterAWS",
            )
            property_a = _seed_property_workspace(
                session,
                workspace=workspace_a,
                property_id="01HWA00000000000000000PRFA",
                label="Filter A",
            )
            workspace_b, _user_b = _bootstrap(
                session,
                email="asset-filter-b@example.com",
                display="AssetFilterB",
                slug="asset-filter-b-ws",
                name="AssetFilterBWS",
            )
            property_b = _seed_property_workspace(
                session,
                workspace=workspace_b,
                property_id="01HWA00000000000000000PRFB",
                label="Filter B",
            )

            # justification: seeding a system catalog row with no workspace
            # requires bypassing the workspace-scoped asset_type filter.
            with tenant_agnostic():
                session.add(
                    AssetType(
                        id="01HWA00000000000000000ATSY",
                        workspace_id=None,
                        key="system_filter_type",
                        name="System filter type",
                        category="other",
                        default_actions_json=[],
                        created_at=_PINNED,
                        updated_at=_PINNED,
                    )
                )
                session.flush()

            type_a = _seed_asset_type(session, workspace_id=workspace_a.id, suffix="FA")
            type_b = _seed_asset_type(session, workspace_id=workspace_b.id, suffix="FB")
            _seed_asset(
                session,
                workspace_id=workspace_a.id,
                property_id=property_a,
                asset_type_id=type_a.id,
                asset_id="01HWA00000000000000000ASFA",
                qr_token="FILTER000001",
            )
            _seed_asset(
                session,
                workspace_id=workspace_b.id,
                property_id=property_b,
                asset_type_id=type_b.id,
                asset_id="01HWA00000000000000000ASFB",
                qr_token="FILTER000001",
            )

            token = set_current(_ctx_for(workspace_a, user_a.id))
            try:
                assets = session.scalars(
                    select(Asset).where(Asset.qr_token == "FILTER000001")
                ).all()
                assert [asset.workspace_id for asset in assets] == [workspace_a.id]

                asset_types = session.scalars(
                    select(AssetType).where(
                        AssetType.key.in_(
                            [
                                "pool_pump_fa",
                                "pool_pump_fb",
                                "system_filter_type",
                            ]
                        )
                    )
                ).all()
                assert {asset_type.key for asset_type in asset_types} == {
                    "pool_pump_fa"
                }
            finally:
                reset_current(token)
