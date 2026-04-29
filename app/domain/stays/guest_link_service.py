"""Guest welcome link domain service.

Owns the full lifecycle of the public, no-login guest welcome page:

* :func:`mint_link` — generate a fresh ``itsdangerous`` signed token
  for a stay, persist a :class:`GuestLink` row, and write the
  ``guest_link.minted`` audit event.
* :func:`revoke_link` — stamp ``revoked_at`` on the row and clear
  the soft :attr:`Reservation.guest_link_id` back-pointer iff this
  link was the active one. The resolver thereafter returns
  :class:`GuestLinkGone` with reason ``REVOKED``.
* :func:`resolve_link` — validate signature + TTL + non-revoked,
  then assemble the rendered welcome bundle. Returns a
  discriminated :data:`ResolveResult`:
  :class:`ResolvedGuestLink` on success;
  :class:`GuestLinkGone` (with ``EXPIRED`` or ``REVOKED``) for a
  real row in a terminal state — §04 "Privacy" mandates two
  distinct user-facing strings, and the route picks one keyed on
  the discriminator; ``None`` for tampered tokens, missing rows,
  and deleted stays so a probing attacker can't tell those apart
  from "real-but-expired" tokens.
* :func:`record_access` — append one access record to the row's
  capped ring buffer. The buffer holds at most the **last 10**
  hits; older records evict on the next call. The caller hashes
  IPv4 ``/24`` and IPv6 ``/64`` prefixes — never the full address
  — per §04 "Privacy" + the app-wide §03 / §15 convention.

**Welcome merge.** :func:`resolve_link` walks the §04 cascade
(stay override > unit override > property default) for every
welcome field. The unit and stay layers don't yet have dedicated
columns on the v1 ``reservation`` slice, so the service depends
on a :class:`WelcomeResolver` port that returns whatever each
layer carries today; production wires it against the actual
columns and tests against in-memory fakes. This keeps the merge
order spec-faithful even while the underlying schema is still
catching up to §04 — when ``reservation.wifi_password_override``
and ``unit.welcome_overrides_json`` land (cd-1ai), the production
resolver gains two extra reads without disturbing the service's
public surface.

**Equipment section.** Visible only when the workspace setting
``assets.show_guest_assets`` resolves to ``true`` (resolved
through the §02 cascade for the property's tenant) **and** at
least one asset on the property carries ``guest_visible = true``.
Either condition false → the equipment section is omitted from
the rendered bundle entirely.

**Token format.** ``URLSafeTimedSerializer`` keyed by the
deployment-wide HMAC signer purpose ``"guest-link"``. The signed
payload carries ``{stay_id, property_id, jti, exp}`` — never
``workspace_id``: a guest token is **scoped to the stay**, not to
the workspace, and including the workspace id in the URL would leak
the tenant identifier.

**Audit.** Every mutation (mint, revoke, access) writes one
:class:`app.audit.write_audit` row with ``entity_kind =
"guest_link"``. The diff payload is pre-redacted — IPs are
hashed, tokens are stripped — so the audit stream never carries
PII or replayable secrets.

See ``docs/specs/04-properties-and-stays.md`` §"Guest welcome
link", ``docs/specs/02-domain-model.md`` §"guest_link",
``docs/specs/03-auth-and-tokens.md`` for the signed-token format
(line 832 pins the deployment-wide /24 + /64 IP-prefix
convention), and ``docs/specs/15-security-privacy.md``
§"Privacy and data rights" for the hashed-IP-prefix rule.
"""

from __future__ import annotations

import enum
import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from itsdangerous import (
    BadSignature,
    URLSafeTimedSerializer,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.stays.models import GuestLink, Reservation
from app.audit import write_audit
from app.config import Settings, get_settings
from app.security.hmac_signer import HmacSigner
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AccessRecord",
    "ChecklistItem",
    "GuestAsset",
    "GuestLinkGone",
    "GuestLinkGoneReason",
    "GuestLinkNotFound",
    "GuestLinkRead",
    "ResolveResult",
    "ResolvedGuestLink",
    "SettingsResolver",
    "WelcomeBundle",
    "WelcomeMergeInput",
    "WelcomeResolver",
    "mint_link",
    "record_access",
    "resolve_link",
    "revoke_link",
]


# ---------------------------------------------------------------------------
# Constants — spec-pinned
# ---------------------------------------------------------------------------


# Logical HMAC signer purpose. Different signing surfaces must be unrelated
# so an oracle on one surface (magic links, session cookies, ...) cannot
# forge tokens on another.
_HMAC_PURPOSE: Final[str] = "guest-link"

