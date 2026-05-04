"""API-token domain service — mint, verify, list, revoke.

Pure domain code. **No FastAPI coupling.** The HTTP router
(:mod:`app.api.v1.auth.tokens`, :mod:`app.api.v1.auth.me_tokens`)
owns status-code mapping + request parsing; this module owns row
lifecycle + argon2id verification + audit writes. The caller's UoW
owns the transaction boundary (§01 "Key runtime invariants" #3) —
this module never calls ``session.commit()``.

**Three token kinds** (§03 "API tokens"):

* ``scoped`` — workspace-pinned, scope-limited, long-lived. The
  cd-c91 default. :func:`mint` requires ``workspace_id`` and a
  non-empty ``scopes`` dict.
* ``delegated`` — workspace-pinned but scope-less: authority
  inherits from the delegating user's :class:`RoleGrant` rows (§11
  embedded agents). :func:`mint` requires
  ``delegate_for_user_id`` + ``workspace_id`` and refuses non-empty
  ``scopes``; default TTL 30 days.
* ``personal`` — PAT minted by a user for themselves, ``me:*``
  scopes only, **no workspace** (§03 "Personal access tokens").
  :func:`mint` refuses ``workspace_id`` for this kind, requires
  ``subject_user_id``, and validates every scope key starts with
  ``me.`` (the router re-validates against the action catalog).

**Token shape** (§03 "API tokens / Creation"):

* ``mip_<key_id>_<secret>``
* ``key_id`` is a 26-char Crockford-base32 ULID (public; stored in
  the clear as :attr:`ApiToken.id` so every request can be O(1)
  located).
* ``secret`` is 256 bits of random drawn from
  :func:`secrets.token_bytes`, encoded as RFC 4648 base32 without
  padding — 52 characters from the alphabet ``A-Z2-7``.
* Total length: ``4 + 26 + 1 + 52 = 83`` characters. Kept opaque to
  callers: they should not parse it beyond the ``mip_`` prefix.

**Hashing** (§03 "Principles" / §15 "Token hashing"): only the
argon2id digest of the secret is stored. :class:`argon2.PasswordHasher`
applies a per-hash random salt, so two tokens sharing a secret would
still produce distinct stored values — the ``hash`` column carries
the full PHC string (``$argon2id$v=19$m=...,t=...,p=...$<salt>$<digest>``)
which the verifier re-parses on every request.

**Argon2 parameters.** ``time_cost=3, memory_cost=65536 (64 MiB),
parallelism=4`` — argon2-cffi's documented defaults. Rationale: the
secret carries 256 bits of entropy already, so the hash is not a
brute-force barrier but a *leak* barrier (if the DB is exfiltrated,
an attacker cannot replay the secret without also breaking argon2id's
memory-hard work factor). The defaults are comfortably above OWASP's
2023 floor (m=19 MiB, t=2) while remaining cheap enough for per-
request verification on modern hardware (~15 ms on a cloud VM).
Rotation (cd-c91 follow-up) will store ``time_cost`` / ``memory_cost``
in a sibling column so a parameter bump can re-hash on next use
without a big-bang migration.

**Caps** (§03 "Guardrails", task spec): 5 active tokens per user per
workspace and 50 active scoped + delegated tokens per workspace.
Creating a 6th for the same user raises :class:`TooManyTokens`, mapped
to HTTP 422 ``too_many_tokens``. Creating a 51st workspace-scoped token
raises :class:`TooManyWorkspaceTokens`, mapped to HTTP 422
``too_many_workspace_tokens``. Counts are computed inside the mint
transaction so concurrent creates cannot both slip past a cap.

**``last_used_at`` debouncing.** Per-request updates are the single
biggest source of write amplification for tokens — every API call
would otherwise touch the token row's PK index. We coalesce writes
to ≤1 per minute per token: :func:`verify` bumps ``last_used_at``
only when the stored value is ``NULL`` or the delta since the last
write exceeds :data:`_LAST_USED_DEBOUNCE`. Matches the spec clause
"Updated best-effort per request (coalesced to ≤1 write/minute per
token to bound write amp)" in §03 verbatim.

**Audit** (§03 "Every enrollment, login, rotation, and revocation
writes to the audit log"):

* ``audit.api_token.minted`` on :func:`mint` — carries ``token_id``,
  ``prefix``, ``label``, ``scopes`` keys. Never the plaintext token.
* ``audit.api_token.revoked`` on :func:`revoke` when a live row is
  flipped to revoked, and on :func:`revoke_personal` for PATs.
* ``audit.api_token.revoked_noop`` on :func:`revoke` when the row
  was already revoked — kept separate so the trail distinguishes an
  intentional double-click from a real revocation event.

Workspace-scoped events (``scoped`` / ``delegated``) land on the
caller's workspace; PAT events land on the tenant-agnostic identity
seam (zero-ULID workspace id + ``actor_id = subject_user_id``, see
:func:`_pat_audit_ctx`) so workspace-scoped audit views exclude
them and the ``/me`` PAT audit view can filter per-user directly.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens" and
``docs/specs/15-security-privacy.md`` §"Token hashing".
"""

from __future__ import annotations

import base64
import ipaddress
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.repositories import SqlAlchemyRoleGrantRepository
from app.adapters.db.identity.models import ApiToken, ApiTokenRequestLog, User
from app.audit import write_audit
from app.auth.audit import AGNOSTIC_WORKSPACE_ID as _AGNOSTIC_WORKSPACE_ID
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DELEGATED_DEFAULT_TTL_DAYS",
    "PERSONAL_DEFAULT_TTL_DAYS",
    "PERSONAL_SCOPE_PREFIX",
    "SCOPED_DEFAULT_TTL_DAYS",
    "DelegatingUserArchived",
    "DelegatingUserInactive",
    "InvalidToken",
    "MintedToken",
    "SubjectUserArchived",
    "SubjectUserInactive",
    "TokenAuditEntry",
    "TokenExpired",
    "TokenKind",
    "TokenKindInvalid",
    "TokenMintFailed",
    "TokenRevoked",
    "TokenShapeError",
    "TokenSummary",
    "TooManyPersonalTokens",
    "TooManyTokens",
    "TooManyWorkspaceTokens",
    "VerifiedToken",
    "list_audit",
    "list_personal_audit",
    "list_personal_tokens",
    "list_tokens",
    "mint",
    "record_request_audit",
    "revoke",
    "revoke_personal",
    "rotate",
    "rotate_personal",
    "truncate_ip_prefix",
    "verify",
]


# ``TokenKind`` is the domain vocabulary for §03's three-way
# discriminator. Defined as a ``Literal`` so callers get compile-time
# validation and the DB CHECK constraint + the service layer share a
# single source of truth for the allowed values.
TokenKind = Literal["scoped", "delegated", "personal"]


# ---------------------------------------------------------------------------
# Constants — spec-pinned
# ---------------------------------------------------------------------------


# ``mip_`` is the crew.day standalone / delegated / personal token
# family prefix. Mirrors ``mip_<key_id>_<secret>`` in §03 "Creation".
# Kept as a module constant so the parser + builder share one rule and
# a future rename (e.g. ``crd_``) is a single-line edit.
_TOKEN_PREFIX: Final[str] = "mip_"

# 32 bytes → 256 bits of secret material per token. The base32 encoding
# below expands this to 52 characters of ASCII so the whole token is
# URL-safe without quoting. 256 bits of entropy is more than enough to
# make brute-force infeasible regardless of the argon2 parameters — the
# hash exists to blunt a DB leak, not to slow down a guessing attack.
_SECRET_BYTES: Final[int] = 32

# First 8 chars of the secret are stored in ``ApiToken.prefix`` so the
# listings page can show a human-recognisable "mip_xxxxxxxx" suffix
# without ever loading the plaintext. 8 characters of base32 carries
# 40 bits of entropy — uniquely recognisable in a manager's token list
# without leaking enough to brute-force the remaining 216 bits.
_PREFIX_CHARS: Final[int] = 8

# Per-user per-workspace active-token cap. Matches the 5-passkey cap
# (§03 "Additional passkeys") — one mental model for the end user,
# same "revoke one to add one" UX shape. Note: this is the per-user
# cap, not the §03 workspace-wide 50-token cap. Applies to ``scoped``
# + ``delegated`` tokens on the same workspace; ``personal`` tokens
# carry their own per-subject 5-token cap below.
_MAX_ACTIVE_TOKENS_PER_USER: Final[int] = 5

# Per-workspace cap for live scoped + delegated tokens (§03
# "Guardrails"). PATs are identity-scoped and excluded.
_MAX_ACTIVE_TOKENS_PER_WORKSPACE: Final[int] = 50

# Per-subject personal-access-token cap (§03 "Personal access tokens"
# guardrails). Separate from the workspace-scoped cap above because
# PATs live at the identity scope — a user with 5 scoped tokens on a
# workspace can still hold 5 PATs.
_MAX_PERSONAL_TOKENS_PER_USER: Final[int] = 5

