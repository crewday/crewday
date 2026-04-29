"""Billing context router scaffold.

Owns organizations, rate cards, work orders, quotes, vendor
invoices, and the client portal surface (spec §01 "Context map",
§12 "Clients, work orders, invoices", §22). Routes land in
cd-eb14; this file is the reserved seat under
``/w/<slug>/api/v1/billing``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.billing.organizations import build_organizations_router
from app.api.billing.quotes import build_quotes_public_router, build_quotes_router


def build_billing_router() -> APIRouter:
    router = APIRouter(tags=["billing"])
    router.include_router(build_organizations_router())
    router.include_router(build_quotes_router())
    return router


router = build_billing_router()
public_router = build_quotes_public_router()

__all__ = ["build_billing_router", "public_router", "router"]
