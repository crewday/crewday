"""Asset catalog, tracked asset, action, and document models.

The assets context follows the §21 schema where the current database
surface can support it:

* ``asset_type`` stores system-seeded rows with ``workspace_id = NULL``
  and workspace-custom rows with a local ``workspace_id``.
* ``asset`` is the workspace-scoped physical item and keeps QR token
  uniqueness scoped to the workspace.
* ``asset_action`` is the scheduled/performable action definition
  shape from §21. The table stores ``last_performed_at`` as the
  history ordering/cache timestamp; it does not add the occurrence
  ``asset_action_id`` hook reserved for cd-vajl.
* ``asset_document`` uses the spec-shaped asset-or-property attachment
  contract. ``file_id`` is a soft reference until the shared ``file``
  table from §02 lands.

See ``docs/specs/02-domain-model.md`` §"Assets" and
``docs/specs/21-assets.md``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract.
from app.adapters.db.identity import models as _identity_models  # noqa: F401
from app.adapters.db.places import models as _places_models  # noqa: F401
from app.adapters.db.tasks import models as _tasks_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["Asset", "AssetAction", "AssetDocument", "AssetType"]


_ASSET_TYPE_CATEGORY_VALUES: tuple[str, ...] = (
    "climate",
    "appliance",
    "plumbing",
    "pool",
    "heating",
    "outdoor",
    "safety",
    "security",
    "vehicle",
    "other",
)
_ASSET_CONDITION_VALUES: tuple[str, ...] = (
    "new",
    "good",
    "fair",
    "poor",
    "needs_replacement",
)
_ASSET_STATUS_VALUES: tuple[str, ...] = (
    "active",
    "in_repair",
    "decommissioned",
    "disposed",
)
_ASSET_ACTION_KIND_VALUES: tuple[str, ...] = (
    "service",
    "repair",
    "replace",
    "inspect",
    "read",
)
_ASSET_DOCUMENT_KIND_VALUES: tuple[str, ...] = (
    "manual",
    "warranty",
    "invoice",
    "receipt",
    "photo",
    "certificate",
    "contract",
    "permit",
    "insurance",
    "other",
)

_ASSET_TYPE_CATEGORY_ENUM = Enum(
    *_ASSET_TYPE_CATEGORY_VALUES,
    name="asset_type_category",
    native_enum=True,
    create_constraint=False,
)
_ASSET_CONDITION_ENUM = Enum(
    *_ASSET_CONDITION_VALUES,
    name="asset_condition",
    native_enum=True,
    create_constraint=False,
)
_ASSET_STATUS_ENUM = Enum(
    *_ASSET_STATUS_VALUES,
    name="asset_status",
    native_enum=True,
    create_constraint=False,
)
_ASSET_ACTION_KIND_ENUM = Enum(
    *_ASSET_ACTION_KIND_VALUES,
    name="asset_action_kind",
    native_enum=True,
    create_constraint=False,
)
_ASSET_DOCUMENT_KIND_ENUM = Enum(
    *_ASSET_DOCUMENT_KIND_VALUES,
    name="asset_document_kind",
    native_enum=True,
    create_constraint=False,
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', ...)`` CHECK body fragment."""
    return "'" + "', '".join(values) + "'"


class AssetType(Base):
    """Catalog row for a class of equipment or appliance."""

    __tablename__ = "asset_type"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=True,
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    slug = synonym("key")
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(_ASSET_TYPE_CATEGORY_ENUM, nullable=False)
    icon_name: Mapped[str | None] = mapped_column(String, nullable=True)
    icon = synonym("icon_name")
    description_md: Mapped[str | None] = mapped_column(String, nullable=True)
    default_lifespan_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_actions_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    default_action_catalog_json = synonym("default_actions_json")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"category IN ({_in_clause(_ASSET_TYPE_CATEGORY_VALUES)})",
            name="asset_type_category",
        ),
        CheckConstraint(
            "default_lifespan_years IS NULL OR default_lifespan_years > 0",
            name="default_lifespan_years_positive",
        ),
        Index(
            "uq_asset_type_workspace_key",
            "workspace_id",
            "key",
            unique=True,
            sqlite_where=text("workspace_id IS NOT NULL"),
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index(
            "uq_asset_type_system_key",
            "key",
            unique=True,
            sqlite_where=text("workspace_id IS NULL"),
            postgresql_where=text("workspace_id IS NULL"),
        ),
    )


