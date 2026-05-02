"""Membership lifecycle — invite, accept, remove, switch workspace.

Click-to-accept, uniformly (§03 "Additional users (invite →
click-to-accept)"). The primary entity is :class:`Invite` in
:mod:`app.adapters.db.identity.models`; this service owns the row's
lifecycle, the branching accept flow (new user vs. existing user),
and the removal path that honours the last-owner guard shared with
:mod:`app.domain.identity.permission_groups`.

High-level surface:

* :func:`invite` — insert a pending :class:`Invite` and mail the
  ``grant_invite`` magic link. Re-inviting the same email refreshes
  the existing pending row and re-mails a fresh token (mirroring
  :func:`app.auth.signup.start_signup`'s idempotent retry shape).
* :func:`consume_invite_token` — bare-host first leg of acceptance.
  Consumes the magic-link token. On a brand-new invitee, creates the
  :class:`User` row and returns an :class:`InviteSession` the passkey
  ceremony can carry. On an existing-user invitee, returns an
  :class:`AcceptanceCard` that lists the pending rows so the SPA can
  render a "here's what will activate" confirmation dialog. A
  missing passkey session surfaces as :class:`PasskeySessionRequired`
  so the SPA can prompt sign-in first.
* :func:`complete_invite` — second leg of acceptance for a brand-new
  invitee, called by the passkey-finish hook. One transaction:
  insert the ``role_grant`` + ``permission_group_member`` rows, flip
  the invite to ``accepted``, emit ``user.enrolled`` audit. The
  derived :class:`UserWorkspace` row materialises on the next
  worker tick (see "derived junction" below).
* :func:`confirm_invite` — second leg of acceptance for an existing
  user who clicked Accept on the card. Same downstream inserts as
  :func:`complete_invite`, audited as ``user.grant_accepted``.
* :func:`remove_member` — delete every ``role_grant`` +
  ``permission_group_member`` row the user holds in the caller's
  workspace, plus every live :class:`Session` scoped to that
  workspace. Refuses the operation if it would empty the ``owners``
  group (reuses
  :class:`app.domain.identity.permission_groups.WouldOrphanOwnersGroup`).
  The derived :class:`UserWorkspace` row drops on the next worker
  tick (see "derived junction" below).
* :func:`list_workspaces_for_user` — what the workspace switcher
  reads.
* :func:`switch_session_workspace` — verify membership + update
  ``Session.workspace_id`` atomically.

**Atomicity.** Every write path never calls ``session.commit()``;
the caller's UoW owns the transaction boundary. Failures roll back
every downstream insert — the invite either activates all its
targeted grants or none.

**derived junction.** ``user_workspace`` is documented as a derived
junction (§02). The canonical reconciler lives in
:func:`app.domain.identity.user_workspace_refresh.reconcile_user_workspace`
and runs on the worker (cd-yqm4) every
:data:`~app.worker.scheduler.USER_WORKSPACE_REFRESH_INTERVAL_SECONDS`
seconds; the membership service writes the upstream rows
(``role_grant`` / ``permission_group_member``) and then drives the
*scoped* reconciler
(:func:`app.domain.identity.user_workspace_refresh.reconcile_user_workspace_for`)
in the same transaction so the post-accept / post-remove redirect
sees the up-to-date junction without waiting on the worker tick.
Steady-state churn (workspaces that grow / shrink between ticks
because of out-of-band writes) is still recovered by the worker's
fan-out — the inline call closes the latency gap on the redirect
target only.

**Audit.** Every mutation emits one :mod:`app.audit` row in the
same transaction as the write; audit diffs carry hashed email only
(never the plaintext, §15). The accept / confirm / remove rows
share the ``actor_grant_role`` / ``audit_correlation_id`` of the
acting caller so forensics can join invite + accept trails.

**Architecture note.** Like
:mod:`app.domain.identity.permission_groups` and
:mod:`app.domain.identity.role_grants`, this module imports ORM
models from :mod:`app.adapters.db.*`. The import-linter stopgap for
``app.domain.identity.*`` is already in place (see
:mod:`pyproject.toml` §"ignore_imports"). cd-duv6 tracks the proper
Protocol-seam refactor for every identity service at once.

Deferred to follow-ups (see report at handoff):

* ``binding_org_id`` scope_transfer on grants — the ``organization``
  table is not part of Phase 1.
* Nightly ``invite`` TTL sweeper ("expired" state flip) — runs
  alongside the existing ``signup_gc`` worker.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)" and ``docs/specs/05-employees-and-roles.md``
§"Role grants".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import (
    Invite,
    MagicLinkNonce,
    PasskeyCredential,
    User,
    canonicalise_email,
)
from app.adapters.db.identity.models import (
    Session as SessionRow,
)
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
    WorkRole,
    Workspace,
)
from app.adapters.mail.ports import MailDeliveryError, Mailer
from app.audit import write_audit
from app.auth import passkey as passkey_service
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import Throttle
from app.auth.keys import derive_subkey
from app.config import Settings, get_settings
from app.domain.agent.preferences import default_approval_mode_for_workspace
from app.domain.identity.email_change_ports import (
    MagicLinkAlreadyConsumed,
    MagicLinkDispatch,
    MagicLinkInvalidToken,
    MagicLinkPort,
    MagicLinkPurposeMismatch,
    MagicLinkTokenExpired,
)
from app.domain.identity.permission_groups import (
    WouldOrphanOwnersGroup,
    write_member_remove_rejected_audit,
)
from app.mail.templates import invite_accept as invite_accept_template
from app.mail.templates import render as render_template
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AcceptanceCard",
    "AlreadyConsumed",
    "ExistingUserAcceptance",
    "InvalidToken",
    "InviteAlreadyAccepted",
    "InviteBodyInvalid",
    "InviteExpired",
    "InviteIntrospection",
    "InviteNotFound",
    "InviteOutcome",
    "InvitePasskeyAlreadyRegistered",
    "InviteSession",
    "InviteStateInvalid",
    "NewUserAcceptance",
    "NotAMember",
    "PasskeySessionRequired",
    "PurposeMismatch",
    "TokenExpired",
    "WorkspaceMembership",
    "WouldOrphanOwnersGroup",
    "complete_invite",
    "confirm_invite",
    "consume_invite_token",
    "introspect_invite",
    "invite",
    "list_workspaces_for_user",
    "register_invite_passkey_finish",
    "register_invite_passkey_start",
    "remove_member",
    "switch_session_workspace",
    "write_member_remove_rejected_audit",
]


# Re-export the seam-level magic-link exceptions under the legacy
# names this module's callers (the invite HTTP router) historically
# caught from :mod:`app.auth.magic_link`. The :class:`MagicLinkPort`
# concretion raises these instead of the auth-layer types so the
# domain stays decoupled from :mod:`app.auth.magic_link` (cd-opmw).
# Mirrors :mod:`app.domain.identity.email_change`'s re-export pattern.
InvalidToken = MagicLinkInvalidToken
PurposeMismatch = MagicLinkPurposeMismatch
TokenExpired = MagicLinkTokenExpired
AlreadyConsumed = MagicLinkAlreadyConsumed


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# §03 "Additional users": 24-hour TTL on every ``grant_invite``
# magic link. Mirrors :data:`app.auth.magic_link._TTL_BY_PURPOSE`.
_INVITE_TTL: Final[timedelta] = timedelta(hours=24)

# Valid v1 grant roles at the domain surface. Matches the DB
# CHECK on ``role_grant.grant_role`` and the frozenset in
# :mod:`app.domain.identity.role_grants`.
_VALID_GRANT_ROLES: Final[frozenset[str]] = frozenset(
    {"manager", "worker", "client", "guest"}
)

# Scope kinds the invite flow accepts in v1. The spec lists
# ``workspace``, ``property`` and ``organization``; Phase 1 only
# supports workspace because the ``organization`` table is not yet
# in the schema and property-scoped invite grants need the
# ``property_workspace`` junction cross-check already implemented
# in :mod:`app.domain.identity.role_grants` — adding it to the
# invite flow means cross-importing that guard. Tracked as
# cd-dagg follow-ups; see module docstring.
_VALID_SCOPE_KINDS: Final[frozenset[str]] = frozenset({"workspace"})

# Engagement kinds the invite flow accepts (cd-4o61). Mirrors §22
# "Engagement kinds" and the DB CHECK on ``work_engagement.engagement_kind``;
# kept as a frozenset rather than re-importing
# :data:`app.adapters.db.workspace.models._ENGAGEMENT_KIND_VALUES`
# so the domain stays decoupled from the private ORM constant.
_VALID_ENGAGEMENT_KINDS: Final[frozenset[str]] = frozenset(
    {"payroll", "contractor", "agency_supplied"}
)

# HKDF purpose for the email-hash pepper carried in audit diffs.
# Reuses the magic-link subkey so an invite's email_hash equals
# the sibling magic-link nonce's email_hash on exact ``canonicalise_email``
# match — abuse correlation joins cleanly.
_HKDF_PURPOSE: Final[str] = "magic-link"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InviteOutcome:
    """Return payload of :func:`invite`.

    ``id`` is the ULID of the freshly-inserted (or refreshed)
    :class:`Invite` row. The caller (HTTP router) uses it in the
    response body so a subsequent "re-send invite" button can point
    at a known row. ``user_created`` is true iff :func:`invite`
    inserted a brand-new :class:`User` row for the invitee —
    downstream abuse telemetry reads this flag to distinguish
    "invited a stranger" from "re-invited a known user".
    """

    id: str
    pending_email: str
    user_id: str | None
    user_created: bool


@dataclass(frozen=True, slots=True)
class InviteSession:
    """Returned by :func:`consume_invite_token` on the new-user branch.

    Mirrors :class:`app.auth.signup.SignupSession`: the field carries
    everything the passkey ceremony needs — an id the challenge can
    bind to and the canonicalised email for the WebAuthn user entity.
    """

    invite_id: str
    user_id: str
    email_lower: str
    display_name: str


@dataclass(frozen=True, slots=True)
class AcceptanceCard:
    """Returned by :func:`consume_invite_token` on the existing-user
    branch.

    Lists the pending grants + group memberships the SPA renders on
    the acceptance card. Structured — not a free-form blob — so the
    UI doesn't have to re-parse ``grants_json``.
    """

    invite_id: str
    workspace_id: str
    workspace_slug: str
    workspace_name: str
    grants: list[dict[str, Any]]
    group_memberships: list[dict[str, Any]]
    expires_at: datetime


# Discriminated union returned by :func:`consume_invite_token` —
# tests + routers match on the concrete type. A typed dataclass
# union keeps mypy honest without reaching for runtime ``isinstance``
# ladders inside the service itself.
@dataclass(frozen=True, slots=True)
class NewUserAcceptance:
    """Consume-token outcome for a brand-new invitee."""

    session: InviteSession


@dataclass(frozen=True, slots=True)
class ExistingUserAcceptance:
    """Consume-token outcome for a known user with an active session."""

    card: AcceptanceCard


@dataclass(frozen=True, slots=True)
class InviteIntrospection:
    """Returned by :func:`introspect_invite` — read-only invite preview.

    Carries everything the SPA's AcceptInvitePage needs to render an
    informed Accept card before the user clicks Accept: who invited
    them, what workspace they're joining, what grants will activate,
    when the invite expires. ``kind`` mirrors the discriminator on
    :func:`consume_invite_token`'s return so the SPA can branch the
    same way (new-user → passkey ceremony, existing-user → Accept
    card).

    The shape matches :class:`AcceptanceCard` for the workspace-side
    fields (so the SPA can keep one renderer) but adds the inviter's
    display name + the invitee's email so the page can confirm "you,
    <email>, were invited by <inviter> to <workspace>".

    Read-only: this preview is generated without burning the
    underlying magic-link nonce. The same ``token`` remains
    redeemable on a subsequent ``POST /invites/{token}/accept``.
    """

    kind: str
    invite_id: str
    workspace_id: str
    workspace_slug: str
    workspace_name: str
    inviter_display_name: str
    email_lower: str
    expires_at: datetime
    grants: list[dict[str, Any]]
    permission_group_memberships: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class WorkspaceMembership:
    """One entry in :func:`list_workspaces_for_user`'s return."""

    workspace_id: str
    workspace_slug: str
    workspace_name: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InviteBodyInvalid(ValueError):
    """Invite payload failed shape validation.

    422-equivalent. Covers missing email, bad grant_role, unknown
    scope_kind, empty grants list, etc. The router maps to 422
    ``invalid_body`` with the offending field in the body when
    available.
    """


