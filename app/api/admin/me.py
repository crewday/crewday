"""``GET /admin/api/v1/me`` and ``GET /admin/api/v1/me/admins``.

Minimal deployment-admin surface. Both routes gate on
:func:`app.api.admin.deps.current_deployment_admin_principal` — a
caller without an active deployment grant (or a deployment-scoped
API token) collapses into the canonical 404 ``not_found`` envelope
per spec §12 "Admin surface" ("the surface does not advertise its
own existence to tenants").

* ``GET /admin/api/v1/me`` — caller's identity + the set of
  ``deployment.*`` capabilities they currently hold. Drives the
  ``/admin`` SPA chrome's "is this caller an admin?" gate (the SPA
  cross-checks via ``GET /api/v1/me``'s ``is_deployment_admin`` flag,
  but the dedicated admin probe lets the chrome render the admin
  identity card without a workspace round-trip).
* ``GET /admin/api/v1/me/admins`` — listing of every deployment
  admin (every ``role_grant`` row with ``scope_kind='deployment'``).
  Drives the admin-team page. The owner flag is resolved from
  ``deployment_owner`` rows.

The response shapes match :interface:`AdminMe` and
:interface:`AdminTeamMember` in ``app/web/src/types/admin.ts`` —
those are the live SPA contract carried into ``mocks/web/`` and
``app/web/`` simultaneously, so the wire shape stays one source of
truth across the mock + production frontends.

See ``docs/specs/12-rest-api.md`` §"Admin surface" and
``docs/specs/14-web-frontend.md`` §"Admin shell".
"""

from __future__ import annotations

from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import User
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.authz.deployment_owners import (
    deployment_owner_user_ids,
    is_deployment_owner,
)
from app.domain.errors import NotFound
from app.tenancy import DeploymentContext, tenant_agnostic

__all__ = [
    "AdminMeResponse",
    "AdminTeamMemberResponse",
    "AdminTeamResponse",
    "build_admin_me_router",
]


_Db = Annotated[Session, Depends(db_session)]


class AdminMeResponse(BaseModel):
    """Body of ``GET /admin/api/v1/me``.

    Mirrors :interface:`AdminMe` in ``app/web/src/types/admin.ts``:

    * ``user_id`` — the caller's :class:`User.id`. For a delegated /
      agent token this is the **delegating** user, mirroring
      :attr:`DeploymentContext.user_id`.
    * ``display_name`` — :attr:`User.display_name`.
    * ``email`` — :attr:`User.email` (display value, not the
      canonicalised lookup form).
    * ``is_owner`` — ``True`` iff the caller belongs to the
      ``owners@deployment`` group. The SPA renders the
      "Deployment owner" / "Deployment admin" footer label off this
      flag.
    * ``capabilities`` — flat ``{scope_key: True}`` map, one entry
      per ``deployment.*`` scope key the caller currently holds. A
      session / delegated principal carries the full
      :data:`DEPLOYMENT_SCOPE_CATALOG`; a scoped token carries the
      subset its row pins. The SPA reads individual flags off this
      map to gate "Edit settings" buttons, etc.
    """

    user_id: str
    display_name: str
    email: str
    is_owner: bool
    capabilities: dict[str, bool]


class AdminTeamMemberResponse(BaseModel):
    """One entry of ``GET /admin/api/v1/me/admins``.

    Mirrors :interface:`AdminTeamMember` in
    ``app/web/src/types/admin.ts``:

    * ``id`` — the :class:`RoleGrant.id` (the row primary key the SPA
      passes back to ``POST /admin/api/v1/admins/{id}/revoke`` once
      that route lands; until then the field is informational).
    * ``user_id`` — the admin's :class:`User.id`.
    * ``display_name`` — :attr:`User.display_name`.
    * ``email`` — :attr:`User.email`.
    * ``is_owner`` — ``True`` iff the user belongs to the deployment
      ``owners`` group.
    * ``granted_at`` — ISO-8601 UTC timestamp of the grant. Read off
      :attr:`RoleGrant.created_at`.
    * ``granted_by`` — :class:`User.id` of the actor that planted
      the grant; ``"system"`` when the column is NULL (the
      bootstrap row seeded at deployment creation has no prior
      actor). The SPA renders this verbatim.
    """

    id: str
    user_id: str
    display_name: str
    email: str
    is_owner: bool
    granted_at: str
    granted_by: str


class AdminTeamResponse(BaseModel):
    """Body of ``GET /admin/api/v1/me/admins``.

    The SPA's :file:`AdminsPage.tsx` consumes
    :interface:`AdminTeamMember[]` directly today; the wrapper
    envelope here keeps the legacy ``groups`` field. The populated
    deployment owners + managers rosters live on
    ``GET /admin/api/v1/admins/groups``.
    """

    admins: list[AdminTeamMemberResponse]
    groups: list[dict[str, object]] = Field(default_factory=list)


def _capabilities_payload(ctx: DeploymentContext) -> dict[str, bool]:
    """Return the flat ``{scope_key: True}`` map for ``ctx``.

    Sorted by key so the wire payload stays deterministic across
    requests — useful for snapshot-style assertions in tests and
    keeps ETag-style caching honest if the SPA ever adds it. The
    map has no ``False`` entries: an absent key means "not granted",
    same convention as the workspace-side ``capabilities`` field on
    ``GET /w/{slug}/api/v1/me``.
    """
    return {key: True for key in sorted(ctx.deployment_scopes)}


