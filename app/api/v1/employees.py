"""Employees roster HTTP router — ``/employees`` (cd-g6nf, cd-jtgo).

Mounted inside the ``/w/<slug>/api/v1`` tree by the app factory.
v1 surface:

* ``GET /employees`` — workspace roster as a flat ``Employee[]``
  array. Manager-only (``employees.read``); workers fall through
  to 403.

**Why a dedicated ``/employees`` router rather than reshape
``/users``?** The SPA's manager pages
(``app/web/src/pages/manager/SchedulesPage.tsx``,
``ExpensesApprovalsPage.tsx``, ``EmployeesPage.tsx``, …) call
``fetchJson<Employee[]>('/api/v1/employees')`` verbatim — a flat
array, no pagination envelope. Refactoring every call site onto
``/users`` + ``/work_engagements`` while the SPA is still being
built would burn turns on a no-op rename. Spec §12 records the
decision (cd-jtgo): keep ``/employees`` as the manager roster
surface; the underlying primitives stay first-class for
non-roster paths.

**Why a bare array, not the ``{data, next_cursor, has_more}``
envelope?** Same reason as above — the SPA's
``fetchJson<Employee[]>`` calls expect a flat list. cd-g6nf calls
out cursor-paginating this endpoint as a separate follow-up task
that pairs the envelope shape with an SPA call-site migration;
doing it in this turn would break the manager pages on first load.
The action catalog gate keeps the page bounded enough that an
unbounded fetch is a manager's choice (they can already enumerate
the roster from the existing ``/users`` paginated endpoint).

**Why manager-only?** The roster projection joins identity-level
profile fields (display_name, email, locale, timezone) with
workspace-scoped engagement / role-grant / property assignments.
A worker cross-roster view is a privacy regression (§15 PII
minimisation); the worker-side surfaces (``/auth/me``,
``/me/schedule``, …) carry the per-actor data instead.

**Field defaults.** The current v1 ORM does not yet carry every
field the SPA's :class:`Employee` shape declares — phone,
weekly_availability, capabilities, evidence_policy,
preferred_locale, settings_override, language, villas. We emit
type-safe defaults (``""`` for strings, ``{}`` for maps,
``"inherit"`` for ``evidence_policy``, ``[]`` for villas) so the
SPA's typed contract stays honoured without a brittle ``null``
bypass. Each default is documented inline against the column it
will eventually resolve from once the matching ORM widening lands.

**Avatar URL.** Spec §12 "avatar_url in user serialisations"
mandates ``/api/v1/files/{file_id}/blob`` for non-null avatars.
The current :class:`User` ORM row carries ``avatar_blob_hash`` but
not ``avatar_file_id`` (the ``file`` table lands in cd-6vq5's
follow-up). Until then we emit ``avatar_url=None`` +
``avatar_file_id=None`` and let the SPA fall back to
``avatar_initials`` — exactly the contract the SPA's
:class:`Avatar` component already honours when ``url`` is null.

See ``docs/specs/12-rest-api.md`` §"Users / work roles / settings",
``docs/specs/05-employees-and-roles.md`` §"User (as worker)" /
§"Action catalog", and ``app/web/src/types/employee.ts``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
    WorkRole,
)
from app.api.deps import current_workspace_context, db_session
from app.authz.dep import Permission
from app.tenancy import WorkspaceContext, tenant_agnostic

__all__ = [
    "EmployeeResponse",
    "build_employees_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Static defaults for fields the v1 ORM does not yet carry. Each constant
# is named after the SPA field it backs so a future migration that lands
# the real column can grep for the constant and remove it in lockstep.
# ---------------------------------------------------------------------------

# §05 mandates ``inherit`` as the per-user evidence-policy default; the
# resolver descends to property + workspace tiers when the user-level
# value stays inherited. The column itself does not exist on
# :class:`User` yet — a sibling task will add it.
_EVIDENCE_POLICY_DEFAULT: Literal["inherit"] = "inherit"

# Mirrors :data:`app.api.v1.auth.me._DEFAULT_LOCALE` — keeps the SPA
# contract honoured until the locale / language column widening lands.
# Returning the locale (rather than a country bias) follows the §05
# "Worker settings" cascade: nearest-explicit-value-wins, with a sane
# deployment-level default at the bottom.
_LANGUAGE_DEFAULT: str = "en"


# ---------------------------------------------------------------------------
# Wire-facing shape — flat ``Employee`` matching app/web/src/types/employee.ts.
# ---------------------------------------------------------------------------


class EmployeeResponse(BaseModel):
    """Flat ``Employee`` projection — see module docstring for the join.

    Mirrors :class:`Employee` in ``app/web/src/types/employee.ts``
    field-for-field. Optional fields the v1 ORM does not yet carry
    are documented inline; future migrations replace the static
    defaults with the real column reads in lockstep.
    """

    id: str
    name: str
    roles: list[str]
    properties: list[str]
    avatar_initials: str
    avatar_file_id: str | None
    avatar_url: str | None
    phone: str
    email: str
    started_on: date
    capabilities: dict[str, bool | None]
    workspaces: list[str]
    villas: list[str]
    language: str
    weekly_availability: dict[str, tuple[str, str] | None]
    evidence_policy: Literal["inherit", "require", "optional", "forbid"]
    preferred_locale: str | None
    # ``Record<string, unknown>`` on the SPA side. ``object`` keeps the
    # value space soundly typed without opting out of mypy strict (which
    # ``Any`` would). Callers re-narrow with ``isinstance`` if they ever
    # consume a value — today the field is a static ``{}`` placeholder
    # until the per-user settings_override column lands.
    settings_override: dict[str, object]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_name_for(user: User) -> str:
    """Return a non-empty display name for the SPA's ``name`` field.

    The :class:`User` schema declares ``display_name`` as ``NOT NULL``,
    but nothing prevents a caller from writing whitespace into it. A
    blank ``name`` would render as an empty cell in every roster view
    — graceless and indistinguishable from a load error. Fall back to
    the email's local part (already non-empty by NOT NULL) when the
    display name carries no glyphs, then to the full email as a last
    resort. Mirrors the SPA's "show *something*" instinct without
    bypassing the type contract: ``name`` stays a non-empty ``str``.
    """
    if user.display_name and user.display_name.strip():
        return user.display_name
    local_part = user.email.split("@", 1)[0].strip()
    return local_part or user.email


def _initials_of(name: str) -> str:
    """Return the same initials the SPA's :func:`initialsOf` computes.

    Mirrors :func:`initialsOf` in
    ``app/web/src/layouts/EmployeeLayout.tsx`` — first letter of up
    to two leading whitespace-delimited tokens, uppercased. A name
    with no letters falls back to ``"·"`` so the avatar circle never
    renders empty. Keeping the rule in lockstep with the SPA helper
    means the same employee gets the same initials regardless of
    which surface looks them up first.
    """
    tokens = [t for t in name.strip().split() if t][:2]
    out = "".join(t[0].upper() for t in tokens if t)
    return out or "·"


def _list_workspace_users(
    session: Session,
    ctx: WorkspaceContext,
) -> list[str]:
    """Return every ``user_id`` with a live membership in the workspace.

    Workspace-scoped through the ORM tenant filter on
    :class:`UserWorkspace`. Order is the table's natural insert order,
    which is "good enough" for a flat roster — the SPA sorts client-
    side. ULID ascending would be a tighter contract; we leave the
    explicit ordering to a follow-up that pairs it with a real cursor.
    """
    stmt = select(UserWorkspace.user_id).where(
        UserWorkspace.workspace_id == ctx.workspace_id
    )
    return list(session.scalars(stmt).all())


def _load_users(session: Session, *, user_ids: list[str]) -> dict[str, User]:
    """Return ``{user_id: User}`` for the given identity rows.

    :class:`User` is identity-scoped, not workspace-scoped — the ORM
    tenant filter does not apply, so the lookup runs under
    :func:`tenant_agnostic`. The membership check upstream guarantees
    every id resolves; a missing row would point at a broken
    invariant (membership without user) and the caller surfaces it as
    an empty payload rather than crashing.
    """
    if not user_ids:
        return {}
    with tenant_agnostic():
        stmt = select(User).where(User.id.in_(user_ids))
        rows = session.scalars(stmt).all()
    return {u.id: u for u in rows}


def _load_active_engagements(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_ids: list[str],
) -> dict[str, WorkEngagement]:
    """Return ``{user_id: active engagement}`` for the workspace.

    The partial UNIQUE index on
    ``(user_id, workspace_id) WHERE archived_on IS NULL`` guarantees
    at most one active row per user — the dict shape is sound.
    Archived engagements are intentionally excluded; the roster shows
    *current* employees. A user without an active engagement still
    appears (their ``UserWorkspace`` row keeps them visible) but
    their ``started_on`` falls back to the engagement-less default.
    """
    if not user_ids:
        return {}
    stmt = select(WorkEngagement).where(
        WorkEngagement.workspace_id == ctx.workspace_id,
        WorkEngagement.user_id.in_(user_ids),
        WorkEngagement.archived_on.is_(None),
    )
    return {row.user_id: row for row in session.scalars(stmt).all()}


def _load_role_keys_by_user(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_ids: list[str],
) -> dict[str, list[str]]:
    """Return ``{user_id: [work_role.key, ...]}`` for the workspace.

    Joins :class:`UserWorkRole` (the user-x-role assignment) with
    :class:`WorkRole` (the role catalogue) so the SPA gets stable
    slugs (``maid``, ``cook``) rather than ULIDs. Both tables carry a
    ``deleted_at`` soft-delete column and **both** are filtered: a
    user's historical role assignment does not surface, and a
    retired :class:`WorkRole` (whose chip would be a phantom slug)
    is also pruned even if some live ``UserWorkRole`` row still
    references it. The retire-cascade lives in the domain service
    (§05 "Archive / reinstate"); this query is the read-side guard.

    Order within each user is deterministic by ``WorkRole.key`` so
    the SPA renders chips in a stable order across reloads.
    Duplicate role keys (same user, role, started_on but different
    rows) are de-duplicated; the §05 invariant is "at most one
    active row per (user, role)" so duplicates would already be a
    data bug, but the de-dup here keeps the surface tolerant.
    """
    if not user_ids:
        return {}
    stmt = (
        select(UserWorkRole.user_id, WorkRole.key)
        .join(WorkRole, WorkRole.id == UserWorkRole.work_role_id)
        .where(
            UserWorkRole.workspace_id == ctx.workspace_id,
            UserWorkRole.user_id.in_(user_ids),
            UserWorkRole.deleted_at.is_(None),
            WorkRole.deleted_at.is_(None),
        )
    )
    seen: dict[str, set[str]] = defaultdict(set)
    for user_id, key in session.execute(stmt).all():
        seen[user_id].add(key)
    return {uid: sorted(keys) for uid, keys in seen.items()}


def _load_property_ids_by_user(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_ids: list[str],
) -> dict[str, list[str]]:
    """Return ``{user_id: [property_id, ...]}`` derived from role grants.

    Property scoping flows through :class:`RoleGrant.scope_property_id`:
    a worker grant pinned to one property narrows them to that
    property; a workspace-scoped grant (``scope_property_id IS NULL``)
    fans out across every property the workspace owns or shares.

    The fan-out for workspace-scoped grants is computed from
    :class:`PropertyWorkspace` joined with :class:`Property` — the
    join enforces ``Property.deleted_at IS NULL`` so retired
    properties never reach the SPA. Property-pinned grants are
    additionally gated through the same live-id set so a grant
    pointing at a retired property collapses to an empty list, not
    a dangling id.
    """
    if not user_ids:
        return {}

    grants_stmt = select(RoleGrant.user_id, RoleGrant.scope_property_id).where(
        RoleGrant.workspace_id == ctx.workspace_id,
        RoleGrant.user_id.in_(user_ids),
    )
    grants_by_user: dict[str, list[str | None]] = defaultdict(list)
    for user_id, scope_property_id in session.execute(grants_stmt).all():
        grants_by_user[user_id].append(scope_property_id)

    if not grants_by_user:
        return {}

    # Precompute the workspace's **live** property ids — used both for
    # the workspace-scoped grant fan-out and as a soft-delete filter
    # for property-pinned grants. Joining :class:`Property` lets us
    # exclude rows that have been retired (``deleted_at IS NOT NULL``)
    # without surfacing their ids on the roster — a soft-deleted
    # property has no live identity for the SPA to render.
    live_property_ids: set[str] = set(
        session.scalars(
            select(PropertyWorkspace.property_id)
            .join(Property, Property.id == PropertyWorkspace.property_id)
            .where(
                PropertyWorkspace.workspace_id == ctx.workspace_id,
                Property.deleted_at.is_(None),
            )
        ).all()
    )

    out: dict[str, list[str]] = {}
    for user_id, scope_property_ids in grants_by_user.items():
        bucket: set[str] = set()
        has_workspace_grant = any(p is None for p in scope_property_ids)
        if has_workspace_grant:
            bucket.update(live_property_ids)
        for pid in scope_property_ids:
            # Property-pinned grants land on a single property; gate
            # them through ``live_property_ids`` so a grant referencing
            # a retired property never leaks into the roster. A grant
            # whose target was retired and whose user has no other
            # property grant ends up with an empty ``properties`` list,
            # which is the correct visible state.
            if pid is not None and pid in live_property_ids:
                bucket.add(pid)
        out[user_id] = sorted(bucket)
    return out


def _project_employee(
    user: User,
    *,
    workspace_id: str,
    engagement: WorkEngagement | None,
    role_keys: list[str],
    property_ids: list[str],
) -> EmployeeResponse:
    """Build one :class:`EmployeeResponse` from the joined rows.

    The fan-in of optional / yet-to-land fields is documented at the
    module top. We resolve ``started_on`` from the active engagement
    when present and fall back to the user's ``created_at`` date
    otherwise — a user without an active engagement should not
    surface a NULL ``started_on`` to the SPA (the type is ``string``
    in :class:`Employee`).
    """
    if engagement is not None:
        started_on = engagement.started_on
    else:
        # SPA contract requires a date string; ``user.created_at`` is
        # the closest defensible fallback (the user joined the
        # workspace at some point — the membership row's
        # ``added_at`` would be tighter, but the join cost is not
        # worth it for a user-without-engagement edge case that
        # mostly happens during invite-accept).
        started_on = user.created_at.date()

    name = _display_name_for(user)
    return EmployeeResponse(
        id=user.id,
        name=name,
        roles=role_keys,
        properties=property_ids,
        avatar_initials=_initials_of(name),
        # See module docstring — the avatar pipeline lands with the
        # ``file`` table in cd-6vq5's follow-up. Until then both
        # ``file_id`` and ``url`` stay null and the SPA renders the
        # initials circle.
        avatar_file_id=None,
        avatar_url=None,
        phone="",
        email=user.email,
        started_on=started_on,
        capabilities={},
        workspaces=[workspace_id],
        villas=[],
        language=user.locale or _LANGUAGE_DEFAULT,
        weekly_availability={},
        evidence_policy=_EVIDENCE_POLICY_DEFAULT,
        preferred_locale=user.locale,
        settings_override={},
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_employees_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the manager roster.

    Mounted by the v1 app factory at
    ``/w/<slug>/api/v1/employees``. Tests instantiate it directly via
    :func:`tests.unit.api.v1.identity.conftest.build_client` to keep
    the dependency-override cache per-case.
    """
    api = APIRouter(prefix="/employees", tags=["identity", "employees"])

    read_gate = Depends(Permission("employees.read", scope_kind="workspace"))

    @api.get(
        "",
        response_model=list[EmployeeResponse],
        operation_id="employees.list",
        summary="List employees in the caller's workspace (manager roster)",
        dependencies=[read_gate],
        openapi_extra={
            "x-cli": {
                "group": "employees",
                "verb": "list",
                "summary": "List employees in a workspace",
                "mutates": False,
            },
        },
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
    ) -> list[EmployeeResponse]:
        """Return every employee in the workspace as a flat array.

        Joins :class:`UserWorkspace` (membership), :class:`User`
        (identity profile), :class:`WorkEngagement` (active engagement
        for the ``started_on`` field), :class:`UserWorkRole` +
        :class:`WorkRole` (role keys for the chip set), and
        :class:`RoleGrant` + :class:`PropertyWorkspace` (property
        assignments for the schedule / approval pages).

        Bare-array response — see module docstring for the rationale.
        """
        user_ids = _list_workspace_users(session, ctx)
        if not user_ids:
            return []

        users = _load_users(session, user_ids=user_ids)
        engagements = _load_active_engagements(session, ctx, user_ids=user_ids)
        role_keys = _load_role_keys_by_user(session, ctx, user_ids=user_ids)
        property_ids = _load_property_ids_by_user(session, ctx, user_ids=user_ids)

        out: list[EmployeeResponse] = []
        for user_id in user_ids:
            user = users.get(user_id)
            if user is None:
                # Membership row without a backing identity — broken
                # invariant. Skip rather than crash; an upstream
                # cleanup task can reconcile.
                continue
            out.append(
                _project_employee(
                    user,
                    workspace_id=ctx.workspace_id,
                    engagement=engagements.get(user_id),
                    role_keys=role_keys.get(user_id, []),
                    property_ids=property_ids.get(user_id, []),
                )
            )
        return out

    return api