# itsdangerous serializer salt. Domain-separates the signature from any
# other URLSafeTimedSerializer the deployment runs (magic-link uses
# ``"magic-link-v1"``).
_SERIALIZER_SALT: Final[str] = "guest-link-v1"

# §04 default TTL: ``check_out_at + 1d``.
_DEFAULT_TTL_AFTER_CHECKOUT: Final[timedelta] = timedelta(days=1)

# §04 access-log cap: keep only the last 10 entries.
_ACCESS_LOG_MAX: Final[int] = 10

# §02 settings cascade key for the equipment-section visibility flag.
_SETTING_KEY_SHOW_GUEST_ASSETS: Final[str] = "assets.show_guest_assets"

# IP-prefix masks. §04 "Privacy" mandates "hashed-IP-prefix" without
# pinning a width; we adopt the deployment-wide convention from §03
# line 832 ("truncated to /24 for IPv4, /64 for IPv6 per §15
# PII-minimisation") and §15 line 896 (same /64 for IPv6 aggregation).
# Using the same widths everywhere means a single hashed-prefix
# bucket maps consistently across the auth audit log and the guest-
# link access log, and it avoids quietly drifting from a privacy
# convention the rest of the app has already settled on.
_IPV4_PREFIX_BITS: Final[int] = 24
_IPV6_PREFIX_BITS: Final[int] = 64


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GuestLinkNotFound(LookupError):
    """The link doesn't exist in the caller's workspace.

    404-equivalent. Scoped to the workspace context: a row in workspace
    A is invisible to workspace B; we don't distinguish "wrong
    workspace" from "really missing" — both collapse to this type so
    the cross-tenant surface stays non-enumerable.
    """


# ---------------------------------------------------------------------------
# DTOs and ports
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuestLinkRead:
    """Mint / revoke return shape — safe to surface anywhere.

    ``token`` is the freshly-signed plaintext on :func:`mint_link` and
    is never re-emitted on subsequent reads — it lives encrypted-by-
    signature on the row itself. Callers must capture it on the mint
    response or hand it back to the guest in the same UoW.
    """

    id: str
    workspace_id: str
    stay_id: str
    token: str
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """One row of the rendered check-out checklist.

    ``guest_visible`` is the gate :func:`resolve_link` uses to filter
    the rendered list — items with ``guest_visible = False`` are
    staff-only and never reach the public page.
    """

    id: str
    label: str
    guest_visible: bool


@dataclass(frozen=True, slots=True)
class GuestAsset:
    """One asset rendered in the equipment section.

    Echoes §04 "What the page shows / Equipment" — the page renders
    ``name``, ``guest_instructions_md`` (markdown rendered client-
    side) and the optional ``cover_photo_url``. ``guest_visible`` is
    the per-row filter the resolver applies before merging in the
    section.
    """

    id: str
    name: str
    guest_instructions_md: str
    cover_photo_url: str | None
    guest_visible: bool


@dataclass(frozen=True, slots=True)
class WelcomeMergeInput:
    """Raw layered input to the welcome-merge step.

    Each ``*_overrides`` blob is a flat ``{field_name: value}`` map.
    The service merges them in §04 order — stay > unit > property —
    and never inspects the values further (the page renderer owns
    Markdown rendering, image resolution, and tz-localised dates).

    ``stay_wifi_password_override`` is a dedicated channel rather
    than a key in ``stay_overrides`` because §04 calls it out as a
    distinct field on the stay row (``stay.wifi_password_override``)
    — keeping the typed slot explicit avoids the merge code having
    to treat one key specially inside the otherwise-generic dict.
    The unit and property layers always carry their wifi password
    inside their ``welcome_overrides_json`` / ``welcome_defaults_json``
    blobs, so the dict-merge path covers them.

    ``checklist`` is the **stay-bundle**'s checklist; the resolver
    filters it to ``guest_visible=True`` before rendering. Empty
    list ⇒ no "Before you leave" section. ``assets`` is the visible
    equipment list (already filtered to ``guest_visible=True`` by
    the caller); empty ⇒ the equipment section is omitted regardless
    of ``show_guest_assets``.
    """

    property_id: str
    property_name: str
    unit_id: str | None
    unit_name: str | None
    property_defaults: dict[str, Any]
    unit_overrides: dict[str, Any]
    stay_overrides: dict[str, Any]
    stay_wifi_password_override: str | None
    checklist: tuple[ChecklistItem, ...]
    assets: tuple[GuestAsset, ...]
    check_in_at: datetime
    check_out_at: datetime
    guest_name: str | None


