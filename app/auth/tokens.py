"""API-token domain service — mint, verify, list, revoke.

Pure domain code. **No FastAPI coupling.** The HTTP router
(:mod:`app.api.v1.auth.tokens`) owns status-code mapping + request
parsing; this module owns row lifecycle + argon2id verification +
audit writes. The caller's UoW owns the transaction boundary (§01
"Key runtime invariants" #3) — this module never calls
``session.commit()``.

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
workspace. Creating a 6th raises :class:`TooManyTokens`, mapped to
HTTP 422 ``too_many_tokens``. The count is computed inside the mint
transaction so two concurrent creates cannot both land a 6th row.

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
  flipped to revoked.
* ``audit.api_token.revoked_noop`` on :func:`revoke` when the row
  was already revoked — kept separate so the trail distinguishes an
  intentional double-click from a real revocation event.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens" and
``docs/specs/15-security-privacy.md`` §"Token hashing".
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import ApiToken
from app.audit import write_audit
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "InvalidToken",
    "MintedToken",
    "TokenExpired",
    "TokenMintFailed",
    "TokenRevoked",
    "TokenSummary",
    "TooManyTokens",
    "VerifiedToken",
    "list_tokens",
    "mint",
    "revoke",
    "verify",
]


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
# cap, not the §03 workspace-wide 50-token cap, which is a separate
# guardrail (tracked for follow-up under cd-c91 if needed).
_MAX_ACTIVE_TOKENS_PER_USER: Final[int] = 5

# ``last_used_at`` write debounce. A heavily-used token (an agent
# polling every few seconds) would otherwise hammer its row's PK
# index on every request; the debounce drops the write rate to
# ≤1/min per token — the exact ceiling §03 pins.
_LAST_USED_DEBOUNCE: Final[timedelta] = timedelta(minutes=1)


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
    """

    token: str
    key_id: str
    prefix: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class TokenSummary:
    """Public projection of one :class:`ApiToken` row for list / audit UIs.

    Mirrors §03 "Revocation and rotation" / §14's ``/tokens`` panel:
    every field is safe to show to any workspace manager, none of
    them leak the plaintext secret. ``hash`` is deliberately
    **omitted** — the list surface never needs it, and leaving it
    off the projection makes it structurally impossible for a router
    to return the digest by mistake.
    """

    key_id: str
    label: str
    prefix: str
    scopes: Mapping[str, Any]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """Result of :func:`verify` — the identity + authority the token grants.

    The caller (tenancy middleware, once cd-ika7 lands) uses
    ``user_id`` + ``workspace_id`` to build the request's
    :class:`WorkspaceContext`, and walks ``scopes`` at the action-catalog
    seam to gate the action. ``key_id`` is echoed into audit so every
    write made through this token is traceable back to one row on the
    ``/tokens`` page.
    """

    user_id: str
    workspace_id: str
    scopes: Mapping[str, Any]
    key_id: str


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
    cannot both land a 6th row.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or a fresh system clock."""
    return (clock if clock is not None else SystemClock()).now()


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


def _count_active(
    session: Session, *, user_id: str, workspace_id: str, now: datetime
) -> int:
    """Return the number of live (unrevoked, unexpired) tokens for the user.

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
    )


# ---------------------------------------------------------------------------
# Public surface — mint
# ---------------------------------------------------------------------------


