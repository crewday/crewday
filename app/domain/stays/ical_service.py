"""``ical_feed`` registration + probe + lifecycle service.

The polling worker is a separate concern (cd-d48). This service owns
everything up to the point the poller would run:

* **Register** a feed against a property. Validates the URL through
  the :class:`~app.adapters.ical.ports.IcalValidator` port, auto-
  detects the provider, envelope-encrypts the URL, and inserts the
  row with ``enabled=False``. If the probe comes back as a
  parseable VCALENDAR we flip ``enabled=True`` in the same
  transaction.
* **Probe** an existing feed. Re-runs validation + fetch; updates
  ``last_polled_at`` and ``last_error`` (the §04 ``ical_url_*``
  error code on failure, cleared on success). Flips ``enabled=True``
  on the first successful probe.
* **Update** an existing feed's URL and/or provider. Swapping the URL
  re-runs the full validate-encrypt-probe path; swapping just the
  provider override skips the probe.
* **Disable / delete** — ``disable_feed`` clears ``enabled`` but
  keeps the row; ``delete_feed`` drops it outright. §04 does not
  (yet) carry a soft-delete column on ``ical_feed``, so "delete"
  is a hard delete — the reservations the feed seeded survive via
  the ``ical_feed_id`` SET NULL cascade.
* **List** — returns a DTO that **never** includes the plaintext
  URL, only a host-prefix preview so the manager UI can render
  "Airbnb feed for ``xxxx.airbnb.com``" without round-tripping the
  secret through HTTP.

**Audit.** Every mutation writes one row via :func:`app.audit.write_audit`.
The URL is redacted to host-only in the audit diff — §15 forbids
plaintext secrets in the audit stream, and the envelope-encrypted
ciphertext would be noise.

**Provider taxonomy.** The auto-detect and override DTO share the
§04 :data:`~app.adapters.ical.ports.IcalProvider` alphabet
(``airbnb | vrbo | booking | gcal | generic``); the DB CHECK admits
every slug in that set plus the legacy ``custom`` spelling for
v1-era rows (cd-ewd7 widened the CHECK and dropped the
``gcal/generic → custom`` collapse — the detector's result now
lands verbatim).

**Port wiring.** The service takes an
:class:`~app.adapters.ical.ports.IcalValidator`, a
:class:`~app.adapters.ical.ports.ProviderDetector`, and an
:class:`~app.adapters.storage.envelope.EnvelopeEncryptor` by DI on
each call. Production wires the concrete adapters; tests pass stubs.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed",
``docs/specs/02-domain-model.md`` §"ical_feed",
``docs/specs/15-security-privacy.md`` §"Secret envelope" / §"SSRF".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.stays.models import DEFAULT_POLL_CADENCE, IcalFeed
from app.adapters.ical.ports import (
    IcalProvider,
    IcalValidation,
    IcalValidationError,
    IcalValidator,
    ProviderDetector,
)
from app.adapters.storage.ports import EnvelopeEncryptor, EnvelopeOwner
from app.audit import write_audit
from app.domain.settings.cascade import SettingScopeChain, resolve_most_specific
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "IcalFeedCreate",
    "IcalFeedNotFound",
    "IcalFeedUpdate",
    "IcalFeedView",
    "IcalProbeResult",
    "IcalProviderOverride",
    "IcalUrlInvalid",
    "delete_feed",
    "disable_feed",
    "get_plaintext_url",
    "list_feeds",
    "probe_feed",
    "register_feed",
    "resolve_allow_self_signed",
    "update_feed",
]


# §04 SSRF carve-out (cd-t2qtg) — workspace + property setting key.
_ALLOW_SELF_SIGNED_KEY = "ical.allow_self_signed"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The HKDF ``purpose`` label the envelope helper uses. Different
# callers (property wifi password, workspace SMTP secret, ...) pick
# different purposes so their ciphertexts can't decrypt each other's
# plaintext. Locked at registration time — a purpose change would
# invalidate every persisted URL.
_URL_PURPOSE = "ical-feed-url"
_MAX_URL_LEN = 2048

# The DB CHECK admits every :data:`IcalProvider` slug (cd-ewd7
# widened it) plus the legacy ``custom`` spelling for v1-era rows.
# The frozenset powers :func:`_narrow_loaded_provider`, which loud-
# fails on an out-of-alphabet value loaded from disk.
_LOADED_PROVIDERS: frozenset[str] = frozenset(get_args(IcalProvider)) | {"custom"}


# Provider override accepted at the service boundary — callers pass a
# §04 slug, the service stores it verbatim.
IcalProviderOverride = IcalProvider


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IcalFeedNotFound(LookupError):
    """The feed doesn't exist in the caller's workspace.

    404-equivalent. Mirrors :class:`app.domain.places.property_service.
    PropertyNotFound`: a feed linked only to workspace A is invisible
    to workspace B; we don't distinguish "wrong workspace" from
    "really missing".
    """


class IcalUrlInvalid(ValueError):
    """URL validation failed.

    422-equivalent. Carries the §04 error ``code`` and the underlying
    message so the router can render a structured response. The
    caller is responsible for ensuring the message doesn't contain
    the URL itself when surfaced into audit / logs (the domain
    service strips to host-only before persisting).
    """

    __slots__ = ("code",)

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class IcalFeedCreate(BaseModel):
    """Body for :func:`register_feed`.

    ``provider_override`` is optional — when ``None`` the service
    auto-detects from the URL host. ``unit_id`` is also optional —
    when ``None`` the feed is property-scoped and stays land at
    the property level until the manager maps a unit. ``poll_cadence``
    defaults to the §04 ``*/15 * * * *`` baseline when omitted.
    The DTO caps the URL length to ``_MAX_URL_LEN`` so a pathological
    caller can't push multi-megabyte strings through the envelope
    path.
    """

    model_config = ConfigDict(extra="forbid")

    property_id: str = Field(..., min_length=1, max_length=64)
    unit_id: str | None = Field(default=None, min_length=1, max_length=64)
    url: str = Field(..., min_length=10, max_length=_MAX_URL_LEN)
    provider_override: IcalProviderOverride | None = None
    poll_cadence: str | None = Field(default=None, min_length=1, max_length=128)


class IcalFeedUpdate(BaseModel):
    """Body for :func:`update_feed`.

    All fields optional — the service diffs against the stored row
    and only re-runs validation / probe on URL changes. Swapping
    just the provider override is a cheap metadata flip that does
    not re-hit the network. Per-unit remapping and cadence tweaks
    go through a follow-up DTO once §04's field-by-field PATCH
    surface lands — today's update only accepts ``url`` and
    ``provider_override``.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, min_length=10, max_length=_MAX_URL_LEN)
    provider_override: IcalProviderOverride | None = None