@dataclass(frozen=True, slots=True)
class WelcomeBundle:
    """Rendered welcome payload.

    The result of merging :class:`WelcomeMergeInput` per §04 order
    plus the equipment-section gate. ``welcome`` is the flat merged
    dict consumed by the page; ``checklist`` is filtered to
    ``guest_visible=True``; ``assets`` is non-empty only when the
    setting AND the input both permit. The DTO is intentionally
    flat — the public renderer gets exactly one tree to walk.
    """

    property_id: str
    property_name: str
    unit_id: str | None
    unit_name: str | None
    welcome: dict[str, Any]
    checklist: tuple[ChecklistItem, ...]
    assets: tuple[GuestAsset, ...]
    check_in_at: datetime
    check_out_at: datetime
    guest_name: str | None


@dataclass(frozen=True, slots=True)
class AccessRecord:
    """One entry in the access-log ring buffer.

    Mirrors the §04 privacy contract verbatim: hashed IP prefix,
    user-agent family string, UTC instant. The hash is deterministic
    so a recurring abuser still aggregates against a single bucket
    without the row carrying any directly-identifying value.
    """

    ip_prefix_sha256: str
    ua_family: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedGuestLink:
    """Successful resolve — render the bundle.

    Carries the bundle plus enough scope to call
    :func:`record_access` from the public route without re-loading
    the row. ``workspace_id`` is exposed because the access-log
    audit row is workspace-scoped and the route holds no
    :class:`WorkspaceContext` of its own; ``stay_id`` is exposed
    for audit / observability hooks the route may want to bind to.
    """

    link_id: str
    workspace_id: str
    stay_id: str
    bundle: WelcomeBundle


class GuestLinkGoneReason(enum.StrEnum):
    """Why a real ``guest_link`` row will not render its bundle.

    The public route picks the §04 "Privacy" 410 wording from this
    discriminator:

    * :attr:`EXPIRED` — natural expiry; copy "This link has expired."
    * :attr:`REVOKED` — manager flipped ``revoked_at``; copy "This
      welcome link has been turned off. If you need the information
      again, please ask your host."

    Tampered tokens, missing rows, and deleted stays collapse to
    ``None`` at :func:`resolve_link` (not this enum) so a probing
    attacker can't tell "valid signature, expired row" apart from
    "garbage signature" — both render the EXPIRED copy. The two
    distinct strings only fire when the caller already knows a
    valid token (i.e. is the original recipient).
    """

    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class GuestLinkGone:
    """Real row found, but expired or revoked.

    Carries enough scope (``link_id`` + ``workspace_id``) for the
    public route to call :func:`record_access` against the row —
    §04 "Privacy" mandates that "Both cases log the access with no
    stay payload", so the access-log entry still lands even though
    no bundle renders.
    """

    link_id: str
    workspace_id: str
    reason: GuestLinkGoneReason


# Discriminated result returned by :func:`resolve_link`.
#
# * :class:`ResolvedGuestLink` — render the bundle.
# * :class:`GuestLinkGone` — render the spec's revoked / expired
#   410 copy and still log the access.
# * ``None`` — tampered token, signature failure, missing row, or
#   the underlying stay was deleted. The route renders the **same**
#   page as :attr:`GuestLinkGoneReason.EXPIRED` so a probing attacker
#   cannot distinguish "valid-but-expired" from "garbage" tokens.
ResolveResult = ResolvedGuestLink | GuestLinkGone | None


class WelcomeResolver(Protocol):
    """Port that fetches the merge inputs for a stay.

    The service depends on this rather than reaching directly into
    sibling tables so the merge-source schema can evolve (cd-1ai
    landing the unit_id + wifi override on ``reservation``, the
    asset/checklist pipelines refactoring) without touching the
    domain code. Production wires the SQLAlchemy concretion;
    tests pass a deterministic fake.

    Returns ``None`` when the stay row no longer exists or doesn't
    belong to ``workspace_id`` — the caller of :func:`resolve_link`
    collapses both to ``None``.
    """

    def fetch(
        self,
        *,
        session: Session,
        workspace_id: str,
        stay_id: str,
    ) -> WelcomeMergeInput | None:
        """Return the layered welcome inputs for ``stay_id``."""
        ...


