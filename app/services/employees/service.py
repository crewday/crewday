"""Employees domain service — workspace-scoped CRUD for worker profiles.

Routes every workspace-scoped read / write through the
:class:`~app.domain.identity.ports.MembershipRepository` Protocol so
this module never imports the ``user_workspace`` / ``work_engagement``
/ ``user_work_role`` ORM classes directly (cd-dv2 / cd-hso7).

Owns four operations on a user *as seen inside a single workspace*:

* :func:`get_employee` — read a user's profile projection scoped to
  the caller's workspace (workspace-scoped membership required;
  cross-workspace lookups collapse to 404 per §01 "tenant surface is
  not enumerable").
* :func:`update_profile` — partial update of the identity-level
  profile fields that matter for employees. Callers who target
  themselves pass through without an authz check (identity-scoped
  self-edit); callers who target someone else must hold
  ``users.edit_profile_other`` (default ``owners``, ``managers`` per
  the action catalog).
* :func:`archive_employee` — soft-archive the user's work engagement
  row for this workspace AND every active user-work-role row in the
  same workspace. Idempotent — re-archiving a row that is already
  archived is a no-op that still writes an audit entry so the trail
  remains linear.
* :func:`reinstate_employee` — reverse archive scoped to the caller's
  workspace. Clears the engagement's ``archived_on`` and the matching
  user-work-role ``deleted_at`` rows. Idempotent. Does NOT clear
  ``users.archived_at`` — that is the deployment-level reinstate's
  job.
* :func:`reinstate_user_deployment` — deployment-wide reinstate
  (cd-pb8p). Clears ``users.archived_at`` AND reinstates every
  ``work_engagement`` the user holds across every workspace, plus
  the matching ``user_work_role`` rows. Authority gate is membership
  in ``owners@deployment``; non-deployment-owners get
  :class:`~app.authz.PermissionDenied` (router maps to 403).
* :func:`seed_pending_work_engagement` — re-export of the accept-time
  seeder lifted into :mod:`app.domain.identity.work_engagements` so
  :mod:`app.domain.identity.membership` can call it without crossing
  the domain → services boundary (cd-hso7). Inserts a minimal pending
  engagement row at the moment the invitee completes their passkey
  challenge. Nothing workspace-scoped is seeded until accept time
  (§03 "Additional users (invite → click-to-accept)").

**Tenancy.** Every read / write passes through the ORM tenant
filter on the registered workspace-scoped tables
(``work_engagement``, ``user_work_role``). The repo also re-asserts
the ``workspace_id`` predicate explicitly as defence-in-depth,
matching the convention used in :mod:`app.domain.places.property_service`
and :mod:`app.domain.identity.role_grants`.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation
writes one :mod:`app.audit` row in the same transaction.

**Audit.** Every mutation emits one ``employee.*`` audit row with
PII-safe payloads. Email + display_name go through the same
redaction seam as the rest of :mod:`app.audit` so a profile update
that lands an email value in a diff cannot survive into on-disk
logs.

**Cross-workspace reinstate (cd-pb8p).** §05 "Archive / reinstate"
describes a deployment-wide reinstate that also clears
``users.archived_at``. The workspace-local :func:`reinstate_employee`
reverses an archive inside ONE workspace and never touches the
identity-level ``users`` row; the deployment-level
:func:`reinstate_user_deployment` clears ``users.archived_at`` AND
reinstates every engagement the user holds across every workspace.
The two surfaces are exposed by ``POST /users/{id}/reinstate`` via
the ``?scope=workspace`` (default) and ``?scope=deployment`` query
parameter. The deployment path requires the caller to belong to the
``owners@deployment`` set (:func:`app.authz.deployment_owners.is_deployment_owner`).
A fresh magic link for the reinstated user is **not** minted here —
spec §05 calls for one but the issuance lives in the dedicated
``POST /users/{id}/magic_link`` route; an operator workflow chains
the two calls.

**Reinstate sweep overreach (follow-up — cd-9vi3).** v1
:func:`reinstate_employee` clears ``deleted_at`` on every archived
:class:`UserWorkRole` in the (user, workspace) pair rather than
only the rows the paired archive marked. A role ended manually
before the archive will come back on reinstate. Accepted for MVP
scope; tightening the sweep to the archive-time window is tracked
as cd-9vi3.

See ``docs/specs/05-employees-and-roles.md`` §"User (as worker)",
§"Work engagement", §"Archive / reinstate",
``docs/specs/02-domain-model.md`` §"users", §"user_workspace",
§"work_engagement", §"role_grants", and
``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.audit import write_audit
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.authz.deployment_owners import is_deployment_owner
from app.domain.identity.ports import (
    MembershipRepository,
    UserWorkspaceRow,
    WorkEngagementRow,
)

# Re-exported for back-compat: the seeder used to live here, and
# tests + ``app.services.employees`` external surface still import
# it from this module. The canonical home is now
# :mod:`app.domain.identity.work_engagements` so the identity-side
# call site (``membership._activate_invite``) does not have to cross
# the domain → services boundary (cd-hso7).
from app.domain.identity.work_engagements import seed_pending_work_engagement
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "EmployeeNotFound",
    "EmployeeProfileUpdate",
    "EmployeeView",
    "ProfileFieldForbidden",
    "archive_employee",
    "get_employee",
    "iter_active_engagements",
    "reinstate_employee",
    "reinstate_user_deployment",
    "seed_pending_work_engagement",
    "update_profile",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmployeeNotFound(LookupError):
    """The target user is not visible as an employee in this workspace.

    404-equivalent. Raised when ``user_id`` is unknown, or when the
    user exists but holds no :class:`UserWorkspace` membership row in
    the caller's workspace — the cross-tenant collapse to "not found"
    is deliberate (§01 "tenant surface is not enumerable").
    """


class ProfileFieldForbidden(PermissionError):
    """Caller tried to touch a field they may not edit.

    403-equivalent. Fires when a non-self caller attempts to update a
    user they do not hold ``users.edit_profile_other`` on. The router
    maps this to :class:`~app.domain.errors.Forbidden`.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


