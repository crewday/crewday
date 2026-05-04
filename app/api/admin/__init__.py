"""Deployment-scoped admin API tree.

Exposes :data:`admin_router` — the :class:`APIRouter` the app
factory mounts under ``/admin/api/v1`` (spec §12 "Base URL",
"Admin surface"). Real admin routes attach their handlers to this
router; the wired surface today is:

* :mod:`app.api.admin.me` — caller identity + admin team listing
  (cd-yj4k).
* :mod:`app.api.admin.workspaces` — workspace lifecycle:
  ``GET /workspaces`` / ``GET /workspaces/{id}`` /
  ``POST /workspaces/{id}/trust`` / ``POST /workspaces/{id}/archive``.
* :mod:`app.api.admin.signup` — self-serve signup settings:
  ``GET /signup/settings`` / ``PUT /signup/settings``.
* :mod:`app.api.admin.signups` — deployment signup-abuse feed:
  ``GET /signups``.
* :mod:`app.api.admin.settings` — deployment settings:
  ``GET /settings`` / ``PUT /settings/{key}``.
* :mod:`app.api.admin.admins` — admin team CRUD + groups:
  ``GET /admins`` / ``POST /admins`` / ``POST /admins/{id}/revoke``
  + ``GET /admins/groups`` + owners-group add / revoke.
* :mod:`app.api.admin.audit` — deployment audit feed:
  ``GET /audit`` + ``GET /audit/tail``.
* :mod:`app.api.admin.usage` — usage aggregates:
  ``GET /usage/summary`` / ``GET /usage/workspaces`` /
  ``PUT /usage/workspaces/{id}/cap``.
* :mod:`app.api.admin.chat_gateway` — deployment-default chat gateway
  status: ``GET /chat/providers`` / ``GET /chat/templates`` /
  ``GET /chat/overrides`` / ``GET /chat/health``.
* :mod:`app.api.admin.agent_docs` — system docs read by chat agents:
  ``GET /agent_docs`` / ``GET /agent_docs/{slug}``.

Subsequent admin families (LLM graph, admin chat agent — per spec
§12 "Admin surface") will add their own routers and register them
here.

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

from app.api.admin.admins import build_admin_admins_router
from app.api.admin.agent_docs import build_admin_agent_docs_router
from app.api.admin.audit import build_admin_audit_router
from app.api.admin.chat_gateway import build_admin_chat_gateway_router
from app.api.admin.llm import build_admin_llm_router
from app.api.admin.me import build_admin_me_router
from app.api.admin.settings import build_admin_settings_router
from app.api.admin.signup import build_admin_signup_router
from app.api.admin.signups import build_admin_signups_router
from app.api.admin.usage import build_admin_usage_router
from app.api.admin.workspaces import build_admin_workspaces_router
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES

__all__ = ["admin_router"]


admin_router = APIRouter(tags=["admin"], responses=IDENTITY_PROBLEM_RESPONSES)
admin_router.include_router(build_admin_me_router())
admin_router.include_router(build_admin_workspaces_router())
admin_router.include_router(build_admin_signup_router())
admin_router.include_router(build_admin_signups_router())
admin_router.include_router(build_admin_settings_router())
admin_router.include_router(build_admin_admins_router())
admin_router.include_router(build_admin_audit_router())
admin_router.include_router(build_admin_usage_router())
admin_router.include_router(build_admin_chat_gateway_router())
admin_router.include_router(build_admin_agent_docs_router())
admin_router.include_router(build_admin_llm_router())
