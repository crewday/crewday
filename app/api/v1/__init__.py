"""REST API v1 routers — one thin router per bounded context.

The :data:`CONTEXT_ROUTERS` registry is the single seam between the
app factory and the 13 bounded-context routers. Order matches the
§01 "Context map" table, which is also the stable tag order rendered
in the merged OpenAPI document.

Explicit registration (over autoload) keeps import-linter happy and
makes it impossible for a new router to appear in the OpenAPI
surface without an explicit line here — i.e. without a reviewer
noticing.

The workspace-scoped admin aggregator (:data:`WORKSPACE_ADMIN_ROUTER`)
is exported **alongside** — not inside — :data:`CONTEXT_ROUTERS`.
It is not one of the §01 13 bounded contexts (it aggregates
owner/manager read-only surfaces spanning multiple contexts), so
folding it into the context map would dilute that invariant and
add a phantom ``admin`` tag to the OpenAPI seed. The factory
mounts it directly at ``/w/{slug}/api/v1/admin/*``.

See ``docs/specs/01-architecture.md`` §"Context map",
``docs/specs/12-rest-api.md`` §"Base URL", and
``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations" for the admin aggregator scope.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from .admin import router as _workspace_admin_router
from .approvals import router as _approvals_router
from .assets import router as assets_router
from .billing import router as billing_router
from .expenses import router as expenses_router
from .identity import router as identity_router
from .instructions import router as instructions_router
from .inventory import router as inventory_router
from .llm import router as llm_router
from .messaging import router as messaging_router
from .payroll import router as payroll_router
from .places import router as places_router
from .stays import router as stays_router
from .tasks import router as tasks_router
from .time import router as time_router

# Ordered registry of (context_name, router). The name is the tag
# key used in the merged OpenAPI doc and the URL segment under
# ``/w/<slug>/api/v1/<context>``; order matches the spec §01
# "Context map" table so the generated OpenAPI has a predictable,
# reviewable shape.
CONTEXT_ROUTERS: Sequence[tuple[str, APIRouter]] = (
    ("identity", identity_router),
    ("places", places_router),
    ("tasks", tasks_router),
    ("stays", stays_router),
    ("instructions", instructions_router),
    ("inventory", inventory_router),
    ("assets", assets_router),
    ("time", time_router),
    ("payroll", payroll_router),
    ("expenses", expenses_router),
    ("billing", billing_router),
    ("messaging", messaging_router),
    ("llm", llm_router),
)

# Workspace-scoped admin aggregator — owner/manager-only cross-context
# surfaces (abuse signals, security posture, workspace health). Kept
# outside :data:`CONTEXT_ROUTERS` so the 13-context invariant survives
# and the OpenAPI tag seed remains exactly the §01 contexts. The
# router tags its operations ``workspace_admin`` (not ``admin``) to
# avoid colliding with the deployment-admin tree's tag; see the
# :mod:`app.api.v1.admin` module docstring for the full rationale.
WORKSPACE_ADMIN_ROUTER: APIRouter = _workspace_admin_router

# Approvals consumer — HITL desk + inline approval HTTP surface.
# Mounted as a sibling of :data:`CONTEXT_ROUTERS` (NOT inside it) so
# the §12 path contract — ``GET /approvals``, ``POST
# /approvals/{id}/approve``, ``POST /approvals/{id}/reject`` — lands
# at the bare path the desk + inline-card SPA fetches expect, not
# nested under ``/llm/approvals``. The router is conceptually owned
# by the LLM context but mounting it under ``/llm`` would dilute the
# bare-path contract; mounting it inside :data:`CONTEXT_ROUTERS` as
# its own context would dilute the §01 13-context invariant. Same
# rationale as :data:`WORKSPACE_ADMIN_ROUTER`. The router tags its
# operations ``approvals`` so the OpenAPI tag list keeps it
# co-located with the LLM context's operations alphabetically.
APPROVALS_ROUTER: APIRouter = _approvals_router

__all__ = ["APPROVALS_ROUTER", "CONTEXT_ROUTERS", "WORKSPACE_ADMIN_ROUTER"]
