"""Billing SQLAlchemy models.

v1 persistence foundation for cd-t8m: organizations, rate cards, work
orders, quotes, and vendor invoices. The richer §22 surface (client/user
rate tables, booking billing snapshots, quote line validation, payout
resolution, and approval-gated transitions) lands in service/domain
follow-ups; this module only defines the mapped tables and DB-level
shape.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` for the shared
# metadata load-order contract.
from app.adapters.db.places import models as _places_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = [
    "Organization",
    "Quote",
    "RateCard",
    "VendorInvoice",
    "WorkOrder",
]


_ORGANIZATION_KIND_VALUES: tuple[str, ...] = ("client", "vendor", "mixed")
_WORK_ORDER_STATUS_VALUES: tuple[str, ...] = (
    "draft",
    "sent",
    "in_progress",
    "completed",
    "invoiced",
)
_QUOTE_STATUS_VALUES: tuple[str, ...] = (
    "draft",
    "sent",
    "accepted",
    "rejected",
    "expired",
)
_VENDOR_INVOICE_STATUS_VALUES: tuple[str, ...] = (
    "received",
    "approved",
    "paid",
    "disputed",
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a portable SQL CHECK ``IN`` value list."""
    return "'" + "', '".join(values) + "'"


class Organization(Base):
    """A client, vendor, or mixed counterparty for a workspace."""

    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    billing_address: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    tax_id: Mapped[str | None] = mapped_column(String, nullable=True)
    default_currency: Mapped[str] = mapped_column(String, nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    notes_md: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_ORGANIZATION_KIND_VALUES)})",
            name="kind",
        ),
        CheckConstraint(
            "LENGTH(default_currency) = 3",
            name="default_currency_length",
        ),
        UniqueConstraint(
            "id",
            "workspace_id",
            name="uq_organization_id_workspace",
        ),
        UniqueConstraint(
            "workspace_id",
            "display_name",
            name="uq_organization_workspace_display_name",
        ),
        Index("ix_organization_workspace_kind", "workspace_id", "kind"),
    )


class RateCard(Base):
    """A workspace's billable hourly rates for one organization."""

    __tablename__ = "rate_card"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    rates_json: Mapped[dict[str, int]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    active_from: Mapped[date] = mapped_column(Date, nullable=False)
    active_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        CheckConstraint("LENGTH(currency) = 3", name="currency_length"),
        CheckConstraint(
            "active_to IS NULL OR active_to > active_from",
            name="active_range",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["organization.id", "organization.workspace_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id",
            "workspace_id",
            name="uq_rate_card_id_workspace",
        ),
        UniqueConstraint(
            "workspace_id",
            "organization_id",
            "label",
            "active_from",
            name="uq_rate_card_workspace_org_label_active_from",
        ),
        Index(
            "ix_rate_card_workspace_organization_active",
            "workspace_id",
            "organization_id",
            "active_from",
        ),
    )


class WorkOrder(Base):
    """A billable envelope for client-facing work."""

    __tablename__ = "work_order"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    property_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rate_card_id: Mapped[str | None] = mapped_column(String, nullable=True)
    total_hours_decimal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_in_clause(_WORK_ORDER_STATUS_VALUES)})",
            name="status",
        ),
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ends_after_starts",
        ),
        CheckConstraint(
            "total_hours_decimal >= 0",
            name="total_hours_decimal_nonneg",
        ),
        CheckConstraint("total_cents >= 0", name="total_cents_nonneg"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["organization.id", "organization.workspace_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["property_id", "workspace_id"],
            ["property_workspace.property_id", "property_workspace.workspace_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["rate_card_id", "workspace_id"],
            ["rate_card.id", "rate_card.workspace_id"],
            ondelete="SET NULL",
        ),
        Index("ix_work_order_workspace_status", "workspace_id", "status"),
        Index(
            "ix_work_order_workspace_property_status",
            "workspace_id",
            "property_id",
            "status",
        ),
    )


class Quote(Base):
    """A proposed price for a client organization and property."""

    __tablename__ = "quote"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    property_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body_md: Mapped[str] = mapped_column(String, nullable=False, default="")
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_in_clause(_QUOTE_STATUS_VALUES)})",
            name="status",
        ),
        CheckConstraint("LENGTH(currency) = 3", name="currency_length"),
        CheckConstraint("total_cents >= 0", name="total_cents_nonneg"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["organization.id", "organization.workspace_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["property_id", "workspace_id"],
            ["property_workspace.property_id", "property_workspace.workspace_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_quote_workspace_status", "workspace_id", "status"),
        Index(
            "ix_quote_workspace_organization_status",
            "workspace_id",
            "organization_id",
            "status",
        ),
    )


class VendorInvoice(Base):
    """An invoice received from a vendor organization."""

    __tablename__ = "vendor_invoice"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    vendor_org_id: Mapped[str] = mapped_column(String, nullable=False)
    invoice_number: Mapped[str] = mapped_column(String, nullable=False)
    issued_at: Mapped[date] = mapped_column(Date, nullable=False)
    due_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    pdf_blob_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    notes_md: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_in_clause(_VENDOR_INVOICE_STATUS_VALUES)})",
            name="status",
        ),
        CheckConstraint("LENGTH(currency) = 3", name="currency_length"),
        CheckConstraint("total_cents >= 0", name="total_cents_nonneg"),
        CheckConstraint("due_at IS NULL OR due_at >= issued_at", name="due_range"),
        ForeignKeyConstraint(
            ["vendor_org_id", "workspace_id"],
            ["organization.id", "organization.workspace_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "vendor_org_id",
            "invoice_number",
            name="uq_vendor_invoice_workspace_vendor_number",
        ),
        Index("ix_vendor_invoice_workspace_status", "workspace_id", "status"),
        Index(
            "ix_vendor_invoice_workspace_vendor_status",
            "workspace_id",
            "vendor_org_id",
            "status",
        ),
    )
