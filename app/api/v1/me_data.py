"""Bearer-authenticated PAT-reachable ``/me`` self-service data routes (§03).

Mounted inside ``/w/<slug>/api/v1`` by the app factory. These are the
routes a **personal access token** (PAT) can actually exercise: the
tenancy middleware confines a PAT (``token_kind == "personal"``) to the
``/w/<slug>/api/v1/me`` subtree (§03 "Personal access tokens"), and this
module is where the documented ``me.*`` scopes finally reach a handler.

Every route here is:

* **Bearer-reachable.** It resolves the caller from
  :func:`app.api.deps.current_workspace_context` — the live
  :class:`WorkspaceContext` the middleware builds from either a passkey
  session cookie **or** a bearer token — so a PAT works end-to-end, not
  just a cookie session (which the bare-host ``/api/v1/me*`` routes are
  limited to).
* **Self-keyed on ``ctx.actor_id``.** For a PAT the middleware pins
  ``ctx.actor_id`` to the token's ``subject_user_id`` (mint sets
  ``user_id == subject_user_id``), so forcing every query onto
  ``ctx.actor_id`` is the structural §03 "subject-row filter applied at
  query time regardless of scope string": a PAT can only ever read/write
  its own subject's rows, never another user's. No route accepts a
  ``user_id`` from the caller.
* **``me.*``-scope gated** via :func:`app.authz.dep.MeScope`. A PAT
  minted with ``me.tasks:read`` reaches ``GET /me/tasks`` but 403s
  (``insufficient_scope``) on ``GET /me/expenses``; ``me.expenses:read``
  cannot POST. Sessions / demo / system / delegated principals fall
  through the scope gate unchecked (their self-service authority is
  unchanged) — see :func:`app.authz.enforce.require_me_scope`.

The handlers deliberately **reuse** the existing workspace surfaces
rather than re-implement the queries:

* tasks → :func:`app.api.v1.tasks.occurrences.list_tasks_route` with
  ``assignee_user_id = ctx.actor_id`` (single-sources the task
  projection + personal-task visibility gate + pagination);
* expenses → :func:`app.api.v1.expenses.list_expense_claims_route`
  (``mine=True``) and :func:`app.api.v1.expenses.create_expense_claim_route`
  (create binds the claim to the caller's own engagement);
* profile → :func:`app.services.employees.update_profile` (its self-edit
  branch is un-gated when ``ctx.actor_id == user_id``).

See ``docs/specs/03-auth-and-tokens.md`` §"Personal access tokens" /
§"Scopes" / §"Usage".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.workspace.repositories import SqlAlchemyMembershipRepository
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import DEFAULT_LIMIT, LimitQuery, PageCursorQuery
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.api.v1.expenses import (
    ExpenseClaimListResponse,
    ExpenseClaimPayload,
    create_expense_claim_route,
    list_expense_claims_route,
)
from app.api.v1.tasks.occurrences import _OccurrenceState, list_tasks_route
from app.api.v1.tasks.payloads import TaskListResponse
from app.authz.dep import MeScope
from app.domain.errors import Forbidden, NotFound
from app.domain.expenses.claims import ExpenseClaimCreate
from app.services.employees import (
    EmployeeNotFound,
    EmployeeProfileUpdate,
    EmployeeView,
    ProfileFieldForbidden,
    update_profile,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "MeProfileUpdateRequest",
    "MeSelfProfileResponse",
    "build_me_data_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Profile wire shapes
# ---------------------------------------------------------------------------


class MeSelfProfileResponse(BaseModel):
    """The caller's own identity row, limited to the self-editable fields.

    Deliberately distinct from
    :class:`app.api.v1.users.EmployeeProfileResponse` so the ``me.*``
    self-service surface can evolve independently of the manager
    ``/users`` surface (the same per-surface-DTO convention the
    workspace switcher follows). Named ``MeSelfProfileResponse`` rather
    than ``MeProfileResponse`` to avoid colliding with the unrelated
    app-shell payload :class:`app.api.v1.auth.me.MeProfileResponse` — a
    duplicate class name would module-qualify **both** schema names in
    the OpenAPI document, churning the existing ``GET /api/v1/me`` schema
    ref. The field set is the §03 "me.profile"
    subset — display name, language (``locale``), timezone, and avatar
    — projectable from both the :class:`User` row (read) and an
    :class:`EmployeeView` (post-update).
    """

    id: str
    email: str
    display_name: str
    locale: str | None
    timezone: str | None
    avatar_blob_hash: str | None


class MeProfileUpdateRequest(BaseModel):
    """Request body for ``PATCH /me/profile``.

    Mirrors :class:`app.api.v1.users.EmployeeUpdateRequest` but is the
    self-service (``me.profile:write``) shape: no ``user_id`` — the
    subject is always ``ctx.actor_id``. §03 "Scopes" limits
    ``me.profile:write`` to "the fields the worker surface already lets
    them self-update (display name, avatar, timezone, ... language)";
    the writable scalar columns on :class:`User` are ``display_name``,
    ``locale``, and ``timezone`` (avatar is a separate binary-upload
    surface, ``/api/v1/me/avatar``). ``extra="forbid"`` so an unknown
    field — or a smuggled ``user_id`` — fails loud at 422 rather than
    being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    locale: str | None = Field(default=None, max_length=35)
    timezone: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _reject_display_name_null(self) -> MeProfileUpdateRequest:
        """Reject an explicit ``display_name=None`` — the column is NOT NULL."""
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("display_name cannot be cleared; it is NOT NULL")
        return self