@dataclass(frozen=True, slots=True)
class IcalFeedView:
    """Read projection — safe to return to any caller.

    ``url_preview`` is the public, non-secret form: scheme + host,
    no path or query (both of which frequently carry the provider's
    secret token). ``url_plaintext`` is deliberately **not** on
    this DTO — the only legal way to reach the plaintext is through
    :func:`get_plaintext_url`, which is the poller's entry point.

    ``provider`` is typed as ``str`` rather than the richer
    :data:`IcalProvider` literal because v1-era rows may still carry
    the legacy ``custom`` slug; callers that want a narrowed literal
    should go through :func:`_narrow_loaded_provider` first.
    ``unit_id`` carries the §04 per-unit feed mapping (NULL when
    the feed is property-scoped). ``poll_cadence`` is the per-feed
    cron the poller (cd-d48) honours.
    """

    id: str
    workspace_id: str
    property_id: str
    unit_id: str | None
    provider: str
    provider_override: IcalProviderOverride | None
    url_preview: str
    enabled: bool
    poll_cadence: str
    last_polled_at: datetime | None
    last_etag: str | None
    last_error: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IcalProbeResult:
    """Outcome of a :func:`probe_feed` call.

    ``parseable_ics`` is the gate the service uses to flip
    ``enabled=True`` on the first successful probe. ``error_code``
    is the §04 vocabulary (``ical_url_*``); populated only when
    the probe failed.
    """

    feed_id: str
    ok: bool
    parseable_ics: bool
    error_code: str | None
    polled_at: datetime


