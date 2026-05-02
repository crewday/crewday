"""Aggregate router for the tasks context.

The bare list / create routes register on this top-level router with
``path=""`` so they resolve at exactly ``/w/<slug>/api/v1/tasks`` (no
trailing slash) — the path the SPA fetches. FastAPI rejects an empty
inner ``path`` once the route has been routed through nested
``include_router`` calls, so they cannot live on
:mod:`occurrences` (which is included via ``router.include_router``).
The handler bodies still live in :mod:`occurrences` to keep the
domain-shaped module cohesive; ``router.py`` only carries the
decorator wiring.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from .comments import router as comments_router
from .evidence import router as evidence_router
from .nl import router as nl_router
from .occurrences import (
    create_task_route as _create_task_route,
)
from .occurrences import (
    list_tasks_route as _list_tasks_route,
)
from .occurrences import (
    router as occurrences_router,
)
from .payloads import TaskListResponse, TaskPayload
from .schedules import router as schedules_router
from .templates import router as templates_router

router = APIRouter(tags=["tasks"])

# List / create at the bare ``/w/<slug>/api/v1/tasks`` path. Using
# ``path=""`` only works when the route is attached directly to the
# outer router that the factory mounts at ``/w/{slug}/api/v1/tasks``;
# FastAPI raises "Prefix and path cannot be both empty" when an inner
# router included via ``include_router`` tries to do the same. The
# handler bodies live in :mod:`occurrences`; the decorators here
# re-register them so the route resolves no-slash without a Starlette
# slash redirect (the SPA catch-all matches first in the production
# router order, suppressing the redirect).
router.get(
    "",
    response_model=TaskListResponse,
    operation_id="list_tasks",
    summary="List occurrences (tasks) with filters",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "list"}},
)(_list_tasks_route)
router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=TaskPayload,
    operation_id="create_task",
    summary="Create a one-off task",
)(_create_task_route)

router.include_router(templates_router)
router.include_router(schedules_router)
router.include_router(occurrences_router)
router.include_router(nl_router)
router.include_router(comments_router)
router.include_router(evidence_router)

__all__ = ["router"]
