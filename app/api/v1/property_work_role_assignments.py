"""Property-work-role-assignments HTTP router (spec §12).

Mounted inside ``/w/<slug>/api/v1`` by the app factory. Surface:

```
GET    /property_work_role_assignments      # ?property_id=…&user_work_role_id=…
POST   /property_work_role_assignments
PATCH  /property_work_role_assignments/{id}
DELETE /property_work_role_assignments/{id}
```

Every route requires an active :class:`~app.tenancy.WorkspaceContext`
and tags ``identity`` + ``property_work_role_assignments``. All
operations gate on ``work_roles.manage`` at workspace scope (§05
action catalog default-allow: ``owners, managers``) — the listing
included, because per-property pinning is roster information that
shouldn't fan out to every grant role.

The router is a thin DTO passthrough over the domain service in
:mod:`app.domain.places.property_work_role_assignments`. Two error
mappings carry weight:

* The service's ``...InvariantViolated`` exception → 422 for
  cross-workspace borrows / unreachable property, **409** for
  duplicate-active rows. The duplicate is the only one we care to
  distinguish on the wire because the SPA's "pin a role to a property"
  affordance must show a different toast than "this row references a
  property you don't operate".
* The service's ``...NotFound`` exception → 404, matching the §01
  "tenant surface is not enumerable" stance.

See ``docs/specs/05-employees-and-roles.md`` §"Property work role
assignment", ``docs/specs/12-rest-api.md`` §"Users / work roles /
settings", ``docs/specs/02-domain-model.md``
§"property_work_role_assignment".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz import Permission
from app.domain.places.property_work_role_assignments import (
    PropertyWorkRoleAssignmentCreate,
    PropertyWorkRoleAssignmentInvariantViolated,
    PropertyWorkRoleAssignmentNotFound,
    PropertyWorkRoleAssignmentUpdate,
    PropertyWorkRoleAssignmentView,
    create_property_work_role_assignment,
    delete_property_work_role_assignment,
    list_property_work_role_assignments,
    update_property_work_role_assignment,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "PropertyWorkRoleAssignmentCreateRequest",
    "PropertyWorkRoleAssignmentListResponse",
    "PropertyWorkRoleAssignmentResponse",
    "PropertyWorkRoleAssignmentUpdateRequest",
    "build_property_work_role_assignments_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


_MAX_ID_LEN = 64


class PropertyWorkRoleAssignmentCreateRequest(BaseModel):
    """Request body for ``POST /property_work_role_assignments``.

    ``workspace_id`` is **deliberately absent** — the service derives
    it from the :class:`WorkspaceContext` so a malicious / buggy
    caller cannot pin a role to a workspace they do not operate.
    """

    model_config = ConfigDict(extra="forbid")

    user_work_role_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN)
    property_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN)
    schedule_ruleset_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    property_pay_rule_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)


class PropertyWorkRoleAssignmentUpdateRequest(BaseModel):
    """Request body for ``PATCH /property_work_role_assignments/{id}``.

    Only ``schedule_ruleset_id`` and ``property_pay_rule_id`` are
    mutable per §05 "Property work role assignment" — mutating the
    identity columns (``user_work_role_id``, ``property_id``) requires
    a delete + re-create flow that the UI surfaces separately.
    """

    model_config = ConfigDict(extra="forbid")

    schedule_ruleset_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    property_pay_rule_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)


class PropertyWorkRoleAssignmentResponse(BaseModel):
    """Response shape for property-work-role-assignment operations."""

    id: str
    workspace_id: str
    user_work_role_id: str
    property_id: str
    schedule_ruleset_id: str | None
    property_pay_rule_id: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class PropertyWorkRoleAssignmentListResponse(BaseModel):
    """Collection envelope for ``GET /property_work_role_assignments``.

    Shape matches §12 "Pagination" verbatim — ``{data, next_cursor,
    has_more}``.
    """

    data: list[PropertyWorkRoleAssignmentResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Query dependencies
# ---------------------------------------------------------------------------


_PropertyIdFilter = Annotated[
    str | None,
    Query(
        max_length=_MAX_ID_LEN,
        description=(
            "Narrow the listing to a single property. Combine with "
            "``user_work_role_id`` to fetch the live row for a "
            "specific (role, property) pair."
        ),
    ),
]


_UserWorkRoleIdFilter = Annotated[
    str | None,
    Query(
        max_length=_MAX_ID_LEN,
        description=(
            "Narrow the listing to one user_work_role. Useful when "
            "rendering the per-employee 'where do they work' panel."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_response(
    view: PropertyWorkRoleAssignmentView,
) -> PropertyWorkRoleAssignmentResponse:
    return PropertyWorkRoleAssignmentResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        user_work_role_id=view.user_work_role_id,
        property_id=view.property_id,
        schedule_ruleset_id=view.schedule_ruleset_id,
        property_pay_rule_id=view.property_pay_rule_id,
        created_at=view.created_at,
        updated_at=view.updated_at,
        deleted_at=view.deleted_at,
    )


def _http_for_invariant(
    exc: PropertyWorkRoleAssignmentInvariantViolated,
) -> HTTPException:
    """Translate an invariant violation into 409 (duplicate) or 422 (other).

    The duplicate flavour fires from the partial UNIQUE on
    ``(user_work_role_id, property_id) WHERE deleted_at IS NULL``;
    we sniff the message text rather than minting a separate error
    type because the service collapses the IntegrityError into the
    same exception class. Matching the ``"already exists"`` substring
    keeps the surface stable without a brittle ``isinstance`` chain
    (the message is asserted in the service tests).
    """
    if "already exists" in str(exc):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "property_work_role_assignment_duplicate",
                "message": str(exc),
            },
        )
    return HTTPException(
        status_code=422,
        detail={
            "error": "property_work_role_assignment_invariant",
            "message": str(exc),
        },
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "property_work_role_assignment_not_found"},
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_property_work_role_assignments_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the CRUD surface."""
    api = APIRouter(
        prefix="/property_work_role_assignments",
        tags=["identity", "property_work_role_assignments"],
    )

    manage_gate = Depends(Permission("work_roles.manage", scope_kind="workspace"))

    @api.get(
        "",
        response_model=PropertyWorkRoleAssignmentListResponse,
        operation_id="property_work_role_assignments.list",
        summary="List property-work-role assignments in the caller's workspace",
        dependencies=[manage_gate],
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
        property_id: _PropertyIdFilter = None,
        user_work_role_id: _UserWorkRoleIdFilter = None,
    ) -> PropertyWorkRoleAssignmentListResponse:
        """Cursor-paginated listing with optional ``property_id`` /
        ``user_work_role_id`` filters."""
        after_id = decode_cursor(cursor)
        views = list_property_work_role_assignments(
            session,
            ctx,
            limit=limit,
            after_id=after_id,
            property_id=property_id,
            user_work_role_id=user_work_role_id,
        )
        page = paginate(
            views,
            limit=limit,
            key_getter=lambda v: v.id,
        )
        return PropertyWorkRoleAssignmentListResponse(
            data=[_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=PropertyWorkRoleAssignmentResponse,
        operation_id="property_work_role_assignments.create",
        summary="Pin a user_work_role to a property",
        dependencies=[manage_gate],
    )
    def create(
        body: PropertyWorkRoleAssignmentCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PropertyWorkRoleAssignmentResponse:
        """Insert a new property_work_role_assignment row.

        Runs the §02 / §05 invariants before flush; a duplicate
        (live row already present for the same ``(role, property)``
        pair) collapses into 409, every other invariant violation
        into 422.
        """
        service_body = PropertyWorkRoleAssignmentCreate.model_validate(
            body.model_dump()
        )
        try:
            view = create_property_work_role_assignment(session, ctx, body=service_body)
        except PropertyWorkRoleAssignmentInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.patch(
        "/{assignment_id}",
        response_model=PropertyWorkRoleAssignmentResponse,
        operation_id="property_work_role_assignments.update",
        summary="Partial update of a property_work_role_assignment",
        dependencies=[manage_gate],
    )
    def update(
        assignment_id: str,
        body: PropertyWorkRoleAssignmentUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PropertyWorkRoleAssignmentResponse:
        """Update the mutable pointer fields."""
        sent = body.model_fields_set
        service_body = PropertyWorkRoleAssignmentUpdate.model_validate(
            {f: getattr(body, f) for f in sent}
        )
        try:
            view = update_property_work_role_assignment(
                session, ctx, assignment_id=assignment_id, body=service_body
            )
        except PropertyWorkRoleAssignmentNotFound as exc:
            raise _http_for_not_found() from exc
        except PropertyWorkRoleAssignmentInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.delete(
        "/{assignment_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="property_work_role_assignments.delete",
        summary="Soft-delete a property_work_role_assignment",
        dependencies=[manage_gate],
    )
    def delete(
        assignment_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Stamp ``deleted_at``; no response body per §12 "Deletion"."""
        try:
            delete_property_work_role_assignment(
                session, ctx, assignment_id=assignment_id
            )
        except PropertyWorkRoleAssignmentNotFound as exc:
            raise _http_for_not_found() from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


router = build_property_work_role_assignments_router()
