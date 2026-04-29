"""assets cd-c66

Revision ID: b2d4f6a8c0e2
Revises: a1c3e5f7b9d1
Create Date: 2026-04-29 16:00:00.000000

Creates the §21 asset schema foundation: asset types, assets, action
definitions with last-performed cache, and asset/property documents.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d4f6a8c0e2"
down_revision: str | Sequence[str] | None = "a1c3e5f7b9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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

_ASSET_TYPE_CATEGORY_ENUM = sa.Enum(
    *_ASSET_TYPE_CATEGORY_VALUES,
    name="asset_type_category",
    native_enum=True,
    create_constraint=False,
)
_ASSET_CONDITION_ENUM = sa.Enum(
    *_ASSET_CONDITION_VALUES,
    name="asset_condition",
    native_enum=True,
    create_constraint=False,
)
_ASSET_STATUS_ENUM = sa.Enum(
    *_ASSET_STATUS_VALUES,
    name="asset_status",
    native_enum=True,
    create_constraint=False,
)
_ASSET_ACTION_KIND_ENUM = sa.Enum(
    *_ASSET_ACTION_KIND_VALUES,
    name="asset_action_kind",
    native_enum=True,
    create_constraint=False,
)
_ASSET_DOCUMENT_KIND_ENUM = sa.Enum(
    *_ASSET_DOCUMENT_KIND_VALUES,
    name="asset_document_kind",
    native_enum=True,
    create_constraint=False,
)


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "asset_type",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", _ASSET_TYPE_CATEGORY_ENUM, nullable=False),
        sa.Column("icon_name", sa.String(), nullable=True),
        sa.Column("description_md", sa.String(), nullable=True),
        sa.Column("default_lifespan_years", sa.Integer(), nullable=True),
        sa.Column(
            "default_actions_json",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"category IN ({_in_clause(_ASSET_TYPE_CATEGORY_VALUES)})",
            name=op.f("ck_asset_type_asset_type_category"),
        ),
        sa.CheckConstraint(
            "default_lifespan_years IS NULL OR default_lifespan_years > 0",
            name=op.f("ck_asset_type_default_lifespan_years_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_asset_type_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_type")),
    )
    op.create_index(
        "uq_asset_type_workspace_key",
        "asset_type",
        ["workspace_id", "key"],
        unique=True,
        sqlite_where=sa.text("workspace_id IS NOT NULL"),
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
    )
    op.create_index(
        "uq_asset_type_system_key",
        "asset_type",
        ["key"],
        unique=True,
        sqlite_where=sa.text("workspace_id IS NULL"),
        postgresql_where=sa.text("workspace_id IS NULL"),
    )

    op.create_table(
        "asset",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("area_id", sa.String(), nullable=True),
        sa.Column("asset_type_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("make", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("serial_number", sa.String(), nullable=True),
        sa.Column("condition", _ASSET_CONDITION_ENUM, nullable=False),
        sa.Column("status", _ASSET_STATUS_ENUM, nullable=False),
        sa.Column("installed_on", sa.Date(), nullable=True),
        sa.Column("purchased_on", sa.Date(), nullable=True),
        sa.Column("purchase_price_cents", sa.Integer(), nullable=True),
        sa.Column("purchase_currency", sa.String(), nullable=True),
        sa.Column("purchase_vendor", sa.String(), nullable=True),
        sa.Column("warranty_expires_on", sa.Date(), nullable=True),
        sa.Column("expected_lifespan_years", sa.Integer(), nullable=True),
        sa.Column("estimated_replacement_on", sa.Date(), nullable=True),
        sa.Column("cover_photo_file_id", sa.String(), nullable=True),
        sa.Column("qr_token", sa.String(), nullable=False),
        sa.Column(
            "guest_visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("guest_instructions_md", sa.String(), nullable=True),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column("settings_override_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"condition IN ({_in_clause(_ASSET_CONDITION_VALUES)})",
            name=op.f("ck_asset_asset_condition"),
        ),
        sa.CheckConstraint(
            f"status IN ({_in_clause(_ASSET_STATUS_VALUES)})",
            name=op.f("ck_asset_asset_status"),
        ),
        sa.CheckConstraint(
            "LENGTH(qr_token) = 12",
            name=op.f("ck_asset_qr_token_length"),
        ),
        sa.CheckConstraint(
            "purchase_price_cents IS NULL OR purchase_price_cents >= 0",
            name=op.f("ck_asset_purchase_price_cents_nonneg"),
        ),
        sa.CheckConstraint(
            "purchase_currency IS NULL OR LENGTH(purchase_currency) = 3",
            name=op.f("ck_asset_purchase_currency_length"),
        ),
        sa.CheckConstraint(
            "expected_lifespan_years IS NULL OR expected_lifespan_years > 0",
            name=op.f("ck_asset_expected_lifespan_years_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["area.id"],
            name=op.f("fk_asset_area_id_area"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["asset_type_id"],
            ["asset_type.id"],
            name=op.f("fk_asset_asset_type_id_asset_type"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_asset_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_asset_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset")),
        sa.UniqueConstraint(
            "workspace_id",
            "qr_token",
            name=op.f("uq_asset_workspace_qr_token"),
        ),
    )
    op.create_index(
        "ix_asset_workspace_property",
        "asset",
        ["workspace_id", "property_id"],
        unique=False,
    )
    op.create_index("ix_asset_type", "asset", ["asset_type_id"], unique=False)

    op.create_table(
        "asset_action",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=True),
        sa.Column("kind", _ASSET_ACTION_KIND_ENUM, nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("description_md", sa.String(), nullable=True),
        sa.Column("task_template_id", sa.String(), nullable=True),
        sa.Column("schedule_id", sa.String(), nullable=True),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        sa.Column("estimated_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("inventory_effects_json", sa.JSON(), nullable=True),
        sa.Column("last_performed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_performed_task_id", sa.String(), nullable=True),
        sa.Column("performed_by", sa.String(), nullable=True),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column(
            "meter_reading",
            sa.Numeric(precision=18, scale=4, asdecimal=True),
            nullable=True,
        ),
        sa.Column("evidence_blob_hash", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"kind IN ({_in_clause(_ASSET_ACTION_KIND_VALUES)})",
            name=op.f("ck_asset_action_asset_action_kind"),
        ),
        sa.CheckConstraint(
            "interval_days IS NULL OR interval_days > 0",
            name=op.f("ck_asset_action_interval_days_positive"),
        ),
        sa.CheckConstraint(
            "estimated_duration_minutes IS NULL OR estimated_duration_minutes > 0",
            name=op.f("ck_asset_action_estimated_duration_minutes_positive"),
        ),
        sa.CheckConstraint(
            "meter_reading IS NULL OR meter_reading >= 0",
            name=op.f("ck_asset_action_meter_reading_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["asset.id"],
            name=op.f("fk_asset_action_asset_id_asset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_performed_task_id"],
            ["occurrence.id"],
            name=op.f("fk_asset_action_last_performed_task_id_occurrence"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["performed_by"],
            ["user.id"],
            name=op.f("fk_asset_action_performed_by_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedule.id"],
            name=op.f("fk_asset_action_schedule_id_schedule"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_template_id"],
            ["task_template.id"],
            name=op.f("fk_asset_action_task_template_id_task_template"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_asset_action_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_action")),
    )
    op.create_index(
        "ix_asset_action_asset_history",
        "asset_action",
        ["asset_id", sa.text("last_performed_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_asset_action_workspace_asset",
        "asset_action",
        ["workspace_id", "asset_id"],
        unique=False,
    )

    op.create_table(
        "asset_document",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=True),
        sa.Column("blob_hash", sa.String(), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("property_id", sa.String(), nullable=True),
        sa.Column("kind", _ASSET_DOCUMENT_KIND_ENUM, nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("amount_currency", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"kind IN ({_in_clause(_ASSET_DOCUMENT_KIND_VALUES)})",
            name=op.f("ck_asset_document_asset_document_kind"),
        ),
        sa.CheckConstraint(
            "(asset_id IS NOT NULL AND property_id IS NULL) OR "
            "(asset_id IS NULL AND property_id IS NOT NULL)",
            name=op.f("ck_asset_document_asset_document_one_parent"),
        ),
        sa.CheckConstraint(
            "amount_cents IS NULL OR amount_cents >= 0",
            name=op.f("ck_asset_document_amount_cents_nonneg"),
        ),
        sa.CheckConstraint(
            "amount_currency IS NULL OR LENGTH(amount_currency) = 3",
            name=op.f("ck_asset_document_amount_currency_length"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["asset.id"],
            name=op.f("fk_asset_document_asset_id_asset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_asset_document_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_asset_document_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_document")),
    )
    op.create_index(
        "ix_asset_document_workspace_asset",
        "asset_document",
        ["workspace_id", "asset_id"],
        unique=False,
    )
    op.create_index(
        "ix_asset_document_workspace_property",
        "asset_document",
        ["workspace_id", "property_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_asset_document_workspace_property",
        table_name="asset_document",
    )
    op.drop_index("ix_asset_document_workspace_asset", table_name="asset_document")
    op.drop_table("asset_document")

    op.drop_index("ix_asset_action_workspace_asset", table_name="asset_action")
    op.drop_index("ix_asset_action_asset_history", table_name="asset_action")
    op.drop_table("asset_action")

    op.drop_index("ix_asset_type", table_name="asset")
    op.drop_index("ix_asset_workspace_property", table_name="asset")
    op.drop_table("asset")

    op.drop_index("uq_asset_type_system_key", table_name="asset_type")
    op.drop_index("uq_asset_type_workspace_key", table_name="asset_type")
    op.drop_table("asset_type")

    if op.get_bind().dialect.name == "postgresql":
        _ASSET_DOCUMENT_KIND_ENUM.drop(op.get_bind(), checkfirst=True)
        _ASSET_ACTION_KIND_ENUM.drop(op.get_bind(), checkfirst=True)
        _ASSET_STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
        _ASSET_CONDITION_ENUM.drop(op.get_bind(), checkfirst=True)
        _ASSET_TYPE_CATEGORY_ENUM.drop(op.get_bind(), checkfirst=True)