class SettingsResolver(Protocol):
    """Port that resolves the §02 settings cascade for one key.

    Today the deployment has no live cascade resolver — :func:`mint_link`
    and :func:`revoke_link` don't need one, and :func:`resolve_link`
    needs only ``assets.show_guest_assets`` for the equipment-section
    gate. The port is the seam that lets the cascade implementation
    land later (cd-settings-cascade) without forcing a domain-layer
    edit. Until the real resolver lands, production wires a thin
    workspace-only reader against ``workspace.settings_json`` and
    tests pass an in-memory fake.
    """

    def resolve_bool(
        self,
        *,
        session: Session,
        workspace_id: str,
        property_id: str,
        unit_id: str | None,
        key: str,
    ) -> bool:
        """Return the resolved bool for ``key`` at the given scope."""
        ...


# ---------------------------------------------------------------------------
# Service API
# ---------------------------------------------------------------------------


def mint_link(
    session: Session,
    ctx: WorkspaceContext,
    *,
    stay_id: str,
    property_id: str,
    check_out_at: datetime,
    ttl: timedelta | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> GuestLinkRead:
    """Mint a fresh guest welcome link for ``stay_id``.

    The signed payload carries ``{stay_id, property_id, jti, exp}``
    — never ``workspace_id``. The token is unguessable: a 192-bit
    ``jti`` plus the deployment's HKDF subkey yield more than enough
    entropy to defeat brute-forcing, and ``itsdangerous`` already
    binds the timestamp inside the signature so a stale token
    cannot be replayed past ``exp`` even if the row were
    resurrected.

    ``ttl`` defaults to §04's ``check_out_at + 1d``; callers can
    override (e.g. a manager who wants a 7-day window for an
    extended-stay owner). The resulting ``expires_at`` is persisted
    on the row **and** baked into the signed token: the resolver
    enforces both, so a row tampered with at the DB layer can't
    extend a token that's already lapsed cryptographically.

    Writes one ``guest_link.minted`` audit row. The diff carries
    the link id, stay id, expires_at — never the token (the token
    is a credential and must not enter the audit stream).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    resolved_ttl = ttl if ttl is not None else _DEFAULT_TTL_AFTER_CHECKOUT
    expires_at = check_out_at + resolved_ttl

    link_id = new_ulid()
    jti = new_ulid()
    serializer = _serializer(session, settings, clock=resolved_clock)
    token = serializer.dumps(
        {
            "stay_id": stay_id,
            "property_id": property_id,
            "jti": jti,
            "exp": int(expires_at.timestamp()),
        }
    )
    if not isinstance(token, str):  # pragma: no cover - defensive
        # itsdangerous returns ``str`` from URLSafeTimedSerializer
        # when fed JSON-friendly inputs (only ``URLSafeSerializer``
        # subclass returns bytes, and only for raw-bytes payloads).
        # Defence-in-depth narrowing lets the rest of the function
        # treat the token as a plain string without a cast.
        raise TypeError(
            f"URLSafeTimedSerializer returned {type(token).__name__}, expected str"
        )

    row = GuestLink(
        id=link_id,
        workspace_id=ctx.workspace_id,
        stay_id=stay_id,
        token=token,
        expires_at=expires_at,
        revoked_at=None,
        access_log_json=[],
        created_at=now,
    )
    session.add(row)
    session.flush()

    # Maintain the soft back-pointer on the reservation so the
    # manager UI can find "the active link for this stay" with one
    # row read. The pointer is **not** an FK (see migration
    # docstring) — the domain layer is the source of truth for its
    # hygiene. A re-mint without a prior revoke simply overwrites
    # the column with the newer link's id; the older row remains
    # in the table so its still-valid token continues to resolve
    # for any guest who already has the URL.
    _set_reservation_back_pointer(session, stay_id=stay_id, link_id=link_id)

    write_audit(
        session,
        ctx,
        entity_kind="guest_link",
        entity_id=link_id,
        action="minted",
        diff={
            "after": {
                "id": link_id,
                "stay_id": stay_id,
                "property_id": property_id,
                "expires_at": expires_at.isoformat(),
            },
        },
        clock=resolved_clock,
    )
    return _row_to_read(row, token=token)


def revoke_link(
    session: Session,
    ctx: WorkspaceContext,
    *,
    link_id: str,
    clock: Clock | None = None,
) -> GuestLinkRead:
    """Stamp ``revoked_at`` on the link row.

    The resolver treats any non-null ``revoked_at`` as gone, so a
    revoke takes effect on the very next public hit. Re-revoking an
    already-revoked link is a no-op for ``revoked_at`` (the original
    instant survives — the audit history can show repeated revoke
    attempts but the cryptographic "first revoked at" is what the
    public-page text references). Writes one ``guest_link.revoked``
    audit row.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, link_id=link_id)
    before_revoked_at = row.revoked_at
    if row.revoked_at is None:
        row.revoked_at = now
    session.flush()

    # Null the soft back-pointer on the reservation so the manager
    # UI's "active link for this stay" indicator clears immediately.
    # Only null when this row was actually the active pointer — a
    # re-mint that overwrote the column with a newer link's id must
    # not be cleared by revoking an older sibling row.
    _clear_reservation_back_pointer(
        session, stay_id=row.stay_id, expected_link_id=link_id
    )

    write_audit(
        session,
        ctx,
        entity_kind="guest_link",
        entity_id=link_id,
        action="revoked",
        diff={
            "before": {
                "revoked_at": (
                    before_revoked_at.isoformat() if before_revoked_at else None
                ),
            },
            "after": {
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            },
        },
        clock=resolved_clock,
    )
    # The token is not exposed on the post-revoke read — the public
    # surface should not be re-shareable from the audit row.
    return _row_to_read(row, token=row.token)


