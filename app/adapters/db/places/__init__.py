"""places — property / unit / area / property_workspace / property_closure
+ property_work_role_assignment.

Importing this package registers per-table tenancy behaviour:

* ``property_workspace``: **IS** workspace-scoped (carries its own
  ``workspace_id`` column). The ORM tenant filter auto-injects a
  ``workspace_id`` predicate on every SELECT / UPDATE / DELETE, so a
  bare read without a :class:`~app.tenancy.WorkspaceContext` raises
  :class:`~app.tenancy.orm_filter.TenantFilterMissing`.
* ``property_work_role_assignment``: **IS** workspace-scoped (cd-e4m3).
  Carries a denormalised ``workspace_id`` column so the tenant
  filter rides a local column without threading a join through
  ``user_work_role`` on every read — same pattern as
  :class:`~app.adapters.db.workspace.models.WorkEngagement`.
* ``property``: intentionally **NOT** registered. The same physical
  property can belong to multiple workspaces via ``property_workspace``
  (§02 "Villa belongs to many workspaces"), so pinning a single
  ``workspace_id`` on the ``property`` row itself would contradict
  the spec. Services that need a workspace-scoped read of properties
  MUST join through ``property_workspace`` — the junction carries the
  tenancy boundary.
* ``unit``, ``area``, ``property_closure``: intentionally **NOT**
  registered in the v1 slice. These tables reach the workspace
  boundary through their parent property's ``property_workspace``
  rows; they do not carry ``workspace_id`` columns themselves. A
  naive auto-inject on such a table would crash in the ORM filter
  (no column to filter on). The service layer is responsible for
  joining ``unit`` / ``area`` / ``property_closure`` → ``property`` →
  ``property_workspace`` for tenant isolation.

  Extending the filter to handle "scoped-but-no-workspace_id-column"
  tables via a mandatory join through the junction is tracked in a
  follow-up Beads task (``cd-8u5`` domain service will thread the
  join; a later filter-side enhancement will make the guarantee
  automatic). For v1 we fail closed by policy, not by automation:
  any bare SELECT against these three tables without a join is a bug
  the service-layer review must catch.

See ``docs/specs/02-domain-model.md`` §"property_workspace",
``docs/specs/04-properties-and-stays.md`` §"Property" / §"Unit" /
§"Area", ``docs/specs/05-employees-and-roles.md`` §"Property work
role assignment", and ``docs/specs/01-architecture.md`` §"Tenant
filter enforcement".
"""

from __future__ import annotations

from app.adapters.db.places.models import (
    Area,
    Property,
    PropertyClosure,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
    Unit,
)
from app.tenancy.registry import register

register("property_workspace")
register("property_work_role_assignment")
# ``property`` is intentionally NOT registered — a property can belong
# to multiple workspaces via ``property_workspace``. See module
# docstring for the full rationale; unit / area / property_closure
# follow the same "service-layer joins the junction" contract.

__all__ = [
    "Area",
    "Property",
    "PropertyClosure",
    "PropertyWorkRoleAssignment",
    "PropertyWorkspace",
    "Unit",
]
