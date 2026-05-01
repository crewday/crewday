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
* ``unit`` / ``area`` / ``property_closure``: registered as
  **scope-through-join** tables (cd-014h). They have no
  ``workspace_id`` column of their own, but the workspace boundary
  is enforced by joining through ``property_workspace`` on
  ``property_id``. The ORM tenant filter either accepts a query
  that already joins the junction with a matching ``workspace_id``
  predicate or auto-injects an
  ``IN (SELECT property_id FROM property_workspace WHERE
  workspace_id = :ctx)`` filter; bare reads without a
  :class:`~app.tenancy.WorkspaceContext` still raise
  :class:`~app.tenancy.orm_filter.TenantFilterMissing`. UPDATE /
  DELETE on these tables fail closed — services thread the
  predicate by hand or wrap in
  :func:`~app.tenancy.current.tenant_agnostic`.

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
from app.tenancy.registry import register, register_scope_through_join

register("property_workspace")
register("property_work_role_assignment")
# ``property`` is intentionally NOT registered — a property can belong
# to multiple workspaces via ``property_workspace``. See module
# docstring for the full rationale.

# ``unit`` / ``area`` / ``property_closure`` reach the workspace
# boundary through ``property_workspace`` on ``property_id``. The
# ORM tenant filter (cd-014h) verifies the junction is joined with
# a matching ``workspace_id`` predicate, otherwise it auto-injects
# the ``IN (SELECT ... FROM property_workspace WHERE workspace_id =
# :ctx)`` equivalent.
for _scoped_through_property in ("unit", "area", "property_closure"):
    register_scope_through_join(
        _scoped_through_property,
        via_table="property_workspace",
        via_local_column="property_id",
        via_remote_column="property_id",
    )
del _scoped_through_property

__all__ = [
    "Area",
    "Property",
    "PropertyClosure",
    "PropertyWorkRoleAssignment",
    "PropertyWorkspace",
    "Unit",
]
