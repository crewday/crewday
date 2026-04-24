"""stays — ical_feed / reservation / stay_bundle.

All three tables in this package are workspace-scoped: each row
carries a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A
bare read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

Unlike the places package — where ``property`` intentionally stays
tenant-agnostic because a single villa may belong to several
workspaces — every stays row is born inside exactly one workspace's
operations (the feed ingests into that workspace, the reservation
is booked against its unit, the bundle materialises its turnover
tasks), so scoping is unambiguous.

See ``docs/specs/02-domain-model.md`` §"reservation", §"ical_feed",
§"stay_bundle", and ``docs/specs/04-properties-and-stays.md``
§"Stay (reservation)" / §"iCal feed".
"""

from __future__ import annotations

from app.adapters.db.stays.models import (
    DEFAULT_POLL_CADENCE,
    IcalFeed,
    Reservation,
    StayBundle,
)
from app.tenancy.registry import register

for _table in ("ical_feed", "reservation", "stay_bundle"):
    register(_table)

__all__ = [
    "DEFAULT_POLL_CADENCE",
    "IcalFeed",
    "Reservation",
    "StayBundle",
]
