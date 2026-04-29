"""billing — organization / rate_card / work_order / quote / vendor_invoice."""

from __future__ import annotations

from app.adapters.db.billing.models import (
    Organization,
    Quote,
    RateCard,
    VendorInvoice,
    WorkOrder,
)
from app.tenancy.registry import register

for _table in (
    "organization",
    "rate_card",
    "work_order",
    "quote",
    "vendor_invoice",
):
    register(_table)

__all__ = [
    "Organization",
    "Quote",
    "RateCard",
    "VendorInvoice",
    "WorkOrder",
]
