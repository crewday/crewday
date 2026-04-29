"""Unit tests for :mod:`app.adapters.db.assets.models`.

Pure-Python coverage for model construction, table/index/constraint
shape, and tenancy-registry intent. The migration and real-DB CRUD
coverage lives in ``tests/integration/test_db_assets.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Enum, Index, Numeric, UniqueConstraint

from app.adapters.db.assets import Asset, AssetAction, AssetDocument, AssetType
from app.adapters.db.assets import models as asset_models

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _checks(table_args: tuple[object, ...]) -> dict[str, CheckConstraint]:
    return {
        str(c.name): c
        for c in table_args
        if isinstance(c, CheckConstraint) and c.name is not None
    }


def _indexes(table_args: tuple[object, ...]) -> dict[str, Index]:
    return {
        str(i.name): i
        for i in table_args
        if isinstance(i, Index) and i.name is not None
    }


class TestAssetTypeModel:
    def test_minimal_construction(self) -> None:
        asset_type = AssetType(
            id="01HWA00000000000000000ATYA",
            workspace_id="01HWA00000000000000000WSPA",
            key="pool_pump",
            name="Pool pump",
            category="pool",
            default_actions_json=[],
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert asset_type.__tablename__ == "asset_type"
        assert asset_type.key == "pool_pump"
        assert asset_type.category == "pool"
        assert asset_type.icon_name is None
        assert asset_type.deleted_at is None

    def test_category_enum_and_check(self) -> None:
        category_type = AssetType.__table__.c.category.type
        assert isinstance(category_type, Enum)
        assert category_type.name == "asset_type_category"
        for value in ("climate", "appliance", "pool", "vehicle", "other"):
            assert value in category_type.enums
        assert "ck_asset_type_asset_type_category" in _checks(AssetType.__table_args__)

    def test_default_actions_has_client_and_server_default(self) -> None:
        column = AssetType.__table__.c.default_actions_json
        assert column.default is not None
        assert column.server_default is not None

    def test_key_unique_for_system_and_workspace_rows(self) -> None:
        indexes = _indexes(AssetType.__table_args__)
        assert indexes["uq_asset_type_workspace_key"].unique is True
        assert [c.name for c in indexes["uq_asset_type_workspace_key"].columns] == [
            "workspace_id",
            "key",
        ]
        assert indexes["uq_asset_type_system_key"].unique is True
        assert [c.name for c in indexes["uq_asset_type_system_key"].columns] == ["key"]

    def test_cd_c66_aliases_write_spec_columns(self) -> None:
        asset_type = AssetType(
            id="01HWA00000000000000000ATYC",
            workspace_id="01HWA00000000000000000WSPA",
            slug="generator",
            name="Generator",
            category="outdoor",
            icon="Zap",
            default_action_catalog_json=[{"key": "oil_change", "label": "Change oil"}],
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert asset_type.key == "generator"
        assert asset_type.icon_name == "Zap"
        assert asset_type.default_actions_json == [
            {"key": "oil_change", "label": "Change oil"}
        ]


class TestAssetModel:
    def test_minimal_construction(self) -> None:
        asset = Asset(
            id="01HWA00000000000000000ASTA",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            name="Living room AC",
            condition="good",
            status="active",
            qr_token="ASSET0000001",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert asset.__tablename__ == "asset"
        assert asset.name == "Living room AC"
        assert asset.qr_token == "ASSET0000001"
        assert asset.asset_type_id is None
        assert asset.area_id is None
        assert asset.guest_visible is None

    def test_rich_construction(self) -> None:
        asset = Asset(
            id="01HWA00000000000000000ASTB",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            asset_type_id="01HWA00000000000000000ATYA",
            name="Pool pump #2",
            make="PumpCo",
            model="P-200",
            serial_number="SN-22",
            condition="fair",
            status="in_repair",
            installed_on=date(2024, 5, 1),
            purchased_on=date(2024, 4, 20),
            purchase_price_cents=120000,
            purchase_currency="USD",
            purchase_vendor="Supply Co",
            warranty_expires_on=date(2029, 4, 20),
            expected_lifespan_years=8,
            qr_token="PUMP00000002",
            guest_visible=True,
            notes_md="Noisy bearing.",
            settings_override_json={"assets.show_guest_assets": True},
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert asset.make == "PumpCo"
        assert asset.purchase_price_cents == 120000
        assert asset.guest_visible is True
        assert asset.settings_override_json == {"assets.show_guest_assets": True}

    def test_condition_status_enums_and_checks(self) -> None:
        condition_type = Asset.__table__.c.condition.type
        status_type = Asset.__table__.c.status.type
        assert isinstance(condition_type, Enum)
        assert condition_type.name == "asset_condition"
        assert "needs_replacement" in condition_type.enums
        assert isinstance(status_type, Enum)
        assert status_type.name == "asset_status"
        assert "decommissioned" in status_type.enums
        checks = _checks(Asset.__table_args__)
        assert "ck_asset_asset_condition" in checks
        assert "ck_asset_asset_status" in checks
        assert "ck_asset_qr_token_length" in checks

    def test_qr_token_unique_per_workspace(self) -> None:
        uniques = [c for c in Asset.__table_args__ if isinstance(c, UniqueConstraint)]
        target = next(c for c in uniques if c.name == "uq_asset_workspace_qr_token")
        assert [col.name for col in target.columns] == ["workspace_id", "qr_token"]

    def test_guest_visible_has_client_and_server_default(self) -> None:
        column = Asset.__table__.c.guest_visible
        assert column.default is not None
        assert column.server_default is not None

    def test_cd_c66_aliases_write_spec_columns(self) -> None:
        asset = Asset(
            id="01HWA00000000000000000ASTC",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            label="Generator",
            condition="new",
            status="active",
            qr_code="GENR00000001",
            purchased_at=date(2026, 1, 10),
            warranty_ends_at=date(2031, 1, 10),
            metadata_json={"vendor_ref": "GEN-1"},
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert asset.name == "Generator"
        assert asset.qr_token == "GENR00000001"
        assert asset.purchased_on == date(2026, 1, 10)
        assert asset.warranty_expires_on == date(2031, 1, 10)
        assert asset.settings_override_json == {"vendor_ref": "GEN-1"}


class TestAssetActionModel:
    def test_scheduled_definition_construction(self) -> None:
        action = AssetAction(
            id="01HWA00000000000000000ACTA",
            workspace_id="01HWA00000000000000000WSPA",
            asset_id="01HWA00000000000000000ASTA",
            key="filter_clean",
            kind="service",
            label="Clean filter",
            interval_days=30,
            estimated_duration_minutes=20,
            inventory_effects_json=[
                {"item_ref": "filter_ac_standard", "kind": "consume", "qty": 1}
            ],
            last_performed_at=_PINNED,
            performed_by="01HWA00000000000000000USRA",
            meter_reading=Decimal("123.4500"),
            evidence_blob_hash="blob-hash",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert action.__tablename__ == "asset_action"
        assert action.kind == "service"
        assert action.last_performed_at == _PINNED
        assert action.meter_reading == Decimal("123.4500")

    def test_kind_enum_and_meter_precision(self) -> None:
        kind_type = AssetAction.__table__.c.kind.type
        assert isinstance(kind_type, Enum)
        assert kind_type.name == "asset_action_kind"
        for value in ("service", "repair", "replace", "inspect", "read"):
            assert value in kind_type.enums
        assert "ck_asset_action_asset_action_kind" in _checks(
            AssetAction.__table_args__
        )
        meter_type = AssetAction.__table__.c.meter_reading.type
        assert isinstance(meter_type, Numeric)
        assert meter_type.precision == 18
        assert meter_type.scale == 4

    def test_history_index_orders_last_performed_desc(self) -> None:
        """§21 stores action definitions; history order uses the cache column."""
        index = _indexes(AssetAction.__table_args__)["ix_asset_action_asset_history"]
        assert [c.name for c in index.columns] == ["asset_id", "last_performed_at"]
        rendered = " ".join(str(expr) for expr in index.expressions).upper()
        assert "LAST_PERFORMED_AT" in rendered
        assert "DESC" in rendered

    def test_performed_at_alias_writes_last_performed_at(self) -> None:
        action = AssetAction(
            id="01HWA00000000000000000ACTB",
            workspace_id="01HWA00000000000000000WSPA",
            asset_id="01HWA00000000000000000ASTA",
            kind="inspect",
            label="Inspect",
            performed_at=_PINNED,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert action.last_performed_at == _PINNED


class TestAssetDocumentModel:
    def test_asset_document_construction(self) -> None:
        doc = AssetDocument(
            id="01HWA00000000000000000DOCA",
            workspace_id="01HWA00000000000000000WSPA",
            file_id="01HWA00000000000000000FILA",
            blob_hash="blob-hash",
            filename="manual.pdf",
            asset_id="01HWA00000000000000000ASTA",
            kind="manual",
            title="Pump manual",
            amount_cents=2500,
            amount_currency="USD",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert doc.__tablename__ == "asset_document"
        assert doc.kind == "manual"
        assert doc.asset_id == "01HWA00000000000000000ASTA"
        assert doc.property_id is None

    def test_kind_enum_and_one_parent_check(self) -> None:
        kind_type = AssetDocument.__table__.c.kind.type
        assert isinstance(kind_type, Enum)
        assert kind_type.name == "asset_document_kind"
        assert "certificate" in kind_type.enums
        checks = _checks(AssetDocument.__table_args__)
        assert "ck_asset_document_asset_document_kind" in checks
        assert "ck_asset_document_asset_document_one_parent" in checks

    def test_category_alias_writes_kind(self) -> None:
        doc = AssetDocument(
            id="01HWA00000000000000000DOCB",
            workspace_id="01HWA00000000000000000WSPA",
            asset_id="01HWA00000000000000000ASTA",
            category="warranty",
            title="Warranty",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert doc.kind == "warranty"


class TestPackageReExports:
    def test_models_re_exported(self) -> None:
        assert AssetType is asset_models.AssetType
        assert Asset is asset_models.Asset
        assert AssetAction is asset_models.AssetAction
        assert AssetDocument is asset_models.AssetDocument


class TestRegistryIntent:
    """Every asset table is intended to be workspace-scoped."""

    def test_every_asset_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("asset_type", "asset", "asset_action", "asset_document"):
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in ("asset_type", "asset", "asset_action", "asset_document"):
            assert table in scoped

    def test_is_scoped_reports_true(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("asset_type", "asset", "asset_action", "asset_document"):
            registry.register(table)
            assert registry.is_scoped(table) is True
