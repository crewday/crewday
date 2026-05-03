"""``GET /api/v1/auth/me`` — identity bootstrap for the SPA.

Bare-host route, tenant-agnostic (runs before a workspace is picked).
The SPA's :mod:`authStore` hits this on every load to decide whether
the cookied user is still live; a 401 drops the store into the
unauthenticated state and bounces the visitor to ``/login``, a 200
seeds ``useAuth()`` with the user + their available workspaces.

Response shape matches :class:`AuthMe` in
``app/web/src/auth/types.ts``. The :class:`AvailableWorkspace` inner
shape matches ``app/web/src/types/auth.ts`` and surfaces every
workspace the caller has a :class:`RoleGrant` on.

Owner detection: users who hold a ``manager`` surface grant on any
workspace, or who are a member of any ``owners`` permission group,
map their grant as ``manager`` in the response (the governance
anchor is already encoded by the ``manager`` surface, per §03 —
``owner`` is no longer a grant-role value in v1).

**Defaults on absent columns.** The v1 :class:`Workspace` row does
not yet carry ``timezone`` / ``default_currency`` / ``default_country``
/ ``default_locale`` (cd-n6p adds them). Until then we emit sensible
defaults so the SPA's typed ``Workspace`` contract is honoured
without a brittle ``null`` field. This is documented as a known
drift on cd-h2t0; the defaults match the deployment's locale bias
and can be overridden once the columns land.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions",
``docs/specs/14-web-frontend.md`` §"Workspace selector", and
``docs/specs/12-rest-api.md`` §"Auth".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.admin._owners import is_deployment_owner
from app.api.deps import db_session
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.auth import session as auth_session
from app.authz.deployment_admin import is_deployment_admin
from app.tenancy import tenant_agnostic
from app.tenancy.current import get_current

__all__ = [
    "AuthMeResponse",
    "AvailableWorkspaceResponse",
    "EmployeeProfileResponse",
    "MeProfileResponse",
    "WorkspaceSummary",
    "WorkspaceSwitcherEntry",
    "build_me_profile_router",
    "build_me_router",
    "build_me_workspaces_router",
]


_log = logging.getLogger(__name__)

_Db = Annotated[Session, Depends(db_session)]


# Defaults used until the v1 workspace row carries the real columns
# (cd-n6p). Kept here rather than in settings because they are
# serialisation defaults, not deploy-tunable policy.
_DEFAULT_TIMEZONE: str = "UTC"
_DEFAULT_CURRENCY: str = "EUR"
_DEFAULT_COUNTRY: str = "FR"
_DEFAULT_LOCALE: str = "en"


class WorkspaceSummary(BaseModel):
    """Subset of :class:`Workspace` surfaced in the ``/auth/me`` envelope.

    Mirrors ``app/web/src/types/core.ts`` ``Workspace``. ``id`` carries
    the URL slug rather than the DB ULID — the SPA's
    :func:`slugFor` helper currently reads the ``id`` field as the
    URL component, and the workspace chooser builds links as
    ``/w/{id}/...``. Returning the slug here keeps the chooser
    working without a follow-up shape migration on the frontend.
    """

    id: str = Field(..., description="Workspace URL slug.")
    name: str
    timezone: str
    default_currency: str
    default_country: str
    default_locale: str


class AvailableWorkspaceResponse(BaseModel):
    """One entry of :attr:`AuthMeResponse.available_workspaces`.

    ``grant_role`` is the caller's highest-privilege surface grant on
    this workspace. ``binding_org_id`` carries the selected grant's
    client organization binding when present; ``source`` is retained
    for type-parity with the SPA contract.
    """

    workspace: WorkspaceSummary
    grant_role: str | None
    binding_org_id: str | None
    source: str


class AuthMeResponse(BaseModel):
    """Body of ``GET /api/v1/auth/me``.

    Matches :class:`AuthMe` in ``app/web/src/auth/types.ts``. The SPA
    expects a flat envelope — no nested ``user`` — because the field
    set is small enough to inline.

    ``is_deployment_admin`` mirrors the same flag on
    :class:`MeProfileResponse` (``GET /api/v1/me``) and is surfaced
    here too so the bare-host SPA shell — which never reaches the
    workspace-scoped ``/me`` when the caller has zero grants — can
    still discover that the caller is a deployment admin and offer
    a deep-link to ``/admin/dashboard`` from the
    ``<WorkspaceGate>`` "no workspaces yet" empty state.
    """

    user_id: str
    display_name: str
    email: str
    available_workspaces: list[AvailableWorkspaceResponse]
    current_workspace_id: str | None
    is_deployment_admin: bool


class EmployeeProfileResponse(BaseModel):
    """Legacy SPA ``Employee`` projection embedded in ``GET /api/v1/me``."""

    id: str
    name: str
    roles: list[str]
    properties: list[str]
    avatar_initials: str
    avatar_file_id: str | None
    avatar_url: str | None
    phone: str
    email: str
    started_on: str
    capabilities: dict[str, bool | None]
    workspaces: list[str]
    villas: list[str]
    language: str
    weekly_availability: dict[str, tuple[str, str] | None]
    evidence_policy: str
    preferred_locale: str | None
    settings_override: dict[str, Any]


class MeProfileResponse(BaseModel):
    """Body of ``GET /api/v1/me`` consumed by the app shell layouts."""

    role: str
    theme: str
    agent_sidebar_collapsed: bool
    employee: EmployeeProfileResponse
    manager_name: str
    today: str
    now: str
    user_id: str | None
    agent_approval_mode: str
    current_workspace_id: str
    available_workspaces: list[AvailableWorkspaceResponse]
    client_binding_org_ids: list[str]
    is_deployment_admin: bool
    is_deployment_owner: bool


class WorkspaceSwitcherEntry(BaseModel):
    """One entry of ``GET /api/v1/me/workspaces``.

    Dedicated switcher payload, distinct from
    :class:`AvailableWorkspaceResponse` so the surface can evolve
    independently. The richer shape carries:

    * ``workspace_id`` — the DB ULID (the slug is also surfaced for
      URL-building convenience).
    * ``slug`` — URL component for the workspace selector links.
    * ``name`` — display name.
    * ``current_role`` — caller's resolved surface role on the workspace
      (``manager`` collapses owners-group members per §03; the value is
      ``"owner"`` only when the caller still carries the legacy ``owner``
      grant_role, which v1 no longer mints — see :func:`AuthMeResponse`'s
      handling).
    * ``last_seen_at`` — ISO-8601 UTC timestamp from the most recent
      :class:`Session` row scoped to ``(user_id, workspace_id)``;
      ``None`` when the user has never picked the workspace yet.
    * ``settings_override`` — a light projection of the workspace's
      :attr:`Workspace.settings_json` so the SPA's switcher can render
      per-workspace branding without a follow-up settings round-trip.
      An empty dict on a workspace with no overrides — never ``None``.

    Spec §12 "Auth": ``GET /api/v1/me/workspaces`` (cd-y5z3).
    """

    workspace_id: str
    slug: str
    name: str
    current_role: str | None
    last_seen_at: str | None
    settings_override: dict[str, Any]


def _client_headers(request: Request) -> tuple[str, str]:
    """Return ``(ua, accept_language)`` for :func:`auth_session.validate`.

    Kept together because the fingerprint gate reads both. Empty
    strings are fine — :func:`validate` skips the fingerprint check
    when the caller supplies neither header.
    """
    return (
        request.headers.get("user-agent", ""),
        request.headers.get("accept-language", ""),
    )


def _session_cookie_value(
    *,
    session_cookie_primary: str | None,
    session_cookie_dev: str | None,
) -> str:
    cookie_value = session_cookie_primary or session_cookie_dev
    if not cookie_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_required"},
        )
    return cookie_value


def _validated_session_user(
    request: Request,
    session: Session,
    *,
    cookie_value: str,
    touch_session: bool = True,
) -> tuple[User, SessionRow]:
    ua, accept_language = _client_headers(request)
    try:
        user_id = auth_session.validate(
            session,
            cookie_value=cookie_value,
            ua=ua,
            accept_language=accept_language,
            touch=touch_session,
        )
    except auth_session.UserArchived as exc:
        # Archive gate (cd-uceg, §03 "Sessions" / "Personal access
        # tokens"). Match the bearer-token side: typed wire code so
        # the SPA can route the operator to "have a deployment owner
        # reinstate this user", not the opaque ``session_invalid``.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": auth_session.USER_ARCHIVED_WIRE_CODE},
        ) from exc
    except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_invalid"},
        ) from exc

    with tenant_agnostic():
        user = session.get(User, user_id)
        session_row = session.get(
            SessionRow,
            auth_session.hash_cookie_value(cookie_value),
        )
    if user is None or session_row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_invalid"},
        )
    return user, session_row


def _load_available_workspaces(
    session: Session, *, user_id: str
) -> list[AvailableWorkspaceResponse]:
    """Return every workspace the user has a :class:`RoleGrant` on.

    Collapses multiple grants on the same workspace onto the
    highest-privilege one (manager > worker > client > guest). Users
    in an ``owners`` permission group are surfaced as ``manager`` —
    §03 collapses governance onto the manager surface in v1.
    """
    # justification: ``role_grant`` and ``workspace`` are tenancy
    # anchors themselves; this lookup runs before a WorkspaceContext
    # exists (auth/me is bare-host), so the ORM tenant filter has
    # nothing to apply.
    with tenant_agnostic():
        rows = session.execute(
            select(RoleGrant, Workspace)
            .join(Workspace, Workspace.id == RoleGrant.workspace_id)
            .where(RoleGrant.user_id == user_id)
            # cd-x1xh: live grants only — a soft-retired grant must
            # not surface a workspace in the user's switcher.
            .where(RoleGrant.revoked_at.is_(None))
        ).all()

        owners_workspace_ids = set(
            session.scalars(
                select(PermissionGroup.workspace_id)
                .join(
                    PermissionGroupMember,
                    PermissionGroupMember.group_id == PermissionGroup.id,
                )
                .where(PermissionGroupMember.user_id == user_id)
                .where(PermissionGroup.slug == "owners")
            ).all()
        )

    # Surface-role precedence (highest → lowest). ``None`` sorts
    # last so an unrecognised value never shadows a known grant.
    _RANK: dict[str, int] = {
        "manager": 0,
        "admin": 0,
        "worker": 1,
        "client": 2,
        "guest": 3,
    }

    best: dict[str, tuple[int, RoleGrant, Workspace]] = {}
    for grant, workspace in rows:
        rank = _RANK.get(grant.grant_role, 99)
        existing = best.get(workspace.id)
        if existing is None or rank < existing[0]:
            best[workspace.id] = (rank, grant, workspace)

    out: list[AvailableWorkspaceResponse] = []
    for ws_id, (_rank, grant, workspace) in best.items():
        role = grant.grant_role
        if ws_id in owners_workspace_ids and role != "manager":
            # Owners-group member without a manager surface grant is
            # still governance-authoritative; surface as manager so
            # the SPA routes to the manager landing.
            role = "manager"
        out.append(
            AvailableWorkspaceResponse(
                workspace=WorkspaceSummary(
                    id=workspace.slug,
                    name=workspace.name,
                    timezone=_DEFAULT_TIMEZONE,
                    default_currency=_DEFAULT_CURRENCY,
                    default_country=_DEFAULT_COUNTRY,
                    default_locale=_DEFAULT_LOCALE,
                ),
                grant_role=role,
                binding_org_id=grant.binding_org_id,
                source="workspace_grant",
            )
        )
    return out


def _client_binding_org_ids(
    session: Session, *, workspace_id: str, user_id: str
) -> list[str]:
    with tenant_agnostic():
        rows = session.scalars(
            select(RoleGrant.binding_org_id)
            .where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.user_id == user_id,
                RoleGrant.scope_kind == "workspace",
                RoleGrant.grant_role == "client",
                RoleGrant.scope_property_id.is_(None),
                RoleGrant.binding_org_id.is_not(None),
                # cd-x1xh: a soft-retired client grant must not
                # widen the portal's binding-org list.
                RoleGrant.revoked_at.is_(None),
            )
            .order_by(RoleGrant.binding_org_id.asc())
        ).all()
    return sorted({org_id for org_id in rows if org_id is not None})


def _workspace_ids_for_user(session: Session, *, user_id: str) -> list[str]:
    with tenant_agnostic():
        return list(
            session.scalars(
                select(UserWorkspace.workspace_id)
                .where(UserWorkspace.user_id == user_id)
                .order_by(UserWorkspace.workspace_id.asc())
            ).all()
        )


def _current_workspace_id(
    session: Session,
    *,
    user_id: str,
    session_row: SessionRow,
) -> str:
    ctx = get_current()
    if ctx is not None:
        return ctx.workspace_id
    if session_row.workspace_id:
        return session_row.workspace_id
    workspace_ids = _workspace_ids_for_user(session, user_id=user_id)
    if workspace_ids:
        return workspace_ids[0]
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "workspace_required"},
    )


def _workspace_role(session: Session, *, user_id: str, workspace_id: str) -> str:
    with tenant_agnostic():
        grants = list(
            session.scalars(
                select(RoleGrant.grant_role)
                .where(RoleGrant.user_id == user_id)
                .where(RoleGrant.workspace_id == workspace_id)
                .where(RoleGrant.scope_kind == "workspace")
                .where(RoleGrant.scope_property_id.is_(None))
                # cd-x1xh: live grants only.
                .where(RoleGrant.revoked_at.is_(None))
            ).all()
        )
        owner_member = (
            session.scalars(
                select(PermissionGroupMember.user_id)
                .join(
                    PermissionGroup,
                    PermissionGroup.id == PermissionGroupMember.group_id,
                )
                .where(PermissionGroupMember.user_id == user_id)
                .where(PermissionGroupMember.workspace_id == workspace_id)
                .where(PermissionGroup.slug == "owners")
                .where(PermissionGroup.system.is_(True))
                .limit(1)
            ).first()
            is not None
        )

    if owner_member:
        return "manager"
    rank = {"manager": 0, "worker": 1, "client": 2, "guest": 3}
    best = min(grants, key=lambda role: rank.get(role, 99), default="worker")
    if best == "manager":
        return "manager"
    if best == "client":
        return "client"
    return "employee"


def _initials(name: str) -> str:
    parts = [part for part in name.replace("-", " ").split() if part]
    if not parts:
        return "??"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


def _employee_profile(
    user: User,
    *,
    role: str,
    workspace_ids: list[str],
) -> EmployeeProfileResponse:
    return EmployeeProfileResponse(
        id=user.id,
        name=user.display_name,
        roles=[role],
        properties=[],
        avatar_initials=_initials(user.display_name),
        avatar_file_id=None,
        avatar_url=None,
        phone="",
        email=user.email,
        started_on=user.created_at.date().isoformat(),
        capabilities={},
        workspaces=workspace_ids,
        villas=[],
        language=user.locale or "en",
        weekly_availability={},
        evidence_policy="inherit",
        preferred_locale=user.locale,
        settings_override={},
    )


def _load_switcher_entries(
    session: Session, *, user_id: str
) -> list[WorkspaceSwitcherEntry]:
    """Return one :class:`WorkspaceSwitcherEntry` per workspace ``user_id`` is in.

    Drives ``GET /api/v1/me/workspaces``. Joins the derived
    :class:`UserWorkspace` junction with :class:`Workspace` to get the
    slug + name + ``settings_json``; resolves ``current_role`` from the
    same precedence ladder used by :func:`_load_available_workspaces`
    (manager > worker > client > guest, with owners-group members
    surfaced as ``manager`` per §03); resolves ``last_seen_at`` from
    the most recent :class:`Session` row for the
    ``(user_id, workspace_id)`` pair.

    No PII enters the response — only workspace metadata + the caller's
    own role + their own session activity.
    """
    # justification: identity-bootstrap query; the user spans multiple
    # workspaces and the ORM tenant filter would narrow inappropriately.
    with tenant_agnostic():
        memberships = session.execute(
            select(UserWorkspace, Workspace)
            .join(Workspace, Workspace.id == UserWorkspace.workspace_id)
            .where(UserWorkspace.user_id == user_id)
            .order_by(Workspace.slug.asc())
        ).all()

        if not memberships:
            return []

        workspace_ids = [ws.id for _, ws in memberships]

        # Pre-load every grant + owners-group membership in two cheap
        # queries so the per-workspace loop below stays O(1) — avoids
        # N + 1 queries on a switcher payload that the SPA hits on
        # every load. ``RoleGrant.scope_property_id IS NULL`` filters
        # to workspace-scope grants only; property-pinned grants do
        # not promote the holder to a workspace-level role surface.
        grants = session.execute(
            select(RoleGrant.workspace_id, RoleGrant.grant_role)
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.workspace_id.in_(workspace_ids))
            .where(RoleGrant.scope_property_id.is_(None))
            # cd-x1xh: live grants only — soft-retired grants must
            # not contribute to the workspace switcher's role pick.
            .where(RoleGrant.revoked_at.is_(None))
        ).all()

        owners_workspace_ids = set(
            session.scalars(
                select(PermissionGroup.workspace_id)
                .join(
                    PermissionGroupMember,
                    PermissionGroupMember.group_id == PermissionGroup.id,
                )
                .where(PermissionGroupMember.user_id == user_id)
                .where(PermissionGroup.workspace_id.in_(workspace_ids))
                .where(PermissionGroup.slug == "owners")
                .where(PermissionGroup.system.is_(True))
            ).all()
        )

        # Most-recent ``last_seen_at`` per workspace — single GROUP BY
        # so the worst case is one round-trip for any number of
        # workspaces. ``Session.workspace_id`` is nullable; we filter
        # those rows out (they belong to the "no workspace picked yet"
        # state, not to any specific workspace).
        last_seen_rows = session.execute(
            select(
                SessionRow.workspace_id,
                sa_func.max(SessionRow.last_seen_at).label("last_seen_at"),
            )
            .where(SessionRow.user_id == user_id)
            .where(SessionRow.workspace_id.in_(workspace_ids))
            .group_by(SessionRow.workspace_id)
        ).all()

    last_seen_by_ws: dict[str, str] = {}
    for row in last_seen_rows:
        ws_id = row[0]
        last_seen = row[1]
        if ws_id is None or last_seen is None:
            continue
        # SQLite drops tzinfo on a ``DateTime(timezone=True)`` column
        # roundtrip; Postgres preserves it. Force UTC so the emitted
        # ISO-8601 string always carries an explicit ``+00:00`` /
        # ``Z`` offset (§02 "Time is UTC at rest, local for display").
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        last_seen_by_ws[ws_id] = last_seen.isoformat()

    # Surface-role precedence (highest → lowest). ``None`` sorts last
    # so an unrecognised value never shadows a known grant. Mirrors
    # the table in :func:`_load_available_workspaces`.
    _RANK: dict[str, int] = {
        "manager": 0,
        "admin": 0,
        "worker": 1,
        "client": 2,
        "guest": 3,
    }
    best_role_by_ws: dict[str, tuple[int, str]] = {}
    for ws_id, role in grants:
        rank = _RANK.get(role, 99)
        existing = best_role_by_ws.get(ws_id)
        if existing is None or rank < existing[0]:
            best_role_by_ws[ws_id] = (rank, role)

    out: list[WorkspaceSwitcherEntry] = []
    for _, workspace in memberships:
        rank_role = best_role_by_ws.get(workspace.id)
        role = rank_role[1] if rank_role is not None else None
        if workspace.id in owners_workspace_ids and role != "manager":
            # Owners-group member without a manager surface grant is
            # still governance-authoritative; surface as manager so
            # the SPA routes to the manager landing.
            role = "manager"

        # ``settings_json`` is the workspace's flat dotted-key map. We
        # pass it through verbatim so the switcher can render per-
        # workspace overrides without a follow-up call. The column is
        # NOT NULL so the dict is always present; copy defensively so
        # a downstream mutation can't bleed back into the ORM-managed
        # row.
        settings_override = dict(workspace.settings_json or {})

        out.append(
            WorkspaceSwitcherEntry(
                workspace_id=workspace.id,
                slug=workspace.slug,
                name=workspace.name,
                current_role=role,
                last_seen_at=last_seen_by_ws.get(workspace.id),
                settings_override=settings_override,
            )
        )
    return out


def build_me_profile_router(*, operation_id: str = "me.profile.get") -> APIRouter:
    """Return the router that serves ``GET /api/v1/me``.

    The production SPA layouts still consume the legacy mock ``/me``
    envelope for shell chrome (display name, role, deployment-admin
    flag). Keep it as a thin authenticated identity/profile view while
    ``/auth/me`` remains the tenant-agnostic bootstrap surface.
    """
    router = APIRouter(
        prefix="/me",
        tags=["identity", "me"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    @router.get(
        "",
        response_model=MeProfileResponse,
        operation_id=operation_id,
        summary="Return the current user's app-shell profile",
        openapi_extra={
            "x-cli": {
                "group": "me",
                "verb": "profile",
                "summary": "Return the app-shell profile payload",
                "mutates": False,
                "hidden": True,
            },
        },
    )
    def get_me_profile(
        request: Request,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias="crewday_session"),
        ] = None,
    ) -> MeProfileResponse:
        cookie_value = _session_cookie_value(
            session_cookie_primary=session_cookie_primary,
            session_cookie_dev=session_cookie_dev,
        )
        user, session_row = _validated_session_user(
            request,
            session,
            cookie_value=cookie_value,
            touch_session=False,
        )
        workspace_id = _current_workspace_id(
            session,
            user_id=user.id,
            session_row=session_row,
        )
        role = _workspace_role(session, user_id=user.id, workspace_id=workspace_id)
        workspace_ids = _workspace_ids_for_user(session, user_id=user.id)
        now = datetime.now(UTC)
        return MeProfileResponse(
            role=role,
            theme=request.cookies.get("crewday_theme", "system"),
            agent_sidebar_collapsed=request.cookies.get("crewday_agent_collapsed")
            == "1",
            employee=_employee_profile(user, role=role, workspace_ids=workspace_ids),
            manager_name=user.display_name,
            today=now.date().isoformat(),
            now=now.isoformat(),
            user_id=user.id,
            agent_approval_mode=user.agent_approval_mode,
            current_workspace_id=workspace_id,
            available_workspaces=_load_available_workspaces(session, user_id=user.id),
            client_binding_org_ids=_client_binding_org_ids(
                session, workspace_id=workspace_id, user_id=user.id
            ),
            is_deployment_admin=is_deployment_admin(session, user_id=user.id),
            is_deployment_owner=is_deployment_owner(session, user_id=user.id),
        )

    return router


def build_me_router() -> APIRouter:
    """Return the router that serves ``GET /api/v1/auth/me``.

    Built as a factory (matching the other auth-router builders in
    this package) so the app factory keeps a uniform wiring seam and
    tests can mount the endpoint against an isolated FastAPI
    instance.
    """
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` stays for fine-grained client filtering.
    router = APIRouter(
        prefix="/auth",
        tags=["identity", "auth"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    @router.get(
        "/me",
        response_model=AuthMeResponse,
        operation_id="auth.me.get",
        summary="Return the authenticated user + their available workspaces",
        openapi_extra={
            # Singleton endpoint: "whoami" is the spec's verb (§13
            # ``crewday auth whoami``). The bare heuristic would
            # classify a GET without a trailing ``{id}`` as ``list``;
            # pin the CLI surface so the committed ``_surface.json``
            # does not drift on the heuristic alone.
            "x-cli": {
                "group": "auth",
                "verb": "whoami",
                "summary": "Show the authenticated user + their workspaces",
                "mutates": False,
            },
        },
    )
    def get_me(
        request: Request,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias="crewday_session"),
        ] = None,
    ) -> AuthMeResponse:
        """Validate the session cookie, hydrate user + workspaces.

        Returns 401 when the cookie is absent or rejected by
        :func:`auth_session.validate`. The SPA's
        :mod:`auth.onUnauthorized` seam routes every 401 to the store
        reset + login bounce.
        """
        cookie_value = _session_cookie_value(
            session_cookie_primary=session_cookie_primary,
            session_cookie_dev=session_cookie_dev,
        )
        user, _session_row = _validated_session_user(
            request,
            session,
            cookie_value=cookie_value,
        )

        return AuthMeResponse(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            available_workspaces=_load_available_workspaces(session, user_id=user.id),
            current_workspace_id=None,
            is_deployment_admin=is_deployment_admin(session, user_id=user.id),
        )

    return router


def build_me_workspaces_router() -> APIRouter:
    """Return the router that serves ``GET /api/v1/me/workspaces``.

    Bare-host (tenant-agnostic) — the SPA hits this from the workspace
    switcher to populate the picker. Distinct from
    ``GET /auth/me``'s ``available_workspaces`` because the switcher
    needs a richer projection (``last_seen_at`` per workspace,
    ``settings_override``) that would be wasteful payload to ship on
    every authenticated load. Built as a separate router (not a
    second route on :func:`build_me_router`) because the prefix differs
    (``/me`` vs ``/auth``); both routers are mounted by the app factory
    on the bare-host ``/api/v1`` prefix.

    See ``docs/specs/12-rest-api.md`` §"Auth" — ``GET /api/v1/me/workspaces``.
    """
    # Tags: ``identity`` surfaces this under the same OpenAPI section
    # as ``/auth/me`` (spec §01 context map + §12 Auth); ``auth`` keeps
    # fine-grained client filtering symmetrical with the sibling
    # ``/auth/me`` route. ``workspaces`` is added so SPA-side filters
    # ("which endpoints power the switcher?") have a stable handle.
    router = APIRouter(
        prefix="/me",
        tags=["identity", "auth", "workspaces"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    @router.get(
        "/workspaces",
        response_model=list[WorkspaceSwitcherEntry],
        operation_id="auth.me.workspaces.list",
        summary="Return the caller's workspaces (switcher payload)",
        openapi_extra={
            # Pin the CLI surface; the bare heuristic would classify a
            # bare GET as ``list``, which is correct here, but spelling
            # it explicitly future-proofs the committed surface JSON
            # against a heuristic change.
            "x-cli": {
                "group": "auth",
                "verb": "workspaces",
                "summary": "List the workspaces the caller can switch into",
                "mutates": False,
            },
        },
    )
    def list_my_workspaces(
        request: Request,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias="crewday_session"),
        ] = None,
    ) -> list[WorkspaceSwitcherEntry]:
        """Validate the session cookie, return the switcher payload.

        Returns 401 when the cookie is absent or rejected by
        :func:`auth_session.validate`. Returns ``[]`` when the user
        has no workspace memberships (a freshly-signed-up user before
        their first invite accept) — never 404, because the caller
        successfully authenticated; the absence of memberships is
        legitimate state, not a missing resource.
        """
        cookie_value = session_cookie_primary or session_cookie_dev
        if not cookie_value:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_required"},
            )
        ua, accept_language = _client_headers(request)
        try:
            user_id = auth_session.validate(
                session,
                cookie_value=cookie_value,
                ua=ua,
                accept_language=accept_language,
            )
        except auth_session.UserArchived as exc:
            # Archive gate (cd-uceg) — see :func:`_validated_session_user`
            # for the rationale; same wire shape on the switcher route
            # so the SPA branches uniformly.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": auth_session.USER_ARCHIVED_WIRE_CODE},
            ) from exc
        except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_invalid"},
            ) from exc

        with tenant_agnostic():
            user = session.get(User, user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_invalid"},
            )

        return _load_switcher_entries(session, user_id=user.id)

    return router
