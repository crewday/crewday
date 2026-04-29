"""billing cd-t8m

Revision ID: e9f1a3b5d7c9
Revises: d8f0a2c4e6b8
Create Date: 2026-04-29 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9f1a3b5d7c9"
down_revision: str | Sequence[str] | None = "d8f0a2c4e6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "organization",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("billing_address", sa.JSON(), nullable=False),
        sa.Column("tax_id", sa.String(), nullable=True),
        sa.Column("default_currency", sa.String(), nullable=False),
        sa.Column("contact_email", sa.String(), nullable=True),
        sa.Column("contact_phone", sa.String(), nullable=True),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "LENGTH(default_currency) = 3",
            name=op.f("ck_organization_default_currency_length"),
        ),
        sa.CheckConstraint(
            "kind IN ('client', 'vendor', 'mixed')",
            name=op.f("ck_organization_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_organization_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization")),
        sa.UniqueConstraint(
            "id",
            "workspace_id",
            name=op.f("uq_organization_id_workspace"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "display_name",
            name=op.f("uq_organization_workspace_display_name"),
        ),
    )
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.create_index(
            "ix_organization_workspace_kind",
            ["workspace_id", "kind"],
            unique=False,
        )

    op.create_table(
        "rate_card",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("rates_json", sa.JSON(), nullable=False),
        sa.Column("active_from", sa.Date(), nullable=False),
        sa.Column("active_to", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "active_to IS NULL OR active_to > active_from",
            name=op.f("ck_rate_card_active_range"),
        ),
        sa.CheckConstraint(
            "LENGTH(currency) = 3",
            name=op.f("ck_rate_card_currency_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["organization.id", "organization.workspace_id"],
            name=op.f("fk_rate_card_organization_id_organization"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_rate_card_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_card")),
        sa.UniqueConstraint(
            "id",
            "workspace_id",
            name=op.f("uq_rate_card_id_workspace"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "organization_id",
            "label",
            "active_from",
            name=op.f("uq_rate_card_workspace_org_label_active_from"),
        ),
    )
    with op.batch_alter_table("rate_card", schema=None) as batch_op:
        batch_op.create_index(
            "ix_rate_card_workspace_organization_active",
            ["workspace_id", "organization_id", "active_from"],
            unique=False,
        )

    op.create_table(
        "work_order",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_card_id", sa.String(), nullable=True),
        sa.Column(
            "total_hours_decimal",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
        ),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name=op.f("ck_work_order_ends_after_starts"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'in_progress', 'completed', 'invoiced')",
            name=op.f("ck_work_order_status"),
        ),
        sa.CheckConstraint(
            "total_cents >= 0",
            name=op.f("ck_work_order_total_cents_nonneg"),
        ),
        sa.CheckConstraint(
            "total_hours_decimal >= 0",
            name=op.f("ck_work_order_total_hours_decimal_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["organization.id", "organization.workspace_id"],
            name=op.f("fk_work_order_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["property_id", "workspace_id"],
            ["property_workspace.property_id", "property_workspace.workspace_id"],
            name=op.f("fk_work_order_property_id_property_workspace"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rate_card_id", "workspace_id"],
            ["rate_card.id", "rate_card.workspace_id"],
            name=op.f("fk_work_order_rate_card_id_rate_card"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_work_order_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_order")),
    )
    with op.batch_alter_table("work_order", schema=None) as batch_op:
        batch_op.create_index(
            "ix_work_order_workspace_property_status",
            ["workspace_id", "property_id", "status"],
            unique=False,
        )
        batch_op.create_index(
            "ix_work_order_workspace_status",
            ["workspace_id", "status"],
            unique=False,
        )

    op.create_table(
        "quote",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "LENGTH(currency) = 3",
            name=op.f("ck_quote_currency_length"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'accepted', 'rejected', 'expired')",
            name=op.f("ck_quote_status"),
        ),
        sa.CheckConstraint(
            "total_cents >= 0",
            name=op.f("ck_quote_total_cents_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["organization.id", "organization.workspace_id"],
            name=op.f("fk_quote_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["property_id", "workspace_id"],
            ["property_workspace.property_id", "property_workspace.workspace_id"],
            name=op.f("fk_quote_property_id_property_workspace"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_quote_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quote")),
    )
    with op.batch_alter_table("quote", schema=None) as batch_op:
        batch_op.create_index(
            "ix_quote_workspace_organization_status",
            ["workspace_id", "organization_id", "status"],
            unique=False,
        )
        batch_op.create_index(
            "ix_quote_workspace_status",
            ["workspace_id", "status"],
            unique=False,
        )

    op.create_table(
        "vendor_invoice",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("vendor_org_id", sa.String(), nullable=False),
        sa.Column("invoice_number", sa.String(), nullable=False),
        sa.Column("issued_at", sa.Date(), nullable=False),
        sa.Column("due_at", sa.Date(), nullable=True),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pdf_blob_hash", sa.String(), nullable=True),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.CheckConstraint(
            "due_at IS NULL OR due_at >= issued_at",
            name=op.f("ck_vendor_invoice_due_range"),
        ),
        sa.CheckConstraint(
            "LENGTH(currency) = 3",
            name=op.f("ck_vendor_invoice_currency_length"),
        ),
        sa.CheckConstraint(
            "status IN ('received', 'approved', 'paid', 'disputed')",
            name=op.f("ck_vendor_invoice_status"),
        ),
        sa.CheckConstraint(
            "total_cents >= 0",
            name=op.f("ck_vendor_invoice_total_cents_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["vendor_org_id", "workspace_id"],
            ["organization.id", "organization.workspace_id"],
            name=op.f("fk_vendor_invoice_vendor_org_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_vendor_invoice_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_invoice")),
        sa.UniqueConstraint(
            "workspace_id",
            "vendor_org_id",
            "invoice_number",
            name=op.f("uq_vendor_invoice_workspace_vendor_number"),
        ),
    )
    with op.batch_alter_table("vendor_invoice", schema=None) as batch_op:
        batch_op.create_index(
            "ix_vendor_invoice_workspace_status",
            ["workspace_id", "status"],
            unique=False,
        )
        batch_op.create_index(
            "ix_vendor_invoice_workspace_vendor_status",
            ["workspace_id", "vendor_org_id", "status"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("vendor_invoice", schema=None) as batch_op:
        batch_op.drop_index("ix_vendor_invoice_workspace_vendor_status")
        batch_op.drop_index("ix_vendor_invoice_workspace_status")
    op.drop_table("vendor_invoice")

    with op.batch_alter_table("quote", schema=None) as batch_op:
        batch_op.drop_index("ix_quote_workspace_status")
        batch_op.drop_index("ix_quote_workspace_organization_status")
    op.drop_table("quote")

    with op.batch_alter_table("work_order", schema=None) as batch_op:
        batch_op.drop_index("ix_work_order_workspace_status")
        batch_op.drop_index("ix_work_order_workspace_property_status")
    op.drop_table("work_order")

    with op.batch_alter_table("rate_card", schema=None) as batch_op:
        batch_op.drop_index("ix_rate_card_workspace_organization_active")
    op.drop_table("rate_card")

    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.drop_index("ix_organization_workspace_kind")
    op.drop_table("organization")