# Default TTLs per kind. The router still owns the HTTP-surface
# default (so the 201 response carries the expected ``expires_at``),
# but the service layer mirrors the constant so direct callers (CLI,
# worker) don't have to import the router module for a policy value.
# §03 "Guardrails": "Scoped tokens default to 90 days TTL if
# ``expires_at_days`` is omitted; delegated tokens default to 30 days;
# personal access tokens default to 90 days."
SCOPED_DEFAULT_TTL_DAYS: Final[int] = 90
DELEGATED_DEFAULT_TTL_DAYS: Final[int] = 30
PERSONAL_DEFAULT_TTL_DAYS: Final[int] = 90

# Scope-key prefix every PAT scope MUST carry. §03 "Personal access
# tokens" pins the ``me:*`` family: ``me.tasks:read``,
# ``me.bookings:read``, etc. The dot separator between ``me`` and the
# resource narrows the family in a way that can't be confused with a
# workspace scope (``tasks:read``) — mixing the two on the same
# token is a 422 ``me_scope_conflict``.
PERSONAL_SCOPE_PREFIX: Final[str] = "me."

# ``last_used_at`` write debounce. A heavily-used token (an agent
# polling every few seconds) would otherwise hammer its row's PK
# index on every request; the debounce drops the write rate to
# ≤1/min per token — the exact ceiling §03 pins.
_LAST_USED_DEBOUNCE: Final[timedelta] = timedelta(minutes=1)
_REQUEST_PATH_MAX_CHARS: Final[int] = 256
_REQUEST_USER_AGENT_MAX_CHARS: Final[int] = 512


# Rotation keeps the old hash usable long enough for already-running
# agents to reload the new plaintext. Workspace-level configuration is
# tracked separately; cd-oa8iz pins the default overlap to one hour.
_ROTATION_OVERLAP: Final[timedelta] = timedelta(hours=1)


# Sentinel workspace id for tenant-agnostic (identity-scope) audit
# rows — PAT mint / revoke have no workspace to borrow. Re-exported
# from :mod:`app.auth.audit` (cd-rqhy consolidated the six byte-
# identical bare-host copies there) under the module-private name
# (see the import block above) so nothing else in this module reaches
# across the boundary for a private symbol. ``_pat_audit_ctx`` itself
# stays here because its actor is the **real** subject user, not the
# zero-ULID — the only audit-ctx in the auth tree that intentionally
# diverges from the canonical shape.

# argon2-cffi's ``PasswordHasher`` is thread-safe and stateless once
# constructed, so sharing a single instance across the process is
# cheap. The parameters match the v1 choice documented in the module
# docstring; a follow-up rotation task (cd-c91 extension) wires a
# per-token ``hash_params`` column so a parameter bump can rehash
# lazily on next :func:`verify`.
_HASHER: Final[PasswordHasher] = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MintedToken:
    """Result of :func:`mint` — the plaintext token, shown to the user once.

    The caller (HTTP router) surfaces ``token`` in the 201 response
    body and the mobile / CLI clients echo it back with every
    subsequent request. After the response lands there is no way to
    retrieve the plaintext again — only :attr:`ApiToken.hash` remains
    in the database, so a lost token forces the user to mint a new
    one.

    ``kind`` echoes the domain discriminator so the caller can render
    the right UI chrome ("Delegated as Alice", "Personal") without a
    follow-up fetch.
    """

    token: str
    key_id: str
    prefix: str
    expires_at: datetime | None
    kind: TokenKind


@dataclass(frozen=True, slots=True)
class TokenSummary:
    """Public projection of one :class:`ApiToken` row for list / audit UIs.

    Mirrors §03 "Revocation and rotation" / §14's ``/tokens`` panel:
    every field is safe to show to any workspace manager, none of
    them leak the plaintext secret. ``hash`` is deliberately
    **omitted** — the list surface never needs it, and leaving it
    off the projection makes it structurally impossible for a router
    to return the digest by mistake.

    ``kind`` + ``delegate_for_user_id`` + ``subject_user_id`` surface
    the cd-i1qe discriminator so the ``/tokens`` UI (workspace view)
    can flag "delegated as Alice" rows and the ``/me`` UI (personal
    view) can list PATs without rejoining :class:`User` or reparsing
    the token. The list endpoints narrow by kind where appropriate
    (manager /tokens surface omits personal; /me surface omits
    scoped / delegated); the projection is shared so both routers
    read the same shape.
    """

    key_id: str
    label: str
    prefix: str
    scopes: Mapping[str, Any]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    kind: TokenKind
    delegate_for_user_id: str | None
    subject_user_id: str | None


@dataclass(frozen=True, slots=True)
class TokenAuditEntry:
    """One audit-log entry projected for the per-token UI.

    §03 "Revocation and rotation" / "per-token audit log view": the
    /tokens page shows a per-token history. Lifecycle events come
    from workspace-scoped ``audit_log`` rows whose
    ``entity_kind == 'api_token'`` and ``entity_id == <key_id>``;
    per-request rows come from ``api_token_request_log`` and carry
    the method, path, response status, IP prefix, and user_agent.

    Columns mirror the wire shape the SPA reads: ``at`` is the
    event timestamp, ``action`` is the audit symbol
    (``api_token.minted`` / ``api_token.request`` etc.), ``actor_id``
    is the workspace user tied to the event, and ``correlation_id``
    joins this event to other rows in the same request.
    """

    at: datetime
    action: str
    actor_id: str
    correlation_id: str
    method: str | None = None
    path: str | None = None
    status: int | None = None
    ip_prefix: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """Result of :func:`verify` — the identity + authority the token grants.

    The caller (tenancy middleware, once cd-ika7 lands) uses
    ``user_id`` + ``workspace_id`` to build the request's
    :class:`WorkspaceContext`, and walks ``scopes`` at the action-catalog
    seam to gate the action. ``key_id`` is echoed into audit so every
    write made through this token is traceable back to one row on the
    ``/tokens`` page.

    ``workspace_id`` is **nullable** because ``personal`` tokens live
    at the identity scope (no workspace pin). The router-level gate
    in the workspace-scoped tree must reject a ``workspace_id is None``
    verify result as ``404 workspace_out_of_scope`` — the domain
    service returns the raw shape and lets the caller decide how to
    surface the mismatch. ``kind`` is echoed so the caller can branch
    on the three families (e.g. delegated → walk the user's grants,
    scoped → walk ``scopes``, personal → narrow to subject).
    """

    user_id: str
    workspace_id: str | None
    scopes: Mapping[str, Any]
    key_id: str
    kind: TokenKind
    delegate_for_user_id: str | None
    subject_user_id: str | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidToken(ValueError):
    """Token format is malformed or its ``key_id`` doesn't resolve.

    Collapsed shape: malformed prefix, wrong segment count, unknown
    ``key_id``, and "secret didn't verify" all raise this same type so
    the HTTP layer can't fingerprint which failure mode fired. The
    router maps it to 401 on the Bearer-auth path and 404 on the
    management path (§03 distinguishes "is this a live credential?"
    from "does this credential exist on the tokens list?").
    """


class TokenExpired(ValueError):
    """Token row exists but ``expires_at`` has passed.

    401-equivalent. Kept distinct from :class:`InvalidToken` so
    metrics can separate "expired tokens still in use" (a client
    that missed the rotation) from "unknown credential" (a probe).
    The HTTP response shape stays opaque.
    """


class TokenRevoked(ValueError):
    """Token row has ``revoked_at`` set.

    401-equivalent, same opaque-response pattern as
    :class:`TokenExpired`.
    """


class TokenMintFailed(RuntimeError):
    """:func:`mint` could not produce a token — internal error.

    Reserved for structural failures (argon2 hasher threw, RNG
    refused). Not mapped to a typed HTTP error — the router lets it
    bubble to 500 so the operator sees the traceback.
    """


class TooManyTokens(ValueError):
    """User already holds :data:`_MAX_ACTIVE_TOKENS_PER_USER` live tokens.

    422-equivalent — the HTTP layer maps to ``too_many_tokens`` with
    the 5-token cap in the message so the UI can surface "revoke
    one to add another" without hard-coding the number. The count
    is computed inside the mint transaction so concurrent creates
    cannot both land a 6th row. Applies to ``scoped`` + ``delegated``
    tokens on the same workspace; ``personal`` tokens get their own
    :class:`TooManyPersonalTokens`.
    """


class TooManyWorkspaceTokens(ValueError):
    """Workspace already holds live scoped/delegated tokens up to the cap.

    422-equivalent — the HTTP layer maps to
    ``too_many_workspace_tokens`` so the UI can tell a manager to
    revoke workspace tokens before minting another. Applies across all
    users in the workspace and excludes personal access tokens.
    """


class TooManyPersonalTokens(ValueError):
    """User already holds :data:`_MAX_PERSONAL_TOKENS_PER_USER` live PATs.

    422-equivalent — §03 "Personal access tokens" guardrails pin the
    per-user cap at 5, separate from the workspace-scoped cap. The
    HTTP layer maps to ``too_many_personal_tokens`` per spec.
    """


class TokenKindInvalid(ValueError):
    """Caller asked to mint a kind outside :data:`TokenKind`.

    422-equivalent. Raised before any DB work so a typo in a CLI
    never reaches argon2.
    """