class InviteNotFound(LookupError):
    """No :class:`Invite` row matches the given id.

    404-equivalent.
    """


class InviteStateInvalid(ValueError):
    """Invite is in the wrong state for the requested operation.

    Raised when :func:`complete_invite` / :func:`confirm_invite` hits
    a row that is already ``accepted`` / ``revoked`` / ``expired``.
    Maps to 409.
    """


class InviteExpired(ValueError):
    """Invite row's ``expires_at`` is in the past.

    410-equivalent. The caller must request a fresh invite.
    """


class InviteAlreadyAccepted(ValueError):
    """The invitee already accepted this invite.

    409-equivalent. Distinct from :class:`InviteStateInvalid` so the
    router can render a friendlier "you already joined" page instead
    of a generic conflict.
    """


class PasskeySessionRequired(PermissionError):
    """The existing-user branch needs an active passkey session.

    401-equivalent. Raised by :func:`consume_invite_token` when the
    invited email matches an existing user but no live
    :class:`Session` is present for them — per spec, the Acceptance
    card is gated on a passkey sign-in so a stolen magic link alone
    can't attach grants.
    """


class InvitePasskeyAlreadyRegistered(ValueError):
    """The invitee already holds a passkey; the new-user enrolment
    flow is closed.

    409-equivalent. Raised by :func:`register_invite_passkey_start`
    and :func:`register_invite_passkey_finish` when the invite's
    linked user already holds at least one
    :class:`PasskeyCredential`. Once the invitee has a key on file,
    new credentials must go through the authenticated
    "add another passkey" flow (`/auth/passkey/register/{start,finish}`)
    or the recovery flow — not the bare-host invite enrolment route.
    Closing the route post-enrolment also stops a leaked
    ``invite_id`` from minting a second uninvited credential.
    """


class NotAMember(LookupError):
    """User has no active grant in the target workspace.

    404-equivalent. Raised by :func:`switch_session_workspace` and
    :func:`remove_member` for users the caller is trying to act on
    in a workspace they don't belong to.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or SystemClock."""
    return (clock if clock is not None else SystemClock()).now()