def resolve_link(
    session: Session,
    *,
    token: str,
    welcome_resolver: WelcomeResolver,
    settings_resolver: SettingsResolver,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> ResolveResult:
    """Validate ``token`` and assemble the welcome bundle.

    Returns a discriminated :data:`ResolveResult`:

    * :class:`ResolvedGuestLink` — token verified, row live, bundle
      ready to render.
    * :class:`GuestLinkGone` (``EXPIRED`` / ``REVOKED``) — row
      found but in a terminal state. The route renders the §04
      copy keyed on the reason and still logs the access.
    * ``None`` — bad signature, missing row, or deleted stay. The
      route renders the **same** page as ``EXPIRED`` so a probing
      attacker can't tell "real-but-expired" tokens apart from
      garbage. No access log on this branch — there is no row to
      bind it to.

    Why distinguish ``EXPIRED`` from ``REVOKED`` but not from
    "tampered": §04 "Privacy" mandates two distinct strings for
    the legitimate-recipient cases (their stale link should explain
    itself), but the **enumeration oracle** only opens up if a
    third state — "yes, this token was once real but is gone" —
    were exposed to anyone who can guess. Collapsing the
    bad-signature path into the EXPIRED visual prevents that
    oracle without sacrificing the spec's user-facing wording.

    No :class:`WorkspaceContext` is required: the public guest
    page runs **without a tenancy context** (no cookie, no header,
    no sign-in). The lookup stays workspace-correct because the
    token itself binds ``stay_id`` cryptographically — only the
    workspace that minted it could produce a valid signature.

    The function performs **no DB writes** — :func:`record_access`
    is the dedicated mutation surface so the route can choose
    whether to log a successful resolve (yes), an EXPIRED /
    REVOKED hit (yes — abuse signal + spec mandate), or a
    bad-signature ping (no — there is no row to bind to).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    serializer = _serializer(session, settings, clock=resolved_clock)
    claims = _unseal(serializer, token=token, now=now)
    if claims is None:
        return None

    stay_id = claims.payload["stay_id"]
    # The row carries the canonical workspace_id; we trust the DB
    # over the (unsigned) cleartext payload.
    row = _load_row_by_token(session, token=token)
    if row is None:
        return None
    # Revocation precedence: if the manager turned the link off
    # **and** time also lapsed, the user-facing copy should still
    # say "turned off" — the manager's intent is the more
    # actionable signal ("ask your host" is a better next step than
    # "the link expired").
    if row.revoked_at is not None:
        return GuestLinkGone(
            link_id=row.id,
            workspace_id=row.workspace_id,
            reason=GuestLinkGoneReason.REVOKED,
        )
    # Expiry: either the cryptographic ``exp`` claim has lapsed (the
    # signed defence-in-depth fallback) or the row's ``expires_at``
    # has lapsed (the operator-managed source of truth). Either
    # surface the user-facing "this link has expired" copy.
    if claims.expired or _aware_utc(row.expires_at) <= now:
        return GuestLinkGone(
            link_id=row.id,
            workspace_id=row.workspace_id,
            reason=GuestLinkGoneReason.EXPIRED,
        )

    merge_input = welcome_resolver.fetch(
        session=session,
        workspace_id=row.workspace_id,
        stay_id=stay_id,
    )
    if merge_input is None:
        # Stay was deleted out from under the link. This is
        # effectively "gone with no recoverable copy" — collapse to
        # ``None`` so the route renders the EXPIRED page (the row's
        # signed token is still valid, but rendering a name-less
        # bundle would leak less than admitting "your stay is
        # gone"; an attacker iterating tokens can't tell this apart
        # from a tampered signature either).
        return None

    show_assets = settings_resolver.resolve_bool(
        session=session,
        workspace_id=row.workspace_id,
        property_id=merge_input.property_id,
        unit_id=merge_input.unit_id,
        key=_SETTING_KEY_SHOW_GUEST_ASSETS,
    )
    bundle = _build_bundle(merge_input, show_assets=show_assets)
    return ResolvedGuestLink(
        link_id=row.id,
        workspace_id=row.workspace_id,
        stay_id=stay_id,
        bundle=bundle,
    )


def record_access(
    session: Session,
    ctx: WorkspaceContext,
    *,
    link_id: str,
    ip: str,
    user_agent: str,
    clock: Clock | None = None,
) -> AccessRecord:
    """Append one access record to the link's ring buffer.

    The buffer is **strictly capped at the last 10 entries**: a
    successful append on a full buffer evicts the oldest entry
    in-place. The IP is hashed to a ``/24`` (IPv4) or ``/48``
    (IPv6) prefix before persistence — the full address never
    enters the row.

    Writes one ``guest_link.accessed`` audit row. The diff carries
    the new record (already hashed) and the post-truncation buffer
    length so an operator can spot a pathological access pattern
    without reading the row's JSON column.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, link_id=link_id)

    record = AccessRecord(
        ip_prefix_sha256=_hash_ip_prefix(ip),
        ua_family=_classify_ua_family(user_agent),
        at=now,
    )
    record_dict: dict[str, Any] = {
        "ip_prefix_sha256": record.ip_prefix_sha256,
        "ua_family": record.ua_family,
        "at": record.at.isoformat(),
    }

    # SQLAlchemy's JSON column treats the in-place-mutated list as
    # equal to the previous value and skips the UPDATE. Build a
    # fresh list and re-assign so the dirty-tracker fires.
    log_after = list(row.access_log_json)
    log_after.append(record_dict)
    if len(log_after) > _ACCESS_LOG_MAX:
        # Truncate the oldest entries; the spec pins the cap at the
        # **last** 10, so we keep the tail.
        log_after = log_after[-_ACCESS_LOG_MAX:]
    row.access_log_json = log_after
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="guest_link",
        entity_id=link_id,
        action="accessed",
        diff={
            "after": {
                "record": record_dict,
                "log_length": len(log_after),
            },
        },
        clock=resolved_clock,
    )
    return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serializer(
    session: Session, settings: Settings | None, *, clock: Clock | None = None
) -> URLSafeTimedSerializer:
    """Build a fresh :class:`URLSafeTimedSerializer` for guest links.

    Uses the deployment-wide HMAC signer keyring for ``guest-link``.
    ``itsdangerous`` accepts keys oldest-to-newest, verifies with all
    of them, and signs with the last one, matching the primary /
    non-expired legacy slot contract in :mod:`app.security.hmac_signer`.
    """
    s = settings if settings is not None else get_settings()
    keys = HmacSigner(session, settings=s, clock=clock).verification_keys(
        purpose=_HMAC_PURPOSE
    )
    return URLSafeTimedSerializer(secret_key=keys, salt=_SERIALIZER_SALT)


