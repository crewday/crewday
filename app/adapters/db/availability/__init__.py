"""availability — user_leave / user_weekly_availability / user_availability_override.

Importing this package registers the three workspace-scoped tables
that drive the §06 availability precedence stack: every SELECT /
UPDATE / DELETE auto-filters on ``workspace_id`` through the ORM
tenant filter (see :mod:`app.tenancy.orm_filter`).

Sibling :class:`PublicHoliday` lives in
:mod:`app.adapters.db.holidays.models` because its FK target and
write authority are workspace-managed config rather than per-user
state.

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
§"Weekly availability", §"user_availability_overrides";
``docs/specs/02-domain-model.md`` §"Work" entity list.
"""

from __future__ import annotations

from app.adapters.db.availability.models import (
    UserAvailabilityOverride,
    UserLeave,
    UserWeeklyAvailability,
)
from app.tenancy.registry import register

for _table in (
    "user_leave",
    "user_weekly_availability",
    "user_availability_override",
):
    register(_table)

__all__ = [
    "UserAvailabilityOverride",
    "UserLeave",
    "UserWeeklyAvailability",
]
