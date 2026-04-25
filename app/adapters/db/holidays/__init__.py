"""holidays — public_holiday.

Importing this package registers ``public_holiday`` as workspace-
scoped — every SELECT / UPDATE / DELETE auto-filters on
``workspace_id`` through the ORM tenant filter (see
:mod:`app.tenancy.orm_filter`).

Sibling per-user availability rows
(:class:`~app.adapters.db.availability.models.UserLeave`,
:class:`~app.adapters.db.availability.models.UserWeeklyAvailability`,
:class:`~app.adapters.db.availability.models.UserAvailabilityOverride`)
live in :mod:`app.adapters.db.availability` because their write
authority and FK target are per-user state rather than workspace
config.

See ``docs/specs/06-tasks-and-scheduling.md`` §"public_holidays";
``docs/specs/02-domain-model.md`` §"Work" entity list.
"""

from __future__ import annotations

from app.adapters.db.holidays.models import PublicHoliday
from app.tenancy.registry import register

register("public_holiday")

__all__ = ["PublicHoliday"]