def _resolve_user_or_404(session: Session, *, user_id: str) -> User:
    """Hydrate the :class:`User` row pinned to ``ctx.user_id``.

    The dep already verified the user holds (or delegates from) an
    active deployment grant, so a missing row here is a hard
    invariant violation — the FK from ``role_grant.user_id`` to
    ``user.id`` is ``ON DELETE CASCADE``, so a deleted user cannot
    leave a dangling grant. Defensive 404 keeps the shape uniform
    with the rest of the admin surface — never advertising the
    distinction between "no user" and "no admin".
    """
    with tenant_agnostic():
        user = session.get(User, user_id)
    if user is None:
        raise NotFound(extra={"error": "not_found"})
    return user


def _format_granted_at(grant: RoleGrant) -> str:
    """Return :attr:`RoleGrant.created_at` as an ISO-8601 UTC string.

    SQLite drops tzinfo on ``DateTime(timezone=True)`` round-trips;
    Postgres preserves it. Force UTC so the emitted value always
    carries an explicit ``+00:00`` offset (§02 "Time is UTC at rest,
    local for display"). Mirrors the same fix-up in
    :func:`app.api.v1.auth.me._load_switcher_entries`.
    """
    moment = grant.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def build_admin_me_router() -> APIRouter:
    """Return the router carrying ``GET /admin/api/v1/me{,/admins}``.

    Built as a factory matching the rest of the API tree's wiring
    convention. The router carries no prefix of its own — the parent
    :data:`app.api.admin.admin_router` pins ``/admin/api/v1`` when
    the app factory mounts it, so this router's routes register at
    ``/me`` and ``/me/admins``.
    """
    router = APIRouter(tags=["admin"])

    @router.get(
        "/me",
        response_model=AdminMeResponse,
        operation_id="admin.me.read",
        summary="Return the deployment-admin caller's identity + capabilities",
        openapi_extra={
            # Matches the spec §12 "Admin surface" CLI surface — the
            # ``crewday admin whoami`` verb projects this route. Pinned
            # explicitly so the committed ``_surface.json`` is robust
            # against the bare-heuristic classifier.
            "x-cli": {
                "group": "admin",
                "verb": "whoami",
                "summary": "Show the deployment-admin caller + capabilities",
                "mutates": False,
            },
        },
    )
    def get_admin_me(
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
    ) -> AdminMeResponse:
        """Return the caller's :class:`AdminMeResponse`.

        ``ctx.user_id`` is the human (or delegating human for
        delegated tokens) — see :class:`DeploymentContext`. We
        hydrate the :class:`User` row to surface display name + email;
        capabilities come straight off
        :attr:`DeploymentContext.deployment_scopes`.

        ``is_owner`` resolves the concrete deployment-owner membership
        row so owner-only admin routes and labels share one source of
        truth.
        """
        user = _resolve_user_or_404(session, user_id=ctx.user_id)
        return AdminMeResponse(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            is_owner=is_deployment_owner(session, user_id=ctx.user_id),
            capabilities=_capabilities_payload(ctx),
        )

    # ``operation_id`` is namespaced under ``admin.me.*`` so it does
    # not collide with the dedicated ``GET /admin/api/v1/admins``
    # listing route owned by :mod:`app.api.admin.admins`. Both
    # endpoints project the same wire shape — the ``/me`` variant is
    # the legacy "caller's view" entry point that ships with cd-yj4k;
    # the ``/admins`` variant is the canonical surface from cd-jlms.
    # Generated OpenAPI clients (and Schemathesis) require unique
    # operation ids, so the two surfaces split the namespace here.
    @router.get(
        "/me/admins",
        response_model=AdminTeamResponse,
        operation_id="admin.me.admins.list",
        summary="List every active deployment admin grant",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "admins-list",
                "summary": "List deployment admins",
                "mutates": False,
            },
        },
    )
    def list_admins(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
    ) -> AdminTeamResponse:
        """Return every ``role_grant`` row with ``scope_kind='deployment'``.

        Joins to :class:`User` to surface display name + email. The
        :attr:`RoleGrant.created_by_user_id` column is NULL for the
        deployment-bootstrap row (no prior actor); we surface the
        sentinel string ``"system"`` rather than ``None`` so the
        SPA's table can render the value without a defensive
        nullish-coalesce on every cell.

        Ordered by :attr:`RoleGrant.created_at` ascending so the
        oldest admins surface first — the team page reads the list
        as a chronological roster.
        """
        # justification: ``role_grant`` is workspace-scoped; this
        # SELECT targets the deployment partition (``workspace_id IS
        # NULL``) which the ORM tenant filter would either narrow
        # away or fail closed on. The opt-out matches the precedent
        # in :func:`app.authz.deployment_admin.is_deployment_admin`.
        with tenant_agnostic():
            rows = session.execute(
                select(RoleGrant, User)
                .join(User, User.id == RoleGrant.user_id)
                .where(RoleGrant.scope_kind == "deployment")
                # cd-x1xh: live grants only — soft-retired admins
                # stay in the table for audit but no longer surface
                # on /me's admin team list.
                .where(RoleGrant.revoked_at.is_(None))
                .order_by(RoleGrant.created_at.asc(), RoleGrant.id.asc())
            ).all()

        owner_user_ids = deployment_owner_user_ids(session)
        admins: list[AdminTeamMemberResponse] = []
        for grant, user in rows:
            admins.append(
                AdminTeamMemberResponse(
                    id=grant.id,
                    user_id=user.id,
                    display_name=user.display_name,
                    email=user.email,
                    is_owner=user.id in owner_user_ids,
                    granted_at=_format_granted_at(grant),
                    granted_by=grant.created_by_user_id or "system",
                )
            )

        # The canonical group-roster route is
        # ``GET /admin/api/v1/admins/groups``; this legacy caller view
        # keeps the envelope field for forward-compatible clients.
        return AdminTeamResponse(admins=admins, groups=[])

    return router