# Caps lifted verbatim from :mod:`app.adapters.db.identity.models` and
# the existing invite body shape so API callers never straddle a
# mismatched limit between the two surfaces.
_MAX_DISPLAY_NAME_LEN = 160
_MAX_LOCALE_LEN = 35
_MAX_TIMEZONE_LEN = 64


class EmployeeProfileUpdate(BaseModel):
    """Partial update body for :func:`update_profile`.

    Every field is optional; an omitted field keeps its current value.
    A field set to ``None`` explicitly is treated as "clear it" (for
    nullable columns only — ``display_name`` is NOT NULL and rejects
    ``None`` at the DTO boundary via :meth:`_reject_display_name_null`).

    The shape is intentionally narrow: §02 ``users`` lists richer
    columns (``full_legal_name``, ``phone_e164``, ``emergency_contact``,
    ``notes_md``, ``agent_approval_mode``, ``preferred_locale``,
    ``languages``, ``avatar_file_id``) but the ORM model today only
    carries ``display_name`` / ``locale`` / ``timezone`` plus the
    avatar-hash column (avatar writes live on ``/api/v1/me/avatar`` —
    cd-6vq5, out of scope here). Later tasks that widen the ORM must
    extend this DTO in lockstep.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(
        default=None, min_length=1, max_length=_MAX_DISPLAY_NAME_LEN
    )
    locale: str | None = Field(default=None, max_length=_MAX_LOCALE_LEN)
    timezone: str | None = Field(default=None, max_length=_MAX_TIMEZONE_LEN)

    @model_validator(mode="after")
    def _reject_display_name_null(self) -> EmployeeProfileUpdate:
        """Reject an explicit ``display_name=None`` at the DTO boundary.

        Pydantic's ``min_length`` constraint only fires when the value
        is a string, so ``display_name=None`` would otherwise slip
        through and hit the :class:`User` column's NOT NULL contract
        as a 500. Raising here surfaces the mistake as a 422 validation
        error alongside the rest of the field-shape violations.
        """
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("display_name cannot be cleared; it is NOT NULL")
        return self


@dataclass(frozen=True, slots=True)
class EmployeeView:
    """Immutable read projection of a user as seen inside a workspace.

    Carries the identity-level fields plus a boolean ``is_archived``
    derived from whether the user holds any active
    :class:`WorkEngagement` in this workspace. The richer engagement
    columns (kind, started_on, supplier_org_id, …) ride on
    :class:`WorkEngagement` directly; this projection is deliberately
    minimal until the engagements service (future) lands.
    """

    id: str
    email: str
    display_name: str
    locale: str | None
    timezone: str | None
    avatar_blob_hash: str | None
    engagement_archived_on: date | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or SystemClock."""
    return (clock if clock is not None else SystemClock()).now()