def _aware_utc(value: datetime) -> datetime:
    """Normalise naive ``datetime`` values to aware UTC.

    Mirrors :func:`app.auth.signup._aware_utc` — SQLite drops tzinfo
    on round-trip and Postgres preserves it, and every TTL
    comparison in this module normalises both sides to aware UTC so
    backend selection doesn't leak into the domain logic.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _email_pepper(settings: Settings | None) -> bytes:
    """HKDF subkey used to pepper the invite-row email hash.

    Mirrors :func:`app.auth.magic_link._subkey`: the magic-link
    nonce row and the invite row both hash emails against the same
    ``"magic-link"`` subkey so abuse-correlation joins stay PII-
    free across the two surfaces.
    """
    s = settings if settings is not None else get_settings()
    return derive_subkey(s.root_key, purpose=_HKDF_PURPOSE)


def _validate_grants(grants: list[dict[str, Any]], *, workspace_id: str) -> None:
    """Raise :class:`InviteBodyInvalid` unless every grant is well-shaped.

    v1 only accepts workspace-scoped grants. Cross-workspace
    scope_ids are rejected so a rogue invite can't bait-and-switch
    the invitee into a sibling workspace mid-acceptance.
    """
    if not grants:
        raise InviteBodyInvalid("grants list must carry at least one entry")
    for idx, g in enumerate(grants):
        scope_kind = g.get("scope_kind")
        scope_id = g.get("scope_id")
        grant_role = g.get("grant_role")
        if scope_kind not in _VALID_SCOPE_KINDS:
            raise InviteBodyInvalid(
                f"grants[{idx}].scope_kind {scope_kind!r} is not in "
                f"{sorted(_VALID_SCOPE_KINDS)} "
                f"(property / organization scopes land in a follow-up)"
            )
        if scope_id != workspace_id:
            raise InviteBodyInvalid(
                f"grants[{idx}].scope_id must match the target workspace; "
                f"got {scope_id!r} vs {workspace_id!r}"
            )
        if grant_role not in _VALID_GRANT_ROLES:
            raise InviteBodyInvalid(
                f"grants[{idx}].grant_role {grant_role!r} is not in "
                f"{sorted(_VALID_GRANT_ROLES)}"
            )


def _validate_group_memberships(
    session: DbSession,
    *,
    group_memberships: list[dict[str, Any]],
    workspace_id: str,
) -> None:
    """Raise :class:`InviteBodyInvalid` if any group_id is not in this workspace.

    Runs a single ``IN (...)`` lookup rather than N ``get`` calls so
    the validation cost stays O(1) regardless of membership count.
    The ORM tenant filter auto-injects the workspace predicate, so
    a group from a sibling workspace appears as "not found" and
    falls through to the error.
    """
    if not group_memberships:
        return
    ids: list[str] = []
    for idx, gm in enumerate(group_memberships):
        raw_id = gm.get("group_id")
        if not isinstance(raw_id, str) or not raw_id:
            raise InviteBodyInvalid(
                f"group_memberships[{idx}].group_id must be a non-empty string"
            )
        ids.append(raw_id)
    known = set(
        session.scalars(
            select(PermissionGroup.id).where(
                PermissionGroup.workspace_id == workspace_id,
                PermissionGroup.id.in_(ids),
            )
        ).all()
    )
    missing = sorted(set(ids) - known)
    if missing:
        raise InviteBodyInvalid(
            f"group_memberships carries unknown group_ids: {missing!r}"
        )


def _validate_work_engagement(
    work_engagement: dict[str, Any] | None,
) -> None:
    """Raise :class:`InviteBodyInvalid` unless the engagement payload is well-shaped.

    cd-4o61. ``None`` is accepted (no override; falls back to the
    default ``payroll`` seed). When present, ``engagement_kind`` must
    sit in :data:`_VALID_ENGAGEMENT_KINDS` and the §02 supplier-pairing
    biconditional is enforced — ``agency_supplied`` requires a
    ``supplier_org_id``; every other kind forbids one. Mirrors the DB
    CHECK on ``work_engagement`` so a bad pair fails at the domain
    boundary (422) rather than at the DB.

    The ``supplier_org_id`` value itself is not joined against the
    ``organization`` table here — Phase 1 has no organization rows
    landing in test fixtures, and the FK on
    :class:`WorkEngagement.supplier_org_id` will raise an
    :class:`IntegrityError` at accept-time flush if the id is bogus.
    Once the organization surface lands a stricter cross-check can
    move to invite time without breaking the wire shape.
    """
    if work_engagement is None:
        return
    kind = work_engagement.get("engagement_kind")
    supplier_org_id = work_engagement.get("supplier_org_id")
    if kind not in _VALID_ENGAGEMENT_KINDS:
        raise InviteBodyInvalid(
            f"work_engagement.engagement_kind {kind!r} is not in "
            f"{sorted(_VALID_ENGAGEMENT_KINDS)}"
        )
    if kind == "agency_supplied" and not supplier_org_id:
        raise InviteBodyInvalid(
            "work_engagement.supplier_org_id is required when "
            "engagement_kind == 'agency_supplied'"
        )
    if kind != "agency_supplied" and supplier_org_id is not None:
        raise InviteBodyInvalid(
            f"work_engagement.supplier_org_id must be NULL when "
            f"engagement_kind == {kind!r}"
        )


def _validate_user_work_roles(
    session: DbSession,
    *,
    user_work_roles: list[dict[str, Any]],
    workspace_id: str,
) -> None:
    """Raise :class:`InviteBodyInvalid` unless every work-role id is local + live.

    cd-4o61. Empty / missing list is a no-op. For each entry the
    ``work_role_id`` must be a non-empty string, must resolve to a
    :class:`WorkRole` in this workspace, and must not be soft-
    deleted. One ``IN (...)`` lookup keeps the cost O(1) regardless
    of the entry count; the ORM tenant filter auto-injects the
    workspace predicate so a foreign-workspace id surfaces as
    "missing".
    """
    if not user_work_roles:
        return
    ids: list[str] = []
    for idx, uwr in enumerate(user_work_roles):
        raw_id = uwr.get("work_role_id")
        if not isinstance(raw_id, str) or not raw_id:
            raise InviteBodyInvalid(
                f"user_work_roles[{idx}].work_role_id must be a non-empty string"
            )
        ids.append(raw_id)
    known = set(
        session.scalars(
            select(WorkRole.id).where(
                WorkRole.workspace_id == workspace_id,
                WorkRole.id.in_(ids),
                WorkRole.deleted_at.is_(None),
            )
        ).all()
    )
    missing = sorted(set(ids) - known)
    if missing:
        raise InviteBodyInvalid(
            f"user_work_roles carries unknown work_role_ids: {missing!r}"
        )


def _find_existing_invite(
    session: DbSession, *, workspace_id: str, email_lower: str
) -> Invite | None:
    """Return the pending invite for ``(workspace_id, email_lower)`` if any."""
    return session.scalar(
        select(Invite).where(
            Invite.workspace_id == workspace_id,
            Invite.pending_email_lower == email_lower,
            Invite.state == "pending",
        )
    )


def _lookup_user_by_email(session: DbSession, *, email_lower: str) -> User | None:
    """Return the :class:`User` row for a canonicalised email, if any.

    ``user`` is identity-scoped (not workspace-scoped) so the lookup
    runs under :func:`tenant_agnostic`.
    """
    with tenant_agnostic():
        return session.scalar(select(User).where(User.email_lower == email_lower))


def _user_has_passkey(session: DbSession, *, user_id: str) -> bool:
    """Return ``True`` if ``user_id`` holds at least one registered passkey.

    This is the right discriminator between the "new user" (no passkey
    yet — needs the enrolment ceremony) and "existing user"
    (has a passkey — needs the Acceptance card gated on a live
    session) branches of :func:`consume_invite_token`. Using session
    presence alone would wrongly route a signed-out existing user
    through the enrol flow and re-enroll an extra passkey without
    showing them the card (spec §03 "Additional users" — "the
    redemption prompts a passkey sign-in if no active session is
    present, then renders the Acceptance card").
    """
    # justification: ``passkey_credential`` is user-scoped; no tenant
    # predicate applies.
    with tenant_agnostic():
        row = session.scalar(
            select(PasskeyCredential)
            .where(PasskeyCredential.user_id == user_id)
            .limit(1)
        )
    return row is not None


def _invalidate_pending_invite_nonces(session: DbSession, *, invite_id: str) -> None:
    """Delete unconsumed ``grant_invite`` magic-link nonces for ``invite_id``.

    Mirrors :func:`app.auth.signup._invalidate_pending_nonces`: the
    refresh path re-mails a fresh token so the old one must stop
    being redeemable. We scope the predicate to
    ``purpose='grant_invite'`` defensively so a freak ULID collision
    against a sibling-purpose nonce can never sweep unrelated rows.
    """
    # justification: magic_link_nonce is identity-scoped.
    from app.adapters.db.identity.models import MagicLinkNonce

    with tenant_agnostic():
        session.execute(
            delete(MagicLinkNonce)
            .where(
                MagicLinkNonce.subject_id == invite_id,
                MagicLinkNonce.purpose == "grant_invite",
                MagicLinkNonce.consumed_at.is_(None),
            )
            .execution_options(synchronize_session=False)
        )
        session.flush()


def _hash_email(email_lower: str, *, settings: Settings | None) -> str:
    """Return the PII-safe audit hash for ``email_lower``."""
    return hash_with_pepper(email_lower, _email_pepper(settings))


# ---------------------------------------------------------------------------
# invite
# ---------------------------------------------------------------------------


def invite(
    session: DbSession,
    ctx: WorkspaceContext,
    *,
    email: str,
    display_name: str,
    grants: list[dict[str, Any]],
    group_memberships: list[dict[str, Any]] | None = None,
    work_engagement: dict[str, Any] | None = None,
    user_work_roles: list[dict[str, Any]] | None = None,
    mailer: Mailer,
    throttle: Throttle,
    base_url: str,
    inviter_display_name: str,
    workspace_name: str,
    link_port: MagicLinkPort,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
    dispatch: MagicLinkDispatch | None = None,
) -> InviteOutcome:
    """Insert (or refresh) a pending :class:`Invite` and mail the magic link.

    Spec §03 "Additional users (invite → click-to-accept)". The
    caller's UoW owns the transaction boundary; nothing commits here.

    Steps:

    1. Validate the payload — email present, grants non-empty, every
       grant's scope_kind + role + scope_id matches the workspace,
       every ``group_memberships[].group_id`` exists in the workspace.
       cd-4o61: also validate the optional ``work_engagement`` and
       ``user_work_roles`` sub-payloads. ``engagement_kind`` must be
       a known value and ``supplier_org_id`` honours the §02
       biconditional; every ``user_work_roles[].work_role_id`` must
       resolve to a live :class:`WorkRole` in this workspace.
    2. Resolve or create the invitee's :class:`User` row. A fresh
       email spawns a new row at invite time so the later
       :func:`consume_invite_token` can bind a passkey to it; the
       ``user.invited`` audit carries the hash. This matches the
       mock's shape (``mocks/app/main.py::api_users_invite``) and
       mirrors the spec's "creates (or re-uses, if email matches)".
    3. If a pending invite already exists for
       ``(workspace_id, email_lower)``, update it in place — refresh
       the TTL, refresh the grants / memberships payload, drop any
       still-pending magic-link nonce. Idempotent retry shape.
    4. Mint the ``grant_invite`` magic link against the invite's id.
       The 24-hour TTL is pinned by
       :data:`app.auth.magic_link._TTL_BY_PURPOSE["grant_invite"]`.
    5. Audit ``user.invited`` with PII-safe ``email_hash``.

    ``settings`` is optional for tests; when ``None`` the module
    falls back to :func:`app.config.get_settings` (the same
    convention the sibling magic-link / signup services use).

    **Outbox ordering (cd-9slq).** When ``dispatch`` is supplied the
    invite-flavoured template is queued onto it for post-commit
    delivery, mirroring the cd-9i7z pattern: the calling HTTP router
    runs this function inside ``with make_uow() as session:`` and
    invokes :meth:`MagicLinkDispatch.deliver` only after the ``with``
    exits, so a commit failure short-circuits the SMTP send. When
    ``dispatch`` is ``None`` the function falls back to the legacy
    synchronous send for tests / direct callers that own the commit
    boundary themselves; production wiring always supplies a
    :class:`MagicLinkDispatch` (the SA-backed
    :class:`app.auth.magic_link.PendingDispatch` satisfies the
    Protocol structurally).
    """
    resolved_now = now if now is not None else _now(clock)
    email_lower = canonicalise_email(email)
    if not email_lower or "@" not in email_lower:
        raise InviteBodyInvalid("email must be a non-empty address")
    if not display_name.strip():
        raise InviteBodyInvalid("display_name must be a non-empty string")
    _validate_grants(grants, workspace_id=ctx.workspace_id)
    memberships = group_memberships or []
    _validate_group_memberships(
        session,
        group_memberships=memberships,
        workspace_id=ctx.workspace_id,
    )
    # cd-4o61: validate the optional work_engagement + user_work_roles
    # sub-payloads. Both stay as JSON on the invite row until accept;
    # validating here means a bad shape fails loud at invite time
    # rather than corrupting the accept transaction later.
    _validate_work_engagement(work_engagement)
    user_work_roles_payload = user_work_roles or []
    _validate_user_work_roles(
        session,
        user_work_roles=user_work_roles_payload,
        workspace_id=ctx.workspace_id,
    )

    email_hash = _hash_email(email_lower, settings=settings)

    # Resolve-or-create the :class:`User` row. A returning invitee
    # shares identity across workspaces; a brand-new email spawns a
    # row we link back via ``Invite.user_id`` so the acceptance
    # flow can flip the passkey enrolment onto a stable id.
    existing_user = _lookup_user_by_email(session, email_lower=email_lower)
    user_created = False
    if existing_user is None:
        user = User(
            id=new_ulid(clock=clock),
            email=email_lower,
            email_lower=email_lower,
            display_name=display_name,
            agent_approval_mode=default_approval_mode_for_workspace(session, ctx),
            created_at=resolved_now,
        )
        with tenant_agnostic():
            session.add(user)
            session.flush()
        user_id = user.id
        user_created = True
    else:
        user_id = existing_user.id

    existing_invite = _find_existing_invite(
        session, workspace_id=ctx.workspace_id, email_lower=email_lower
    )
    if existing_invite is not None:
        invite_id = existing_invite.id
        existing_invite.display_name = display_name
        existing_invite.grants_json = list(grants)
        existing_invite.group_memberships_json = list(memberships)
        # cd-4o61: refresh path overwrites the pending sub-payloads
        # so a re-invite with a different engagement shape wins; the
        # caller's last write is authoritative until accept consumes
        # the row.
        existing_invite.work_engagement_json = (
            dict(work_engagement) if work_engagement is not None else None
        )
        existing_invite.user_work_roles_json = list(user_work_roles_payload)
        existing_invite.invited_by_user_id = ctx.actor_id
        existing_invite.expires_at = resolved_now + _INVITE_TTL
        existing_invite.user_id = user_id
        existing_invite.pending_email = email_lower
        existing_invite.pending_email_lower = email_lower
        existing_invite.email_hash = email_hash
        session.flush()
        _invalidate_pending_invite_nonces(session, invite_id=invite_id)
    else:
        invite_id = new_ulid(clock=clock)
        row = Invite(
            id=invite_id,
            workspace_id=ctx.workspace_id,
            user_id=user_id,
            pending_email=email_lower,
            pending_email_lower=email_lower,
            email_hash=email_hash,
            display_name=display_name,
            state="pending",
            grants_json=list(grants),
            group_memberships_json=list(memberships),
            work_engagement_json=(
                dict(work_engagement) if work_engagement is not None else None
            ),
            user_work_roles_json=list(user_work_roles_payload),
            invited_by_user_id=ctx.actor_id,
            created_at=resolved_now,
            expires_at=resolved_now + _INVITE_TTL,
            accepted_at=None,
            revoked_at=None,
        )
        session.add(row)
        session.flush()

    # Mint the magic link against the invite id — consume_invite_token
    # reads the subject back to load the row. ``send_email=False`` skips
    # the generic magic-link mailer and hands us the signed URL so
    # :func:`_send_invite_email` can ship it with the invite-flavoured
    # template (workspace + inviter in the subject, TTL in hours).
    invite_link = link_port.request_link(
        email=email_lower,
        purpose="grant_invite",
        # ``ip`` is a forensic hint; the invite HTTP handler forwards
        # ``request.client.host``. The magic_link service already
        # pepper-hashes it before touching the DB.
        ip="",
        mailer=None,
        base_url=base_url,
        now=resolved_now,
        ttl=_INVITE_TTL,
        throttle=throttle,
        settings=settings,
        clock=clock,
        subject_id=invite_id,
        send_email=False,
    )
    if invite_link is None:
        # Defensive: ``request_link`` only returns ``None`` when
        # :func:`_resolve_subject_id` finds no subject — here we pass
        # ``subject_id=invite_id`` explicitly, so a ``None`` would
        # indicate a bug in the magic-link service rather than a
        # legitimate enumeration-guard short-circuit.
        raise RuntimeError(
            f"link_port.request_link returned None for invite {invite_id!r}"
        )
    # ``send_email=False`` so ``deliver()`` is a no-op; we send the
    # invite-flavoured template ourselves below. Calling it anyway
    # keeps the deferred-send protocol consistent across call sites
    # so a future template-routing refactor on this surface lights
    # up the same outbox seam without re-discovering it.
    invite_link.deliver()
    url = invite_link.url
    # Capture every input :func:`_send_invite_email` needs at mint
    # time so the deferred entry is a parameter-free closure. The
    # outbox shape (cd-9slq) ensures the SMTP send fires only after
    # the invite + nonce + audit rows are durable on disk; a commit
    # failure short-circuits :meth:`PendingDispatch.deliver` so no
    # working invite token reaches the user inbox without the
    # matching invite row.
    captured_invite_mailer = mailer
    captured_invite_url = url
    captured_invitee_email = email_lower
    captured_invitee_display_name = display_name
    captured_inviter_display_name = inviter_display_name
    captured_workspace_name = workspace_name

    def _deferred_invite_send() -> None:
        _send_invite_email(
            mailer=captured_invite_mailer,
            captured_url=captured_invite_url,
            to_email=captured_invitee_email,
            invitee_display_name=captured_invitee_display_name,
            inviter_display_name=captured_inviter_display_name,
            workspace_name=captured_workspace_name,
        )

    if dispatch is not None:
        # Production path — calling router commits then drains the
        # dispatch. Invite is manager-gated, so this isn't an
        # enumeration-guard path per se; but a mailer outage must
        # not fail the write so the invite row + nonce + audit can
        # commit and an operator can re-issue from the invite UI.
        # :meth:`PendingDispatch.deliver` swallows MailDeliveryError
        # uniformly across auth-adjacent mail sends.
        dispatch.add_callback(_deferred_invite_send)
    else:
        # Legacy fallback for tests / direct callers that own their
        # own commit boundary. Same swallow-and-log policy as the
        # sibling magic-link / recovery flows.
        try:
            _deferred_invite_send()
        except MailDeliveryError:
            _log.warning(
                "invite mail send failed for invite %r; swallowing so the "
                "invite row commits and an operator can re-issue",
                invite_id,
                exc_info=True,
            )

    write_audit(
        session,
        ctx,
        entity_kind="invite",
        entity_id=invite_id,
        action="user.invited",
        diff={
            "email_hash": email_hash,
            "user_id": user_id,
            "user_created": user_created,
            "grants": list(grants),
            "group_memberships": list(memberships),
            # cd-4o61: forensic snapshot of the pending sub-payloads.
            # No PII — engagement_kind / supplier_org_id / work_role_id
            # are domain ids, not user content.
            "work_engagement": (
                dict(work_engagement) if work_engagement is not None else None
            ),
            "user_work_roles": list(user_work_roles_payload),
        },
        clock=clock,
    )

    return InviteOutcome(
        id=invite_id,
        pending_email=email_lower,
        user_id=user_id,
        user_created=user_created,
    )


# ---------------------------------------------------------------------------
# Invite-email mailer helper
# ---------------------------------------------------------------------------


def _send_invite_email(
    *,
    mailer: Mailer,
    captured_url: str,
    to_email: str,
    invitee_display_name: str,
    inviter_display_name: str,
    workspace_name: str,
) -> None:
    """Render + send the invite-accept email with the signed URL.

    Pure presentation: the signed token + URL are produced by
    :func:`app.auth.magic_link.request_link` (called with
    ``send_email=False``), which returns the URL so this helper can
    re-frame the body copy with workspace / inviter context without
    a round-trip through a recording mailer.
    """
    subject = render_template(
        invite_accept_template.SUBJECT,
        inviter_display_name=inviter_display_name,
        workspace_name=workspace_name,
    )
    body_text = render_template(
        invite_accept_template.BODY_TEXT,
        invitee_display_name=invitee_display_name,
        inviter_display_name=inviter_display_name,
        workspace_name=workspace_name,
        url=captured_url,
        ttl_hours="24",
    )
    mailer.send(to=[to_email], subject=subject, body_text=body_text)


# ---------------------------------------------------------------------------
# introspect_invite (bare host, read-only)
# ---------------------------------------------------------------------------


def _resolve_inviter_display_name(session: DbSession, *, user_id: str | None) -> str:
    """Return the inviter's display name, or an empty string if absent.

    The :class:`Invite.invited_by_user_id` FK can technically be
    ``None`` (e.g. a system-issued invite, future); in that case the
    UI falls back to "(system)" without us baking that copy into the
    domain layer. ``user`` is identity-scoped so the lookup runs
    under :func:`tenant_agnostic`.
    """
    if user_id is None:
        return ""
    with tenant_agnostic():
        row = session.get(User, user_id)
    return row.display_name if row is not None else ""


def introspect_invite(
    session: DbSession,
    *,
    token: str,
    ip: str,
    throttle: Throttle,
    link_port: MagicLinkPort,
    settings: Settings | None = None,
    active_user_id: str | None = None,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> InviteIntrospection:
    """Read-only preview of an invite — does NOT burn the magic-link nonce.

    Mirrors :func:`consume_invite_token`'s validation surface but
    delegates to :meth:`MagicLinkPort.peek_link` so the underlying
    nonce stays redeemable. Returns enough data for the SPA's
    AcceptInvitePage to render an informed Accept card before the
    user clicks Accept (inviter, workspace, grants, expiry).

    Branch decision (``kind``) matches
    :func:`consume_invite_token`: a user with at least one
    registered passkey is "existing_user"; otherwise "new_user".
    Unlike consume, this function does **not** raise
    :class:`PasskeySessionRequired` — introspect is decoupled from
    session state by design (the SPA needs the preview to render
    *before* the user signs in). ``active_user_id`` is reserved for
    a future "show 'you are signed in as X' hint" affordance and
    documented here so callers can pass it without re-plumbing.

    Throttle: same bucket as accept (consume) — peeks count toward
    the 3-fails / 60s → 10-minute lockout, so an attacker cannot
    burn through more peeks than consumes against a given IP.

    Audit: none. The preview is read-only; the actual accept owns
    the audit row.

    Raises:

    * :class:`MagicLinkInvalidToken` — signature failed / payload
      malformed.
    * :class:`MagicLinkPurposeMismatch` — token purpose !=
      ``"grant_invite"``.
    * :class:`MagicLinkTokenExpired` — token / row lapsed.
    * :class:`MagicLinkAlreadyConsumed` — the underlying nonce was
      already redeemed (the user already clicked Accept).
    * :class:`~app.auth._throttle.ConsumeLockout` — IP locked out.
    * :class:`InviteNotFound` — token's subject doesn't match any
      invite row.
    * :class:`InviteStateInvalid` — invite is revoked / corrupted.
    * :class:`InviteAlreadyAccepted` — invite row was already
      accepted (separate from token-already-consumed because the
      DB state can diverge from the nonce state under partial
      failure).
    * :class:`InviteExpired` — invite row's ``expires_at`` has
      passed.

    The router is responsible for collapsing the token-validity
    family onto a 404 ``invite_not_found`` so existence does not
    leak across the bare-host surface; the domain still raises
    typed exceptions so a CLI / scripted caller can branch.
    """
    # ``active_user_id`` is reserved — see docstring. The peek does
    # not branch on it (introspect is session-agnostic), but keeping
    # it in the signature lets future "you are signed in as <user>"
    # affordances ride through without breaking callers.
    del active_user_id

    resolved_now = now if now is not None else _now(clock)

    outcome = link_port.peek_link(
        token=token,
        expected_purpose="grant_invite",
        ip=ip,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
    )
    invite_id = outcome.subject_id

    with tenant_agnostic():
        invite_row = session.get(Invite, invite_id)
    if invite_row is None:
        raise InviteNotFound(invite_id)

    if invite_row.state == "accepted":
        raise InviteAlreadyAccepted(invite_id)
    if invite_row.state in ("revoked", "expired"):
        raise InviteStateInvalid(
            f"invite {invite_id!r} is in state {invite_row.state!r}"
        )
    if _aware_utc(invite_row.expires_at) <= resolved_now:
        raise InviteExpired(f"invite {invite_id!r} expired")

    user_id = invite_row.user_id
    if user_id is None:
        raise InviteStateInvalid(f"invite {invite_id!r} has no linked user_id")

    with tenant_agnostic():
        workspace = session.get(Workspace, invite_row.workspace_id)
    if workspace is None:
        raise InviteStateInvalid(
            f"invite {invite_id!r}: workspace {invite_row.workspace_id!r} missing"
        )

    # Branch on passkey-presence — same predicate as consume so the
    # SPA's preview kind exactly matches what consume_invite_token
    # would return on the subsequent POST. A user who has at least one
    # passkey on file is "existing_user" (Accept card path); otherwise
    # "new_user" (passkey enrol ceremony path).
    has_passkey = _user_has_passkey(session, user_id=user_id)
    kind = "existing_user" if has_passkey else "new_user"

    inviter_display_name = _resolve_inviter_display_name(
        session, user_id=invite_row.invited_by_user_id
    )

    with tenant_agnostic():
        invitee = session.get(User, user_id)
    email_lower = (
        invitee.email_lower if invitee is not None else invite_row.pending_email_lower
    )

    return InviteIntrospection(
        kind=kind,
        invite_id=invite_id,
        workspace_id=invite_row.workspace_id,
        workspace_slug=workspace.slug,
        workspace_name=workspace.name,
        inviter_display_name=inviter_display_name,
        email_lower=email_lower,
        expires_at=_aware_utc(invite_row.expires_at),
        grants=list(invite_row.grants_json),
        permission_group_memberships=list(invite_row.group_memberships_json),
    )


# ---------------------------------------------------------------------------
# consume_invite_token (bare host)
# ---------------------------------------------------------------------------


def consume_invite_token(
    session: DbSession,
    *,
    token: str,
    ip: str,
    throttle: Throttle,
    link_port: MagicLinkPort,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
    active_user_id: str | None = None,
) -> NewUserAcceptance | ExistingUserAcceptance:
    """First leg of accept — consume the magic link, branch on user shape.

    ``active_user_id`` is the authenticated user id from the inbound
    cookie, if any. The router resolves it via
    :func:`app.auth.session.validate` before calling in — a cookie
    absent / invalid / expired lands ``None`` here so we can raise
    :class:`PasskeySessionRequired` for the existing-user branch.

    Returns one of:

    * :class:`NewUserAcceptance` — the token matches an invite whose
      ``user.created_at`` equals the invite's own ``created_at``
      (i.e. we spawned the user row at invite time, no passkey yet).
      Caller pipes the :class:`InviteSession` into the passkey
      enrol ceremony and calls :func:`complete_invite` on finish.
    * :class:`ExistingUserAcceptance` — the token matches an invite
      whose user already has a passkey. Requires an active session
      scoped to that user; the SPA renders an Acceptance card and
      POSTs to ``/invite/{id}/confirm`` on click.

    Raises:

    * :class:`InviteNotFound` — the token's subject doesn't match
      any invite row.
    * :class:`InviteStateInvalid` — the row has already been
      accepted or revoked.
    * :class:`InviteExpired` — the row's ``expires_at`` has passed
      (or the magic-link token expired, which surfaces as the seam-
      level :class:`MagicLinkTokenExpired`).
    * :class:`PasskeySessionRequired` — the invited email resolves
      to an existing user but no active session is present.
    * Re-raises from the :class:`MagicLinkPort` seam:
      :class:`MagicLinkInvalidToken`,
      :class:`MagicLinkPurposeMismatch`,
      :class:`MagicLinkAlreadyConsumed`,
      :class:`MagicLinkTokenExpired`.
    """
    resolved_now = now if now is not None else _now(clock)

    outcome = link_port.consume_link(
        token=token,
        expected_purpose="grant_invite",
        ip=ip,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
    )
    invite_id = outcome.subject_id

    with tenant_agnostic():
        invite_row = session.get(Invite, invite_id)
    if invite_row is None:
        raise InviteNotFound(invite_id)

    if invite_row.state == "accepted":
        raise InviteAlreadyAccepted(invite_id)
    if invite_row.state in ("revoked", "expired"):
        raise InviteStateInvalid(
            f"invite {invite_id!r} is in state {invite_row.state!r}"
        )
    if _aware_utc(invite_row.expires_at) <= resolved_now:
        raise InviteExpired(f"invite {invite_id!r} expired")

    user_id = invite_row.user_id
    if user_id is None:
        # Defensive: :func:`invite` always writes a user_id. A missing
        # value means someone mutated the row out-of-band.
        raise InviteStateInvalid(f"invite {invite_id!r} has no linked user_id")

    # Decide the branch. "New user" = the user row has no passkey
    # registered yet — the invite flow spawned the row at invite
    # time, so the passkey enrolment ceremony has to run before
    # grants activate. "Existing user" = the user has at least one
    # passkey credential on file; per spec §03 "Additional users",
    # we gate on a live session (prompting sign-in if absent) and
    # render the Acceptance card. We MUST branch on passkey presence,
    # not on session presence — an existing user who simply signed
    # out would be mis-routed through the new-user enrol flow and
    # silently gain an extra passkey.
    has_passkey = _user_has_passkey(session, user_id=user_id)

    if not has_passkey:
        # New user — no passkey yet. The passkey ceremony runs next,
        # and :func:`complete_invite` completes the accept.
        with tenant_agnostic():
            user = session.get(User, user_id)
        if user is None:
            raise InviteStateInvalid(
                f"invite {invite_id!r}: linked user {user_id!r} missing"
            )
        return NewUserAcceptance(
            session=InviteSession(
                invite_id=invite_id,
                user_id=user_id,
                email_lower=user.email_lower,
                display_name=invite_row.display_name,
            )
        )

    # Existing user — gate on a session that matches them. If none is
    # present the SPA renders the ``needs_sign_in`` hint; after sign-in
    # it POSTs to ``/invite/{id}/confirm`` directly (the magic-link
    # token was already spent on this call).
    if active_user_id != user_id:
        raise PasskeySessionRequired(
            f"invite {invite_id!r} requires a passkey session for user {user_id!r}"
        )

    with tenant_agnostic():
        workspace = session.get(Workspace, invite_row.workspace_id)
    if workspace is None:
        raise InviteStateInvalid(
            f"invite {invite_id!r}: workspace {invite_row.workspace_id!r} missing"
        )

    card = AcceptanceCard(
        invite_id=invite_id,
        workspace_id=invite_row.workspace_id,
        workspace_slug=workspace.slug,
        workspace_name=workspace.name,
        grants=list(invite_row.grants_json),
        group_memberships=list(invite_row.group_memberships_json),
        expires_at=_aware_utc(invite_row.expires_at),
    )
    return ExistingUserAcceptance(card=card)


# ---------------------------------------------------------------------------
# complete_invite (new user) + confirm_invite (existing user)
# ---------------------------------------------------------------------------


def _activate_invite(
    session: DbSession,
    ctx: WorkspaceContext,
    *,
    invite_row: Invite,
    now: datetime,
    audit_action: str,
    clock: Clock | None,
) -> None:
    """Insert every downstream row for an accepted invite.

    Writes:

    * one :class:`RoleGrant` per ``grants_json`` entry;
    * one :class:`PermissionGroupMember` per
      ``group_memberships_json`` entry (idempotent on duplicates).

    The derived :class:`UserWorkspace` row is then materialised inline
    via :func:`reconcile_user_workspace_for` so the post-accept
    redirect to ``/w/<slug>/today`` finds an up-to-date junction;
    steady-state churn is still recovered by the worker (cd-yqm4),
    but the redirect target cannot tolerate the tick's lag.

    Flips the invite row's ``state`` to ``accepted`` and fills
    ``accepted_at``.

    Audit lands via ``write_audit(action=audit_action)`` — callers
    differentiate between ``user.enrolled`` (new user) and
    ``user.grant_accepted`` (existing user).
    """
    user_id = invite_row.user_id
    if user_id is None:
        raise InviteStateInvalid(
            f"invite {invite_row.id!r} carries no user_id; cannot activate"
        )
    workspace_id = invite_row.workspace_id
    # Both callers (:func:`complete_invite` synthesises a fresh ctx;
    # :func:`confirm_invite` takes it from the route, which already
    # pins it to ``invite.workspace_id``) guarantee this equality. Be
    # loud about a divergence rather than silently seeding the
    # workspace-scoped :class:`WorkEngagement` in the wrong tenant:
    # the audit trail and the row itself would both land under
    # ``ctx.workspace_id`` while the grants and membership land under
    # ``invite_row.workspace_id``.
    if ctx.workspace_id != workspace_id:
        raise InviteStateInvalid(
            f"invite {invite_row.id!r}: ctx workspace {ctx.workspace_id!r} "
            f"does not match invite workspace {workspace_id!r}"
        )

    activated_grants: list[str] = []
    for g in invite_row.grants_json:
        grant_role = g.get("grant_role")
        if grant_role not in _VALID_GRANT_ROLES:
            # Defensive — :func:`invite` already validated; this
            # fires only if the JSON was tampered with post-insert.
            continue
        grant = RoleGrant(
            id=new_ulid(clock=clock),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=now,
            created_by_user_id=invite_row.invited_by_user_id,
        )
        session.add(grant)
        activated_grants.append(grant.id)

    activated_group_members: list[str] = []
    for gm in invite_row.group_memberships_json:
        group_id = gm.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            continue
        # Skip duplicates — a user who already holds the membership
        # (e.g. re-invited into the same group) would collide on the
        # composite PK otherwise.
        existing = session.get(PermissionGroupMember, (group_id, user_id))
        if existing is not None:
            continue
        member = PermissionGroupMember(
            group_id=group_id,
            user_id=user_id,
            workspace_id=workspace_id,
            added_at=now,
            added_by_user_id=invite_row.invited_by_user_id,
        )
        session.add(member)
        activated_group_members.append(group_id)

    # §03 "Additional users": seed a minimal pending
    # :class:`WorkEngagement` at accept time, never at invite-create
    # time — nothing workspace-scoped exists for the invitee until
    # they complete the passkey challenge. The helper is idempotent
    # (returns the existing row if one is already active), so an
    # accept-replay after partial failure lands the same engagement
    # id rather than a duplicate. Only run it for ``worker`` /
    # ``manager`` grants — ``client`` + ``guest`` grants do not carry
    # a pay pipeline, so a pending engagement for them would be
    # misleading. cd-4o61: when the invite carried a
    # ``work_engagement_json`` payload, that overrides the default
    # ``payroll`` row for ``engagement_kind`` and ``supplier_org_id``;
    # absent payload falls back to the legacy ``payroll`` default.
    pending_engagement = invite_row.work_engagement_json
    needs_engagement_seed = pending_engagement is not None or any(
        g.get("grant_role") in {"worker", "manager"} for g in invite_row.grants_json
    )
    if needs_engagement_seed:
        # cd-hso7 lifted the seed helper into
        # :func:`app.domain.identity.work_engagements.seed_pending_work_engagement`
        # behind the :class:`MembershipRepository` Protocol. This call
        # site cannot use the helper directly: constructing the SA
        # concretion (:class:`SqlAlchemyMembershipRepository`) here
        # would re-introduce the forbidden domain → adapters edge that
        # the seam was created to remove, and plumbing a repo
        # parameter through ``complete_invite`` / ``confirm_invite``
        # is out of scope for cd-hso7. Until cd-vc3r splits the invite
        # lifecycle out of this module (at which point the new home
        # can take a repo argument cleanly), we duplicate the helper's
        # body here as an inline write. The ORM import is already
        # stopgapped by cd-jpa (membership reads ``UserWorkspace`` /
        # ``Workspace`` ORM models for its own joins) so adding
        # ``WorkEngagement`` does not widen the contract surface.
        #
        # KEEP IN SYNC with
        # :func:`app.domain.identity.work_engagements.seed_pending_work_engagement`
        # — same idempotency rule, same row defaults, same audit
        # action / diff shape.
        #
        # Idempotency: skip the insert if an active engagement already
        # exists for the (user, workspace) pair, matching the
        # accept-replay safety the helper offers.
        existing_active = session.scalar(
            select(WorkEngagement).where(
                WorkEngagement.user_id == user_id,
                WorkEngagement.workspace_id == workspace_id,
                WorkEngagement.archived_on.is_(None),
            )
        )
        if existing_active is None:
            # cd-4o61: when the invite carried an explicit engagement
            # payload, use it; otherwise fall back to the legacy
            # ``payroll`` default. ``invite()`` already validated the
            # supplier-pairing biconditional so the payload is safe to
            # write directly.
            if pending_engagement is not None:
                engagement_kind = str(pending_engagement.get("engagement_kind"))
                supplier_org_id = pending_engagement.get("supplier_org_id")
            else:
                engagement_kind = "payroll"
                supplier_org_id = None
            engagement_id = new_ulid(clock=clock)
            engagement = WorkEngagement(
                id=engagement_id,
                user_id=user_id,
                workspace_id=workspace_id,
                engagement_kind=engagement_kind,
                supplier_org_id=supplier_org_id,
                pay_destination_id=None,
                reimbursement_destination_id=None,
                started_on=now.date(),
                archived_on=None,
                notes_md="",
                created_at=now,
                updated_at=now,
            )
            session.add(engagement)
            session.flush()
            write_audit(
                session,
                ctx,
                entity_kind="work_engagement",
                entity_id=engagement_id,
                action="work_engagement.seeded_on_accept",
                diff={
                    "user_id": user_id,
                    "engagement_kind": engagement_kind,
                    "supplier_org_id": supplier_org_id,
                    "started_on": engagement.started_on.isoformat(),
                },
                clock=clock,
            )

    # cd-4o61: insert the pending ``user_work_role`` rows captured at
    # invite time. Idempotent on accept-replay — a row already
    # present for the (user, workspace, role, started_on) tuple is
    # skipped rather than colliding on the unique index. The
    # workspace pinning was validated at invite time
    # (:func:`_validate_user_work_roles`) so we only need a defensive
    # re-check on the JSON shape here.
    activated_user_work_roles: list[str] = []
    pending_user_work_roles = invite_row.user_work_roles_json or []
    for uwr in pending_user_work_roles:
        if not isinstance(uwr, dict):
            continue
        work_role_id = uwr.get("work_role_id")
        if not isinstance(work_role_id, str) or not work_role_id:
            continue
        existing_uwr = session.scalar(
            select(UserWorkRole).where(
                UserWorkRole.user_id == user_id,
                UserWorkRole.workspace_id == workspace_id,
                UserWorkRole.work_role_id == work_role_id,
                UserWorkRole.started_on == now.date(),
            )
        )
        if existing_uwr is not None:
            continue
        uwr_id = new_ulid(clock=clock)
        row = UserWorkRole(
            id=uwr_id,
            user_id=user_id,
            workspace_id=workspace_id,
            work_role_id=work_role_id,
            started_on=now.date(),
            ended_on=None,
            pay_rule_id=None,
            created_at=now,
            deleted_at=None,
        )
        session.add(row)
        session.flush()
        activated_user_work_roles.append(uwr_id)
        write_audit(
            session,
            ctx,
            entity_kind="user_work_role",
            entity_id=uwr_id,
            action="user_work_role.created",
            diff={
                "user_id": user_id,
                "work_role_id": work_role_id,
                "started_on": now.date().isoformat(),
                "ended_on": None,
                "source": "invite_accept",
            },
            clock=clock,
        )

    invite_row.state = "accepted"
    invite_row.accepted_at = now
    session.flush()

    # The accept handler returns a redirect to ``/w/<slug>/today``;
    # the tenancy resolver fails closed on a missing ``user_workspace``
    # row, and the cd-yqm4 derive-refresh worker only catches up on
    # its own cadence (5 min by default). Without a synchronous
    # scoped reconcile here the post-accept GET would 404 for up to
    # one tick. Deferred imports avoid a domain → domain circular
    # import at module load time (membership and user_workspace_refresh
    # both live under ``app.domain.identity``).
    from app.domain.identity.user_workspace_refresh import (
        reconcile_user_workspace_for,
    )

    reconcile_user_workspace_for(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
        now=now,
    )

    write_audit(
        session,
        ctx,
        entity_kind="invite",
        entity_id=invite_row.id,
        action=audit_action,
        diff={
            "email_hash": invite_row.email_hash,
            "user_id": user_id,
            "activated_grant_ids": activated_grants,
            "activated_group_memberships": activated_group_members,
            # cd-4o61: surface the freshly-inserted user_work_role
            # ids so a forensic join over the accept transaction is
            # one query instead of N. Empty list when the invite did
            # not carry a ``user_work_roles`` payload.
            "activated_user_work_role_ids": activated_user_work_roles,
        },
        clock=clock,
    )


def _load_pending_invite_for_accept(
    session: DbSession, *, invite_id: str, now: datetime
) -> Invite:
    """Load an invite in the ``pending`` state and enforce its TTL.

    Used by :func:`complete_invite` and :func:`confirm_invite` — both
    run after the initial consume, so the caller's UoW still holds
    the row's pre-flip state. The ORM tenant filter is bypassed via
    :func:`tenant_agnostic` because accept flows runs at the bare
    host / under the incoming user's own ctx.
    """
    with tenant_agnostic():
        invite_row = session.get(Invite, invite_id)
    if invite_row is None:
        raise InviteNotFound(invite_id)
    if invite_row.state == "accepted":
        raise InviteAlreadyAccepted(invite_id)
    if invite_row.state in ("revoked", "expired"):
        raise InviteStateInvalid(
            f"invite {invite_id!r} is in state {invite_row.state!r}"
        )
    if _aware_utc(invite_row.expires_at) <= now:
        raise InviteExpired(f"invite {invite_id!r} expired")
    return invite_row


def complete_invite(
    session: DbSession,
    *,
    invite_id: str,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> str:
    """Second leg — activate the invite for a brand-new invitee.

    Called from the passkey-finish hook after the invitee's fresh
    credential lands. One transaction: insert role_grant +
    permission_group_member + user_workspace, flip the invite to
    ``accepted``, audit ``user.enrolled``.

    **Authorisation gate.** The only client evidence this function
    receives is the ``invite_id``; a ULID is not a secret. To stop a
    bare ``POST /invite/complete`` with a guessed / leaked id from
    activating grants, we require that the invite's linked user
    holds at least one registered :class:`PasskeyCredential` at the
    moment of this call. The passkey enrol ceremony writes the
    credential row before the SPA reaches this hook, so on the happy
    path the check is a single cheap SELECT; on the attack path, the
    invite is pending + user has no passkey yet and we raise
    :class:`PasskeySessionRequired` (mapped to 401 by the router).

    Returns the target workspace's id so the router can redirect
    the SPA to ``/w/<slug>/today``.

    The caller's UoW owns the transaction boundary.
    """
    # ``settings`` is reserved — Phase 1 doesn't derive anything
    # invite-specific at complete time (the signed token was already
    # redeemed by :func:`consume_invite_token`); the param keeps the
    # signature symmetric with :func:`invite` / :func:`consume_invite_token`.
    del settings
    resolved_now = now if now is not None else _now(clock)
    invite_row = _load_pending_invite_for_accept(
        session, invite_id=invite_id, now=resolved_now
    )
    user_id = invite_row.user_id
    if user_id is None:
        raise InviteStateInvalid(
            f"invite {invite_id!r} carries no user_id; cannot complete"
        )

    # Authorisation gate — see docstring. Until cd-kd26 folds the
    # completion into the passkey-finish hook, we guard on
    # passkey-presence: the enrolment ceremony MUST have landed a
    # credential before ``/invite/complete`` is reachable.
    if not _user_has_passkey(session, user_id=user_id):
        raise PasskeySessionRequired(
            f"invite {invite_id!r}: linked user {user_id!r} has no "
            "passkey registered; the enrolment ceremony must complete "
            "before /invite/complete is called"
        )

    # Build a user-scoped ctx attributing the audit row to the
    # freshly-enrolled user (same pattern as :func:`app.auth.signup.complete_signup`).
    #
    # ``principal_kind="system"``: ``POST /invite/complete`` is the
    # public new-user passkey-finish hook — the invitee has no
    # session cookie yet (they were just created), so the request
    # carries neither cookie nor bearer header. The audit row keeps
    # ``actor_kind="user"`` (attributing the enrolment to the
    # invitee) but the transport is system-driven. Mirrors
    # :func:`app.auth.signup.complete_signup`'s ``real_ctx`` so a
    # future guard that branches on ``principal_kind`` treats both
    # invite-complete and signup-finish symmetrically.
    real_ctx = WorkspaceContext(
        workspace_id=invite_row.workspace_id,
        workspace_slug="",  # router fills this on response; audit only uses ids
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="manager",  # overwritten per grant on subsequent writes
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(clock=clock),
        principal_kind="system",
    )

    # ``POST /invite/complete`` is a bare-host route: the tenancy
    # middleware never installed a :class:`WorkspaceContext`, so the
    # ContextVar the orm tenant filter reads is unset. Without this
    # explicit push, ``seed_pending_work_engagement``'s SELECT against
    # :class:`WorkEngagement` (a workspace-scoped table) raises
    # :class:`~app.tenancy.orm_filter.TenantFilterMissing`. Mirror the
    # FastAPI tenancy middleware's set/reset pair (see
    # :class:`app.tenancy.middleware.TenancyMiddleware`) so the same
    # filter rules apply on this synthesised context.
    token = set_current(real_ctx)
    try:
        _activate_invite(
            session,
            real_ctx,
            invite_row=invite_row,
            now=resolved_now,
            audit_action="user.enrolled",
            clock=clock,
        )
    finally:
        reset_current(token)
    return invite_row.workspace_id


def confirm_invite(
    session: DbSession,
    ctx: WorkspaceContext,
    *,
    invite_id: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> str:
    """Second leg — existing user confirms via the Acceptance card.

    Expects ``ctx.actor_id`` to match the invite's ``user_id`` (the
    router validated the session already). Writes the same
    downstream rows as :func:`complete_invite` but audits
    ``user.grant_accepted``.
    """
    resolved_now = now if now is not None else _now(clock)
    invite_row = _load_pending_invite_for_accept(
        session, invite_id=invite_id, now=resolved_now
    )
    if invite_row.user_id != ctx.actor_id:
        raise PasskeySessionRequired(
            f"invite {invite_id!r}: acting user {ctx.actor_id!r} "
            f"does not match invite user {invite_row.user_id!r}"
        )
    # ``POST /invite/{invite_id}/confirm`` is a bare-host route just
    # like ``/invite/complete``: the tenancy middleware skipped, so
    # the ContextVar the orm tenant filter reads is unset and
    # :func:`_activate_invite`'s downstream call to
    # ``seed_pending_work_engagement`` would raise
    # :class:`~app.tenancy.orm_filter.TenantFilterMissing`. Mirror the
    # FastAPI tenancy middleware's set/reset pair so the same filter
    # rules apply on the synthesised context the router built.
    token = set_current(ctx)
    try:
        _activate_invite(
            session,
            ctx,
            invite_row=invite_row,
            now=resolved_now,
            audit_action="user.grant_accepted",
            clock=clock,
        )
    finally:
        reset_current(token)
    return invite_row.workspace_id


# ---------------------------------------------------------------------------
# Invitee passkey enrolment (bridges /invite/accept and /invite/complete)
# ---------------------------------------------------------------------------


def _assert_magic_link_consumed(session: DbSession, *, invite_id: str) -> None:
    """Reject when no ``grant_invite`` magic link has been consumed yet.

    The invite_id alone is not authority — §03 says a leaked invite_id
    must not let an attacker mint a passkey. The legitimate flow must
    have called :func:`consume_invite_token` (i.e. ``POST /invite/accept``),
    which flips the matching :class:`MagicLinkNonce`'s ``consumed_at``
    to the current wall-clock. We re-discover that signal here rather
    than threading per-invite state through the :class:`Invite` row,
    so the gate works on existing data without a schema migration.

    Re-issued invites delete prior unconsumed nonces in
    :func:`_invalidate_pending_invite_nonces` but leave consumed rows
    intact — once the invitee proved email control once, that proof
    rides through subsequent re-sends. Stolen invite_id can still
    reach this guard but the magic-link nonce delivered only to the
    invitee is the seed an attacker cannot synthesise.

    Raises :class:`PasskeySessionRequired` (mapped to 401 by the
    router) when no consumed grant_invite nonce exists for the
    invite. The class is reused rather than introducing a fourth
    invite-state error so the SPA's existing "redirect to /accept"
    handler covers this case naturally.
    """
    with tenant_agnostic():
        consumed = session.scalar(
            select(MagicLinkNonce)
            .where(
                MagicLinkNonce.subject_id == invite_id,
                MagicLinkNonce.purpose == "grant_invite",
                MagicLinkNonce.consumed_at.is_not(None),
            )
            .limit(1)
        )
    if consumed is None:
        raise PasskeySessionRequired(
            f"invite {invite_id!r}: magic link not consumed; "
            "POST /invite/accept must run before /invite/passkey/{start,finish}"
        )


def _load_pending_invite_for_passkey(
    session: DbSession, *, invite_id: str, now: datetime
) -> tuple[Invite, User]:
    """Return ``(invite, user)`` for an invite ready for passkey enrolment.

    Reuses :func:`_load_pending_invite_for_accept` to enforce the
    invite-state contract (pending, not expired, not revoked), then
    re-checks via :func:`_assert_magic_link_consumed` that the
    invitee actually clicked the magic link (proves email control
    against a leaked / guessed invite_id), then loads the linked user
    row tenant-agnostically (``user`` is identity-scoped), and
    rejects when the user is already enrolled.

    Raises:

    * :class:`InviteNotFound` / :class:`InviteAlreadyAccepted` /
      :class:`InviteStateInvalid` / :class:`InviteExpired` — same as
      :func:`complete_invite` (state machine).
    * :class:`PasskeySessionRequired` — the magic link was never
      consumed; ``/invite/accept`` must run first.
    * :class:`InvitePasskeyAlreadyRegistered` — the user already
      has at least one passkey credential; the bare-host enrolment
      route is closed.
    """
    invite_row = _load_pending_invite_for_accept(session, invite_id=invite_id, now=now)
    _assert_magic_link_consumed(session, invite_id=invite_id)
    user_id = invite_row.user_id
    if user_id is None:
        raise InviteStateInvalid(
            f"invite {invite_id!r} carries no user_id; cannot enrol passkey"
        )
    if _user_has_passkey(session, user_id=user_id):
        raise InvitePasskeyAlreadyRegistered(
            f"invite {invite_id!r}: user {user_id!r} already has a passkey"
        )
    with tenant_agnostic():
        user = session.get(User, user_id)
    if user is None:
        raise InviteStateInvalid(
            f"invite {invite_id!r}: linked user {user_id!r} missing"
        )
    return invite_row, user


def register_invite_passkey_start(
    session: DbSession,
    *,
    invite_id: str,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> passkey_service.RegistrationOptions:
    """Mint a registration challenge for a brand-new invitee's first passkey.

    Bridges :func:`consume_invite_token` (which left the invite in
    ``state=pending`` with ``user_id`` set but no passkey) and
    :func:`complete_invite` (which requires a passkey on file). The
    challenge is stamped against the ``invite_id`` (carried in the
    challenge row's ``signup_session_id`` slot — the column is a
    free-form challenge subject, not exclusive to signups) so the
    finish call can verify the credential lands under the right user
    without taking an authenticated session.

    **Authorisation gates.**

    1. **Magic-link consumed.** ``/invite/accept`` must have run (the
       matching :class:`MagicLinkNonce`'s ``consumed_at`` is set).
       This is the email-control proof — the magic link is delivered
       only to the invitee, so the gate stops a leaked / guessed
       ``invite_id`` from skipping straight to passkey enrolment.
       Failure raises :class:`PasskeySessionRequired` (401).
    2. **Invite still pending.** Not accepted / revoked / expired
       (TTL).
    3. **No passkey on file yet.** Once a credential lands, this
       route rejects further calls — the invitee must follow the
       recovery / "add another passkey" flow instead. Closing the
       route post-enrolment stops a leaked ``invite_id`` from minting
       a second uninvited credential.

    The caller's UoW owns the transaction boundary.
    """
    # ``settings`` reserved for future per-deployment knobs (e.g.
    # rate-limit overrides); the symmetry with :func:`complete_invite`
    # keeps the router's wiring uniform.
    del settings
    resolved_now = now if now is not None else _now(clock)
    invite_row, user = _load_pending_invite_for_passkey(
        session, invite_id=invite_id, now=resolved_now
    )
    return passkey_service.register_start_signup(
        session,
        signup_session_id=invite_id,
        email=user.email_lower,
        display_name=invite_row.display_name,
        user_handle=user.id.encode("utf-8"),
        clock=clock,
        now=resolved_now,
    )


def register_invite_passkey_finish(
    session: DbSession,
    *,
    invite_id: str,
    challenge_id: str,
    credential: dict[str, Any],
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> passkey_service.PasskeyCredentialRef:
    """Verify the attestation + persist the invitee's first passkey.

    Mirrors :func:`register_invite_passkey_start` for the second leg.
    Reuses :func:`app.auth.passkey.register_finish_signup` so the
    challenge-TTL, attestation verification, and challenge-burn
    semantics match the self-serve signup ceremony exactly.

    A ``passkey.registered`` audit row lands inline (scoped to the
    invite's workspace, attributing the actor to the freshly-enrolled
    invitee) so a §03 "Every enrollment writes to the audit log"
    invariant holds even if the SPA never reaches
    :func:`complete_invite`. The complementary ``user.enrolled`` audit
    is emitted by :func:`complete_invite` itself once the grants
    activate. cd-kd26 will fold both into one callback.

    **Concurrency** — the ``_user_has_passkey`` gate inside
    :func:`_load_pending_invite_for_passkey` is point-in-time. Two
    concurrent ``/passkey/finish`` calls for the same invite could
    both pass the gate before either commits. We take a row-level
    lock on the invitee's :class:`User` row before the gate re-check
    so the check-then-insert sequence serialises across processes
    (Postgres). SQLite ignores ``with_for_update`` but already
    serialises writes at the engine level, so the same invariant
    holds. The loser of the race observes one passkey on the user
    and raises :class:`InvitePasskeyAlreadyRegistered`.

    Raises the same state errors as :func:`register_invite_passkey_start`
    plus the passkey domain errors :func:`register_finish_signup`
    surfaces (challenge unknown / consumed / expired / subject
    mismatch / invalid attestation / too-many-passkeys).
    """
    del settings
    resolved_now = now if now is not None else _now(clock)
    # Reload the invite + user so a finish call posted after the user
    # already has a passkey (e.g. a stale tab replaying a finish that
    # already succeeded) is rejected with the same shape as start.
    invite_row, user = _load_pending_invite_for_passkey(
        session, invite_id=invite_id, now=resolved_now
    )
    # Race defence: serialise concurrent finishes against the same
    # user row, then re-check under the lock — another transaction
    # may have just committed the first credential between the
    # initial gate read and our lock acquisition.
    with tenant_agnostic():
        session.execute(select(User.id).where(User.id == user.id).with_for_update())
    if _user_has_passkey(session, user_id=user.id):
        raise InvitePasskeyAlreadyRegistered(
            f"invite {invite_id!r}: concurrent enrolment registered "
            f"a passkey for user {user.id!r}"
        )
    ref = passkey_service.register_finish_signup(
        session,
        signup_session_id=invite_id,
        user_id=user.id,
        challenge_id=challenge_id,
        credential=credential,
        clock=clock,
        now=resolved_now,
    )
    # §03 audit: the credential row landed; emit before /complete is
    # reachable so an abandoned flow still leaves a forensic trail.
    # ``principal_kind="system"`` matches :func:`complete_invite` —
    # the invitee has no session at this seam, so the request is
    # system-driven even though the actor is the invitee.
    audit_ctx = WorkspaceContext(
        workspace_id=invite_row.workspace_id,
        workspace_slug="",
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(clock=clock),
        principal_kind="system",
    )
    write_audit(
        session,
        audit_ctx,
        entity_kind="passkey_credential",
        entity_id=ref.credential_id_b64url,
        action="passkey.registered",
        diff={
            "user_id": user.id,
            "aaguid": ref.aaguid,
            "transports": ref.transports,
            "backup_eligible": ref.backup_eligible,
            "via": "invite",
        },
        clock=clock,
    )
    return ref


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


def remove_member(
    session: DbSession,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    clock: Clock | None = None,
) -> None:
    """Strip every grant + permission_group membership + session for ``user_id``.

    Spec §03 / §05: the workspace admin clicks "remove from workspace"
    on a user's profile. Owners can remove anyone except the last
    owner; the last-owner guard reuses
    :class:`app.domain.identity.permission_groups.WouldOrphanOwnersGroup`
    so the invariant definition lives in one place (§02
    "permission_group" §"Invariants").

    Writes (in one transaction):

    1. Delete every :class:`RoleGrant` for ``(workspace, user)``.
    2. Delete every :class:`PermissionGroupMember` for
       ``(workspace, user)``. If the user is the sole owner, the
       guard refuses BEFORE the DELETE; the caller's UoW keeps the
       rows intact.
    3. Delete every :class:`Session` row whose ``workspace_id``
       matches the caller's workspace.

    The derived :class:`UserWorkspace` row is dropped inline via
    :func:`reconcile_user_workspace_for` so a removed user does not
    keep a stale ``guest``-fallback :class:`WorkspaceContext` for up
    to one worker tick; the cd-yqm4 worker still owns steady-state
    reconciliation but the security-critical drop on the removal
    path runs synchronously.

    Audit: one ``user.removed`` row with the list of deleted grant
    ids + group memberships + session count (PII-hash only). On the
    last-owner refusal, the router writes a fresh-UoW audit row via
    :func:`write_member_remove_rejected_audit` (already exported
    from :mod:`app.domain.identity.permission_groups`) and the
    primary UoW rolls back.
    """
    resolved_now = _now(clock)

    # Resolve the owners group to run the last-owner guard. The
    # guard mirrors :mod:`app.domain.identity.permission_groups` so
    # both the remove_member entry point and direct group mutations
    # reject the same shape.
    owners_group = session.scalar(
        select(PermissionGroup).where(
            PermissionGroup.workspace_id == ctx.workspace_id,
            PermissionGroup.slug == "owners",
            PermissionGroup.system.is_(True),
        )
    )
    if owners_group is None:
        # Every workspace has an owners group; a missing row means
        # somebody bypassed bootstrap. Fail loud so the operator
        # can investigate rather than silently leave a workspace
        # ungoverned.
        raise InviteStateInvalid(f"workspace {ctx.workspace_id!r} has no owners group")

    membership = session.get(PermissionGroupMember, (owners_group.id, user_id))
    if membership is not None:
        # Last-owner guard: mirror the shape in
        # :mod:`app.domain.identity.permission_groups` so both entry
        # points enforce the same invariant (§02 "permission_group"
        # §"Invariants"). ``func.count()`` avoids loading every row
        # and stays honest about the membership head count without
        # a subsequent materialisation.
        from sqlalchemy import func as sa_func

        total_owner_members = (
            session.scalar(
                select(sa_func.count())
                .select_from(PermissionGroupMember)
                .where(PermissionGroupMember.group_id == owners_group.id)
            )
            or 0
        )
        if total_owner_members <= 1:
            raise WouldOrphanOwnersGroup(
                f"cannot remove the last member of the 'owners' group; "
                f"workspace_id={ctx.workspace_id!r} user_id={user_id!r}"
            )

    # Gather forensic fields before the DELETE — the rows disappear
    # in the next statement and the audit row needs their ids.
    grant_rows = list(
        session.scalars(
            select(RoleGrant).where(
                RoleGrant.workspace_id == ctx.workspace_id,
                RoleGrant.user_id == user_id,
            )
        ).all()
    )
    group_member_rows = list(
        session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.workspace_id == ctx.workspace_id,
                PermissionGroupMember.user_id == user_id,
            )
        ).all()
    )
    if not grant_rows and not group_member_rows:
        # No live membership — the caller targeted a user who was
        # never part of this workspace (or was already removed).
        # Audit the refusal for forensics but don't raise; the HTTP
        # router maps an empty delete as 404 / 204 per its own
        # vocabulary. We raise :class:`NotAMember` so the caller has
        # the choice.
        raise NotAMember(
            f"user {user_id!r} has no grants in workspace {ctx.workspace_id!r}"
        )

    deleted_grant_ids = [row.id for row in grant_rows]
    deleted_group_ids = [row.group_id for row in group_member_rows]

    session.execute(
        delete(RoleGrant)
        .where(
            RoleGrant.workspace_id == ctx.workspace_id,
            RoleGrant.user_id == user_id,
        )
        .execution_options(synchronize_session="fetch")
    )
    session.execute(
        delete(PermissionGroupMember)
        .where(
            PermissionGroupMember.workspace_id == ctx.workspace_id,
            PermissionGroupMember.user_id == user_id,
        )
        .execution_options(synchronize_session="fetch")
    )

    # Revoke every session scoped to this workspace. A session with
    # ``workspace_id IS NULL`` (user is signed in but hasn't picked
    # a workspace) stays — it's identity-level, not membership-level.
    # justification: ``session`` is user-scoped, filter by workspace_id explicitly.
    with tenant_agnostic():
        # Pre-count so the audit row carries the number accurately
        # (DML ``rowcount`` depends on the driver: -1 on SQLite when
        # ``synchronize_session="fetch"`` flattens the returning-rows
        # path, and the generic :class:`Result` stub in SQLAlchemy's
        # typing doesn't surface it anyway).
        from sqlalchemy import func as sa_func

        sessions_revoked = (
            session.scalar(
                select(sa_func.count())
                .select_from(SessionRow)
                .where(
                    SessionRow.user_id == user_id,
                    SessionRow.workspace_id == ctx.workspace_id,
                )
            )
            or 0
        )
        session.execute(
            delete(SessionRow)
            .where(
                SessionRow.user_id == user_id,
                SessionRow.workspace_id == ctx.workspace_id,
            )
            .execution_options(synchronize_session="fetch")
        )

    session.flush()

    # Drop the derived ``user_workspace`` row synchronously — without
    # this the removed user keeps a stale membership for up to one
    # cd-yqm4 worker tick (5 min by default), and the tenancy resolver
    # would happily build a ``guest``-fallback :class:`WorkspaceContext`
    # against the now-empty ``role_grant`` set. Deferred import —
    # see the matching note in :func:`_activate_invite`.
    from app.domain.identity.user_workspace_refresh import (
        reconcile_user_workspace_for,
    )

    reconcile_user_workspace_for(
        session,
        user_id=user_id,
        workspace_id=ctx.workspace_id,
        now=resolved_now,
    )

    write_audit(
        session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="user.removed",
        diff={
            # PII minimisation (§15): forensic joins travel via
            # ``user_id``, which is identity-anchored and non-PII.
            # The email lives on the :class:`User` row and never
            # rides the audit diff for remove; audit readers that
            # want the hash can join to the invite trail.
            "user_id": user_id,
            "deleted_grant_ids": deleted_grant_ids,
            "deleted_group_memberships": deleted_group_ids,
            "sessions_revoked": sessions_revoked,
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# list_workspaces_for_user + switch_session_workspace
# ---------------------------------------------------------------------------


def list_workspaces_for_user(
    session: DbSession,
    *,
    user_id: str,
) -> Sequence[WorkspaceMembership]:
    """Return every workspace ``user_id`` is a member of.

    Drives the workspace switcher UI (§14) and the
    ``GET /api/v1/me/workspaces`` route. Reads the derived
    :class:`UserWorkspace` junction directly — the user_workspace
    derive-refresh worker (cd-yqm4) keeps it in sync.

    No tenant filter: the user spans multiple workspaces and this
    call deliberately aggregates across them. We run it under
    :func:`tenant_agnostic` so the ORM filter doesn't narrow the
    result to the caller's current workspace.
    """
    with tenant_agnostic():
        rows = session.execute(
            select(UserWorkspace, Workspace)
            .join(Workspace, Workspace.id == UserWorkspace.workspace_id)
            .where(UserWorkspace.user_id == user_id)
            .order_by(Workspace.slug.asc())
        ).all()
    return [
        WorkspaceMembership(
            workspace_id=ws.id,
            workspace_slug=ws.slug,
            workspace_name=ws.name,
        )
        for _, ws in rows
    ]


def switch_session_workspace(
    session: DbSession,
    *,
    session_id: str,
    user_id: str,
    workspace_id: str,
    clock: Clock | None = None,
) -> None:
    """Update ``Session.workspace_id`` after verifying membership.

    Spec §03 "Sessions": a single passkey session hops between
    workspaces. The row's ``user_id`` stays pinned; only
    ``workspace_id`` moves, gated by an explicit membership check
    (:class:`UserWorkspace` row exists for the pair).

    Raises:

    * :class:`NotAMember` — the user has no :class:`UserWorkspace`
      row for ``workspace_id``.
    * :class:`InviteNotFound` — no :class:`Session` row for
      ``session_id`` / ``user_id`` combination. (Reused symbol
      avoids a bespoke ``SessionNotFound`` when the router already
      distinguishes 401 vs 404 on this family.)
    """
    resolved_now = _now(clock)
    # Verify the user is actually a member of the target workspace.
    with tenant_agnostic():
        member = session.get(UserWorkspace, (user_id, workspace_id))
    if member is None:
        raise NotAMember(
            f"user {user_id!r} is not a member of workspace {workspace_id!r}"
        )

    # justification: ``session`` is user-scoped; no tenant predicate applies.
    with tenant_agnostic():
        row = session.get(SessionRow, session_id)
    if row is None or row.user_id != user_id:
        raise InviteNotFound(session_id)

    old_workspace_id = row.workspace_id
    row.workspace_id = workspace_id
    row.last_seen_at = resolved_now
    session.flush()

    # Synthesise a ctx attributing the audit row to the actor + the
    # new workspace — the event belongs to the workspace the session
    # moved to so dashboard queries ("what did I do in workspace
    # X?") surface the hop.
    ctx = WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="",
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(clock=clock),
    )
    write_audit(
        session,
        ctx,
        entity_kind="session",
        entity_id=session_id,
        action="session.workspace_switched",
        diff={
            "user_id": user_id,
            "old_workspace_id": old_workspace_id,
            "new_workspace_id": workspace_id,
        },
        clock=clock,
    )
