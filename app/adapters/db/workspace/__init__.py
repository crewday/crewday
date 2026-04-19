"""workspace + user_workspace tables.

Importing this package:

* Registers ``user_workspace`` as a workspace-scoped table (so the ORM
  tenant filter auto-injects a ``workspace_id`` predicate on every
  SELECT / UPDATE / DELETE against it — see
  :mod:`app.tenancy.orm_filter`).
* Does **not** register ``workspace``. The slug→id resolver in the
  signup + request middleware has to scan this table *before* any
  :class:`~app.tenancy.WorkspaceContext` exists, so the table is
  tenant-agnostic by design (see
  ``docs/specs/01-architecture.md`` §"Workspace addressing").

See ``docs/specs/02-domain-model.md`` §"workspaces" and
§"user_workspace".
"""

from __future__ import annotations

from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.tenancy.registry import register

register("user_workspace")
# ``workspace`` is intentionally NOT registered — the tenancy anchor is
# tenant-agnostic by design (slug lookup before ctx exists).

__all__ = ["UserWorkspace", "Workspace"]