def _assert_membership(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
) -> UserWorkspaceRow:
    """Return the caller-scoped membership row or raise.

    Workspace-scoped membership is the authority check for "is this
    user visible as an employee here?". No row → 404 (not 403), per
    §01. The repo's ``get_user_workspace`` already pins the lookup on
    ``workspace_id`` as defence-in-depth.
    """
    row = repo.get_user_workspace(workspace_id=ctx.workspace_id, user_id=user_id)
    if row is None:
        raise EmployeeNotFound(user_id)
    return row


def _load_user(session: Session, *, user_id: str) -> User:
    """Load a :class:`User` row by id under :func:`tenant_agnostic`.

    ``user`` is identity-scoped, not workspace-scoped. The caller
    must have already verified workspace membership via
    :func:`_assert_membership` before reaching this helper. The
    identity-side ORM read still uses the bare session — the
    ``users`` table sits outside the cd-dv2 stopgap (and the
    email-change flow already has its own
    :class:`~app.domain.identity.email_change_ports.EmailChangeRepository`
    seam); a follow-up task can route this through a sibling
    identity-side port if needed.
    """
    # ``users`` is identity-scoped (no workspace_id column); caller
    # verifies workspace membership via :func:`_assert_membership` first.
    # justification: identity-scoped table with no workspace pin.
    with tenant_agnostic():
        row = session.get(User, user_id)
    if row is None:
        raise EmployeeNotFound(user_id)
    return row


def _row_to_view(
    user: User,
    *,
    engagement: WorkEngagementRow | None,
) -> EmployeeView:
    """Project a :class:`User` + optional engagement into an :class:`EmployeeView`."""
    return EmployeeView(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        locale=user.locale,
        timezone=user.timezone,
        avatar_blob_hash=user.avatar_blob_hash,
        engagement_archived_on=(
            engagement.archived_on if engagement is not None else None
        ),
        created_at=user.created_at,
    )


def _require_edit_other(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
) -> None:
    """Enforce ``users.edit_profile_other`` on the caller's workspace.

    Wraps :func:`app.authz.require` + translates a caller-bug
    (unknown key / invalid scope) into a :class:`RuntimeError` so the
    router can surface it as a 500 instead of a 403. Matches the
    :func:`app.domain.time.shifts._require_capability` shape. The
    threaded ``repo.session`` keeps the authz check inside the
    caller's UoW; the authz module still takes a concrete session
    until its own port lands.
    """
    try:
        require(
            repo.session,
            ctx,
            action_key="users.edit_profile_other",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'users.edit_profile_other': {exc!s}"
        ) from exc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_employee(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
) -> EmployeeView:
    """Return the employee projection for ``user_id`` in the caller's workspace.

    Raises :class:`EmployeeNotFound` when the user is unknown to this
    workspace.
    """
    _assert_membership(repo, ctx, user_id=user_id)
    user = _load_user(repo.session, user_id=user_id)
    engagement = repo.get_active_engagement(
        workspace_id=ctx.workspace_id, user_id=user_id
    )
    return _row_to_view(user, engagement=engagement)


