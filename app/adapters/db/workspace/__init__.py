"""workspace + user_workspace + work_role + user_work_role + work_engagement tables.

Importing this package:

* Registers ``user_workspace``, ``work_role``, ``user_work_role``,
  and ``work_engagement`` as workspace-scoped tables (so the ORM
  tenant filter auto-injects a ``workspace_id`` predicate on every
  SELECT / UPDATE / DELETE against them — see
  :mod:`app.tenancy.orm_filter`).
* Does **not** register ``workspace``. The slug→id resolver in the
  signup + request middleware has to scan this table *before* any
  :class:`~app.tenancy.WorkspaceContext` exists, so the table is
  tenant-agnostic by design (see
  ``docs/specs/01-architecture.md`` §"Workspace addressing").

See ``docs/specs/02-domain-model.md`` §"workspaces",
§"user_workspace", §"work_engagement";
``docs/specs/05-employees-and-roles.md`` §"Work role" / §"User work
role" / §"Work engagement".
"""

from __future__ import annotations

from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
    WorkRole,
    Workspace,
)
from app.tenancy.registry import register

# ``workspace`` is intentionally NOT registered — the tenancy anchor
# is tenant-agnostic by design (slug lookup before ctx exists).
for _table in (
    "user_workspace",
    "work_role",
    "user_work_role",
    "work_engagement",
):
    register(_table)

__all__ = [
    "UserWorkRole",
    "UserWorkspace",
    "WorkEngagement",
    "WorkRole",
    "Workspace",
]