class Asset(Base):
    """A workspace-tracked physical item installed at a property."""

    __tablename__ = "asset"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    area_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("area.id", ondelete="SET NULL"),
        nullable=True,
    )
    asset_type_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("asset_type.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    label = synonym("name")
    make: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String, nullable=True)
    condition: Mapped[str] = mapped_column(
        _ASSET_CONDITION_ENUM, nullable=False, default="good"
    )
    status: Mapped[str] = mapped_column(
        _ASSET_STATUS_ENUM, nullable=False, default="active"
    )
    installed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchased_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    purchased_at = synonym("purchased_on")
    purchase_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchase_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    purchase_vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    warranty_expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    warranty_ends_at = synonym("warranty_expires_on")
    expected_lifespan_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_replacement_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    cover_photo_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    qr_token: Mapped[str] = mapped_column(String, nullable=False)
    qr_code = synonym("qr_token")
    guest_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    guest_instructions_md: Mapped[str | None] = mapped_column(String, nullable=True)
    notes_md: Mapped[str | None] = mapped_column(String, nullable=True)
    settings_override_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    metadata_json = synonym("settings_override_json")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"condition IN ({_in_clause(_ASSET_CONDITION_VALUES)})",
            name="asset_condition",
        ),
        CheckConstraint(
            f"status IN ({_in_clause(_ASSET_STATUS_VALUES)})",
            name="asset_status",
        ),
        CheckConstraint("LENGTH(qr_token) = 12", name="qr_token_length"),
        CheckConstraint(
            "purchase_price_cents IS NULL OR purchase_price_cents >= 0",
            name="purchase_price_cents_nonneg",
        ),
        CheckConstraint(
            "purchase_currency IS NULL OR LENGTH(purchase_currency) = 3",
            name="purchase_currency_length",
        ),
        CheckConstraint(
            "expected_lifespan_years IS NULL OR expected_lifespan_years > 0",
            name="expected_lifespan_years_positive",
        ),
        UniqueConstraint(
            "workspace_id",
            "qr_token",
            name="uq_asset_workspace_qr_token",
        ),
        Index("ix_asset_workspace_property", "workspace_id", "property_id"),
        Index("ix_asset_type", "asset_type_id"),
    )


class AssetAction(Base):
    """Maintenance action definition with cached last-performed state."""

    __tablename__ = "asset_action"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("asset.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(
        _ASSET_ACTION_KIND_ENUM, nullable=False, default="service"
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    description_md: Mapped[str | None] = mapped_column(String, nullable=True)
    task_template_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("task_template.id", ondelete="SET NULL"),
        nullable=True,
    )
    schedule_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("schedule.id", ondelete="SET NULL"),
        nullable=True,
    )
    interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_duration_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    inventory_effects_json: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSON, nullable=True
    )
    last_performed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    performed_at = synonym("last_performed_at")
    last_performed_task_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("occurrence.id", ondelete="SET NULL"),
        nullable=True,
    )
    performed_by: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes_md: Mapped[str | None] = mapped_column(String, nullable=True)
    meter_reading: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4, asdecimal=True), nullable=True
    )
    evidence_blob_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_ASSET_ACTION_KIND_VALUES)})",
            name="asset_action_kind",
        ),
        CheckConstraint(
            "interval_days IS NULL OR interval_days > 0",
            name="interval_days_positive",
        ),
        CheckConstraint(
            "estimated_duration_minutes IS NULL OR estimated_duration_minutes > 0",
            name="estimated_duration_minutes_positive",
        ),
        CheckConstraint(
            "meter_reading IS NULL OR meter_reading >= 0",
            name="meter_reading_nonneg",
        ),
        Index(
            "ix_asset_action_asset_history",
            "asset_id",
            last_performed_at.desc(),
        ),
        Index("ix_asset_action_workspace_asset", "workspace_id", "asset_id"),
    )


class AssetDocument(Base):
    """Document attached to exactly one asset or property."""

    __tablename__ = "asset_document"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    blob_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    asset_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("asset.id", ondelete="CASCADE"),
        nullable=True,
    )
    property_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(_ASSET_DOCUMENT_KIND_ENUM, nullable=False)
    category = synonym("kind")
    title: Mapped[str] = mapped_column(String, nullable=False)
    notes_md: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_ASSET_DOCUMENT_KIND_VALUES)})",
            name="asset_document_kind",
        ),
        CheckConstraint(
            "(asset_id IS NOT NULL AND property_id IS NULL) OR "
            "(asset_id IS NULL AND property_id IS NOT NULL)",
            name="asset_document_one_parent",
        ),
        CheckConstraint(
            "amount_cents IS NULL OR amount_cents >= 0",
            name="amount_cents_nonneg",
        ),
        CheckConstraint(
            "amount_currency IS NULL OR LENGTH(amount_currency) = 3",
            name="amount_currency_length",
        ),
        Index("ix_asset_document_workspace_asset", "workspace_id", "asset_id"),
        Index("ix_asset_document_workspace_property", "workspace_id", "property_id"),
    )