# ---------------------------------------------------------------------------
# Writes — profile update
# ---------------------------------------------------------------------------


def update_profile(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    body: EmployeeProfileUpdate,
    clock: Clock | None = None,
) -> EmployeeView:
    """Partial update of an employee's profile fields.

    Authorisation:

    * ``ctx.actor_id == user_id`` → self-edit. No capability check.
    * Otherwise → caller must hold ``users.edit_profile_other`` on the
      workspace. A missing capability raises
      :class:`~app.authz.PermissionDenied`; the router maps it to 403.

    Raises :class:`EmployeeNotFound` if the user is not a member of
    the caller's workspace. The membership check runs BEFORE the
    capability check so a cross-tenant probe still collapses to 404.

    One ``employee.profile_updated`` audit row per call, carrying a
    redacted before / after diff of the changed fields.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    _assert_membership(repo, ctx, user_id=user_id)

    if ctx.actor_id != user_id:
        try:
            _require_edit_other(repo, ctx)
        except PermissionDenied as exc:
            raise ProfileFieldForbidden(
                f"caller {ctx.actor_id!r} may not edit profile of {user_id!r}"
            ) from exc

    user = _load_user(repo.session, user_id=user_id)

    # ``model_fields_set`` is the Pydantic-v2 truth of "which fields
    # did the caller actually send?" — we use it to distinguish
    # "explicitly set to None" (clear) from "omitted" (keep). For
    # ``display_name`` a ``None`` is rejected at the DTO layer (the
    # column is NOT NULL); the other two columns are nullable.
    sent = body.model_fields_set
    if not sent:
        # No-op update — still return the current view so the router
        # doesn't have to special-case an empty body. No audit row:
        # a zero-change write is not a forensic event.
        engagement = repo.get_active_engagement(
            workspace_id=ctx.workspace_id, user_id=user_id
        )
        return _row_to_view(user, engagement=engagement)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if "display_name" in sent:
        # ``display_name=None`` is rejected by
        # :meth:`EmployeeProfileUpdate._reject_display_name_null` at
        # the DTO boundary (422). The ``assert`` narrows the type for
        # mypy and guards against a caller that bypassed the DTO.
        assert body.display_name is not None, (
            "display_name null reached service layer — DTO guard bypassed"
        )
        if body.display_name != user.display_name:
            before["display_name"] = user.display_name
            after["display_name"] = body.display_name
            user.display_name = body.display_name

    if "locale" in sent and body.locale != user.locale:
        before["locale"] = user.locale
        after["locale"] = body.locale
        user.locale = body.locale

    if "timezone" in sent and body.timezone != user.timezone:
        before["timezone"] = user.timezone
        after["timezone"] = body.timezone
        user.timezone = body.timezone

    if not after:
        # Every sent field matched the current value — no actual change.
        engagement = repo.get_active_engagement(
            workspace_id=ctx.workspace_id, user_id=user_id
        )
        return _row_to_view(user, engagement=engagement)

    repo.session.flush()

    write_audit(
        repo.session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="employee.profile_updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )

    engagement = repo.get_active_engagement(
        workspace_id=ctx.workspace_id, user_id=user_id
    )
    return _row_to_view(user, engagement=engagement)


# ---------------------------------------------------------------------------
# Writes — archive / reinstate
# ---------------------------------------------------------------------------


def archive_employee(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    clock: Clock | None = None,
) -> EmployeeView:
    """Archive the user's engagement + every user_work_role in this workspace.

    §05 "Archive / reinstate" scope #2 ("Archive a work_engagement"):

    * Set ``WorkEngagement.archived_on = today`` on the active
      engagement (if any). The partial UNIQUE index guarantees at
      most one matches; archiving the same user twice is a no-op on
      the engagement side.
    * Soft-delete every active :class:`UserWorkRole` in this
      workspace by stamping ``deleted_at``.

    **Idempotent.** A repeated call with no live rows to touch is a
    no-op for the DB state, but still writes an audit entry so the
    forensic trail does not swallow the operator action.

    Returns the employee view so the router can echo the archived
    engagement timestamp back.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    today: date = now.date()

    _assert_membership(repo, ctx, user_id=user_id)
    # Archive is a write on *other* users' workspace pipeline, so the
    # ``users.archive`` capability (not ``edit_profile_other``) gates
    # it. Matching §05 spec which lists archive among the capabilities
    # owners + managers hold by default.
    _require_archive(repo, ctx)

    engagement = repo.get_active_engagement(
        workspace_id=ctx.workspace_id, user_id=user_id
    )
    engagement_was_active = engagement is not None
    if engagement is not None:
        engagement = repo.set_engagement_archived_on(
            workspace_id=ctx.workspace_id,
            engagement_id=engagement.id,
            archived_on=today,
            updated_at=now,
        )

    active_roles = repo.list_user_work_roles(
        workspace_id=ctx.workspace_id, user_id=user_id, active_only=True
    )
    archived_role_ids: list[str] = [r.id for r in active_roles]
    repo.archive_user_work_roles(
        workspace_id=ctx.workspace_id,
        role_ids=archived_role_ids,
        deleted_at=now,
        ended_on=today,
    )
    repo.session.flush()

    write_audit(
        repo.session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="employee.archived",
        diff={
            "user_id": user_id,
            "engagement_id": engagement.id if engagement is not None else None,
            "engagement_was_active": engagement_was_active,
            "archived_user_work_role_ids": archived_role_ids,
        },
        clock=resolved_clock,
    )

    user = _load_user(repo.session, user_id=user_id)
    refreshed_engagement = repo.get_active_engagement(
        workspace_id=ctx.workspace_id, user_id=user_id
    )
    return _row_to_view(user, engagement=refreshed_engagement)


