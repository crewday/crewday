"""billing — organization / rate_card / work_order / quote / vendor_invoice."""

from __future__ import annotations

from app.adapters.db.billing.models import (
    Organization,
    Quote,
    RateCard,
    VendorInvoice,
    WorkOrder,
    WorkOrderShiftAccrual,
)
from app.tenancy.registry import register

for _table in (
    "organization",
    "rate_card",
    "work_order",
    "quote",
    "vendor_invoice",
    "work_order_shift_accrual",
):
    register(_table)

__all__ = [
    "Organization",
    "Quote",
    "RateCard",
    "VendorInvoice",
    "WorkOrder",
    "WorkOrderShiftAccrual",
]