class TokenShapeError(ValueError):
    """Mint arguments violate a per-kind invariant (§03 "API tokens").

    Shape violations that map to 422 validation errors at the HTTP
    layer:

    * ``scoped`` without a workspace, or with ``delegate_for_user_id``
      / ``subject_user_id`` populated;
    * ``delegated`` without a ``delegate_for_user_id``, or with
      non-empty scopes;
    * ``personal`` with a ``workspace_id``, or with a scope key
      outside the ``me:*`` family, or with an empty scope dict.

    The router maps each case to its spec-specific error code
    (``me_scope_conflict`` / ``scopes_required`` / ``kind_conflict``);
    the service layer collapses them into one error type with a
    human message so the router owns the code taxonomy in one place.
    """


class DelegatingUserArchived(ValueError):
    """Delegated token's :attr:`ApiToken.delegate_for_user_id` is archived.

    401-equivalent. Raised by :func:`verify` when the row's
    ``kind == 'delegated'`` and the delegating user's
    :attr:`User.archived_at` is non-NULL (§03 "Delegated tokens": "If
    the delegating user is archived, globally deactivated, or loses
    every non-revoked grant, requests with the token return 401 with
    a clear message"). Distinct from :class:`InvalidToken` /
    :class:`TokenRevoked` / :class:`TokenExpired` so the HTTP layer
    can return ``error = "delegating_user_archived"`` instead of the
    opaque "not a real token" 404 — the agent gets a clear signal
    that re-minting won't help; the human owner has to be reinstated.
    """


class SubjectUserArchived(ValueError):
    """Personal-access token's :attr:`ApiToken.subject_user_id` is archived.

    401-equivalent. Raised by :func:`verify` when the row's
    ``kind == 'personal'`` and the subject user's
    :attr:`User.archived_at` is non-NULL (§03 "Personal access
    tokens": "If the subject user is archived, globally deactivated,
    or loses every non-revoked grant in every workspace, PAT
    requests return 401 with a clear message. Reinstating the user
    reinstates their PATs only if they survived archive (spec is
    archive-preserves-rows; ``users.archived_at`` is set, the token
    stays but returns 401 until the archive flag clears)"). Distinct
    from :class:`InvalidToken` / :class:`TokenRevoked` /
    :class:`TokenExpired` so the HTTP layer can return
    ``error = "subject_user_archived"`` instead of the opaque "not a
    real token" 404 — the user needs to be reinstated, not re-mint
    a fresh token.
    """


class DelegatingUserInactive(ValueError):
    """Delegated token's delegating user has no live ``role_grant`` here.

    401-equivalent. Raised by :func:`verify` when the row's
    ``kind == 'delegated'`` and the delegating user holds **zero**
    role grants with ``revoked_at IS NULL`` in the token's workspace
    (§03 "Delegated tokens": "If the delegating user is archived,
    globally deactivated, or loses every non-revoked grant, requests
    with the token return 401 with a clear message"). cd-x1xh's
    soft-retire columns make this observable: a user whose every
    grant in the workspace has been revoked is materially distinct
    from one who never held one. The verifier checks
    workspace-scoped liveness — a live grant in a *sibling*
    workspace does not unblock this token because the delegated
    token's authority is anchored on the workspace it was minted in.

    Distinct from :class:`InvalidToken` / :class:`TokenRevoked` /
    :class:`TokenExpired` / :class:`DelegatingUserArchived` so the
    HTTP layer can return ``error = "delegating_user_inactive"``
    instead of the opaque "not a real token" 404 — the agent gets a
    clear signal that re-minting won't help; granting the human a
    fresh role grant in the workspace will. Ordered AFTER the
    archive check so an archived user with no grants surfaces as
    ``delegating_user_archived`` (the older / lower-level fact).
    """


class SubjectUserInactive(ValueError):
    """PAT subject user holds no live ``role_grant`` in any workspace.

    401-equivalent. Raised by :func:`verify` when the row's
    ``kind == 'personal'`` and the subject user holds **zero**
    role grants with ``revoked_at IS NULL`` across every workspace
    (§03 "Personal access tokens": "If the subject user is archived,
    globally deactivated, or loses every non-revoked grant in every
    workspace, PAT requests return 401 with a clear message"). PATs
    are workspace-agnostic at issue time (``workspace_id IS NULL``)
    so the liveness check is too — a live grant in *any* workspace
    keeps the token usable; only "no live grants anywhere" gates it.

    Distinct from :class:`InvalidToken` / :class:`TokenRevoked` /
    :class:`TokenExpired` / :class:`SubjectUserArchived` so the HTTP
    layer can return ``error = "subject_user_inactive"`` instead of
    the opaque "not a real token" 404 — granting the user a fresh
    role grant in any workspace reinstates the PAT. Ordered AFTER
    the archive check so an archived subject with no grants
    surfaces as ``subject_user_archived``.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or a fresh system clock."""
    return (clock if clock is not None else SystemClock()).now()