def reinstate_employee(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    clock: Clock | None = None,
) -> EmployeeView:
    """Reverse archive for a user in the caller's workspace.

    §05 "Archive / reinstate" — reinstates the user's most recent
    :class:`WorkEngagement` (clearing ``archived_on``) AND every
    archived :class:`UserWorkRole` in this workspace (clearing
    ``deleted_at`` / ``ended_on``). Does NOT clear
    ``users.archived_at`` — that is the deployment-level
    :func:`reinstate_user_deployment`'s job.

    Idempotent. A repeated call on an already-active user writes an
    audit row with ``changed_rows = 0`` so the trail is linear.

    **Reinstate sweep overreach (cd-9vi3).** The sweep clears
    ``deleted_at`` on *every* archived :class:`UserWorkRole` for the
    (user, workspace) pair — not only the rows the corresponding
    archive touched. Spec §05 describes per-row reinstatement; if an
    operator had manually ended a single role before the archive,
    this path brings it back. Accepted for the MVP scope; tightening
    the sweep is tracked as cd-9vi3.

    Returns the refreshed employee view.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _assert_membership(repo, ctx, user_id=user_id)
    _require_archive(repo, ctx)

    engagement = repo.get_latest_engagement(
        workspace_id=ctx.workspace_id, user_id=user_id
    )
    engagement_was_archived = (
        engagement is not None and engagement.archived_on is not None
    )
    if engagement is not None and engagement.archived_on is not None:
        engagement = repo.set_engagement_archived_on(
            workspace_id=ctx.workspace_id,
            engagement_id=engagement.id,
            archived_on=None,
            updated_at=now,
        )

    all_roles = repo.list_user_work_roles(
        workspace_id=ctx.workspace_id, user_id=user_id, active_only=False
    )
    reinstated_role_ids: list[str] = [
        r.id for r in all_roles if r.deleted_at is not None
    ]
    repo.reinstate_user_work_roles(
        workspace_id=ctx.workspace_id,
        role_ids=reinstated_role_ids,
    )
    repo.session.flush()

    write_audit(
        repo.session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="employee.reinstated",
        diff={
            "user_id": user_id,
            "engagement_id": engagement.id if engagement is not None else None,
            "engagement_was_archived": engagement_was_archived,
            "reinstated_user_work_role_ids": reinstated_role_ids,
        },
        clock=resolved_clock,
    )

    user = _load_user(repo.session, user_id=user_id)
    refreshed_engagement = repo.get_active_engagement(
        workspace_id=ctx.workspace_id, user_id=user_id
    )
    return _row_to_view(user, engagement=refreshed_engagement)


def reinstate_user_deployment(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    clock: Clock | None = None,
) -> EmployeeView:
    """Deployment-wide reinstate for ``user_id`` (cd-pb8p, §05 scope #3).

    Clears ``users.archived_at`` AND reinstates every
    :class:`WorkEngagement` the user holds across every workspace,
    plus the matching :class:`UserWorkRole` rows. Runs in the caller's
    UoW — the cross-workspace mutations land atomically with the
    identity-row clear and every audit row.

    Authority: caller MUST belong to the ``owners@deployment`` set
    (:func:`app.authz.deployment_owners.is_deployment_owner`). Workspace
    owners and managers do NOT hold this authority — the deployment-
    wide tombstone clear is a root-tier operation. Non-deployment-
    owners get :class:`~app.authz.PermissionDenied`; the router maps
    it to 403 ``permission_denied``.

    Raises :class:`EmployeeNotFound` when ``user_id`` does not match
    a :class:`User` row. The probe runs under
    :func:`tenant_agnostic` because ``users`` is identity-scoped (no
    workspace pin); a missing row collapses to 404 the same way the
    workspace-local path does.

    Audit: writes one ``user.reinstated`` row scoped to the caller's
    workspace (carrying the actor identity) plus one
    ``employee.reinstated`` row per workspace where engagements were
    cleared (mirroring the workspace-local
    :func:`reinstate_employee` shape so existing audit consumers see
    a consistent action vocabulary).

    **Magic link (TODO).** Spec §05 calls for a fresh magic link to
    be issued on a deployment-level reinstate (the user's prior
    passkeys were revoked at archive time). Issuance is NOT done
    here — it lives on the dedicated ``POST /users/{id}/magic_link``
    surface (cd-y5z3) so the operator chains the two calls. The
    docstring on the route documents this contract.

    Returns the :class:`EmployeeView` projected against the caller's
    workspace (matches the workspace-local reinstate's return shape).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    if not is_deployment_owner(repo.session, user_id=ctx.actor_id):
        raise PermissionDenied(
            f"caller {ctx.actor_id!r} is not a deployment owner; "
            "cannot reinstate a user deployment-wide"
        )

    # Identity-scoped probe — ``users`` has no workspace pin and the
    # caller may be reinstating someone with no membership in the
    # caller's workspace, so we must NOT route through
    # :func:`_assert_membership` here. ``_load_user`` already wraps
    # the lookup in :func:`tenant_agnostic`.
    user = _load_user(repo.session, user_id=user_id)

    # Cross-workspace engagement scan — the SA repo wraps this in
    # :func:`tenant_agnostic` because the ORM tenant filter would
    # otherwise narrow the result to the caller's workspace.
    engagements = repo.list_engagements_for_user_all_workspaces(user_id=user_id)

    # Group by workspace so we can fold per-workspace audit rows. A
    # user typically has one engagement per workspace today; the
    # grouping is defence-in-depth in case archived siblings exist.
    by_workspace: dict[str, list[WorkEngagementRow]] = {}
    for row in engagements:
        by_workspace.setdefault(row.workspace_id, []).append(row)

    cleared_engagement_ids: dict[str, list[str]] = {}
    reinstated_role_ids: dict[str, list[str]] = {}
    # Deployment-level reinstate (cd-pb8p) writes engagement +
    # user_work_role rows in EVERY workspace the user belongs to.
    # justification: cross-workspace mutation; deployment-owner gate above.
    with tenant_agnostic():
        for workspace_id, ws_engagements in by_workspace.items():
            engagement_ids: list[str] = []
            for engagement in ws_engagements:
                if engagement.archived_on is None:
                    continue
                repo.set_engagement_archived_on(
                    workspace_id=workspace_id,
                    engagement_id=engagement.id,
                    archived_on=None,
                    updated_at=now,
                )
                engagement_ids.append(engagement.id)
            cleared_engagement_ids[workspace_id] = engagement_ids

            all_roles = repo.list_user_work_roles(
                workspace_id=workspace_id, user_id=user_id, active_only=False
            )
            ws_role_ids = [r.id for r in all_roles if r.deleted_at is not None]
            repo.reinstate_user_work_roles(
                workspace_id=workspace_id,
                role_ids=ws_role_ids,
            )
            reinstated_role_ids[workspace_id] = ws_role_ids

    # Identity-row clear — ``user`` is tenant-agnostic and the ORM
    # tenant filter does not apply, so a bare attribute write is
    # enough. The flush happens below alongside the audit writes.
    user_was_archived = user.archived_at is not None
    if user_was_archived:
        user.archived_at = None
    repo.session.flush()

    # Audit fan-out: one ``user.reinstated`` row scoped to the caller's
    # workspace (the deployment-tier action), plus one
    # ``employee.reinstated`` row per workspace where engagements were
    # cleared. The per-workspace rows mirror the workspace-local
    # :func:`reinstate_employee` shape so dashboards / forensic tools
    # see a consistent vocabulary across the two surfaces.
    write_audit(
        repo.session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="user.reinstated",
        diff={
            "user_id": user_id,
            "user_was_archived": user_was_archived,
            "workspace_ids": sorted(by_workspace.keys()),
            "cleared_engagement_ids": {
                ws: sorted(ids) for ws, ids in cleared_engagement_ids.items()
            },
            "reinstated_user_work_role_ids": {
                ws: sorted(ids) for ws, ids in reinstated_role_ids.items()
            },
        },
        clock=resolved_clock,
    )
    for workspace_id in sorted(by_workspace.keys()):
        write_audit(
            repo.session,
            ctx,
            entity_kind="user",
            entity_id=user_id,
            action="employee.reinstated",
            diff={
                "user_id": user_id,
                "workspace_id": workspace_id,
                "cleared_engagement_ids": cleared_engagement_ids[workspace_id],
                "reinstated_user_work_role_ids": reinstated_role_ids[workspace_id],
                "deployment_scope": True,
            },
            clock=resolved_clock,
        )

    refreshed_engagement = repo.get_active_engagement(
        workspace_id=ctx.workspace_id, user_id=user_id
    )
    return _row_to_view(user, engagement=refreshed_engagement)


def _require_archive(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
) -> None:
    """Enforce ``users.archive`` on the caller's workspace or raise.

    Same wrapper shape as :func:`_require_edit_other`.
    """
    try:
        require(
            repo.session,
            ctx,
            action_key="users.archive",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'users.archive': {exc!s}"
        ) from exc


# ---------------------------------------------------------------------------
# Utility — list iterable helper used by tests and potentially callers
# ---------------------------------------------------------------------------


def iter_active_engagements(
    repo: MembershipRepository,
    ctx: WorkspaceContext,
    *,
    user_ids: Iterable[str],
) -> Mapping[str, WorkEngagementRow]:
    """Return a mapping ``user_id -> active engagement`` for a user set.

    Helper for roster views that need to annotate a batch of users
    with their engagement state without issuing N queries. Workspace-
    scoped via the repo's explicit predicate (and the underlying ORM
    tenant filter on the SA-backed concretion).
    """
    return repo.list_active_engagements_for_users(
        workspace_id=ctx.workspace_id, user_ids=user_ids
    )
