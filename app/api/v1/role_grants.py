"""Role-grants HTTP router — ``/role_grants`` + ``/users/{id}/role_grants``.

Spec §12 "Users / work roles / settings":

```
GET    /users/{id}/role_grants
POST   /role_grants
PATCH  /role_grants/{id}
DELETE /role_grants/{id}
```

Mounted inside the ``/w/<slug>/api/v1`` tree by the app factory. Every
route requires an active :class:`~app.tenancy.WorkspaceContext`. v1
surface:

* ``GET /users/{user_id}/role_grants`` — cursor-paginated list of every
  role grant the named user holds in the caller's workspace
  (optional ``scope_property_id`` filter).
* ``POST /role_grants`` — mint a new role grant. Honours the §05
  owner-authority rules at the domain layer (only owners may grant
  ``manager``; owners + active managers may grant
  ``worker`` / ``client`` / ``guest``).
* ``PATCH /role_grants/{id}`` — partial update (limited to
  ``scope_property_id`` in v1; ``user_id`` / ``grant_role`` /
  ``workspace_id`` are frozen by design — change them by revoking and
  re-granting).
* ``DELETE /role_grants/{id}`` — revoke a role grant. Honours the
  last-owner protection at the domain layer.

Every route tags ``identity`` (§01 context map) + ``role_grants``.
The action gates are ``role_grants.create`` for POST, ``role_grants.revoke``
for DELETE, and ``scope.view`` for the user-scoped GET (so workers can
read their own grant list); PATCH gates on ``role_grants.create`` since
mutating the scope of a grant is morally equivalent to granting it
fresh (the only field PATCH can touch in v1 is ``scope_property_id``,
which narrows / widens the grant — neither read).

See ``docs/specs/05-employees-and-roles.md`` §"Role grants",
``docs/specs/12-rest-api.md`` §"Users / work roles / settings", and
``docs/specs/02-domain-model.md`` §"role_grants".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import PropertyWorkspace
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.audit import write_audit
from app.authz import is_owner_member
from app.authz.dep import Permission
from app.domain.identity.role_grants import (
    CrossWorkspaceProperty,
    GrantRoleInvalid,
    LastOwnerGrantProtected,
    NotAuthorizedForRole,
    RoleGrantNotFound,
    RoleGrantRef,
    grant,
    list_grants,
    revoke,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "RoleGrantCreateRequest",
    "RoleGrantListResponse",
    "RoleGrantPatchRequest",
    "RoleGrantResponse",
    "build_role_grants_router",
    "build_users_role_grants_router",
    "router",
    "users_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# v1 grant_role enum mirrored on the wire so OpenAPI surfaces a typed
# Literal rather than a free-form string. Matches
# :data:`app.domain.identity.role_grants._VALID_GRANT_ROLES`. ``admin``
# (deployment scope) is NOT in this set per §05 — workspace-scoped
# grants only.
GrantRoleLiteral = Literal["manager", "worker", "client", "guest"]


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class RoleGrantCreateRequest(BaseModel):
    """Request body for ``POST /role_grants``.

    ``scope_property_id`` is optional: when set, the grant is narrowed
    to a single property the caller's workspace owns or shares (the
    domain service enforces the cross-workspace check). When omitted,
    the grant covers the whole workspace.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=64)
    grant_role: GrantRoleLiteral
    scope_property_id: str | None = Field(default=None, max_length=64)


class RoleGrantPatchRequest(BaseModel):
    """Request body for ``PATCH /role_grants/{id}``.

    Only ``scope_property_id`` is mutable; ``user_id`` / ``grant_role``
    are frozen by design (change them by revoking + re-granting). The
    PATCH semantics are explicit-sparse — omitted fields leave the
    column untouched, an explicit ``null`` widens the grant from a
    property-scoped row to a workspace-scoped row.
    """

    model_config = ConfigDict(extra="forbid")

    scope_property_id: str | None = Field(default=None, max_length=64)