# ---------------------------------------------------------------------------
# Service API
# ---------------------------------------------------------------------------


def register_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: IcalFeedCreate,
    validator: IcalValidator,
    detector: ProviderDetector,
    envelope: EnvelopeEncryptor,
    clock: Clock | None = None,
) -> IcalFeedView:
    """Register a new feed for ``property_id``.

    Pipeline:

    1. Validate the URL via the SSRF-guarded :class:`IcalValidator`.
    2. Auto-detect the provider unless ``body.provider_override`` is
       set.
    3. Encrypt the canonicalised URL via the envelope port.
    4. Insert the row with ``enabled`` mirroring
       ``validation.parseable_ics`` — a probe that comes back with a
       real VCALENDAR envelope lights the feed immediately; anything
       less (e.g. a non-ICS body) lands disabled for the operator
       to investigate.
    5. Write one ``ical_feed.register`` audit row with host-only URL.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    try:
        validation = validator.validate(body.url)
    except IcalValidationError as exc:
        raise IcalUrlInvalid(exc.code, str(exc)) from exc

    # Provider override wins when present; fall through to auto-
    # detection only when the override is absent. Skipping the detect
    # call in the override path keeps the service side-effect-free on
    # that branch (detector stubs in tests see zero calls).
    effective_provider: IcalProvider = (
        body.provider_override
        if body.provider_override is not None
        else detector.detect(validation.url)
    )

    feed_id = new_ulid()
    ciphertext = envelope.encrypt(
        validation.url.encode("utf-8"),
        purpose=_URL_PURPOSE,
        owner=_owner_for_feed(feed_id),
    )

    row = IcalFeed(
        id=feed_id,
        workspace_id=ctx.workspace_id,
        property_id=body.property_id,
        unit_id=body.unit_id,
        url=_ciphertext_to_str(ciphertext),
        provider=effective_provider,
        poll_cadence=(
            body.poll_cadence if body.poll_cadence is not None else DEFAULT_POLL_CADENCE
        ),
        last_polled_at=now,
        last_etag=None,
        last_error=None,
        enabled=validation.parseable_ics,
        created_at=now,
    )
    session.add(row)
    session.flush()

    view = _row_to_view(
        row,
        validation=validation,
        provider_override=body.provider_override,
    )
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=row.id,
        action="register",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def update_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    body: IcalFeedUpdate,
    validator: IcalValidator,
    detector: ProviderDetector,
    envelope: EnvelopeEncryptor,
    clock: Clock | None = None,
) -> IcalFeedView:
    """Mutate an existing feed.

    ``url`` set → re-validate + re-encrypt + re-probe; enabled flips
    to match the new probe's ``parseable_ics``.
    ``provider_override`` set → swap the stored provider slug
    without re-probing (cheap metadata flip).

    At least one of the two must be set; an empty body raises
    :class:`ValueError` (422) — there's no such thing as a no-op
    update, and silently succeeding would be an audit surprise.
    """
    if body.url is None and body.provider_override is None:
        raise ValueError("update_feed requires at least one of url / provider_override")

    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, feed_id=feed_id)
    before_view = _row_to_view(row, validation=None, provider_override=None)

    validation: IcalValidation | None = None
    if body.url is not None:
        try:
            validation = validator.validate(body.url)
        except IcalValidationError as exc:
            raise IcalUrlInvalid(exc.code, str(exc)) from exc
        ciphertext = envelope.encrypt(
            validation.url.encode("utf-8"),
            purpose=_URL_PURPOSE,
            owner=_owner_for_feed(row.id),
        )
        row.url = _ciphertext_to_str(ciphertext)
        row.last_polled_at = now
        # A fresh URL invalidates any prior probe error — the new URL
        # has not been judged yet, so ``last_error`` is cleared and
        # will be re-populated on the next probe-level failure.
        row.last_error = None
        row.enabled = validation.parseable_ics

    if body.provider_override is not None:
        # Override wins; auto-detect only runs when the override is
        # absent. When both ``url`` and ``provider_override`` are
        # set, the override still wins — matches :func:`register_feed`.
        row.provider = body.provider_override
    elif validation is not None:
        # URL changed but override is absent — re-run auto-detect on
        # the new URL.
        row.provider = detector.detect(validation.url)

    session.flush()
    after_view = _row_to_view(
        row,
        validation=validation,
        provider_override=body.provider_override,
    )
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=row.id,
        action="update",
        diff={
            "before": _view_to_diff_dict(before_view),
            "after": _view_to_diff_dict(after_view),
        },
        clock=resolved_clock,
    )
    return after_view


def disable_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    clock: Clock | None = None,
) -> IcalFeedView:
    """Flip ``enabled=False`` on a feed without dropping the row.

    The row survives so the reservation history keyed off
    ``ical_feed_id`` stays navigable; the poller simply skips
    disabled feeds.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, feed_id=feed_id)
    before_view = _row_to_view(row, validation=None, provider_override=None)
    row.enabled = False
    session.flush()
    after_view = _row_to_view(row, validation=None, provider_override=None)
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=row.id,
        action="disable",
        diff={
            "before": _view_to_diff_dict(before_view),
            "after": _view_to_diff_dict(after_view),
        },
        clock=resolved_clock,
    )
    return after_view


