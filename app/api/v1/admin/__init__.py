"""Workspace-scoped admin surface aggregator.

This router is the reserved seat for future workspace-scoped admin
views that don't fit any single bounded context. Signup abuse
surfacing is **not** here: cd-1h7k resolved `/admin/signups` as a
deployment-admin surface because pre-workspace signup signals have no
workspace to scope to.

Mounted by :mod:`app.api.factory` via :data:`app.api.v1.CONTEXT_ROUTERS`
under the workspace prefix, so every route lands at
``/w/<slug>/api/v1/admin/...``. The tenancy middleware resolves the
active :class:`~app.tenancy.WorkspaceContext` from the ``<slug>``
segment before any handler runs — admin endpoints therefore always
operate on a concrete workspace, never on the bare host.

**Not the deployment-scoped admin tree.** :mod:`app.api.admin`
(``/admin/api/v1/*``) is a separate, deployment-operator surface
gated on ``(scope_kind='deployment', grant_role='manager')``. The two
trees never overlap: the deployment admin mounts LLM provider
config, cross-workspace usage, deployment-wide audit, and signup
abuse signals; this workspace admin seat remains available for
future per-workspace security or health views.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations", ``docs/specs/12-rest-api.md`` §"Base URL", and
``docs/specs/13-cli.md`` §"CLI surface".
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["workspace_admin"])

__all__ = ["router"]