class RoleGrantResponse(BaseModel):
    """Response shape for grant reads + writes."""

    id: str
    workspace_id: str
    user_id: str
    grant_role: str
    scope_property_id: str | None
    created_at: datetime
    created_by_user_id: str | None


class RoleGrantListResponse(BaseModel):
    """Collection envelope for ``GET /users/{id}/role_grants``."""

    data: list[RoleGrantResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ref_to_response(ref: RoleGrantRef) -> RoleGrantResponse:
    return RoleGrantResponse(
        id=ref.id,
        workspace_id=ref.workspace_id,
        user_id=ref.user_id,
        grant_role=ref.grant_role,
        scope_property_id=ref.scope_property_id,
        created_at=ref.created_at,
        created_by_user_id=ref.created_by_user_id,
    )


def _row_to_ref(row: RoleGrant) -> RoleGrantRef:
    """Project a workspace-scoped ORM row into an immutable :class:`RoleGrantRef`.

    cd-wchi widened :class:`RoleGrant.workspace_id` to nullable so the
    deployment-scope partition can omit it. Every PATCH/GET path on
    this router filters on ``RoleGrant.workspace_id == ctx.workspace_id``
    before reaching this helper, so a deployment-scope row can never
    surface here. The assertion narrows the static type without papering
    over the new invariant.
    """
    assert row.workspace_id is not None, (
        "role_grant row reached the workspace router with workspace_id "
        f"IS NULL (id={row.id!r}, scope_kind={row.scope_kind!r}); "
        "deployment-scope rows must use the admin surface helpers"
    )
    return RoleGrantRef(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        grant_role=row.grant_role,
        scope_property_id=row.scope_property_id,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "role_grant_not_found"},
    )


def _http_for_invalid_role(exc: GrantRoleInvalid) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "invalid_grant_role", "message": str(exc)},
    )


def _http_for_cross_workspace(exc: CrossWorkspaceProperty) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "cross_workspace_property", "message": str(exc)},
    )