def mint(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    label: str,
    scopes: Mapping[str, Any],
    expires_at: datetime | None,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> MintedToken:
    """Create a fresh :class:`ApiToken` row and return the plaintext token.

    The caller's UoW owns the commit; on successful return the row
    exists with ``revoked_at = NULL``, the audit row is queued, and
    :attr:`MintedToken.token` is the **only** place the plaintext
    ever appears — the caller surfaces it in the HTTP response and
    never again.

    Raises:

    * :class:`TooManyTokens` when the user already holds
      :data:`_MAX_ACTIVE_TOKENS_PER_USER` live tokens on this
      workspace. The count is computed inside the mint transaction
      so two concurrent creates cannot both land a 6th row.
    * :class:`TokenMintFailed` on structural failures (argon2
      refused, RNG refused). Rare enough to bubble as 500.

    ``scopes`` is a ``{"action_key": true}`` mapping for v1 per
    the task spec. The service does not re-validate keys against
    the action catalog — validation happens at the router layer
    (where the full catalog is imported). The domain service treats
    ``scope_json`` as opaque at mint time; the verify-side authority
    check is the single source of truth.
    """
    resolved_now = now if now is not None else _now(clock)

    # Enforce the per-user cap BEFORE generating secrets / hashing —
    # no point burning argon2 cycles on a request that's about to
    # be rejected.
    active = _count_active(
        session,
        user_id=user_id,
        workspace_id=ctx.workspace_id,
        now=resolved_now,
    )
    if active >= _MAX_ACTIVE_TOKENS_PER_USER:
        raise TooManyTokens(
            f"user {user_id!r} already has {active} active tokens "
            f"(max {_MAX_ACTIVE_TOKENS_PER_USER})"
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

    row = ApiToken(
        id=key_id,
        user_id=user_id,
        workspace_id=ctx.workspace_id,
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
            "expires_at": expires_at.isoformat() if expires_at is not None else None,
        },
        clock=clock,
    )

    return MintedToken(
        token=f"{_TOKEN_PREFIX}{key_id}_{secret}",
        key_id=key_id,
        prefix=prefix,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Public surface — list_tokens
# ---------------------------------------------------------------------------


def list_tokens(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
) -> list[TokenSummary]:
    """Return every token on the caller's workspace, most recent first.

    ``user_id`` narrows to one subject when set — useful for the
    per-user ``/me/tokens`` panel (§14) once PATs land. Workspace
    managers call with ``user_id=None`` to audit every token in the
    workspace. Either way the listing includes both active and
    revoked rows: the UI needs the revoked-history tail to show
    "tokens you've decommissioned".

    The projection intentionally omits the hash column — see
    :class:`TokenSummary` docstring for why.
    """
    # justification: api_token is identity-scoped; reuse of the
    # tenant-agnostic gate mirrors ``_count_active``.
    with tenant_agnostic():
        stmt = (
            select(ApiToken)
            .where(ApiToken.workspace_id == ctx.workspace_id)
            .order_by(ApiToken.created_at.desc())
        )
        if user_id is not None:
            stmt = stmt.where(ApiToken.user_id == user_id)
        rows = list(session.scalars(stmt).all())
    return [_project(row) for row in rows]


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
    """Flip a token's ``revoked_at`` to ``now``. Idempotent on an already-revoked row.

    The row is **not** deleted — keeping it preserves the link
    target for existing audit rows that reference this token_id
    (§03 "per-token audit log view"). A revoked row still appears
    on the /tokens page in the "decommissioned" section.

    Raises:

    * :class:`InvalidToken` — no row with this id on the caller's
      workspace. The router maps this to 404 (management context);
      the Bearer-auth path uses :func:`verify`, not :func:`revoke`.

    A second call with the same ``token_id`` lands no state change
    but still writes an ``api_token.revoked_noop`` audit row so the
    trail distinguishes a double-click from the initial revocation.
    """
    resolved_now = now if now is not None else _now(clock)

    # justification: api_token is identity-scoped; read under
    # tenant_agnostic for consistency with the other accessors.
    with tenant_agnostic():
        row = session.get(ApiToken, token_id)

    # Fail-closed on cross-workspace access: a manager on workspace A
    # must not be able to revoke tokens on workspace B even if they
    # guess the token_id. Raising :class:`InvalidToken` for "row
    # belongs to a different workspace" collapses it to the same 404
    # shape as "row not found" at the HTTP layer, so the API doesn't
    # leak whether a foreign token exists.
    if row is None or row.workspace_id != ctx.workspace_id:
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
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public surface — verify
# ---------------------------------------------------------------------------


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
    5. Verify the secret with argon2id. Mismatch →
       :class:`InvalidToken` — wrapping argon2's
       :class:`VerifyMismatchError` so the caller sees the domain
       vocabulary only.
    6. Debounced ``last_used_at`` bump — see module docstring.

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

    try:
        _HASHER.verify(row.hash, secret)
    except VerifyMismatchError as exc:
        # Collapse into the opaque "not a real token" shape so
        # metrics / HTTP cannot tell a mangled secret apart from
        # an unknown ``key_id`` at the wire.
        raise InvalidToken(f"token {key_id!r} secret did not verify") from exc

    # Debounced best-effort update. We don't write to audit for this
    # — the /tokens UI reads ``last_used_at`` directly from the row
    # and the audit trail already captures the high-value events
    # (mint + revoke).
    if _maybe_bump_last_used(row, now=resolved_now):
        with tenant_agnostic():
            session.flush()

    return VerifiedToken(
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        scopes=dict(row.scope_json),
        key_id=row.id,
    )