def delete_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    clock: Clock | None = None,
) -> IcalFeedView:
    """Hard-delete the feed row.

    §02 "ical_feed" does not carry a ``deleted_at`` column; deleting
    is a plain DELETE. Reservations survive via the
    ``reservation.ical_feed_id`` ``SET NULL`` cascade (§02
    "reservation"). If v2 adds a soft-delete column this path
    switches to stamping ``deleted_at`` and leaves the audit shape
    intact.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, feed_id=feed_id)
    before_view = _row_to_view(row, validation=None, provider_override=None)
    session.delete(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=before_view.id,
        action="delete",
        diff={"before": _view_to_diff_dict(before_view)},
        clock=resolved_clock,
    )
    return before_view


def probe_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    validator: IcalValidator,
    envelope: EnvelopeEncryptor,
    clock: Clock | None = None,
) -> IcalProbeResult:
    """Re-run validation + fetch against the stored URL.

    Used by both the operator's "test this feed" button (future API)
    and the first-success gate — a newly-registered feed lands
    ``enabled=False`` if the probe body didn't look like an ICS
    envelope; a later probe can flip it on.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, feed_id=feed_id)

    plaintext_url = get_plaintext_url(session, ctx, feed_id=feed_id, envelope=envelope)
    try:
        validation = validator.validate(plaintext_url)
    except IcalValidationError as exc:
        row.last_polled_at = now
        # Persist the §04 error code on the feed so the operator UI
        # can render a live-vs-stale indicator without tailing the
        # audit stream. Cleared on the next successful probe below.
        row.last_error = exc.code
        session.flush()
        write_audit(
            session,
            ctx,
            entity_kind="ical_feed",
            entity_id=row.id,
            action="probe",
            diff={"ok": False, "error_code": exc.code, "polled_at": now.isoformat()},
            clock=resolved_clock,
        )
        return IcalProbeResult(
            feed_id=row.id,
            ok=False,
            parseable_ics=False,
            error_code=exc.code,
            polled_at=now,
        )

    row.last_polled_at = now
    # Success clears any prior error so the "most recent outcome"
    # shape holds — a feed that healed after a transient failure
    # looks healthy again to the operator UI.
    row.last_error = None
    if validation.parseable_ics and not row.enabled:
        # First-success gate: flip ``enabled`` only when we've seen a
        # real VCALENDAR. A non-parseable body leaves ``enabled``
        # alone (if it was true we keep it true; if false we keep it
        # false pending a real body).
        row.enabled = True
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=row.id,
        action="probe",
        diff={
            "ok": True,
            "parseable_ics": validation.parseable_ics,
            "polled_at": now.isoformat(),
        },
        clock=resolved_clock,
    )
    return IcalProbeResult(
        feed_id=row.id,
        ok=True,
        parseable_ics=validation.parseable_ics,
        error_code=None,
        polled_at=now,
    )