def _http_for_unauthorised(exc: NotAuthorizedForRole) -> HTTPException:
    """Map the §05 owner-authority rejection to 403.

    The :func:`Permission` action gate already cleared the caller for
    ``role_grants.create``; this 403 is the *finer-grained* authority
    rule (§05 "Surface grants at a glance") — only owners may grant
    ``manager``. Distinguishing the two via a dedicated error code
    (``not_authorized_for_role``) keeps the SPA's "you can't grant
    that role" message specific.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "not_authorized_for_role", "message": str(exc)},
    )


def _http_for_last_owner(exc: LastOwnerGrantProtected) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "last_owner_grant_protected", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


_ScopePropertyFilter = Annotated[
    str | None,
    Query(
        max_length=64,
        description=(
            "Narrow the listing to grants pinned to this property. "
            "Omitted matches every grant (workspace + property scoped)."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Router factories
# ---------------------------------------------------------------------------


def build_role_grants_router() -> APIRouter:
    """Return the top-level ``/role_grants`` router (POST + PATCH + DELETE).

    The list endpoint lives on :func:`build_users_role_grants_router`
    since it is user-keyed (``/users/{id}/role_grants``).
    """
    api = APIRouter(prefix="/role_grants", tags=["identity", "role_grants"])

    create_gate = Depends(Permission("role_grants.create", scope_kind="workspace"))
    revoke_gate = Depends(Permission("role_grants.revoke", scope_kind="workspace"))

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=RoleGrantResponse,
        operation_id="role_grants.create",
        summary="Mint a new role grant in the caller's workspace",
        dependencies=[create_gate],
        openapi_extra={
            "x-cli": {
                "group": "role-grants",
                "verb": "create",
                "summary": "Mint a role grant for a user",
                "mutates": True,
            },
        },
    )
    def create(
        body: RoleGrantCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> RoleGrantResponse:
        """Insert a new ``role_grant`` row.

        The action gate (``role_grants.create``) cleared the caller for
        the *write* operation; the §05 owner-authority rule layered on
        top — only owners may grant ``manager``, owners or active
        managers may grant ``worker`` / ``client`` / ``guest`` — fires
        at the domain layer and surfaces here as
        :class:`NotAuthorizedForRole` → 403 ``not_authorized_for_role``.
        """
        try:
            ref = grant(
                session,
                ctx,
                user_id=body.user_id,
                grant_role=body.grant_role,
                scope_property_id=body.scope_property_id,
            )
        except GrantRoleInvalid as exc:
            raise _http_for_invalid_role(exc) from exc
        except NotAuthorizedForRole as exc:
            raise _http_for_unauthorised(exc) from exc
        except CrossWorkspaceProperty as exc:
            raise _http_for_cross_workspace(exc) from exc
        return _ref_to_response(ref)

    @api.patch(
        "/{grant_id}",
        response_model=RoleGrantResponse,
        operation_id="role_grants.update",
        summary="Partial update of a role grant — re-scope only",
        dependencies=[create_gate],
        openapi_extra={
            "x-cli": {
                "group": "role-grants",
                "verb": "update",
                "summary": "Re-scope a role grant",
                "mutates": True,
            },
        },
    )
    def update(
        grant_id: str,
        body: RoleGrantPatchRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> RoleGrantResponse:
        """Update mutable fields on an existing grant.

        v1 only supports re-scoping (``scope_property_id``). Mutating
        the role itself would alter the §05 owner-authority surface —
        the caller revokes + re-grants instead.

        Implementation detail: the domain service treats grants as
        immutable today, so we hand-roll the PATCH at the router edge.
        Any tampering (a body that tries to switch user / role) trips
        the ``extra=forbid`` validator before we ever load the row.
        """
        sent = body.model_fields_set
        if not sent:
            # Empty PATCH — return the current row without writing
            # (mirrors the user-PATCH no-op convention; still emits a
            # read but no audit).
            row = session.scalars(
                select(RoleGrant).where(
                    RoleGrant.id == grant_id,
                    RoleGrant.workspace_id == ctx.workspace_id,
                )
            ).one_or_none()
            if row is None:
                raise _http_for_not_found()
            return _ref_to_response(_row_to_ref(row))

        row = session.scalars(
            select(RoleGrant).where(
                RoleGrant.id == grant_id,
                RoleGrant.workspace_id == ctx.workspace_id,
            )
        ).one_or_none()
        if row is None:
            raise _http_for_not_found()

        # §05 owner-authority: re-scoping a ``manager`` grant is morally
        # a manager grant write — only ``owners@<workspace>`` members may
        # touch it. The action gate above (``role_grants.create``)
        # cleared the caller for the *write* operation; this finer-grained
        # rule mirrors the create-side check in :func:`grant`. Without it
        # a non-owner manager could narrow the sole owner's manager grant
        # to a single property (silent governance bypass).
        if row.grant_role == "manager" and not is_owner_member(
            session, workspace_id=ctx.workspace_id, user_id=ctx.actor_id
        ):
            raise _http_for_unauthorised(
                NotAuthorizedForRole(
                    "only members of 'owners' may re-scope a manager grant"
                )
            )

        before_scope = row.scope_property_id
        new_scope = body.scope_property_id
        if new_scope is not None:
            # Re-validate the property is in the caller's workspace —
            # mirrors the create-side guard so a PATCH cannot leak the
            # grant across tenants.
            stmt = select(
                exists().where(
                    PropertyWorkspace.property_id == new_scope,
                    PropertyWorkspace.workspace_id == ctx.workspace_id,
                )
            )
            if not session.scalar(stmt):
                raise _http_for_cross_workspace(
                    CrossWorkspaceProperty(
                        f"property {new_scope!r} is not linked to this workspace"
                    )
                )
        row.scope_property_id = new_scope
        session.flush()
        write_audit(
            session,
            ctx,
            entity_kind="role_grant",
            entity_id=row.id,
            action="rescoped",
            diff={
                "before": {"scope_property_id": before_scope},
                "after": {"scope_property_id": new_scope},
            },
        )
        return _ref_to_response(_row_to_ref(row))

    @api.delete(
        "/{grant_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="role_grants.revoke",
        summary="Revoke a role grant",
        dependencies=[revoke_gate],
        openapi_extra={
            "x-cli": {
                "group": "role-grants",
                "verb": "revoke",
                "summary": "Revoke a role grant",
                "mutates": True,
            },
        },
    )
    def delete(
        grant_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Hard-delete the row (v1 has no ``revoked_at`` column yet).

        The last-owner guard fires at the domain layer for ``manager``
        grants belonging to the sole owners-group member; the router
        maps it to 409 ``last_owner_grant_protected`` so the SPA can
        prompt the operator to transfer owners-membership first.
        """
        try:
            revoke(session, ctx, grant_id=grant_id)
        except RoleGrantNotFound as exc:
            raise _http_for_not_found() from exc
        except LastOwnerGrantProtected as exc:
            raise _http_for_last_owner(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


def build_users_role_grants_router() -> APIRouter:
    """Return the ``/users/{user_id}/role_grants`` list router.

    Separated from the top-level router so the list URL keeps the
    spec's user-keyed shape (``/users/{id}/role_grants``) without
    mounting any other ``/users`` endpoint under the same tree. Reads
    gate on ``scope.view`` so any grant role can read their own
    grants — managers + owners can read anyone's.
    """
    api = APIRouter(prefix="/users", tags=["identity", "role_grants"])

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))

    @api.get(
        "/{user_id}/role_grants",
        response_model=RoleGrantListResponse,
        operation_id="role_grants.list_by_user",
        summary="List a user's role grants in the caller's workspace",
        dependencies=[view_gate],
        openapi_extra={
            "x-cli": {
                "group": "role-grants",
                "verb": "list",
                "summary": "List a user's role grants",
                "mutates": False,
            },
        },
    )
    def list_(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
        scope_property_id: _ScopePropertyFilter = None,
    ) -> RoleGrantListResponse:
        """Cursor-paginated listing of role grants for ``user_id``.

        The path's ``user_id`` is workspace-scoped through the
        :func:`list_grants` filter (it ANDs ``user_id`` with
        ``workspace_id`` from the active ctx) so a ULID from a sibling
        tenant returns an empty page rather than leaking data.
        """
        # ``after_id`` is the previous page's last-row id. v1 fetches
        # the full filtered set and does cursor-trim in memory because
        # the domain service does not yet expose a SQL after-id filter
        # for this query shape; the row count per user is bounded
        # (one workspace grant + per-property grants) so the in-memory
        # walk is safe. Once the surface needs to scale we extend
        # :func:`list_grants` with an ``after_id`` kwarg in lockstep.
        after_id = decode_cursor(cursor)
        refs = list_grants(
            session,
            ctx,
            user_id=user_id,
            scope_property_id=scope_property_id,
        )
        if after_id is not None:
            # ULID is monotonic-ascending in the same millisecond, so
            # filtering on `id > after_id` walks forward correctly.
            refs = [r for r in refs if r.id > after_id]
        # ``paginate`` expects up-to-``limit + 1`` so it can decide
        # ``has_more``. We slice ourselves since :func:`list_grants` does
        # not honour ``limit`` natively in v1.
        sliced = refs[: limit + 1]
        page = paginate(
            sliced,
            limit=limit,
            key_getter=lambda r: r.id,
        )
        return RoleGrantListResponse(
            data=[_ref_to_response(r) for r in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    return api


# Module-level routers for the v1 app factory's eager import. Tests
# that want a fresh instance per case should call the ``build_*``
# factories directly to avoid cross-test leaks on FastAPI's
# dependency-override cache.
router = build_role_grants_router()
users_router = build_users_role_grants_router()
