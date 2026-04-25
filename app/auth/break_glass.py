"""Break-glass code domain helpers — step-up classifier + redemption.

Spec §03 "Break-glass codes" + "Self-service lost-device recovery"
step 3 (cd-gh7l). Two public entry points wire the
``POST /recover/passkey/request`` step-up gate inside
:mod:`app.auth.recovery`:

* :func:`is_step_up_user` — classify whether the user holds **any**
  active ``manager`` surface grant on any workspace OR membership in
  any ``owners`` permission group on any scope. The recovery flow
  uses this to decide whether a break-glass code is required before
  a magic link will be minted.
* :func:`redeem_code` — verify a submitted plaintext code against the
  user's unused :class:`BreakGlassCode` rows; on a match, burn the
  row (``used_at = now()``) and return the row's id so the recovery
  flow can stamp ``consumed_magic_link_id`` once the magic link is
  minted.

Plus a redemption-rate-limit gate
(:func:`check_redeem_allowed` / :func:`record_redeem_failure` /
:func:`record_redeem_success`) implementing the spec's §15 cap of
**3 failed attempts per user / 1-hour rolling window → 24-hour
lockout**. Storage is the same in-memory shape :mod:`app.auth._throttle`
uses; cd-7huk absorbs both into the shared deployment-wide state
store. ``crew.day`` runs one worker per deployment (§01) so the
process-local counter is correct semantics for v1.

**Tenant-agnostic.** Every read/write here runs under
:func:`tenant_agnostic`. The redemption flow executes during recovery
— before any :class:`~app.tenancy.WorkspaceContext` is resolved (the
user may belong to any number of workspaces and we don't pick one at
recovery time). The ``role_grant`` / ``permission_group_member`` /
``break_glass_code`` tables are workspace-scoped by registration, so
the ORM tenant filter would otherwise refuse the read.

**PII minimisation (§15).** No plaintext code is persisted, logged,
or audited; the hash is the only durable form. Callers passing a
plaintext through :func:`redeem_code` are expected to drop it from
memory once this function returns.

See ``docs/specs/03-auth-and-tokens.md`` §"Break-glass codes" /
§"Self-service lost-device recovery" and
``docs/specs/15-security-privacy.md`` §"Step-up bypass is not a
fallback".
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import BreakGlassCode
from app.tenancy import tenant_agnostic

__all__ = [
    "BreakGlassLockedOut",
    "check_redeem_allowed",
    "is_step_up_user",
    "record_redeem_failure",
    "record_redeem_success",
    "redeem_code",
    "reset_rate_limit_for_tests",
]


_log = logging.getLogger(__name__)


# argon2-cffi's :class:`PasswordHasher` is thread-safe and stateless
# once constructed, so sharing a single instance across the process
# is cheap. Parameters mirror :data:`app.auth.tokens._HASHER` — the
# same v1 cost target the API-token surface picked.
_HASHER: Final[PasswordHasher] = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
)


# §15 "Step-up bypass is not a fallback" / §03 "Break-glass codes" →
# "Redemption rate limit". 3 failed attempts within a 1-hour rolling
# window flips the user into a 24-hour lockout. Module-level Finals
# so tests can monkey-patch them to tight values without re-plumbing
# the helper.
_REDEEM_FAIL_LIMIT: Final[int] = 3
_REDEEM_FAIL_WINDOW: Final[timedelta] = timedelta(hours=1)
_REDEEM_LOCKOUT: Final[timedelta] = timedelta(hours=24)


# Slug of the spec's deployment-wide owners group inside every
# workspace. Matches :class:`PermissionGroup.slug`'s seeded value
# (cd-ctb).
_OWNERS_SLUG: Final[str] = "owners"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BreakGlassLockedOut(Exception):
    """User exceeded the 3-failed-attempt cap inside the rolling window.

    429-equivalent. Spec §03 "Break-glass codes" → "Redemption rate
    limit": "all subsequent ``POST /auth/magic/consume`` calls keyed
    to this user return ``429 break_glass_locked_out``, regardless of
    source IP or client". The recovery router maps to a 202 anyway
    (the enumeration guard never lets the caller see the lockout
    state — it's a forensic trail only); the helper raises so the
    audit row + early-out path is single-source.
    """


# ---------------------------------------------------------------------------
# Rate-limit store — process-local, identical shape to
# :class:`app.auth._throttle.Throttle`.
# ---------------------------------------------------------------------------


@dataclass
class _RateLimitState:
    """Per-user redemption attempt counters + lockout marker.

    ``failures`` is a rolling window of failed-attempt timestamps;
    when its length crosses :data:`_REDEEM_FAIL_LIMIT`, ``locked_until``
    is set to ``now + _REDEEM_LOCKOUT`` and the window is cleared so
    the user has to earn the next lockout from scratch once this one
    expires (matches the magic-link consume-lockout shape).
    """

    failures: deque[datetime]
    locked_until: datetime | None


_RATE_LIMIT: dict[str, _RateLimitState] = defaultdict(
    lambda: _RateLimitState(failures=deque(), locked_until=None)
)
_RATE_LIMIT_LOCK: threading.Lock = threading.Lock()


def reset_rate_limit_for_tests() -> None:
    """Clear the per-user redemption counters.

    Underscore-suffixed variant lives in tests' autouse fixtures so
    a crashing case doesn't bleed lockout state into the next case.
    Public name (no leading underscore) so the test suite can import
    it through the normal ``from app.auth.break_glass import …``
    path; mirrors :func:`app.auth.recovery.prune_expired_recovery_sessions`.
    """
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT.clear()


def _evict_expired_failures(state: _RateLimitState, *, now: datetime) -> None:
    """Drop failed-attempt timestamps older than ``now - window``."""
    cutoff = now - _REDEEM_FAIL_WINDOW
    while state.failures and state.failures[0] < cutoff:
        state.failures.popleft()


def _evict_expired_lockout(state: _RateLimitState, *, now: datetime) -> None:
    """Clear ``locked_until`` if the ban has elapsed."""
    if state.locked_until is not None and state.locked_until <= now:
        state.locked_until = None


def check_redeem_allowed(*, user_id: str, now: datetime) -> None:
    """Raise :class:`BreakGlassLockedOut` if ``user_id`` is locked out.

    Called by :func:`app.auth.recovery.request_recovery` **before**
    any DB read so a locked-out user never even touches the
    :class:`BreakGlassCode` index. Clears a lapsed lockout in
    passing.
    """
    with _RATE_LIMIT_LOCK:
        state = _RATE_LIMIT[user_id]
        _evict_expired_lockout(state, now=now)
        if state.locked_until is not None:
            raise BreakGlassLockedOut(
                f"user {user_id!r} locked out of break-glass redemption "
                f"until {state.locked_until.isoformat()}"
            )


def record_redeem_failure(*, user_id: str, now: datetime) -> None:
    """Increment the per-user failure counter; flip lockout on the Nth fail.

    Called after :func:`redeem_code` returns ``None`` (no matching
    unused row) on a step-up branch. Success does **not** call this.
    """
    with _RATE_LIMIT_LOCK:
        state = _RATE_LIMIT[user_id]
        _evict_expired_failures(state, now=now)
        state.failures.append(now)
        if len(state.failures) >= _REDEEM_FAIL_LIMIT:
            state.locked_until = now + _REDEEM_LOCKOUT
            # Clear the rolling window so the user has to earn the
            # next lockout from scratch once this one expires —
            # mirrors the magic-link consume-lockout shape.
            state.failures.clear()


def record_redeem_success(*, user_id: str) -> None:
    """Reset the per-user failure counter on a successful redemption.

    A redemption that returned a row id means the user finally got
    through; we don't want one bad attempt 30 minutes ago to still
    count against their next legitimate try.
    """
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT.pop(user_id, None)


# ---------------------------------------------------------------------------
# Step-up classifier
# ---------------------------------------------------------------------------


def is_step_up_user(session: SqlaSession, *, user_id: str) -> bool:
    """Return ``True`` iff ``user_id`` is in the spec's step-up population.

    Spec §03 "Self-service lost-device recovery" step 3: the
    step-up population is the union of

    * users holding **any** active ``manager`` ``role_grant`` on any
      workspace (``RoleGrant.grant_role == 'manager'``);
    * users who are members of **any** ``owners`` permission group
      on any scope (``PermissionGroup.slug == 'owners'`` joined to
      ``PermissionGroupMember.user_id``).

    Either condition lands the user in the step-up population — a
    matching break-glass code is required before
    :func:`app.auth.recovery.request_recovery` will mint a magic
    link.

    **Tenant-agnostic.** The recovery flow runs outside any
    workspace ctx; both the ``role_grant`` and
    ``permission_group_member`` tables are workspace-scoped by
    registration. We open a :func:`tenant_agnostic` block so the
    ORM tenant filter doesn't refuse the read.

    **Forward-compat — non-archived grants.** v1's ``role_grant``
    schema has no ``revoked_at`` column (revocation is a hard DELETE
    today, see :mod:`app.domain.identity.role_grants` module
    docstring), so every extant row is an active grant by
    construction. ``users.archived_at`` likewise hasn't landed on
    the read path here. When those columns arrive (cd-x1xh)
    the WHERE clause extends with ``RoleGrant.revoked_at IS NULL`` /
    a ``users.archived_at IS NULL`` pre-gate. The helper's public
    contract (return ``True`` iff the user holds a step-up role
    *somewhere*) stays unchanged.
    """
    with tenant_agnostic():
        # Manager-grant check — one row anywhere is enough.
        manager_grant = session.scalar(
            select(RoleGrant.id)
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.grant_role == "manager")
            .limit(1)
        )
        if manager_grant is not None:
            return True
        # Owners-group membership check — join member → group on
        # ``slug == 'owners'`` so a workspace whose owners group has
        # been renamed (impossible in v1 but defensive) wouldn't
        # silently bypass the gate.
        owners_member = session.scalar(
            select(PermissionGroupMember.user_id)
            .join(
                PermissionGroup,
                PermissionGroup.id == PermissionGroupMember.group_id,
            )
            .where(PermissionGroupMember.user_id == user_id)
            .where(PermissionGroup.slug == _OWNERS_SLUG)
            .limit(1)
        )
        return owners_member is not None


# ---------------------------------------------------------------------------
# Code redemption — argon2id verify + atomic UPDATE
# ---------------------------------------------------------------------------


def redeem_code(
    session: SqlaSession,
    *,
    user_id: str,
    plaintext_code: str,
    now: datetime,
) -> str | None:
    """Verify ``plaintext_code`` against an unused row for ``user_id``; burn on hit.

    Walks every unused :class:`BreakGlassCode` row for the user and
    runs :meth:`PasswordHasher.verify` against each. On the first
    match, stamps ``used_at = now`` on that row and returns its id
    so the caller can stamp ``consumed_magic_link_id`` once the
    matching magic link is minted. ``None`` is returned when no
    unused row matches (wrong code, every code already burnt, no
    codes ever issued).

    **Walks every row** rather than fingerprinting the plaintext
    first because §03 mandates argon2id at rest — we never store the
    plaintext, never store an HMAC of it, and never index by
    plaintext-derived columns. The walk is bounded by the user's
    issued code-set size (the bootstrap ritual mints 8 codes per
    user; §03 line 80) so the worst case is 8 argon2id verifies on
    a wrong-code submission. argon2id at the v1 parameters
    (``time_cost=3, memory_cost=65536``) is ~50ms/op locally — a
    miss therefore costs ~400ms of CPU, which is well inside the
    rate-limited budget the recovery surface accepts.

    **Atomic burn.** The ``used_at`` write happens via the ORM on
    the verified row; the caller commits the surrounding UoW so the
    burn lands together with the audit row + magic-link nonce. A
    racing redemption attempt against the same row by a second
    request would observe ``used_at IS NOT NULL`` on its own scan
    (the partial unused-index loads only ``used_at IS NULL`` rows)
    and fall through to a miss — both attackers cannot redeem the
    same code even on a SQLite engine that doesn't take a row lock.

    **Tenant-agnostic.** Wraps the read + write in
    :func:`tenant_agnostic` because :class:`BreakGlassCode` is
    registered as workspace-scoped (so the management surfaces can
    rely on the standard filter) but the recovery flow runs
    outside every workspace ctx — same pattern :func:`Invite` uses
    on the bare-host accept flow.

    Returns the burnt row's id on success, ``None`` on miss. The
    caller's audit + rate-limit decisions branch on the
    ``None``-vs-id discriminator.
    """
    with tenant_agnostic():
        rows = session.scalars(
            select(BreakGlassCode)
            .where(BreakGlassCode.user_id == user_id)
            .where(BreakGlassCode.used_at.is_(None))
        ).all()
        for row in rows:
            try:
                _HASHER.verify(row.hash, plaintext_code)
            except VerifyMismatchError:
                # Try the next unused row — the user's code-set carries
                # multiple valid codes, so a mismatch on one row says
                # nothing about the others. Mirrors the §03 "redeem
                # any unused code" semantic.
                continue
            except Argon2Error as exc:
                # Structural failure — corrupt hash, parameter
                # mismatch the verifier can't parse. Log and treat as
                # "this row is unusable" so a poisoned row can't lock
                # the user out of every other code in the set.
                _log.warning(
                    "break_glass_code: argon2 verify failed structurally for row %s",
                    row.id,
                    exc_info=exc,
                )
                continue
            # Hit — burn the row and return its id. The caller stamps
            # ``consumed_magic_link_id`` after the magic link is minted.
            row.used_at = now
            session.flush()
            return row.id
    return None
