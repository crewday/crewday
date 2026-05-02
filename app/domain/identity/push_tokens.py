"""Native-app push-token registration + lifecycle (cd-nq9s).

Identity-scoped surface backing ``/api/v1/me/push-tokens`` (§12 "Device
push tokens"). Each :class:`~app.adapters.db.identity.models.UserPushToken`
row is the per-(user, device) handle the §10 "Agent-message delivery"
worker walks when fanning out a push notification to the user's
installed native devices. One row per device install; the platform
(``android`` / ``ios``) discriminates the FCM / APNS adapter.

**Distinct from the workspace-scoped web-push surface**
(:mod:`app.domain.messaging.push_tokens`): that surface stores a
browser ``PushSubscription.endpoint`` URL plus the per-subscription
encryption material, scoped to a workspace. This surface stores a bare
FCM / APNS token plus a platform discriminator, scoped to the user
only — one app install delivers notifications for every workspace the
user belongs to (the push payload names the target workspace).

Public surface:

* **Errors** — :class:`InvalidPlatform` (422), :class:`TokenClaimed`
  (409), :class:`PushTokenNotFound` (404).
* **DTO** — :class:`UserPushTokenView` is the frozen read projection
  the router returns. Deliberately drops the raw ``token`` column —
  PII-adjacent, never echoed back over the wire.
* **Service functions** — :func:`register`, :func:`list_for_user`,
  :func:`refresh`, :func:`unregister`. Each takes ``repo`` plus
  the authenticated ``user_id`` (NOT a workspace context — this
  surface is identity-scoped).

**Authz.** Every operation is self-only: the caller registers their
own device, refreshes their own row, deletes their own row, lists
their own rows. No cross-user view of any kind exists on the REST
surface (§02 "user_push_token" §"Visibility" — "neither owners /
managers nor deployment admins see another user's push tokens").

**Audit (§02 "user_push_token").** Audit rows fire on register,
unregister, and (future) vendor-ack disable. Not on idempotent
re-register (same ``(user_id, platform, token)`` triple) and not on
no-op delete — those are routine client housekeeping, not
audit-worthy state changes. The audit ``diff`` carries
``user_id`` / ``platform`` / ``device_label`` / ``app_version`` —
NEVER the raw ``token`` (PII). The audit writer's redaction seam
re-applies the rule at persistence time as defence-in-depth.

**Architecture.** This module talks to a
:class:`~app.domain.identity.push_tokens_ports.UserPushTokenRepository`
Protocol — never to the SQLAlchemy model class directly. The
SA-backed concretion lives at
:class:`app.adapters.db.identity.repositories.SqlAlchemyUserPushTokenRepository`;
unit tests inject a fake or wire the SA repo over an in-memory SQLite
session. The repo also threads its open ``Session`` through
``repo.session`` so the audit writer can keep using the same UoW.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every audit-emitting
mutation writes one :mod:`app.audit` row in the same transaction.

**501 gating happens in the router**, not here. The router-level
Settings gate maps to ``501 push_unavailable`` for ``POST`` until
the deployment provisions FCM / APNS credentials; ``GET`` and
``DELETE`` are always live so a sign-out can prune a stale row even
on a deployment with push delivery off. The domain service is
always callable so unit tests can exercise the register / refresh /
unregister paths regardless of deployment configuration.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/12-rest-api.md`` §"Device push tokens",
``docs/specs/14-web-frontend.md`` §"Native wrapper readiness", and
``docs/specs/10-messaging-notifications.md`` §"Agent-message delivery".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, get_args

from app.audit import write_audit
from app.domain.identity.push_tokens_ports import (
    UserPushTokenRepository,
    UserPushTokenRow,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "InvalidPlatform",
    "Platform",
    "PushTokenNotFound",
    "TokenClaimed",
    "UserPushTokenView",
    "list_for_user",
    "refresh",
    "register",
    "unregister",
]


# v1 platform whitelist. Extending to a new vendor (Huawei, Windows
# notifications, …) is a one-line widening here plus a code-reviewable
# CHECK-constraint migration. The seam intentionally matches the §02
# enum so a typo at the call site fails at type-check time without
# the router having to re-pin the value.
Platform = Literal["android", "ios"]


# Sentinel zero-ULID identity ctx for audit emission. Native push
# tokens are identity-scoped — every read and write happens before /
# outside any :class:`WorkspaceContext`. Mirrors the same sentinel
# :mod:`app.auth.magic_link` and :mod:`app.domain.identity.email_change`
# already use for pre-tenant / identity-scope audit rows. The audit
# reader recognises the zero-ULID workspace and renders the row as a
# pre-tenant identity event.
_AGNOSTIC_WORKSPACE_ID: Final[str] = "00000000000000000000000000"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidPlatform(ValueError):
    """The ``platform`` field is not one of the v1 whitelisted values.

    422-equivalent. Raised by :func:`register` when a caller submits
    a platform string outside the :data:`Platform` literal whitelist.
    The router-level Pydantic DTO catches the common case before the
    service runs; this exception fires when a Python caller bypasses
    the DTO (a worker test, a future CLI bridge).
    """


class TokenClaimed(Exception):
    """The ``(platform, token)`` pair is already owned by another user.

    409-equivalent. Raised by :func:`register` when the unique
    ``(platform, token)`` index detects a cross-user collision —
    spec §02 "user_push_token" "deterministic ``409 token_claimed``".
    The expected resolution path is the previous owner's native shell
    calling ``DELETE /me/push-tokens/{id}`` on sign-out; until that
    happens the new install cannot register the same device id.
    """


class PushTokenNotFound(LookupError):
    """The push-token id does not belong to the caller (or does not exist).

    404-equivalent. The domain service collapses cross-user PUT /
    DELETE targeting an id that exists for *another* user into the
    same not-found error so the surface does not leak whether the id
    is enrollable elsewhere — same enumeration-guard rule as
    :mod:`app.api.v1.auth.me_tokens`.
    """


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserPushTokenView:
    """Immutable read projection of a ``user_push_token`` row.

    Deliberately drops the raw ``token`` column — it is PII-adjacent
    (uniquely identifies the device install) and §02 "user_push_token"
    explicitly bans logging or echoing it. Callers (router, future
    CLI) that need the row id receive :attr:`id`; the delivery worker
    reads the raw ``token`` directly from the
    :class:`~app.domain.identity.push_tokens_ports.UserPushTokenRow`
    seam projection, never from this view.
    """

    id: str
    user_id: str
    platform: str
    device_label: str | None
    app_version: str | None
    created_at: datetime
    last_seen_at: datetime
    disabled_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_view(row: UserPushTokenRow) -> UserPushTokenView:
    """Project the seam-level row into the public view (drops raw token)."""
    return UserPushTokenView(
        id=row.id,
        user_id=row.user_id,
        platform=row.platform,
        device_label=row.device_label,
        app_version=row.app_version,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        disabled_at=row.disabled_at,
    )


def _agnostic_audit_ctx(user_id: str) -> WorkspaceContext:
    """Build the bare-host :class:`WorkspaceContext` used for audit emission.

    Native push tokens are identity-scoped (no workspace pin), so the
    sentinel zero-ULID workspace + the resolved ``user_id`` actor is
    the most informative shape we can hand the audit writer today.
    Mirrors :func:`app.auth.magic_link._agnostic_audit_ctx` and
    :func:`app.domain.identity.email_change._agnostic_audit_ctx` — same
    pattern every pre-tenant / identity-scope writer uses.
    """
    return WorkspaceContext(
        workspace_id=_AGNOSTIC_WORKSPACE_ID,
        workspace_slug="",
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="manager",  # unused for identity-scope rows
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
        # ``principal_kind="session"`` because every caller landing on
        # this seam authenticated via the passkey session cookie at
        # the router (the surface is bare-host self-only); a future
        # PAT-shaped caller would override.
        principal_kind="session",
    )


def _validate_platform(platform: str) -> Platform:
    """Return ``platform`` narrowed to the v1 whitelist or raise."""
    if platform not in get_args(Platform):
        raise InvalidPlatform(
            f"platform {platform!r} is not in the v1 whitelist "
            f"({', '.join(get_args(Platform))})"
        )
    return platform  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def register(
    repo: UserPushTokenRepository,
    *,
    user_id: str,
    platform: str,
    token: str,
    device_label: str | None = None,
    app_version: str | None = None,
    clock: Clock | None = None,
) -> UserPushTokenView:
    """Upsert a push-token registration for ``user_id`` and return the view.

    Idempotent on ``(user_id, platform, token)``: a second call for
    the same triple returns the existing row, refreshes
    ``last_seen_at``, and writes NO audit row (the native shell
    re-registers on every sign-in; only the first registration is
    audit-worthy).

    Cross-user ``(platform, token)`` collisions raise
    :class:`TokenClaimed` so the router can map to ``409 token_claimed``
    — §02 "user_push_token" "a token that surfaces on two user
    accounts (device hand-off without a sign-out) fails registration
    with a deterministic 409".

    The 501 ``push_unavailable`` short-circuit lives in the **router**
    layer (a Settings-gated boolean): the domain service is always
    callable so unit tests can exercise it regardless of deployment
    config.

    Raises:

    * :class:`InvalidPlatform` — ``platform`` outside the v1 whitelist.
    * :class:`TokenClaimed` — ``(platform, token)`` belongs to another user.
    """
    resolved_platform = _validate_platform(platform)
    now = (clock if clock is not None else SystemClock()).now()

    # Idempotent re-registration: same user, same device, same token —
    # bump ``last_seen_at`` and skip the audit row.
    same_owner = repo.find_by_user_platform_token(
        user_id=user_id,
        platform=resolved_platform,
        token=token,
    )
    if same_owner is not None:
        refreshed = repo.update_last_seen(
            user_id=user_id,
            token_id=same_owner.id,
            last_seen_at=now,
        )
        return _row_to_view(refreshed)

    # Cross-user collision: someone else owns this device handle. The
    # native shell of the previous user is expected to ``DELETE`` on
    # sign-out; until that happens the new owner cannot claim the row.
    other_owner = repo.find_by_platform_token(
        platform=resolved_platform,
        token=token,
    )
    if other_owner is not None:
        # ``other_owner.user_id != user_id`` because the same-owner
        # branch above would have matched first. The error is
        # deliberately non-leaking — the caller sees a generic
        # ``token_claimed`` and learns nothing about which user holds
        # the row.
        raise TokenClaimed("platform / token pair already registered for another user")

    row = repo.insert(
        token_id=new_ulid(),
        user_id=user_id,
        platform=resolved_platform,
        token=token,
        device_label=device_label,
        app_version=app_version,
        created_at=now,
    )

    write_audit(
        repo.session,
        _agnostic_audit_ctx(user_id),
        entity_kind="user_push_token",
        entity_id=row.id,
        action="user_push_token.registered",
        # Deliberately do NOT log ``token`` raw — §02 "user_push_token"
        # "no token payload" in the audit log. The diff carries the
        # routing metadata (which user, which platform, the
        # client-supplied label) so the forensic record stays useful
        # without leaking the device-scope id.
        diff={
            "user_id": user_id,
            "platform": resolved_platform,
            "device_label": device_label,
            "app_version": app_version,
        },
        clock=clock,
    )
    return _row_to_view(row)


def list_for_user(
    repo: UserPushTokenRepository,
    *,
    user_id: str,
) -> tuple[UserPushTokenView, ...]:
    """Return every push-token row owned by ``user_id`` (active + disabled).

    Self-only by construction — the caller resolves ``user_id`` from
    its session cookie at the router seam, and there is no cross-user
    surface on this route. Disabled rows are surfaced (with their
    ``disabled_at`` populated) so the SPA can render them as
    tombstones; the delivery worker filters them out at its own seam.
    """
    rows = repo.list_for_user(user_id=user_id)
    return tuple(_row_to_view(row) for row in rows)


def refresh(
    repo: UserPushTokenRepository,
    *,
    user_id: str,
    token_id: str,
    token: str | None = None,
    clock: Clock | None = None,
) -> UserPushTokenView:
    """Bump ``last_seen_at`` (and optionally swap ``token``) on the named row.

    ``token`` is the OS-rotated value: when supplied, the row's
    ``token`` column is swapped to it (and ``last_seen_at`` is bumped
    to ``now``). When omitted, only ``last_seen_at`` moves.

    Self-only: a refresh targeting an id that exists under another
    user collapses to :class:`PushTokenNotFound` so the surface does
    not leak which user owns the row — same enumeration-guard rule
    as :mod:`app.api.v1.auth.me_tokens`. ``user_id`` here is the
    authenticated session user.

    Raises :class:`PushTokenNotFound` when ``token_id`` does not
    exist for ``user_id``.
    """
    existing = repo.find_by_id(user_id=user_id, token_id=token_id)
    if existing is None:
        raise PushTokenNotFound(f"push token {token_id!r} not found")

    now = (clock if clock is not None else SystemClock()).now()

    if token is not None and token != existing.token:
        # Token rotation: swap the value in place and bump
        # ``last_seen_at`` in the same UoW. We do NOT audit this —
        # OS-driven token rotation is a routine native-shell event
        # and audit volume would dilute the signal. The domain
        # service treats it as a refresh, not a re-registration.
        refreshed = repo.update_token(
            user_id=user_id,
            token_id=token_id,
            token=token,
            last_seen_at=now,
        )
    else:
        refreshed = repo.update_last_seen(
            user_id=user_id,
            token_id=token_id,
            last_seen_at=now,
        )
    return _row_to_view(refreshed)


def unregister(
    repo: UserPushTokenRepository,
    *,
    user_id: str,
    token_id: str,
    clock: Clock | None = None,
) -> None:
    """Delete the named push-token row owned by ``user_id``.

    Idempotent on miss: a delete targeting a row that does not exist
    (already-deleted client retry, cross-user attempt) is a silent
    no-op — no audit row, no error. The SPA / native shell can call
    DELETE without first checking the row's existence.

    Self-only: a delete targeting an id that exists under another
    user is collapsed to a no-op so the surface does not leak which
    user owns the row. The audit row only fires when a row was
    actually removed for ``user_id``.
    """
    existing = repo.find_by_id(user_id=user_id, token_id=token_id)
    if existing is None:
        # Idempotent miss — see docstring. No audit row, no error.
        return

    removed = repo.delete(user_id=user_id, token_id=token_id)
    if not removed:
        # Defensive: the row was visible to ``find_by_id`` but
        # vanished mid-UoW (a concurrent delete from a sibling
        # session). Treat as the same idempotent no-op — no audit
        # row, no error.
        return

    write_audit(
        repo.session,
        _agnostic_audit_ctx(user_id),
        entity_kind="user_push_token",
        entity_id=existing.id,
        action="user_push_token.deleted",
        # Same redaction discipline as :func:`register` — never log
        # the raw ``token``.
        diff={
            "user_id": user_id,
            "platform": existing.platform,
            "device_label": existing.device_label,
            "app_version": existing.app_version,
        },
        clock=clock,
    )
