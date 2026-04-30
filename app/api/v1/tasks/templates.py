"""Task template routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.domain.tasks.templates import (
    ScopeInconsistent,
    TaskTemplateCreate,
    TaskTemplateNotFound,
    TaskTemplateUpdate,
    TemplateInUseError,
    list_templates,
)
from app.domain.tasks.templates import create as create_template
from app.domain.tasks.templates import delete as delete_template
from app.domain.tasks.templates import read as read_template
from app.domain.tasks.templates import update as update_template

from .deps import _Ctx, _Db
from .errors import _http_for_template_mutation, _template_not_found
from .payloads import TaskTemplateListResponse, TaskTemplatePayload

router = APIRouter()


@router.get(
    "/task_templates",
    response_model=TaskTemplateListResponse,
    operation_id="list_task_templates",
    summary="List task templates in the caller's workspace",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "templates-list"}},
)
def list_task_templates_route(
    ctx: _Ctx,
    session: _Db,
    q: Annotated[str | None, Query(max_length=200)] = None,
    role_id: Annotated[str | None, Query(max_length=64)] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> TaskTemplateListResponse:
    """Return a cursor-paginated page of live templates."""
    after_id = decode_cursor(cursor)
    # The service returns every row ordered (created_at, id); we pull
    # the whole set and slice client-side. Workspaces with >500
    # templates are not a realistic v1 shape; cd-template-pagination
    # tracks the proper DB-side cursor when that changes.
    views = list(list_templates(session, ctx, q=q, role_id=role_id))
    if after_id is not None:
        views = [v for v in views if v.id > after_id]
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    return TaskTemplateListResponse(
        data=[TaskTemplatePayload.from_view(v) for v in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post(
    "/task_templates",
    status_code=status.HTTP_201_CREATED,
    response_model=TaskTemplatePayload,
    operation_id="create_task_template",
    summary="Create a task template",
)
def create_task_template_route(
    body: TaskTemplateCreate,
    ctx: _Ctx,
    session: _Db,
) -> TaskTemplatePayload:
    """Insert a fresh template row."""
    try:
        view = create_template(session, ctx, body=body)
    except ScopeInconsistent as exc:
        raise _http_for_template_mutation(exc) from exc
    return TaskTemplatePayload.from_view(view)


@router.get(
    "/task_templates/{template_id}",
    response_model=TaskTemplatePayload,
    operation_id="get_task_template",
    summary="Read a task template",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "template-show"}},
)
def get_task_template_route(
    template_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskTemplatePayload:
    """Return the template identified by ``template_id``."""
    try:
        view = read_template(session, ctx, template_id=template_id)
    except TaskTemplateNotFound as exc:
        raise _template_not_found() from exc
    return TaskTemplatePayload.from_view(view)


@router.patch(
    "/task_templates/{template_id}",
    response_model=TaskTemplatePayload,
    operation_id="update_task_template",
    summary="Replace the mutable body of a task template",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "template-update"}},
)
def patch_task_template_route(
    template_id: str,
    body: TaskTemplateUpdate,
    ctx: _Ctx,
    session: _Db,
) -> TaskTemplatePayload:
    """PATCH = full-body replace per the v1 template contract."""
    try:
        view = update_template(session, ctx, template_id=template_id, body=body)
    except (TaskTemplateNotFound, ScopeInconsistent) as exc:
        raise _http_for_template_mutation(exc) from exc
    return TaskTemplatePayload.from_view(view)


@router.delete(
    "/task_templates/{template_id}",
    response_model=TaskTemplatePayload,
    operation_id="delete_task_template",
    summary="Soft-delete a task template",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "template-delete"}},
)
def delete_task_template_route(
    template_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskTemplatePayload:
    """Soft-delete; 409 ``template_in_use`` when consumers remain."""
    try:
        view = delete_template(session, ctx, template_id=template_id)
    except (TaskTemplateNotFound, TemplateInUseError) as exc:
        raise _http_for_template_mutation(exc) from exc
    return TaskTemplatePayload.from_view(view)