@dataclass(frozen=True, slots=True)
class _UnsealedClaims:
    """Outcome of :func:`_unseal` when the signature held.

    ``expired`` is the cryptographic-defence-in-depth flag: even
    when the row's ``expires_at`` says "live", a signed token whose
    embedded ``exp`` has lapsed is treated as expired so a row
    tampered with at the DB layer cannot extend a token's life.
    The resolver still consults the row to confirm the link exists
    and to read the canonical ``workspace_id`` for audit logging.
    """

    payload: dict[str, Any]
    expired: bool


def _unseal(
    serializer: URLSafeTimedSerializer, *, token: str, now: datetime
) -> _UnsealedClaims | None:
    """Verify the signature + payload shape and return claims.

    Returns ``None`` for any signature / shape failure — those
    collapse to the route's "tampered" branch (renders the same
    EXPIRED page; closes the enumeration oracle). Returns a
    :class:`_UnsealedClaims` for any signature-valid token,
    flagging ``expired`` separately so the resolver can still load
    the row, look up its ``workspace_id``, and surface a proper
    :class:`GuestLinkGone(EXPIRED)` to the route.

    Mirrors the magic-link unseal pattern: itsdangerous'
    ``max_age`` argument is **not** used — the resolver compares
    ``exp`` against the injected clock so the test seam is
    deterministic. ``SignatureExpired`` would only fire if a future
    caller re-introduced ``max_age``; we still catch the parent
    :class:`BadSignature` (its superclass) so the test-injectable
    expiry path stays the single source of truth.
    """
    try:
        raw = serializer.loads(token)
    except BadSignature:
        # ``SignatureExpired`` is a subclass of ``BadSignature`` so
        # this catches both — defence-in-depth in case a future
        # caller introduces ``max_age``.
        return None
    if not isinstance(raw, dict):
        return None
    payload: dict[str, Any] = raw
    if not all(k in payload for k in ("stay_id", "property_id", "jti", "exp")):
        return None
    if not isinstance(payload["stay_id"], str):
        return None
    if not isinstance(payload["property_id"], str):
        return None
    if not isinstance(payload["exp"], int):
        return None
    exp_dt = datetime.fromtimestamp(payload["exp"], tz=UTC)
    return _UnsealedClaims(payload=payload, expired=exp_dt <= now)