def list_feeds(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None = None,
) -> Sequence[IcalFeedView]:
    """Enumerate feeds in the caller's workspace.

    Optional ``property_id`` filter. Ordered by ``created_at`` then
    ``id`` for a stable tie-break. The plaintext URL is **never**
    materialised — the view carries only the non-secret
    ``url_preview``.
    """
    stmt = select(IcalFeed).where(IcalFeed.workspace_id == ctx.workspace_id)
    if property_id is not None:
        stmt = stmt.where(IcalFeed.property_id == property_id)
    stmt = stmt.order_by(IcalFeed.created_at.asc(), IcalFeed.id.asc())
    rows = session.scalars(stmt).all()
    return [_row_to_view(row, validation=None, provider_override=None) for row in rows]


def get_plaintext_url(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    envelope: EnvelopeEncryptor,
) -> str:
    """Return the decrypted URL for ``feed_id``.

    The **only** legal plaintext reach. The poller (cd-d48) calls
    this inside its fetch loop; the operator-facing HTTP layer must
    never surface the result. An operator UI that wants to show the
    URL should copy through a signed, short-lived echo path instead
    — the plaintext URL can carry vendor secret tokens.

    Raises :class:`IcalFeedNotFound` for unknown / wrong-workspace
    ids; ciphertext corruption surfaces as
    :class:`app.adapters.storage.envelope.EnvelopeDecryptError`.
    """
    row = _load_row(session, ctx, feed_id=feed_id)
    plaintext = envelope.decrypt(
        _str_to_ciphertext(row.url),
        purpose=_URL_PURPOSE,
        expected_owner=_owner_for_feed(row.id),
    )
    return plaintext.decode("utf-8")