def _user_to_me_profile(user: User) -> MeSelfProfileResponse:
    return MeSelfProfileResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        locale=user.locale,
        timezone=user.timezone,
        avatar_blob_hash=user.avatar_blob_hash,
    )


def _view_to_me_profile(view: EmployeeView) -> MeSelfProfileResponse:
    return MeSelfProfileResponse(
        id=view.id,
        email=view.email,
        display_name=view.display_name,
        locale=view.locale,
        timezone=view.timezone,
        avatar_blob_hash=view.avatar_blob_hash,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_me_data_router() -> APIRouter:
    """Return the PAT-reachable ``/me`` self-service data router."""
    router = APIRouter(
        prefix="/me",
        tags=["identity", "me"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    @router.get(
        "/tasks",
        response_model=TaskListResponse,
        operation_id="me.tasks.list",
        summary="List tasks assigned to the caller (self-only)",
        dependencies=[Depends(MeScope("me.tasks:read"))],
        openapi_extra={"x-cli": {"group": "me", "verb": "tasks"}},
    )
    def list_my_tasks(
        ctx: _Ctx,
        session: _Db,
        state: Annotated[_OccurrenceState | None, Query()] = None,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> TaskListResponse:
        """Tasks assigned to the caller's subject, newest-cursor paginated.

        Forces ``assignee_user_id = ctx.actor_id`` before delegating to
        the shared workspace list route, so a PAT only ever sees its own
        subject's tasks (the "unassigned tasks matching the subject's
        ``user_work_role``" arm of §03 "me.tasks:read" is a follow-up —
        see cd-fktzw notes / the reconciled spec).
        """
        return list_tasks_route(
            ctx,
            session,
            state=state,
            assignee_user_id=ctx.actor_id,
            cursor=cursor,
            limit=limit,
        )

    @router.get(
        "/expenses",
        response_model=ExpenseClaimListResponse,
        operation_id="me.expenses.list",
        summary="List the caller's own expense claims (self-only)",
        dependencies=[Depends(MeScope("me.expenses:read"))],
        openapi_extra={"x-cli": {"group": "me", "verb": "expenses"}},
    )
    def list_my_expenses(
        ctx: _Ctx,
        session: _Db,
        state: Annotated[str | None, Query(max_length=32)] = None,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> ExpenseClaimListResponse:
        """Own expense claims via the shared route with ``mine=True``.

        ``mine=True`` pins the listing to ``ctx.actor_id`` and skips the
        manager ``expenses.approve`` branch, so the subject always
        succeeds and can never widen the query to another user.
        """
        return list_expense_claims_route(
            ctx,
            session,
            mine=True,
            state=state,
            cursor=cursor,
            limit=limit,
        )

    @router.post(
        "/expenses",
        status_code=status.HTTP_201_CREATED,
        response_model=ExpenseClaimPayload,
        operation_id="me.expenses.create",
        summary="Create a draft expense claim for the caller (self-only)",
        dependencies=[Depends(MeScope("me.expenses:write"))],
    )
    def create_my_expense(
        body: ExpenseClaimCreate,
        ctx: _Ctx,
        session: _Db,
    ) -> ExpenseClaimPayload:
        """Create a draft claim bound to the caller's own engagement.

        The shared create service enforces ``engagement.user_id ==
        ctx.actor_id`` (403 otherwise), so a PAT can only file a claim
        for itself — never `expenses:approve`, never another subject.
        """
        return create_expense_claim_route(body, ctx, session)

    @router.get(
        "/profile",
        response_model=MeSelfProfileResponse,
        operation_id="me.profile.self.get",
        summary="Return the caller's own editable profile (self-only)",
        dependencies=[Depends(MeScope("me.profile:read"))],
        openapi_extra={"x-cli": {"group": "me", "verb": "profile-get"}},
    )
    def get_my_profile(ctx: _Ctx, session: _Db) -> MeSelfProfileResponse:
        """Return the subject's own :class:`User` row (limited fields)."""
        user = session.get(User, ctx.actor_id)
        if user is None:
            raise NotFound(extra={"error": "employee_not_found"})
        return _user_to_me_profile(user)

    @router.patch(
        "/profile",
        response_model=MeSelfProfileResponse,
        operation_id="me.profile.self.update",
        summary="Update the caller's own profile (self-only)",
        dependencies=[Depends(MeScope("me.profile:write"))],
    )
    def update_my_profile(
        body: MeProfileUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> MeSelfProfileResponse:
        """Self-update display name / language / timezone.

        Delegates to :func:`app.services.employees.update_profile` with
        ``user_id = ctx.actor_id``; its self-edit branch is un-gated
        (no ``users.edit_profile_other``) precisely because the target
        is the caller. Only fields the client sent are forwarded so an
        omitted field is left untouched.
        """
        sent_fields = body.model_fields_set
        service_body = EmployeeProfileUpdate.model_validate(
            {field: getattr(body, field) for field in sent_fields}
        )
        try:
            view = update_profile(
                SqlAlchemyMembershipRepository(session),
                ctx,
                user_id=ctx.actor_id,
                body=service_body,
            )
        except EmployeeNotFound as exc:
            raise NotFound(extra={"error": "employee_not_found"}) from exc
        except ProfileFieldForbidden as exc:
            raise Forbidden(extra={"error": "forbidden"}) from exc
        return _view_to_me_profile(view)

    return router
