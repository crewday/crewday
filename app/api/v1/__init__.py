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
It is not one of the §01 13 bounded contexts, so folding it into the
context map would dilute that invariant and add a phantom ``admin``
tag to the OpenAPI seed. The factory mounts it directly at
``/w/{slug}/api/v1/admin/*`` for future workspace-scoped admin
views; signup abuse surfacing lives on the deployment-admin tree
after cd-1h7k.

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
from .assets import asset_types_alias_router, assets_alias_router, documents_router
from .assets import router as assets_router
from .assets import scan_router as asset_scan_router
from .billing import public_router as billing_public_router
from .billing import router as billing_router
from .expenses import router as expenses_router
from .identity import router as identity_router
from .instructions import router as instructions_router
from .inventory import router as inventory_router
from .inventory import stocktakes_router as _inventory_stocktakes_router
from .issues import router as issues_router
from .llm import router as llm_router
from .messaging import router as messaging_router
from .payroll import router as payroll_router
from .places import router as places_router
from .stays import public_router as stays_public_router
from .stays import router as stays_router
from .tasks import router as tasks_router
from .time import router as time_router
from .webhooks import router as webhooks_router

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

# Workspace-scoped admin aggregator — currently empty after cd-1h7k
# moved signup abuse surfacing to the deployment-admin tree. Kept
# outside :data:`CONTEXT_ROUTERS` so the 13-context invariant survives
# and the future OpenAPI tag seed remains exactly the §01 contexts.
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
ISSUES_ROUTER: APIRouter = issues_router
WEBHOOKS_ROUTER: APIRouter = webhooks_router
INVENTORY_STOCKTAKES_ROUTER: APIRouter = _inventory_stocktakes_router

STAYS_PUBLIC_ROUTER: APIRouter = stays_public_router
BILLING_PUBLIC_ROUTER: APIRouter = billing_public_router
DOCUMENTS_ROUTER: APIRouter = documents_router
ASSET_TYPES_ALIAS_ROUTER: APIRouter = asset_types_alias_router
ASSETS_ALIAS_ROUTER: APIRouter = assets_alias_router

__all__ = [
    "APPROVALS_ROUTER",
    "ASSETS_ALIAS_ROUTER",
    "ASSET_TYPES_ALIAS_ROUTER",
    "BILLING_PUBLIC_ROUTER",
    "CONTEXT_ROUTERS",
    "DOCUMENTS_ROUTER",
    "INVENTORY_STOCKTAKES_ROUTER",
    "ISSUES_ROUTER",
    "STAYS_PUBLIC_ROUTER",
    "WEBHOOKS_ROUTER",
    "WORKSPACE_ADMIN_ROUTER",
    "asset_scan_router",
]