def resolve_allow_self_signed(
    session: Session,
    *,
    workspace_id: str,
    property_id: str | None,
) -> bool:
    """Resolve the ``ical.allow_self_signed`` setting for a feed.

    Walks the §02 cascade for the (workspace, property) pair. Returns
    ``True`` only when the setting evaluates truthy at the most-
    specific layer found — workspace default is ``False`` (catalog),
    property override wins when present.

    Used by the API registration / probe / poll-once routes and by
    the worker's per-feed loop. Both routes need the answer **before**
    they construct the validator's TLS context (see
    :func:`app.adapters.ical.validator.build_tls_context` and
    :func:`app.worker.tasks.poll_ical.fetch_ical_body`). Centralising
    the lookup here keeps the §15 "Allow self-signed iCal" policy
    decision in one place — adapters never reach into the cascade.

    ``property_id=None`` falls back to the workspace layer; the
    catalog default ``False`` applies if neither layer overrides.

    Production posture: every workspace ships with the catalog
    default ``False``. The setting flips to ``True`` only when an
    operator opts in for one workspace / property explicitly through
    the settings API. See :doc:`docs/specs/04-properties-and-stays`
    §"Dev / e2e carve-out".
    """
    chain = SettingScopeChain(workspace_id=workspace_id, property_id=property_id)
    raw = resolve_most_specific(session, _ALLOW_SELF_SIGNED_KEY, chain, default=False)
    return bool(raw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_row(session: Session, ctx: WorkspaceContext, *, feed_id: str) -> IcalFeed:
    """Workspace-scoped loader. Raises :class:`IcalFeedNotFound` on miss."""
    stmt = select(IcalFeed).where(
        IcalFeed.id == feed_id,
        IcalFeed.workspace_id == ctx.workspace_id,
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise IcalFeedNotFound(feed_id)
    return row


def _ciphertext_to_str(ciphertext: bytes) -> str:
    """Encode ciphertext bytes for the ``ical_feed.url`` TEXT column.

    The column is TEXT (v1 slice — §02 ``secret_envelope`` carries
    the canonical bytes; the inline blob lives here for back-compat).
    We latin-1 encode the raw bytes, which is the canonical "1:1
    byte-to-codepoint" text mapping — every byte value ``0..255``
    maps to exactly one Unicode codepoint so round-tripping through
    TEXT is lossless. cd-znv4 row-backed pointers are short ASCII
    (``0x02 || ULID``) so the encoding is identity-on-the-wire for
    the new format too.
    """
    return ciphertext.decode("latin-1")


def _owner_for_feed(feed_id: str) -> EnvelopeOwner:
    """Build the §15 owner pointer for an iCal feed's URL secret.

    cd-znv4 row-backed mode stamps every persisted ``secret_envelope``
    row with ``(owner_entity_kind, owner_entity_id)`` so the rotation
    worker can scope a re-encrypt sweep to "every secret for this
    feed" and so a future delete-cascade helper can sweep the row
    when the iCal feed is hard-deleted. The kind slug matches the
    §02 entity name verbatim.
    """
    return EnvelopeOwner(kind="ical_feed", id=feed_id)


def _str_to_ciphertext(stored: str) -> bytes:
    """Inverse of :func:`_ciphertext_to_str`."""
    return stored.encode("latin-1")


def _row_to_view(
    row: IcalFeed,
    *,
    validation: IcalValidation | None,
    provider_override: IcalProviderOverride | None,
) -> IcalFeedView:
    """Project an :class:`IcalFeed` row into the safe read shape.

    ``validation`` is passed through only during register / update so
    the returned view carries a fresh ``url_preview`` for the
    caller; reads that don't have a validation handy never decrypt —
    the list path explicitly must not round-trip plaintext. Instead
    the view carries ``"(encrypted)"`` when we can't derive a preview
    without decryption.

    ``last_error`` is sourced straight from the row (cd-ewd7); the
    domain service stamps it on probe failure and clears it on
    success, so the caller always sees the most recent outcome.
    """
    preview: str
    if validation is not None:
        preview = _host_only_preview(validation.url)
    else:
        # List / disable / delete — we don't decrypt. This keeps the
        # read path free of envelope dependencies. Operators who want
        # the public preview can trigger a probe (which has a
        # validation in hand) or hit a dedicated "reveal" endpoint
        # that goes through :func:`get_plaintext_url` with an
        # elevated capability.
        preview = "(encrypted)"
    return IcalFeedView(
        id=row.id,
        workspace_id=row.workspace_id,
        property_id=row.property_id,
        unit_id=row.unit_id,
        provider=_narrow_loaded_provider(row.provider),
        provider_override=provider_override,
        url_preview=preview,
        enabled=row.enabled,
        poll_cadence=row.poll_cadence,
        last_polled_at=row.last_polled_at,
        last_etag=row.last_etag,
        last_error=row.last_error,
        created_at=row.created_at,
    )


def _narrow_loaded_provider(value: str) -> str:
    """Validate that a DB-loaded ``provider`` slug is in the accept set.

    The CHECK constraint on ``ical_feed.provider`` already rejects
    anything else at write time; the narrow here surfaces schema
    drift as a loud :class:`ValueError` rather than silently
    returning junk when a row is loaded. Returns the original value
    untyped because :class:`IcalFeedView.provider` is a plain ``str``
    — the accept set is the union of :data:`IcalProvider` slugs
    (``airbnb | vrbo | booking | gcal | generic``) plus the v1-era
    ``custom`` spelling.
    """
    if value in _LOADED_PROVIDERS:
        return value
    raise ValueError(f"unknown ical_feed.provider {value!r} on loaded row")


def _host_only_preview(url: str) -> str:
    """Return ``scheme://host`` — strip path and query (often secret)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def _view_to_diff_dict(view: IcalFeedView) -> dict[str, Any]:
    """Flatten an :class:`IcalFeedView` into a JSON-safe audit payload.

    Intentionally omits anything URL-derived that could carry the
    plaintext secret. ``url_preview`` is already host-only; still
    passes through the audit writer's redactor for defence in
    depth.
    """
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "property_id": view.property_id,
        "unit_id": view.unit_id,
        "provider": view.provider,
        "provider_override": view.provider_override,
        "url_preview": view.url_preview,
        "enabled": view.enabled,
        "poll_cadence": view.poll_cadence,
        "last_polled_at": (
            view.last_polled_at.isoformat() if view.last_polled_at else None
        ),
        "last_etag": view.last_etag,
        "last_error": view.last_error,
        "created_at": view.created_at.isoformat(),
    }
