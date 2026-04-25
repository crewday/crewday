"""Deployment-scoped admin API tree.

Exposes :data:`admin_router` — the :class:`APIRouter` the app
factory mounts under ``/admin/api/v1`` (spec §12 "Base URL",
"Admin surface"). Real admin routes attach their handlers to this
router; today the wired surface is the cd-yj4k minimum:

* ``GET /admin/api/v1/me`` — caller identity + capabilities;
* ``GET /admin/api/v1/me/admins`` — deployment admin team listing.

Both routes live in :mod:`app.api.admin.me`. Subsequent admin
families (LLM graph, usage, workspace lifecycle, signup, audit, …
per spec §12 "Admin surface") will add their own routers and
register them here.

Authorisation lives on the per-route deps. Every route gates on
:func:`app.api.admin.deps.current_deployment_admin_principal`
(or its :func:`require_deployment_scope` companion) so a caller
without an active ``scope_kind='deployment'`` ``role_grant`` row
or a deployment-scoped API token receives ``404`` —
**not** ``403`` — per spec §12: "the surface does not advertise
its own existence to tenants".

See ``docs/specs/12-rest-api.md`` §"Admin surface".
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.admin.me import build_admin_me_router

__all__ = ["admin_router"]


admin_router = APIRouter(tags=["admin"])
admin_router.include_router(build_admin_me_router())