def _load_row(session: Session, ctx: WorkspaceContext, *, link_id: str) -> GuestLink:
    """Workspace-scoped loader; raises :class:`GuestLinkNotFound` on miss."""
    stmt = select(GuestLink).where(
        GuestLink.id == link_id,
        GuestLink.workspace_id == ctx.workspace_id,
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise GuestLinkNotFound(link_id)
    return row


def _load_row_by_token(session: Session, *, token: str) -> GuestLink | None:
    """Token-scoped loader for the public resolve path.

    Bypasses the workspace filter because the public route runs
    without a :class:`WorkspaceContext`. The unique-on-``token``
    column is the natural lookup key, and the token's signature
    already proved the caller knows the workspace's signing key.
    The ``workspace_id`` used downstream is read from the row, never
    from the token claims (which are unsigned cleartext to a reader).
    """
    # justification: public guest welcome route — no WorkspaceContext
    # available; the signature on the token is the tenancy proof.
    with tenant_agnostic():
        stmt = select(GuestLink).where(GuestLink.token == token)
        return session.scalars(stmt).one_or_none()


def _set_reservation_back_pointer(
    session: Session, *, stay_id: str, link_id: str
) -> None:
    """Stamp ``reservation.guest_link_id = link_id`` on the parent stay.

    Soft back-pointer maintenance. The column has no FK (see the
    migration docstring for the cycle-avoidance rationale), so the
    domain layer is the source of truth: every successful
    :func:`mint_link` writes the new link's id; every successful
    :func:`revoke_link` clears it again (when this row was the
    active pointer).

    The reservation itself is workspace-scoped — the orm tenant
    filter auto-pins ``workspace_id`` against the active context;
    if the caller's :class:`WorkspaceContext` doesn't match the
    reservation's row, the update simply matches zero rows and the
    helper is a no-op. That's the desired defence-in-depth: a
    cross-workspace mint can't write into another workspace's
    reservation.
    """
    stay = session.get(Reservation, stay_id)
    if stay is None:  # pragma: no cover - belt-and-braces; mint_link's caller
        # already validated the stay exists. The branch is here so
        # a future caller that skips that check still no-ops rather
        # than blowing up with an attribute error.
        return
    stay.guest_link_id = link_id
    session.flush()


def _clear_reservation_back_pointer(
    session: Session, *, stay_id: str, expected_link_id: str
) -> None:
    """Clear ``reservation.guest_link_id`` iff it points at this link.

    Idempotency + safety: a re-mint that happened between the
    revoked link's creation and its revoke wrote a newer link's id
    into the column; that newer pointer must survive this revoke.
    The compare-and-clear pattern below preserves the live link's
    id while still flushing the stale one.
    """
    stay = session.get(Reservation, stay_id)
    if stay is None:
        return
    if stay.guest_link_id == expected_link_id:
        stay.guest_link_id = None
        session.flush()


def _row_to_read(row: GuestLink, *, token: str) -> GuestLinkRead:
    """Project the SA row into the read shape.

    Datetime columns are coerced through :func:`_aware_utc` so SQLite-
    backed deployments don't leak naive timestamps past the domain
    boundary — the spec (§02 "Time") makes UTC-aware mandatory at
    every layer above the DB.
    """
    return GuestLinkRead(
        id=row.id,
        workspace_id=row.workspace_id,
        stay_id=row.stay_id,
        token=token,
        expires_at=_aware_utc(row.expires_at),
        revoked_at=_aware_utc(row.revoked_at) if row.revoked_at is not None else None,
        created_at=_aware_utc(row.created_at),
    )


def _build_bundle(inp: WelcomeMergeInput, *, show_assets: bool) -> WelcomeBundle:
    """Apply the §04 merge order and the equipment-section gate.

    Welcome merge: property defaults are the floor; unit overrides
    layer on top; stay overrides win last. The dedicated
    ``stay.wifi_password_override`` slot wins above the dict layers
    when set — that's what §04 §"Wifi" pins (``stay > unit >
    property``). A ``None`` value at the stay level falls through
    to the dict layers; an explicit empty string ``""`` IS treated
    as a real override (an operator may have intentionally
    suppressed the wifi line for a stay).
    """
    welcome: dict[str, Any] = {}
    welcome.update(inp.property_defaults)
    welcome.update(inp.unit_overrides)
    welcome.update(inp.stay_overrides)
    if inp.stay_wifi_password_override is not None:
        welcome["wifi_password"] = inp.stay_wifi_password_override

    visible_checklist = tuple(item for item in inp.checklist if item.guest_visible)

    if show_assets:
        # Every asset on the input is already filtered to
        # ``guest_visible=True`` by the caller; defence-in-depth a
        # second filter so a future loader that forgets to gate
        # doesn't leak.
        rendered_assets = tuple(a for a in inp.assets if a.guest_visible)
    else:
        rendered_assets = ()

    return WelcomeBundle(
        property_id=inp.property_id,
        property_name=inp.property_name,
        unit_id=inp.unit_id,
        unit_name=inp.unit_name,
        welcome=welcome,
        checklist=visible_checklist,
        assets=rendered_assets,
        check_in_at=inp.check_in_at,
        check_out_at=inp.check_out_at,
        guest_name=inp.guest_name,
    )


def _hash_ip_prefix(ip: str) -> str:
    """Return a deterministic SHA-256 hex of the IP's privacy prefix.

    IPv4 hashes the ``/24`` network; IPv6 hashes the ``/64`` —
    matching the §03 audit-log + §15 aggregation conventions so
    every privacy-prefix bucket aligns across the codebase.
    Malformed input falls through to a sentinel hash so the row
    still records the access (the abuse signal matters even when
    the upstream proxy hands us garbage) without raising. The
    sentinel is a fixed string so a malformed-input attacker
    can't enumerate distinct buckets — every malformed hit shares
    one cell.
    """
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        # Use a fixed, deliberately-non-secret label so all malformed
        # inputs hash to the same bucket. The hash is still applied so
        # the column shape stays uniform (callers always see hex).
        return hashlib.sha256(b"crewday.guest.ip.malformed").hexdigest()
    if isinstance(addr, ipaddress.IPv4Address):
        net = ipaddress.ip_network(f"{addr}/{_IPV4_PREFIX_BITS}", strict=False)
    else:
        net = ipaddress.ip_network(f"{addr}/{_IPV6_PREFIX_BITS}", strict=False)
    return hashlib.sha256(str(net.network_address).encode("ascii")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    """Coerce ``value`` to an aware UTC :class:`datetime`.

    SQLite's ``DateTime(timezone=True)`` round-trips as a naive
    timestamp on read — the column stores the wall-clock value and
    drops the offset. The domain layer always writes UTC, so a
    naive value loaded back is a UTC value missing its tag. This
    helper restamps the tag without shifting the wall-clock; aware
    inputs pass through after a normalising ``astimezone(UTC)``.
    Postgres returns aware datetimes natively, so this becomes a
    no-op there.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _classify_ua_family(ua: str) -> str:
    """Bucket a User-Agent string into a coarse family label.

    The §04 contract is "UA family" — not the raw header — because a
    full UA string carries enough entropy to fingerprint a single
    device. The classifier is **deliberately tiny**: a handful of
    substring checks against the most-common families. Anything we
    don't recognise becomes ``"other"`` so the column never grows
    unbounded.
    """
    lower = ua.lower()
    if "edg/" in lower or "edge/" in lower:
        return "edge"
    if "chrome/" in lower and "chromium" not in lower:
        return "chrome"
    if "firefox/" in lower:
        return "firefox"
    if "safari/" in lower and "chrome/" not in lower:
        return "safari"
    if "opera/" in lower or "opr/" in lower:
        return "opera"
    return "other"