def _pat_audit_ctx(subject_user_id: str) -> WorkspaceContext:
    """Return a tenant-agnostic :class:`WorkspaceContext` for a PAT audit row.

    Personal access tokens live at the identity scope — they have no
    workspace, so the usual :func:`app.audit.write_audit` path (which
    demands a live :class:`WorkspaceContext`) has nothing to borrow.
    We mint a synthetic context that pins:

    * ``workspace_id`` to the zero-ULID sentinel shared with
      :mod:`app.auth.session`, :mod:`app.auth.magic_link`,
      :mod:`app.auth.signup`, and :mod:`app.auth.recovery` — the
      audit reader recognises that value as "identity-scope event"
      and workspace-scoped views naturally exclude it.
    * ``actor_id`` to the **real** subject user's id (not the
      zero-ULID). The ``/me`` "Personal access tokens" audit view
      (§03, §14) filters on
      ``workspace_id=<zero-ulid> AND actor_id=<user>``, so the
      subject's id has to land on the row itself — putting it only
      inside ``diff`` would force a JSON scan on every read. The
      four sibling auth-module helpers use the zero-ULID actor
      because their events (magic link consumed, session revoked
      "everywhere", …) do not yet have a bound user at the moment
      of emission; a PAT mint / revoke always does.
    * ``actor_kind`` to ``"user"`` (the domain literal). The row
      represents a user-initiated rotation / revocation, not a
      system worker firing on schedule.

    ``actor_grant_role`` and ``actor_was_owner_member`` are unused
    for this event family (PATs grant no workspace authority) and
    follow the neutral defaults the sibling helpers pick. The
    correlation id is fresh per call so sibling writes (rare — each
    mint / revoke is a single audit row) still get their own
    trace cursor.
    """
    return WorkspaceContext(
        workspace_id=_AGNOSTIC_WORKSPACE_ID,
        workspace_slug="",
        actor_id=subject_user_id,
        actor_kind="user",
        actor_grant_role="manager",  # unused for identity-scope events
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _generate_secret() -> str:
    """Return a 52-char RFC 4648 base32 secret with padding stripped.

    :func:`secrets.token_bytes(32)` draws 32 bytes from the OS CSPRNG;
    :func:`base64.b32encode` produces 56 chars with 4 trailing ``=``
    pads, which we strip. The result is URL-safe (base32's
    ``A-Z2-7`` alphabet), fixed length (52), and case-insensitive —
    the same shape as ULIDs so it round-trips cleanly through
    Authorization headers and shell one-liners without quoting.
    """
    raw = secrets.token_bytes(_SECRET_BYTES)
    # ``b32encode`` always pads to a multiple of 8 characters; for 32
    # bytes of input that's 56 chars with 4 ``=`` suffix. Stripping
    # padding is lossless because the decoder re-derives it from the
    # remaining length (we never decode — this is a verifier-side
    # opaque string — but the shape is still predictable).
    encoded = base64.b32encode(raw).rstrip(b"=").decode("ascii")
    return encoded


def _parse(token: str) -> tuple[str, str]:
    """Return ``(key_id, secret)`` or raise :class:`InvalidToken`.

    Format: ``mip_<key_id>_<secret>``. We split on the **first two**
    underscores only — every downstream character belongs to the
    secret, including any ``_`` that could appear in a future
    encoding. Today's base32 alphabet excludes ``_``, so the current
    secret portion never carries one, but keeping the parse
    future-proof costs nothing.
    """
    if not token.startswith(_TOKEN_PREFIX):
        raise InvalidToken("token does not start with 'mip_'")
    body = token[len(_TOKEN_PREFIX) :]
    # ``split("_", 1)`` keeps the secret unsplit if it ever gains an
    # underscore — we still split on exactly one separator between
    # key_id and secret.
    parts = body.split("_", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise InvalidToken("token body is not <key_id>_<secret>")
    return parts[0], parts[1]


def _count_active_workspace_for_user(
    session: Session, *, user_id: str, workspace_id: str, now: datetime
) -> int:
    """Return the number of live workspace tokens for ``user_id``.

    Runs under :func:`tenant_agnostic` because ``api_token`` isn't a
    workspace-scoped table in the ORM filter sense (see
    :mod:`app.adapters.db.identity` docstring) — scoping is explicit
    on the ``workspace_id`` column rather than a registered tenant
    filter.

    "Live" means ``revoked_at IS NULL`` **and** ``expires_at IS NULL
    OR expires_at > now``. Expired-but-not-revoked tokens shouldn't
    count against the cap because they're effectively inert; a user
    with 5 dead tokens gathering dust on their /tokens page would
    otherwise be stuck.

    ``personal`` tokens are **excluded** by the ``kind != 'personal'``
    predicate — they get their own per-subject cap via
    :func:`_count_active_personal`. The workspace CAP is about
    "how many workspace-scoped authorities has this user minted
    here"; a PAT doesn't live on this workspace, so counting it
    would over-restrict a user that happens to hold a workspace
    grant + some PATs.
    """
    # justification: api_token is identity-scoped; the ORM tenant
    # filter has no predicate registered for this table and would
    # otherwise refuse the read under a live WorkspaceContext.
    with tenant_agnostic():
        stmt = (
            select(func.count())
            .select_from(ApiToken)
            .where(
                ApiToken.user_id == user_id,
                ApiToken.workspace_id == workspace_id,
                ApiToken.revoked_at.is_(None),
                ApiToken.kind != "personal",
            )
        )
        # Expiry gate: ``expires_at IS NULL`` (no-expiry tokens, via
        # the workspace-override setting) OR ``expires_at > now``
        # (still live). We build the predicate inline because
        # SQLAlchemy's ``func.coalesce`` would require a fallback
        # sentinel that outlives real timestamps, which is harder to
        # reason about than the two-branch OR.
        stmt = stmt.where((ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > now))
        return session.scalar(stmt) or 0


def _count_active_workspace_total(
    session: Session, *, workspace_id: str, now: datetime
) -> int:
    """Return the number of live scoped + delegated tokens in ``workspace_id``."""
    with tenant_agnostic():
        stmt = (
            select(func.count())
            .select_from(ApiToken)
            .where(
                ApiToken.workspace_id == workspace_id,
                ApiToken.revoked_at.is_(None),
                ApiToken.kind != "personal",
            )
        )
        stmt = stmt.where((ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > now))
        return session.scalar(stmt) or 0


def _count_active_personal(
    session: Session, *, subject_user_id: str, now: datetime
) -> int:
    """Return the number of live PATs for a given subject user.

    Per-subject cap (§03 "Personal access tokens" guardrails): 5
    PATs per user, separate from the workspace-scoped cap. "Live"
    follows the same rule as :func:`_count_active_workspace_for_user`
    (``revoked_at IS NULL`` and unexpired).
    """
    with tenant_agnostic():
        stmt = (
            select(func.count())
            .select_from(ApiToken)
            .where(
                ApiToken.subject_user_id == subject_user_id,
                ApiToken.kind == "personal",
                ApiToken.revoked_at.is_(None),
            )
        )
        stmt = stmt.where((ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > now))
        return session.scalar(stmt) or 0


def _normalise_expires_at(value: datetime, now: datetime) -> datetime:
    """Return ``value`` as an aware datetime aligned to ``now``'s tzinfo.

    SQLite's ``DateTime(timezone=True)`` drops tzinfo on roundtrip;
    :func:`verify` compares the round-tripped value against ``now``
    and needs both sides to share an offset. This mirrors the
    pattern used by :mod:`app.auth.session` and
    :mod:`app.auth.passkey`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=now.tzinfo)
    return value


def _maybe_bump_last_used(row: ApiToken, *, now: datetime) -> bool:
    """Return ``True`` and mutate ``row.last_used_at`` if the debounce allows.

    The write actually lands when the caller's UoW flushes — we only
    mutate the ORM-attached instance here. Keeping the decision
    read-only on ``now - row.last_used_at`` means two concurrent
    verifies hitting the same token within the debounce window
    collapse cleanly (they both see "no bump needed" and neither
    write races the other). The first verify past the window wins
    the update; any sibling concurrent verify past the window will
    also bump, which is fine — the ``last_used_at`` column is
    best-effort and idempotent at the minute granularity we care
    about.
    """
    last = row.last_used_at
    if last is None:
        row.last_used_at = now
        return True
    normalised = _normalise_expires_at(last, now)
    if (now - normalised) >= _LAST_USED_DEBOUNCE:
        row.last_used_at = now
        return True
    return False


def _previous_hash_is_live(row: ApiToken, *, now: datetime) -> bool:
    if row.previous_hash is None or row.previous_hash_expires_at is None:
        return False
    expires_at = _normalise_expires_at(row.previous_hash_expires_at, now)
    return expires_at > now


def _clear_expired_previous_hash(row: ApiToken, *, now: datetime) -> bool:
    if row.previous_hash is None and row.previous_hash_expires_at is None:
        return False
    if _previous_hash_is_live(row, now=now):
        return False
    row.previous_hash = None
    row.previous_hash_expires_at = None
    return True


def _narrow_kind(value: str) -> TokenKind:
    """Narrow a raw DB string to the :data:`TokenKind` literal.

    The CHECK constraint guarantees only the three allowed values
    ever land on disk; the narrow is defensive against a future
    hand-edited row and gives mypy the specific literal type the
    projection + verify return shapes depend on. A truly unknown
    value raises :class:`TokenKindInvalid` so the caller sees a
    domain error instead of a silent collapse.
    """
    if value == "scoped":
        return "scoped"
    if value == "delegated":
        return "delegated"
    if value == "personal":
        return "personal"
    raise TokenKindInvalid(f"unknown token kind {value!r}")


def _project(row: ApiToken) -> TokenSummary:
    """Project an :class:`ApiToken` ORM row onto the public summary.

    Hash column is intentionally absent — see :class:`TokenSummary`
    docstring.
    """
    return TokenSummary(
        key_id=row.id,
        label=row.label,
        prefix=row.prefix,
        scopes=dict(row.scope_json),
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        kind=_narrow_kind(row.kind),
        delegate_for_user_id=row.delegate_for_user_id,
        subject_user_id=row.subject_user_id,
    )


# ---------------------------------------------------------------------------
# Public surface — mint
# ---------------------------------------------------------------------------


def _validate_scoped_shape(
    *, scopes: Mapping[str, Any], ctx: WorkspaceContext | None
) -> None:
    """Raise :class:`TokenShapeError` if a scoped-token mint is malformed."""
    if ctx is None:
        raise TokenShapeError("scoped tokens require a WorkspaceContext")
    for key in scopes:
        if key.startswith(PERSONAL_SCOPE_PREFIX):
            # §03 "Personal access tokens": mixing me:* with workspace
            # scopes is a hard error, flagged as ``me_scope_conflict``
            # at the router.
            raise TokenShapeError(f"scoped token must not carry me:* scope {key!r}")


def _validate_delegated_shape(
    *,
    scopes: Mapping[str, Any],
    ctx: WorkspaceContext | None,
    delegate_for_user_id: str | None,
) -> None:
    """Raise :class:`TokenShapeError` if a delegated-token mint is malformed."""
    if ctx is None:
        raise TokenShapeError("delegated tokens require a WorkspaceContext")
    if delegate_for_user_id is None:
        raise TokenShapeError(
            "delegated tokens require delegate_for_user_id (the session user's id)"
        )
    if scopes:
        # §03 "Delegated tokens": "scopes: empty. Permission checks
        # resolve against the delegating user's role_grants." A
        # non-empty scopes dict would give the agent a narrower
        # authority than the spec reserves; reject to keep the
        # invariant obvious to callers.
        raise TokenShapeError("delegated tokens must have empty scopes")


def _validate_personal_shape(
    *,
    scopes: Mapping[str, Any],
    ctx: WorkspaceContext | None,
    subject_user_id: str | None,
) -> None:
    """Raise :class:`TokenShapeError` if a PAT mint is malformed."""
    if ctx is not None:
        raise TokenShapeError(
            "personal access tokens are identity-scoped; pass ctx=None"
        )
    if subject_user_id is None:
        raise TokenShapeError(
            "personal access tokens require subject_user_id (the session user's id)"
        )
    if not scopes:
        raise TokenShapeError("personal access tokens require at least one me:* scope")
    for key in scopes:
        if not key.startswith(PERSONAL_SCOPE_PREFIX):
            raise TokenShapeError(
                f"personal access tokens accept only me:* scopes — got {key!r}"
            )


def mint(
    session: Session,
    ctx: WorkspaceContext | None,
    *,
    user_id: str,
    label: str,
    scopes: Mapping[str, Any],
    expires_at: datetime | None,
    kind: TokenKind = "scoped",
    delegate_for_user_id: str | None = None,
    subject_user_id: str | None = None,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> MintedToken:
    """Create a fresh :class:`ApiToken` row and return the plaintext token.

    The caller's UoW owns the commit; on successful return the row
    exists with ``revoked_at = NULL``, the audit row is queued, and
    :attr:`MintedToken.token` is the **only** place the plaintext
    ever appears — the caller surfaces it in the HTTP response and
    never again.

    **Per-kind contract** (§03 "API tokens"):

    * ``kind='scoped'`` (default) — pass a live :class:`WorkspaceContext`
      and a ``scopes`` dict of workspace-level action keys. Do NOT
      pass ``delegate_for_user_id`` / ``subject_user_id``.
    * ``kind='delegated'`` — pass a live :class:`WorkspaceContext`
      AND ``delegate_for_user_id`` (the session user's id). ``scopes``
      MUST be empty (delegated tokens inherit the user's grants per
      §03 "Delegated tokens").
    * ``kind='personal'`` — pass ``ctx=None`` AND ``subject_user_id``
      (the session user's id). ``scopes`` MUST be non-empty and every
      key MUST start with ``me.`` (the ``me:*`` scope family,
      §03 "Personal access tokens"). The resulting row carries
      ``workspace_id=NULL``.

    Raises:

    * :class:`TokenKindInvalid` — ``kind`` is outside :data:`TokenKind`.
    * :class:`TokenShapeError` — per-kind input shape invariant
      violated (missing / extra fields, ``me:*`` vs workspace scope
      mixing, empty scope set on PAT, non-empty scope set on delegated).
    * :class:`TooManyTokens` — scoped / delegated cap of 5 live tokens
      per user per workspace tripped. Checked before the workspace cap
      so the existing per-user error vocabulary is preserved.
    * :class:`TooManyWorkspaceTokens` — workspace-wide cap of 50 live
      scoped + delegated tokens tripped. PAT holders can still mint
      workspace tokens because PATs do not count here.
    * :class:`TooManyPersonalTokens` — per-user PAT cap of 5 tripped.
    * :class:`TokenMintFailed` — structural failure (argon2 refused,
      RNG refused). Rare enough to bubble as 500.

    Every successful mint emits one ``api_token.minted`` audit row
    with ``kind`` stamped into its diff so the ``/tokens`` page can
    filter (§03 "per-token audit log view") and the downstream
    owner-revoke path can walk delegated tokens per delegating-user
    without re-joining. Workspace-scoped mints land on the caller's
    workspace via ``ctx``; PAT mints land on the tenant-agnostic
    identity seam (zero-ULID workspace id, real subject user as the
    actor) so the ``/me`` audit view can filter per-user without a
    JSON scan.
    """
    resolved_now = now if now is not None else _now(clock)

    # Narrow to the domain literal before any DB work so a CLI typo
    # (``kind='scopped'``) fails cheap and clear.
    if kind not in ("scoped", "delegated", "personal"):
        raise TokenKindInvalid(f"unknown token kind {kind!r}")

    # Per-kind input-shape validation. Each branch raises
    # :class:`TokenShapeError` with a human message the router
    # translates into the spec's error taxonomy (``me_scope_conflict``,
    # ``scopes_required``, etc.).
    if kind == "scoped":
        _validate_scoped_shape(scopes=scopes, ctx=ctx)
    elif kind == "delegated":
        _validate_delegated_shape(
            scopes=scopes, ctx=ctx, delegate_for_user_id=delegate_for_user_id
        )
    else:
        _validate_personal_shape(
            scopes=scopes, ctx=ctx, subject_user_id=subject_user_id
        )

    # Cap enforcement — distinct quotas per kind. Run BEFORE hashing
    # so a rejected request doesn't burn an argon2 cycle.
    if kind in ("scoped", "delegated"):
        # ``ctx`` is guaranteed non-None here by the shape validators
        # above; mypy needs the explicit narrowing so the attribute
        # access below type-checks.
        assert ctx is not None
        active = _count_active_workspace_for_user(
            session,
            user_id=user_id,
            workspace_id=ctx.workspace_id,
            now=resolved_now,
        )
        if active >= _MAX_ACTIVE_TOKENS_PER_USER:
            raise TooManyTokens(
                f"user {user_id!r} already has {active} active workspace tokens "
                f"(max {_MAX_ACTIVE_TOKENS_PER_USER})"
            )
        active_workspace = _count_active_workspace_total(
            session,
            workspace_id=ctx.workspace_id,
            now=resolved_now,
        )
        if active_workspace >= _MAX_ACTIVE_TOKENS_PER_WORKSPACE:
            raise TooManyWorkspaceTokens(
                f"workspace {ctx.workspace_id!r} already has {active_workspace} "
                f"active workspace tokens (max {_MAX_ACTIVE_TOKENS_PER_WORKSPACE})"
            )
    else:
        assert subject_user_id is not None
        active_pat = _count_active_personal(
            session,
            subject_user_id=subject_user_id,
            now=resolved_now,
        )
        if active_pat >= _MAX_PERSONAL_TOKENS_PER_USER:
            raise TooManyPersonalTokens(
                f"user {subject_user_id!r} already has {active_pat} active personal "
                f"tokens (max {_MAX_PERSONAL_TOKENS_PER_USER})"
            )

    key_id = new_ulid(clock=clock)
    secret = _generate_secret()
    prefix = secret[:_PREFIX_CHARS]

    # argon2-cffi raises subclasses of :class:`Argon2Error` on
    # structural failure (parameters out of range, hash refused by the
    # native lib). Rewrap into the domain vocabulary so the HTTP layer
    # doesn't have to reach past the seam; the cause chain preserves
    # the upstream message for operator logs. We narrow to
    # ``Argon2Error`` specifically — anything else is a programming
    # bug that should bubble as a 500 with full traceback.
    try:
        hash_value = _HASHER.hash(secret)
    except Argon2Error as exc:
        raise TokenMintFailed(f"argon2id hash failed: {exc}") from exc

    workspace_id: str | None = ctx.workspace_id if ctx is not None else None
    row = ApiToken(
        id=key_id,
        user_id=user_id,
        workspace_id=workspace_id,
        kind=kind,
        delegate_for_user_id=delegate_for_user_id if kind == "delegated" else None,
        subject_user_id=subject_user_id if kind == "personal" else None,
        label=label,
        scope_json=dict(scopes),
        prefix=prefix,
        hash=hash_value,
        expires_at=expires_at,
        last_used_at=None,
        revoked_at=None,
        created_at=resolved_now,
    )
    # justification: api_token is identity-scoped; writing under a
    # live WorkspaceContext would otherwise force the ORM filter to
    # inject a predicate the table doesn't carry.
    with tenant_agnostic():
        session.add(row)
        session.flush()

    # Every mint writes an audit row. Workspace-scoped tokens
    # (``scoped`` / ``delegated``) land on the caller's workspace via
    # ``ctx``; PATs land on the tenant-agnostic identity seam via
    # :func:`_pat_audit_ctx` — zero-ULID workspace id + real subject
    # user id as the actor, so the ``/me`` audit view can filter on
    # ``workspace_id=<zero-ulid> AND actor_id=<user>`` without a JSON
    # scan (§03 "API tokens", §14 "/me Personal access tokens").
    if ctx is not None:
        write_audit(
            session,
            ctx,
            entity_kind="api_token",
            entity_id=key_id,
            action="api_token.minted",
            diff={
                "token_id": key_id,
                "user_id": user_id,
                "workspace_id": ctx.workspace_id,
                "label": label,
                "prefix": prefix,
                "scopes": sorted(scopes.keys()),
                "expires_at": (
                    expires_at.isoformat() if expires_at is not None else None
                ),
                "kind": kind,
                "delegate_for_user_id": (
                    delegate_for_user_id if kind == "delegated" else None
                ),
            },
            clock=clock,
        )
    else:
        # ``subject_user_id`` is guaranteed non-None on this branch by
        # :func:`_validate_personal_shape` above; mypy needs the
        # explicit narrowing for the helper call.
        assert subject_user_id is not None
        write_audit(
            session,
            _pat_audit_ctx(subject_user_id=subject_user_id),
            entity_kind="api_token",
            entity_id=key_id,
            action="api_token.minted",
            diff={
                "token_id": key_id,
                "user_id": user_id,
                "subject_user_id": subject_user_id,
                "label": label,
                "prefix": prefix,
                "scopes": sorted(scopes.keys()),
                "expires_at": (
                    expires_at.isoformat() if expires_at is not None else None
                ),
                "kind": kind,  # always "personal" on this branch
            },
            clock=clock,
        )

    return MintedToken(
        token=f"{_TOKEN_PREFIX}{key_id}_{secret}",
        key_id=key_id,
        prefix=prefix,
        expires_at=expires_at,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Public surface — list_tokens
# ---------------------------------------------------------------------------


def list_tokens(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
    limit: int | None = None,
    after_id: str | None = None,
) -> list[TokenSummary]:
    """Return ``scoped`` / ``delegated`` tokens on the caller's workspace.

    ``user_id`` narrows to one subject when set. Workspace managers
    call with ``user_id=None`` to audit every workspace token; the
    list includes both active and revoked rows (the UI shows the
    revoked-history tail).

    **Pagination.** When ``limit`` is provided the service returns up
    to ``limit + 1`` rows so the router's
    :func:`~app.api.pagination.paginate` helper can compute
    ``has_more`` without a second query. Rows are ordered by
    ``id DESC`` (newest first); ULIDs are time-ordered, so this maps
    cleanly to the spec's "most recent first" wording without
    materialising a composite ``(created_at, id)`` cursor key.
    ``after_id`` is the previously-returned last-row id (decoded from
    the opaque cursor); rows with ``id >= after_id`` are skipped so a
    forward traversal is strictly monotonic. ``limit=None`` keeps the
    historic "fetch everything" shape so non-router callers (audits,
    workers) don't have to thread a cap through.

    **Personal access tokens are deliberately excluded** — §03 "PATs
    are not listed on the workspace-wide /tokens admin page". A
    manager's audit view should not surface "every worker's printer
    script"; PATs are governed by the subject user on ``/me``. Use
    :func:`list_personal_tokens` for the subject-side listing.

    The projection intentionally omits the hash column — see
    :class:`TokenSummary` docstring for why.
    """
    # justification: api_token is identity-scoped; reuse of the
    # tenant-agnostic gate mirrors ``_count_active_workspace_for_user``.
    with tenant_agnostic():
        stmt = select(ApiToken).where(
            ApiToken.workspace_id == ctx.workspace_id,
            ApiToken.kind != "personal",
        )
        if user_id is not None:
            stmt = stmt.where(ApiToken.user_id == user_id)
        if after_id is not None:
            # DESC traversal — "after" the previous page's last row
            # means strictly smaller id (older).
            stmt = stmt.where(ApiToken.id < after_id)
        stmt = stmt.order_by(ApiToken.id.desc())
        if limit is not None:
            stmt = stmt.limit(limit + 1)
        rows = list(session.scalars(stmt).all())
    return [_project(row) for row in rows]


def list_personal_tokens(
    session: Session,
    *,
    subject_user_id: str,
) -> list[TokenSummary]:
    """Return every PAT (active + revoked) for a given subject user.

    Identity-scoped — no :class:`WorkspaceContext` needed because
    PATs live outside any workspace. The ``/me`` "Personal access
    tokens" panel (§14, cd-i1qe-me-panel follow-up) reads through
    this surface.
    """
    with tenant_agnostic():
        stmt = (
            select(ApiToken)
            .where(
                ApiToken.subject_user_id == subject_user_id,
                ApiToken.kind == "personal",
            )
            .order_by(ApiToken.created_at.desc())
        )
        rows = list(session.scalars(stmt).all())
    return [_project(row) for row in rows]


def list_personal_audit(
    session: Session,
    *,
    token_id: str,
    subject_user_id: str,
    limit: int = 200,
) -> list[TokenAuditEntry]:
    """Return a PAT audit timeline for ``subject_user_id`` only.

    Mirrors :func:`list_audit` for identity-scoped PATs. Unknown,
    non-personal, and cross-subject ids return an empty list so the
    read never discloses whether another user's token exists.
    """
    with tenant_agnostic():
        token_exists = session.scalar(
            select(func.count())
            .select_from(ApiToken)
            .where(
                ApiToken.id == token_id,
                ApiToken.kind == "personal",
                ApiToken.subject_user_id == subject_user_id,
            )
        )
        if not token_exists:
            return []
        lifecycle_stmt = (
            select(AuditLog)
            .where(
                AuditLog.entity_kind == "api_token",
                AuditLog.entity_id == token_id,
                AuditLog.workspace_id == _AGNOSTIC_WORKSPACE_ID,
                AuditLog.actor_id == subject_user_id,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        lifecycle_rows = list(session.scalars(lifecycle_stmt).all())
        request_stmt = (
            select(ApiTokenRequestLog, ApiToken.subject_user_id)
            .join(ApiToken, ApiToken.id == ApiTokenRequestLog.token_id)
            .where(
                ApiTokenRequestLog.token_id == token_id,
                ApiToken.kind == "personal",
                ApiToken.subject_user_id == subject_user_id,
            )
            .order_by(ApiTokenRequestLog.at.desc())
            .limit(limit)
        )
        request_rows = list(session.execute(request_stmt).all())
    entries = [
        TokenAuditEntry(
            at=row.created_at,
            action=row.action,
            actor_id=row.actor_id,
            correlation_id=row.correlation_id,
        )
        for row in lifecycle_rows
    ]
    entries.extend(
        TokenAuditEntry(
            at=row.at,
            action="api_token.request",
            actor_id=actor_id or subject_user_id,
            correlation_id=row.correlation_id,
            method=row.method,
            path=row.path,
            status=row.status,
            ip_prefix=row.ip_prefix,
            user_agent=row.user_agent,
        )
        for row, actor_id in request_rows
    )
    entries.sort(key=lambda entry: entry.at, reverse=True)
    return entries[:limit]


# ---------------------------------------------------------------------------
# Public surface — per-request audit writes
# ---------------------------------------------------------------------------


def truncate_ip_prefix(value: str | None) -> str | None:
    """Return the §15-minimised IP prefix for ``value``.

    IPv4 addresses are stored as their ``/24`` network and IPv6
    addresses as their ``/64`` network. Invalid or absent source-IP
    signals collapse to ``None`` so the audit row records the request
    without persisting a misleading raw string.
    """
    if value is None or not value:
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    prefix = 24 if ip.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def record_request_audit(
    session: Session,
    *,
    token_id: str,
    method: str,
    path: str,
    status: int,
    ip_prefix: str | None,
    user_agent: str | None,
    correlation_id: str,
    at: datetime | None = None,
    clock: Clock | None = None,
) -> None:
    """Queue one per-request audit row for a verified Bearer token."""
    resolved_clock = clock or SystemClock()
    row_at = at or resolved_clock.now()
    session.add(
        ApiTokenRequestLog(
            id=new_ulid(),
            token_id=token_id,
            method=method.upper()[:16],
            path=path[:_REQUEST_PATH_MAX_CHARS],
            status=status,
            ip_prefix=ip_prefix,
            user_agent=(
                user_agent[:_REQUEST_USER_AGENT_MAX_CHARS]
                if user_agent is not None
                else None
            ),
            correlation_id=correlation_id,
            at=row_at,
        )
    )


# ---------------------------------------------------------------------------
# Public surface — audit log projection
# ---------------------------------------------------------------------------


def list_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    token_id: str,
    limit: int = 200,
) -> list[TokenAuditEntry]:
    """Return the per-token audit timeline (newest first).

    §03 "Revocation and rotation": the /tokens page surfaces a
    per-token audit log so a manager can see lifecycle events and
    request rows against a single key_id.

    Refuses cross-workspace lookups by joining on
    ``ctx.workspace_id`` — a manager on workspace A cannot read
    audit rows for a token that lives on workspace B even if they
    correctly guess its id. An unknown / cross-workspace token id
    returns an empty list rather than raising; the router wraps a
    PAT / unknown id check into a 404 separately so the empty
    list here is unambiguously "no events yet".
    """
    with tenant_agnostic():
        lifecycle_stmt = (
            select(AuditLog)
            .where(
                AuditLog.entity_kind == "api_token",
                AuditLog.entity_id == token_id,
                AuditLog.workspace_id == ctx.workspace_id,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        lifecycle_rows = list(session.scalars(lifecycle_stmt).all())
        request_stmt = (
            select(ApiTokenRequestLog, ApiToken.user_id)
            .join(ApiToken, ApiToken.id == ApiTokenRequestLog.token_id)
            .where(
                ApiTokenRequestLog.token_id == token_id,
                ApiToken.workspace_id == ctx.workspace_id,
            )
            .order_by(ApiTokenRequestLog.at.desc())
            .limit(limit)
        )
        request_rows = list(session.execute(request_stmt).all())
    entries = [
        TokenAuditEntry(
            at=row.created_at,
            action=row.action,
            actor_id=row.actor_id,
            correlation_id=row.correlation_id,
        )
        for row in lifecycle_rows
    ]
    entries.extend(
        TokenAuditEntry(
            at=row.at,
            action="api_token.request",
            actor_id=actor_id,
            correlation_id=row.correlation_id,
            method=row.method,
            path=row.path,
            status=row.status,
            ip_prefix=row.ip_prefix,
            user_agent=row.user_agent,
        )
        for row, actor_id in request_rows
    )
    entries.sort(key=lambda entry: entry.at, reverse=True)
    return entries[:limit]


# ---------------------------------------------------------------------------
# Public surface — revoke
# ---------------------------------------------------------------------------


def revoke(
    session: Session,
    ctx: WorkspaceContext,
    *,
    token_id: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> None:
    """Revoke a ``scoped`` / ``delegated`` token on the caller's workspace.

    Idempotent on an already-revoked row. The row is **not** deleted —
    keeping it preserves the link target for existing audit rows
    that reference this token_id (§03 "per-token audit log view").
    A revoked row still appears on the /tokens page in the
    "decommissioned" section.

    **Personal access tokens are refused here.** §03 "Revocation":
    "Personal access tokens are revocable only by their subject
    user or via a cascade" — a manager on the workspace /tokens
    page cannot revoke a worker's PAT directly. A PAT token_id
    surfaced on this router therefore collapses to
    :class:`InvalidToken` (404), same shape as "unknown token";
    the router maps it to ``token_not_found``.

    Raises:

    * :class:`InvalidToken` — no row with this id on the caller's
      workspace, OR the row is a PAT (which isn't workspace-managed).
      Both map to 404 at the router.

    A second call with the same ``token_id`` lands no state change
    but still writes an ``api_token.revoked_noop`` audit row so the
    trail distinguishes a double-click from the initial revocation.
    """
    resolved_now = now if now is not None else _now(clock)

    # justification: api_token is identity-scoped; read under
    # tenant_agnostic for consistency with the other accessors.
    with tenant_agnostic():
        row = session.get(ApiToken, token_id)

    # Fail-closed on cross-workspace access, unknown ids, AND
    # personal tokens (which live outside any workspace). All three
    # collapse into the same 404 shape at the HTTP layer so the API
    # doesn't leak which of the three actually happened.
    if row is None or row.kind == "personal" or row.workspace_id != ctx.workspace_id:
        raise InvalidToken(f"token {token_id!r} not found on this workspace")

    if row.revoked_at is not None:
        # Idempotent no-op — leave the existing ``revoked_at``
        # untouched so the trail keeps the original revocation time.
        write_audit(
            session,
            ctx,
            entity_kind="api_token",
            entity_id=token_id,
            action="api_token.revoked_noop",
            diff={
                "token_id": token_id,
                "already_revoked_at": row.revoked_at.isoformat(),
                "at": resolved_now.isoformat(),
            },
            clock=clock,
        )
        return

    with tenant_agnostic():
        row.revoked_at = resolved_now
        session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="api_token",
        entity_id=token_id,
        action="api_token.revoked",
        diff={
            "token_id": token_id,
            "user_id": row.user_id,
            "workspace_id": row.workspace_id,
            "at": resolved_now.isoformat(),
            "kind": row.kind,
        },
        clock=clock,
    )


def rotate(
    session: Session,
    ctx: WorkspaceContext,
    *,
    token_id: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> MintedToken:
    """Rotate a ``scoped`` / ``delegated`` token's secret in place.

    Generates a fresh secret + argon2id hash and writes them onto the
    existing row, leaving ``id`` (the public ``key_id``), ``label``,
    ``scopes``, ``expires_at``, and the kind discriminators untouched.
    The old hash is retained in ``previous_hash`` for one hour so
    already-running agents can reload the new plaintext without an
    immediate auth outage.

    **Personal access tokens are refused here.** §03 "Revocation":
    PATs are revocable / rotatable only by their subject. A workspace
    manager calling rotate on a PAT collapses to :class:`InvalidToken`
    (404) at the router seam, same shape as "unknown token", so the
    API doesn't leak whose tokens exist.

    A revoked or expired token cannot be rotated — the agent should
    mint a fresh one. Both collapse to :class:`InvalidToken` for the
    same opacity reason. Pinning the rotation surface to live tokens
    keeps the per-token audit log trivially partitioned: one
    ``api_token.rotated`` event per token until revocation closes it.

    Raises:

    * :class:`InvalidToken` — unknown id, cross-workspace, PAT, or
      already-revoked / expired row. All map to 404 at the router.
    * :class:`TokenMintFailed` — argon2 hasher refused (rare).

    Writes one ``api_token.rotated`` audit row carrying the old +
    new prefix so a forensic walk can correlate before / after on a
    single key_id.
    """
    resolved_now = now if now is not None else _now(clock)

    # justification: api_token is identity-scoped; reuse of the
    # tenant-agnostic gate mirrors the other accessors.
    with tenant_agnostic():
        row = session.get(ApiToken, token_id)

    # Fail-closed: cross-workspace, unknown, PAT, revoked, or expired
    # all collapse to the opaque "not found" shape. Expiry uses the
    # same normalisation pattern as :func:`verify` so SQLite roundtrips
    # don't drop tzinfo on the comparison.
    if (
        row is None
        or row.kind == "personal"
        or row.workspace_id != ctx.workspace_id
        or row.revoked_at is not None
    ):
        raise InvalidToken(f"token {token_id!r} not found on this workspace")
    if row.expires_at is not None:
        expires_at = _normalise_expires_at(row.expires_at, resolved_now)
        if expires_at <= resolved_now:
            raise InvalidToken(f"token {token_id!r} not found on this workspace")

    old_prefix = row.prefix
    secret = _generate_secret()
    new_prefix = secret[:_PREFIX_CHARS]

    try:
        new_hash = _HASHER.hash(secret)
    except Argon2Error as exc:
        raise TokenMintFailed(f"argon2id hash failed: {exc}") from exc

    with tenant_agnostic():
        row.previous_hash = row.hash
        row.previous_hash_expires_at = resolved_now + _ROTATION_OVERLAP
        row.hash = new_hash
        row.prefix = new_prefix
        # Reset ``last_used_at`` so the /tokens page's "stale token"
        # heuristic doesn't mark the freshly-rotated row as
        # immediately stale based on the previous secret's traffic.
        row.last_used_at = None
        session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="api_token",
        entity_id=token_id,
        action="api_token.rotated",
        diff={
            "token_id": token_id,
            "user_id": row.user_id,
            "workspace_id": row.workspace_id,
            "old_prefix": old_prefix,
            "new_prefix": new_prefix,
            "at": resolved_now.isoformat(),
            "kind": row.kind,
        },
        clock=clock,
    )

    kind = _narrow_kind(row.kind)
    # Normalise the post-flush ``expires_at`` so the wire shape stays
    # tz-aware UTC. SQLite roundtrips drop tzinfo on the column read;
    # _normalise_expires_at restamps UTC so the JSON serializer emits
    # the trailing ``Z`` consistently with the mint response shape.
    expires_at_out: datetime | None = None
    if row.expires_at is not None:
        expires_at_out = _normalise_expires_at(row.expires_at, resolved_now)
    return MintedToken(
        token=f"{_TOKEN_PREFIX}{row.id}_{secret}",
        key_id=row.id,
        prefix=new_prefix,
        expires_at=expires_at_out,
        kind=kind,
    )


def rotate_personal(
    session: Session,
    *,
    token_id: str,
    subject_user_id: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> MintedToken:
    """Rotate a PAT owned by ``subject_user_id``.

    Unknown, workspace-scoped, cross-subject, revoked, and expired ids
    collapse to :class:`InvalidToken` so the HTTP surface does not leak
    another user's PAT existence. The old secret remains valid for the
    same one-hour previous-hash overlap as workspace tokens.
    """
    resolved_now = now if now is not None else _now(clock)

    with tenant_agnostic():
        row = session.get(ApiToken, token_id)

    if (
        row is None
        or row.kind != "personal"
        or row.subject_user_id != subject_user_id
        or row.revoked_at is not None
    ):
        raise InvalidToken(f"personal token {token_id!r} not found for this user")
    if row.expires_at is not None:
        expires_at = _normalise_expires_at(row.expires_at, resolved_now)
        if expires_at <= resolved_now:
            raise InvalidToken(f"personal token {token_id!r} not found for this user")

    old_prefix = row.prefix
    secret = _generate_secret()
    new_prefix = secret[:_PREFIX_CHARS]

    try:
        new_hash = _HASHER.hash(secret)
    except Argon2Error as exc:
        raise TokenMintFailed(f"argon2id hash failed: {exc}") from exc

    with tenant_agnostic():
        row.previous_hash = row.hash
        row.previous_hash_expires_at = resolved_now + _ROTATION_OVERLAP
        row.hash = new_hash
        row.prefix = new_prefix
        row.last_used_at = None
        session.flush()

    write_audit(
        session,
        _pat_audit_ctx(subject_user_id=subject_user_id),
        entity_kind="api_token",
        entity_id=token_id,
        action="api_token.rotated",
        diff={
            "token_id": token_id,
            "subject_user_id": subject_user_id,
            "old_prefix": old_prefix,
            "new_prefix": new_prefix,
            "at": resolved_now.isoformat(),
            "kind": "personal",
        },
        clock=clock,
    )

    expires_at_out: datetime | None = None
    if row.expires_at is not None:
        expires_at_out = _normalise_expires_at(row.expires_at, resolved_now)
    return MintedToken(
        token=f"{_TOKEN_PREFIX}{row.id}_{secret}",
        key_id=row.id,
        prefix=new_prefix,
        expires_at=expires_at_out,
        kind="personal",
    )


def revoke_personal(
    session: Session,
    *,
    token_id: str,
    subject_user_id: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> None:
    """Revoke a PAT owned by ``subject_user_id``. Identity-scoped.

    §03 "Personal access tokens are revocable only by their subject
    user" — the caller passes the session user's id and the row is
    only revoked if it matches. A mismatch, a workspace token, or an
    unknown id all collapse into :class:`InvalidToken` (404) so the
    API doesn't leak whose tokens exist.

    Writes one ``api_token.revoked`` audit row through the tenant-
    agnostic identity seam (see :func:`_pat_audit_ctx`) so the
    ``/me`` "Personal access tokens" audit view has a trail. An
    already-revoked row is an idempotent no-op and does NOT write a
    second row — matching the workspace-side :func:`revoke`
    precedent of "one revoke event per token lifetime" (the
    workspace path writes an ``api_token.revoked_noop`` for the
    double-click; PATs don't currently need that distinction).
    """
    resolved_now = now if now is not None else _now(clock)

    with tenant_agnostic():
        row = session.get(ApiToken, token_id)

    if row is None or row.kind != "personal" or row.subject_user_id != subject_user_id:
        raise InvalidToken(f"personal token {token_id!r} not found for this user")

    if row.revoked_at is not None:
        # Idempotent no-op — no second audit row, matching the
        # "one revoke per token lifetime" invariant.
        return

    with tenant_agnostic():
        row.revoked_at = resolved_now
        session.flush()

    write_audit(
        session,
        _pat_audit_ctx(subject_user_id=subject_user_id),
        entity_kind="api_token",
        entity_id=token_id,
        action="api_token.revoked",
        diff={
            "token_id": token_id,
            "subject_user_id": subject_user_id,
            "at": resolved_now.isoformat(),
            "kind": "personal",
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public surface — verify
# ---------------------------------------------------------------------------


def _user_is_archived(session: Session, *, user_id: str) -> bool:
    """Return ``True`` iff ``user.archived_at`` is set for ``user_id``.

    Single-column probe — we don't materialise the whole :class:`User`
    row. The verifier is on the per-request hot path and the only
    field it needs is ``archived_at``; an explicit ``SELECT
    archived_at`` keeps the read O(1) on the PK index without
    pulling display name / email / ... into the ORM cache.

    Missing rows (FK target hard-deleted out from under the token —
    the FK is ``ON DELETE SET NULL`` for both ``delegate_for_user_id``
    and ``subject_user_id``, so this should not normally happen on a
    live token, but defensive readers belong here) return ``False``;
    the caller's PK lookup of the token would have already collapsed
    that case to :class:`InvalidToken` upstream.

    Runs under :func:`tenant_agnostic` to mirror the ``api_token``
    accessors above — :class:`User` is not registered as workspace-
    scoped either, but the other reads in this module pay the same
    gate so the pattern stays uniform across the verifier.
    """
    # justification: user is identity-scoped (no workspace_id column);
    # the verifier runs before any WorkspaceContext exists.
    with tenant_agnostic():
        archived_at = session.scalar(select(User.archived_at).where(User.id == user_id))
    return archived_at is not None


def verify(
    session: Session,
    *,
    token: str,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> VerifiedToken:
    """Return the :class:`VerifiedToken` for a valid plaintext token.

    Resolution:

    1. Parse ``mip_<key_id>_<secret>``. Malformed → :class:`InvalidToken`.
    2. Look up by ``id = key_id``. Missing → :class:`InvalidToken`.
    3. ``revoked_at is not None`` → :class:`TokenRevoked`.
    4. ``expires_at <= now`` (when set) → :class:`TokenExpired`.
    5. Verify the secret with argon2id. Mismatch tries
       ``previous_hash`` only while ``previous_hash_expires_at > now``;
       otherwise mismatch → :class:`InvalidToken` — wrapping argon2's
       :class:`VerifyMismatchError` so the caller sees the domain
       vocabulary only.
    6. **Delegating / subject liveness** (cd-et6y + cd-ljvs, §03
       "Delegated tokens" / "Personal access tokens"):

       * ``kind == 'delegated'`` and
         :attr:`User.archived_at` is set on the row's
         ``delegate_for_user_id`` → :class:`DelegatingUserArchived`.
       * ``kind == 'personal'`` and
         :attr:`User.archived_at` is set on the row's
         ``subject_user_id`` → :class:`SubjectUserArchived`.
       * ``kind == 'delegated'`` and the delegating user holds zero
         live ``role_grant`` rows (``revoked_at IS NULL``) in the
         token's workspace → :class:`DelegatingUserInactive`
         (cd-ljvs).
       * ``kind == 'personal'`` and the subject user holds zero
         live ``role_grant`` rows across **every** workspace →
         :class:`SubjectUserInactive` (cd-ljvs).

       The HTTP layer maps all four to 401 with a typed error code
       (``delegating_user_archived`` / ``subject_user_archived`` /
       ``delegating_user_inactive`` / ``subject_user_inactive``) so
       the agent gets a clear signal that re-minting won't help —
       the human owner / subject needs to be reinstated or
       re-granted. Run AFTER the secret verification so a probe
       with a random secret still collapses to :class:`InvalidToken`
       (we don't leak "this user is archived / inactive" to a
       caller who never proved knowledge of the secret). Archive
       checks run BEFORE the inactivity checks so an archived user
       with no live grants surfaces as ``…_archived`` (the
       lower-level fact); reinstating clears the archive gate, and
       the verifier then re-evaluates liveness on the next request.
       The cd-ljvs half became enforceable once cd-x1xh added the
       ``role_grant.revoked_at`` soft-retire columns — before that,
       "no live grants" was indistinguishable from "never granted"
       at the SQL level.
    7. Debounced ``last_used_at`` bump — see module docstring.

    Caller's UoW owns the transaction; this function never commits.
    A successful verify returns the ``user_id``, ``workspace_id``,
    and ``scopes`` the caller needs to authorise the action; the
    middleware (cd-ika7) walks ``scopes`` at the action catalog
    seam to decide whether to admit the request.

    The return deliberately does **not** enforce a
    :class:`WorkspaceContext` match — the service layer returns the
    row's ``workspace_id`` and the caller asserts the route's
    workspace agrees with the token's (§03 "A scoped token used
    against the wrong workspace returns 404 workspace_out_of_scope"
    is enforced at the router seam). Keeping the match at the
    router keeps the domain service usable from contexts that don't
    yet have a tenancy middleware (CLI, worker).
    """
    resolved_now = now if now is not None else _now(clock)

    key_id, secret = _parse(token)

    # justification: api_token is identity-scoped; reuse of the
    # tenant-agnostic gate mirrors the other accessors.
    with tenant_agnostic():
        row = session.get(ApiToken, key_id)
    if row is None:
        raise InvalidToken(f"no token with key_id {key_id!r}")

    if row.revoked_at is not None:
        raise TokenRevoked(f"token {key_id!r} revoked at {row.revoked_at}")

    if row.expires_at is not None:
        expires_at = _normalise_expires_at(row.expires_at, resolved_now)
        if expires_at <= resolved_now:
            raise TokenExpired(f"token {key_id!r} expired at {expires_at}")

    previous_hash_cleared = _clear_expired_previous_hash(row, now=resolved_now)
    try:
        _HASHER.verify(row.hash, secret)
    except VerifyMismatchError as exc:
        previous_hash_matches = False
        previous_hash = row.previous_hash
        if previous_hash is not None and _previous_hash_is_live(row, now=resolved_now):
            try:
                _HASHER.verify(previous_hash, secret)
            except VerifyMismatchError:
                pass
            else:
                previous_hash_matches = True
        if not previous_hash_matches:
            if previous_hash_cleared:
                with tenant_agnostic():
                    session.flush()
            # Collapse into the opaque "not a real token" shape so
            # metrics / HTTP cannot tell a mangled secret apart from
            # an unknown ``key_id`` at the wire.
            raise InvalidToken(f"token {key_id!r} secret did not verify") from exc

    # Liveness gate (cd-et6y + cd-ljvs). Run AFTER the secret check
    # so a probe with a random secret still collapses to
    # :class:`InvalidToken` rather than leaking that "this token's
    # user is archived / inactive". ``scoped`` tokens do not consult
    # either FK — their authority is the explicit scope set on the
    # row, not a delegating user.
    kind = _narrow_kind(row.kind)
    if (
        kind == "delegated"
        and row.delegate_for_user_id is not None
        and _user_is_archived(session, user_id=row.delegate_for_user_id)
    ):
        raise DelegatingUserArchived(
            f"token {key_id!r} delegates for archived user {row.delegate_for_user_id!r}"
        )
    if (
        kind == "personal"
        and row.subject_user_id is not None
        and _user_is_archived(session, user_id=row.subject_user_id)
    ):
        raise SubjectUserArchived(
            f"token {key_id!r} subject user {row.subject_user_id!r} is archived"
        )

    # cd-ljvs: live-grant gate. Order is archive-first then
    # inactive-second so an archived user with no grants always
    # reports the archive-shape error (the lower-level fact);
    # reinstating clears the archive flag and the verifier then
    # re-evaluates liveness on the next request. The role-grant repo
    # is constructed inline because :func:`verify` takes a session,
    # not a repo seam — it is on the per-request hot path so the
    # cheapest probe (``EXISTS`` on the partial-unique index) is
    # what we want here.
    if (
        kind == "delegated"
        and row.delegate_for_user_id is not None
        and row.workspace_id is not None
    ):
        repo = SqlAlchemyRoleGrantRepository(session)
        if not repo.has_live_grants_in_workspace(
            workspace_id=row.workspace_id,
            user_id=row.delegate_for_user_id,
        ):
            raise DelegatingUserInactive(
                f"token {key_id!r} delegates for user "
                f"{row.delegate_for_user_id!r} who holds no live "
                f"grants in workspace {row.workspace_id!r}"
            )
    if kind == "personal" and row.subject_user_id is not None:
        repo = SqlAlchemyRoleGrantRepository(session)
        if not repo.has_live_grants_anywhere(user_id=row.subject_user_id):
            raise SubjectUserInactive(
                f"token {key_id!r} subject user "
                f"{row.subject_user_id!r} holds no live grants in any workspace"
            )

    # Debounced best-effort update. We don't write to audit for this
    # — the /tokens UI reads ``last_used_at`` directly from the row
    # and the audit trail already captures the high-value events
    # (mint + revoke).
    if _maybe_bump_last_used(row, now=resolved_now) or previous_hash_cleared:
        with tenant_agnostic():
            session.flush()

    return VerifiedToken(
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        scopes=dict(row.scope_json),
        key_id=row.id,
        kind=kind,
        delegate_for_user_id=row.delegate_for_user_id,
        subject_user_id=row.subject_user_id,
    )
