"""``poll_ical`` — periodic iCal feed ingestion (cd-d48).

Walks every enabled :class:`~app.adapters.db.stays.models.IcalFeed`
in the caller's workspace whose ``next_poll_at`` (derived from
``last_polled_at + poll_cadence``) has slipped below ``now``,
fetches the upstream calendar through the §04 SSRF-guarded HTTPS
fetcher with conditional ``If-None-Match`` headers, parses the
VCALENDAR envelope with the :pypi:`icalendar` library, and writes
the diff:

* **Booked-pattern VEVENT** (no Blocked summary, ``STATUS != CANCELLED``)
  → upsert :class:`~app.adapters.db.stays.models.Reservation` keyed on
  ``(ical_feed_id, external_uid)`` with a ``reservation.upserted``
  event whose ``change_kind`` is ``created`` / ``updated``.
* **Cancelled VEVENT** (``STATUS = CANCELLED`` or ``METHOD:CANCEL``)
  → flip the reservation's ``status`` to ``cancelled`` with a
  ``reservation.upserted`` event whose ``change_kind`` is
  ``cancelled``. Idempotent: a row already cancelled is a no-op.
* **Blocked-pattern VEVENT** (``SUMMARY`` in ``_BLOCKED_SUMMARIES`` —
  Airbnb "Not available", VRBO "Blocked", Google Calendar "Reserved")
  → insert a :class:`~app.adapters.db.places.models.PropertyClosure`
  row with ``reason='ical_unavailable'`` and ``source_ical_feed_id``
  set, plus a ``property.closure.created`` event. Closures are NOT
  retracted when a VEVENT disappears upstream — operators may want
  to keep the historical record; spec §04 explicitly notes
  "deleting them manually is allowed".

**Idempotency.** Re-polling the same feed body produces zero new
rows + zero new events (the upsert path matches every existing
``(feed_id, external_uid)`` pair, sees no field changes, and
short-circuits before publishing). The closure path keys on
``(property_id, source_ical_feed_id, source_external_uid)`` and keeps
soft-deleted tombstones so a manually deleted Blocked VEVENT is not
recreated by the next identical poll.

**Per-host rate limit.** A 1 s minimum gap between consecutive
fetches against the same host applies inside one tick. Exceeding
the gap costs the feed a tick — the loop logs the skip but does
not error. ``Retry-After`` on a 429 response writes ``last_error =
'rate_limited'`` and skips the rest of the tick for that feed.

**Error envelope.** Every per-feed failure (validation, fetch,
parse, upsert) is captured into ``ical_feed.last_error`` (the §04
``ical_url_*`` vocabulary plus ``ical_parse_error`` and
``rate_limited``) and the loop continues — never poison the loop.

**WorkspaceContext** is threaded through every DB read, write, and
event publish. The caller (APScheduler tick fan-out, CLI, test)
resolves a context per workspace before calling in.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed" and
``docs/specs/16-deployment-operations.md`` §"Worker process".
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal
from urllib.parse import SplitResult, urlsplit

from icalendar import Calendar
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import PropertyClosure
from app.adapters.db.stays.models import IcalFeed, Reservation
from app.adapters.ical.ports import IcalValidationError
from app.adapters.ical.validator import (
    DEFAULT_ALLOWED_CONTENT_TYPES,
    Fetcher,
    FetchResponse,
    Resolver,
    StdlibHttpsFetcher,
    resolve_public_address,
)
from app.adapters.storage.ports import EnvelopeEncryptor
from app.audit import write_audit
from app.domain.stays import ical_service
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import (
    PropertyClosureCreated,
    PropertyClosureUpdated,
    ReservationChangeKind,
    ReservationUpserted,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DEFAULT_PER_HOST_RATE_LIMIT_SECONDS",
    "DEFAULT_POLL_FETCH_TIMEOUT_SECONDS",
    "DEFAULT_PROBE_BODY_BYTES",
    "PollOutcome",
    "PollReport",
    "PolledFeedResult",
    "fetch_ical_body",
    "poll_ical",
]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# §04 "iCal feed" §"Limits" — 2 MB body cap, 10 s deadline. We re-use
# the validator's defaults rather than picking new numbers; the poll
# path and the probe path have identical limits by design.
DEFAULT_PROBE_BODY_BYTES: Final[int] = 2 * 1024 * 1024
DEFAULT_POLL_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0

# Per-host minimum gap between consecutive HTTPS GETs inside a single
# tick. Spec §04 says "Rate-limit per host"; 1 s is the spec's
# implicit baseline (matches the §04 "soft per-host throttle" comment
# in cd-1ai's design notes). The check applies across feeds in the
# same tick; a feed whose host was hit less than this ago skips the
# tick and gets re-attempted on the next.
DEFAULT_PER_HOST_RATE_LIMIT_SECONDS: Final[float] = 1.0

# Canonical Blocked summaries (case-insensitive, after strip) on a
# VEVENT ``SUMMARY`` that trip the Blocked → ``property_closure`` path.
# Pulled from the §04 spec + the providers each support:
#
# * "Not available" / "CLOSED - Not available"  — Airbnb's bracketing
#   summary on a host-blocked night (no booking, no guest data).
# * "Blocked" / "Airbnb (Not available)"        — VRBO + cross-channel.
# * "Reserved" / "Reservation"                  — Google Calendar +
#   Booking.com sometimes use a generic "Reserved" without further
#   detail; we treat those as Blocked because the lack of a
#   recognisable guest-name leg means the slot is definitively
#   unavailable from our perspective.
#
# The match is **whole-token + case-insensitive**: a SUMMARY whose
# stripped lowercase form equals one of these tokens, OR begins with
# the token immediately followed by a separator (``" "``, ``"-"``,
# ``"("``, ``":"``), is Blocked. ``"Reserved (Jane Doe)"`` does NOT
# match — the parenthesis-suffixed name is the Airbnb / VRBO real-
# booking shape — but ``"Reserved"`` alone (Booking.com generic) and
# ``"CLOSED - Not available"`` (Airbnb bracketed) both do. The
# whole-token discipline is what keeps "Reservation for Jane" from
# being flagged Blocked.
_BLOCKED_SUMMARIES: Final[tuple[str, ...]] = (
    "not available",
    "blocked",
    "reserved",
    "unavailable",
    "closed",
)
# Separators that admit a Blocked-token prefix into the Blocked path.
# Anything else immediately after the token (a letter, a name, a
# digit) means the summary carries an additional payload — most likely
# the guest name — and the row is a real booking, not a Blocked
# window. The set is intentionally narrow (whitespace, dash, colon,
# open / close paren, slash, comma) so a future provider's punctuation
# choice is a conscious decision to extend rather than silent
# inclusion.
_BLOCKED_TOKEN_SEPARATORS: Final[frozenset[str]] = frozenset(
    {" ", "-", ":", "(", ")", "/", ","}
)

# Tokens that ONLY match on whole-summary equality (not prefix). The
# "Reserved" channel-generic Blocked summary is a Booking.com /
# Google-calendar emission that means "blocked window with no further
# detail". The Airbnb / VRBO real-booking shape is
# ``"Reserved (Jane Doe)"`` — same prefix, different payload — and
# would mis-trip the Blocked path under a prefix-match. Equality-only
# keeps the Blocked branch from swallowing real bookings while still
# catching the bare-token closure.
_BLOCKED_EXACT_ONLY_TOKENS: Final[frozenset[str]] = frozenset({"reserved"})


# Provider-supplied UID convention varies; we only require it to be
# stable per booking. Some channels (Booking.com early ICS) use a
# trailing ``@booking.com`` host; we keep the full UID verbatim
# because the ``(ical_feed_id, external_uid)`` UNIQUE index treats
# ``"foo@booking.com"`` and ``"foo"`` as distinct (correct: a
# provider that changes its UID convention should be treated as a
# new feed even if the host UID prefix matches).
_UID_RE: Final[re.Pattern[str]] = re.compile(r"^[\x20-\x7e]+$")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolledFeedResult:
    """Per-feed outcome the tick body returns to the caller.

    Frozen + slotted so the audit writer can flatten to JSON
    deterministically and tests can equality-check the full shape.

    * ``feed_id`` — the row this slot polled.
    * ``status`` — closed enum: ``"polled"`` (fetch + parse + upsert
      ran), ``"not_modified"`` (304 — body skipped, ``last_polled_at``
      bumped), ``"rate_limited"`` (per-host gap or upstream 429),
      ``"skipped_disabled"`` (feed has ``enabled=False``),
      ``"skipped_not_due"`` (cadence not yet up), or ``"error"``
      (validation / fetch / parse failure — ``error_code`` populated).
    * ``error_code`` — §04 vocabulary on failure
      (``ical_url_timeout``, ``ical_parse_error``, ``rate_limited``,
      …); ``None`` on success.
    * ``reservations_created`` / ``_updated`` / ``_cancelled``,
      ``closures_created`` — counters per outcome bucket.
    """

    feed_id: str
    status: str
    error_code: str | None
    reservations_created: int
    reservations_updated: int
    reservations_cancelled: int
    closures_created: int


@dataclass(frozen=True, slots=True)
class PollReport:
    """Summary of one ``poll_ical`` invocation across a workspace.

    Frozen + slotted so the audit writer can flatten to JSON
    deterministically.
    """

    feeds_walked: int
    feeds_polled: int
    feeds_not_modified: int
    feeds_rate_limited: int
    feeds_errored: int
    feeds_skipped: int
    reservations_created: int
    reservations_updated: int
    reservations_cancelled: int
    closures_created: int
    tick_started_at: datetime
    tick_ended_at: datetime
    per_feed_results: tuple[PolledFeedResult, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ParsedVEvent:
    """One iCal VEVENT after light parsing.

    Coerces the few fields the upsert path consumes into a stable
    dataclass so the rest of the worker doesn't have to walk the
    icalendar component tree. ``cancelled`` is set when the VEVENT
    carries ``STATUS:CANCELLED``.
    """

    uid: str
    summary: str | None
    description: str | None
    starts_at: datetime
    ends_at: datetime
    cancelled: bool


@dataclass(frozen=True, slots=True)
class _PolledBody:
    """Outcome of :func:`fetch_ical_body`. ``body`` is ``None`` on 304."""

    status: int
    body: bytes | None
    etag: str | None
    retry_after_seconds: int | None


class PollOutcome:
    """Closed enum for :class:`PolledFeedResult.status`. Class to keep
    the constants grouped (and the type-checker enforcing exhaustive
    matches when callers switch on the value)."""

    POLLED: Final[str] = "polled"
    NOT_MODIFIED: Final[str] = "not_modified"
    RATE_LIMITED: Final[str] = "rate_limited"
    SKIPPED_DISABLED: Final[str] = "skipped_disabled"
    SKIPPED_NOT_DUE: Final[str] = "skipped_not_due"
    ERROR: Final[str] = "error"


# ---------------------------------------------------------------------------
# Fetch layer (re-uses §04 SSRF guard from the validator)
# ---------------------------------------------------------------------------


def fetch_ical_body(
    url: str,
    *,
    last_etag: str | None,
    deadline: float,
    fetcher: Fetcher | None = None,
    resolver: Resolver | None = None,
    max_body_bytes: int = DEFAULT_PROBE_BODY_BYTES,
    user_agent: str = "crewday-ical-poller/1.0",
    allow_private_addresses: bool = False,
    allow_self_signed: bool = False,
) -> _PolledBody:
    """Fetch ``url`` through the SSRF-pinned HTTPS path.

    Delegates DNS lookup + public-IP filtering to the validator's
    :func:`~app.adapters.ical.validator.resolve_public_address`
    (single source of truth for "is this host safe to fetch"),
    pins the chosen IP through the TCP connection, and issues one
    GET with optional ``If-None-Match: <last_etag>``. Returns the
    response body on 200, ``None`` on 304, and raises
    :class:`~app.adapters.ical.ports.IcalValidationError` on
    validation / size / timeout failures and on 4xx/5xx ≠ 304/429.

    The optional ``resolver`` parameter overrides the validator's
    default :func:`socket.getaddrinfo`-backed resolver — production
    leaves it ``None`` and tests inject a deterministic stub.

    ``allow_private_addresses`` is the §04 SSRF carve-out (cd-xr652).
    Default ``False`` — loopback / RFC 1918 / link-local are rejected
    with ``ical_url_private_address``, matching the registration-time
    validator. The fan-out body in
    :func:`app.worker.jobs.stays._make_poll_ical_fanout_body` reads
    :attr:`app.config.Settings.ical_allow_private_addresses` and
    forwards it here so the e2e compose override
    (``CREWDAY_ICAL_ALLOW_PRIVATE_ADDRESSES=1``) lets the worker poll
    the same in-cluster ICS server that registration accepted.
    Production must keep it ``False``.

    ``allow_self_signed`` is the §04 SSRF carve-out (cd-t2qtg) for
    the per-feed ``ical.allow_self_signed`` workspace / property
    setting. Default ``False`` — full TLS verification + hostname
    checks. ``True`` flips :class:`StdlibHttpsFetcher` to
    ``check_hostname=False`` + ``verify_mode=CERT_NONE`` for this fetch
    only. The worker resolves the per-feed cascade in
    :func:`_poll_one_feed` and forwards the result; every other gate
    (scheme, DNS-rebind pin, body cap, redirects, timeout) still
    applies. Production must keep the catalog default ``false`` —
    flipping the setting on a workspace lets a malicious feed at a
    self-signed endpoint avoid the chain-of-trust check.

    Rate-limit short-circuit: a 429 response returns a
    :class:`_PolledBody` with ``status=429`` and any ``Retry-After``
    seconds value parsed from the header (``None`` if the header is
    absent / malformed). Higher 5xx and other 4xx surface as
    :class:`IcalValidationError(code=ical_url_unreachable)`.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise IcalValidationError(
            "ical_url_insecure_scheme",
            f"only https:// URLs are accepted; got {parsed.scheme!r}",
        )
    host = parsed.hostname or ""
    if not host:
        raise IcalValidationError("ical_url_malformed", f"URL is missing host: {url!r}")

    # Re-use the validator's :func:`resolve_public_address` so the SSRF
    # pinning logic stays single-sourced — DNS lookup, public-IP
    # filter, "any non-public address rejects the whole lookup"
    # rebinding-defence rule, and the "first address wins" pin all
    # come from one place. A test that wants a custom resolver
    # injects it via ``resolver=``; ``None`` defers to the validator's
    # default :func:`socket.getaddrinfo` resolver inside
    # :class:`IcalValidatorConfig`.
    port = parsed.port if parsed.port is not None else 443
    if resolver is None:
        # Lazy import the validator's private default so we don't
        # widen the public surface for one fetch path. The validator
        # already exposes :class:`IcalValidatorConfig` whose
        # ``resolver`` field defaults to the same callable; importing
        # here keeps the call site terse and avoids constructing a
        # full validator config for one DNS lookup.
        from app.adapters.ical.validator import _system_resolver

        resolved_resolver: Resolver = _system_resolver
    else:
        resolved_resolver = resolver
    resolved_ip = resolve_public_address(
        host,
        port,
        resolver=resolved_resolver,
        allow_private_addresses=allow_private_addresses,
    )

    # Default fetcher honours ``allow_self_signed`` for the same
    # reason the validator does: when no etag is set we delegate
    # straight to :meth:`StdlibHttpsFetcher.fetch`, which builds its
    # TLS context off the constructor flag. Passing the flag through
    # the conditional path (``last_etag is not None``) is handled
    # below in :func:`_fetch_conditional`.
    resolved_fetcher = (
        fetcher
        if fetcher is not None
        else StdlibHttpsFetcher(allow_self_signed=allow_self_signed)
    )
    response = _fetch_conditional(
        resolved_fetcher,
        parsed=parsed,
        resolved_ip=resolved_ip,
        deadline=deadline,
        max_body_bytes=max_body_bytes,
        last_etag=last_etag,
        user_agent=user_agent,
        allow_self_signed=allow_self_signed,
    )

    if response.status == 304:
        # Body is empty by definition; carry the inbound ETag forward
        # if the upstream echoed one (some CDNs do, some don't —
        # ``last_etag`` survives unchanged when the response omits it).
        return _PolledBody(
            status=304,
            body=None,
            etag=_first_header(response.headers, "ETag") or last_etag,
            retry_after_seconds=None,
        )
    if response.status == 429:
        retry_after = _parse_retry_after(_first_header(response.headers, "Retry-After"))
        return _PolledBody(
            status=429, body=None, etag=last_etag, retry_after_seconds=retry_after
        )
    if 200 <= response.status < 300:
        content_type = _first_header(response.headers, "Content-Type")
        if not _looks_like_ics(response.body, content_type):
            raise IcalValidationError(
                "ical_url_bad_content",
                f"body did not look like a VCALENDAR envelope (content-type "
                f"{content_type!r}, first bytes "
                f"{response.body[:16]!r})",
            )
        return _PolledBody(
            status=response.status,
            body=response.body,
            etag=_first_header(response.headers, "ETag"),
            retry_after_seconds=None,
        )

    # Any other status — 3xx, 4xx (except 429), 5xx — is a poll
    # failure. We do not follow redirects in the poll path; the
    # validator did the same-origin redirect dance at registration
    # time. A new redirect is a sign the upstream URL changed and
    # the operator must re-validate.
    raise IcalValidationError(
        "ical_url_unreachable",
        f"upstream returned HTTP {response.status} on poll",
    )


def _fetch_conditional(
    fetcher: Fetcher,
    *,
    parsed: SplitResult,
    resolved_ip: str,
    deadline: float,
    max_body_bytes: int,
    last_etag: str | None,
    user_agent: str,
    allow_self_signed: bool = False,
) -> FetchResponse:
    """Issue one GET via ``fetcher`` with conditional headers.

    Rebuilds the same SSRF-pinned ``HTTPSConnection`` shape the
    validator uses but adds an ``If-None-Match`` header when
    ``last_etag`` is set. The :class:`StdlibHttpsFetcher` does not
    expose a "set extra headers" hook, so we drop into a thin
    subclass-only path: we wrap ``fetcher.fetch`` with a captured
    closure that injects the headers via the same ``http.client``
    surface the production fetcher uses. To keep this composable
    with test stubs, we accept any :class:`Fetcher` and rely on the
    contract that the production fetcher is the one we instantiate
    by default; tests pass their own that synthesises a
    :class:`FetchResponse` (and decides itself whether to honour
    conditional GET).

    The signature mirrors ``Fetcher.fetch`` so the production path
    stays identical: when ``last_etag`` is ``None`` we delegate
    straight through; when it's set we hand the call to
    :func:`_stdlib_conditional_fetch` (only valid for the production
    :class:`StdlibHttpsFetcher`). Tests that need conditional-GET
    behaviour subclass our test-only :class:`_FakeConditionalFetcher`
    in ``tests/unit/worker/test_poll_ical.py``.
    """
    if last_etag is None:
        return fetcher.fetch(
            parsed,
            resolved_ip,
            deadline=deadline,
            max_body_bytes=max_body_bytes,
        )

    # Production path — conditional GET via the stdlib fetcher's
    # private hook (we re-issue via the same HTTPSConnection shape).
    if isinstance(fetcher, StdlibHttpsFetcher):
        return _stdlib_conditional_fetch(
            parsed=parsed,
            resolved_ip=resolved_ip,
            deadline=deadline,
            max_body_bytes=max_body_bytes,
            last_etag=last_etag,
            user_agent=user_agent,
            allow_self_signed=allow_self_signed,
        )

    # Test stubs that want conditional-GET semantics implement their
    # own :class:`Fetcher`. The poll path passes ``last_etag`` along
    # via a header-injection seam; for non-stdlib fetchers we delegate
    # straight through and let the stub interpret the request from
    # the URL parts (which it controls).
    return fetcher.fetch(
        parsed,
        resolved_ip,
        deadline=deadline,
        max_body_bytes=max_body_bytes,
    )


def _stdlib_conditional_fetch(
    *,
    parsed: SplitResult,
    resolved_ip: str,
    deadline: float,
    max_body_bytes: int,
    last_etag: str,
    user_agent: str,
    allow_self_signed: bool = False,
) -> FetchResponse:
    """Stdlib :mod:`http.client` GET with ``If-None-Match`` set.

    Mirrors :class:`StdlibHttpsFetcher` but adds the conditional
    header. We re-implement (rather than monkey-patch the validator's
    fetcher) because the validator's
    :meth:`StdlibHttpsFetcher.fetch` does not accept extra headers
    today and bolting one on would widen the validator's API
    surface for a poll-only need. Localising the
    duplication keeps the validator's contract narrow.

    ``allow_self_signed`` is the §04 SSRF carve-out (cd-t2qtg);
    forwarded via the per-feed ``ical.allow_self_signed`` setting
    cascade. The TLS context is built through
    :func:`app.adapters.ical.validator.build_tls_context` so the
    validator and the worker share a single source of truth for the
    TLS posture (no drift between registration-probe and poll-time
    fetches).
    """
    import http.client
    import time as _time

    from app.adapters.ical.validator import (
        _IpPinnedHTTPSConnection,
        _read_body_capped,
        build_tls_context,
    )

    host = parsed.hostname or ""
    port = parsed.port if parsed.port is not None else 443
    remaining = deadline - _time.monotonic()
    if remaining <= 0:
        raise IcalValidationError(
            "ical_url_timeout", f"deadline exceeded before connect to {host!r}"
        )

    ctx = build_tls_context(allow_self_signed=allow_self_signed)
    conn: http.client.HTTPSConnection = _IpPinnedHTTPSConnection(
        host=host,
        resolved_ip=resolved_ip,
        port=port,
        timeout=remaining,
        context=ctx,
    )
    try:
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        host_header = host if port == 443 else f"{host}:{port}"
        request_headers = {
            "Host": host_header,
            "User-Agent": user_agent,
            "Accept": "text/calendar, text/plain;q=0.9, */*;q=0.5",
            "If-None-Match": last_etag,
        }
        try:
            conn.request("GET", path, headers=request_headers)
            response = conn.getresponse()
        except TimeoutError as exc:
            raise IcalValidationError(
                "ical_url_timeout", f"timeout during request to {host!r}"
            ) from exc
        except (http.client.HTTPException, OSError) as exc:
            raise IcalValidationError(
                "ical_url_unreachable",
                f"HTTP error talking to {host!r}: {exc}",
            ) from exc
        status = response.status
        resp_headers: tuple[tuple[str, str], ...] = tuple(response.getheaders())
        body = _read_body_capped(response, deadline, max_body_bytes)
        return FetchResponse(status=status, headers=resp_headers, body=body)
    finally:
        conn.close()


def _first_header(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    """Return the first header matching ``name`` (case-insensitive)."""
    lowered = name.lower()
    for key, value in headers:
        if key.lower() == lowered:
            return value
    return None


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` header to a positive integer of seconds.

    Accepts the integer-seconds form (``"60"``) only — the HTTP-date
    form is rare on iCal endpoints and parsing it would drag in a
    full RFC 7231 date parser for one edge. ``None`` / malformed →
    ``None``; the caller falls back to a fixed cooldown.
    """
    if value is None:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return seconds


def _looks_like_ics(body: bytes, content_type: str | None) -> bool:
    """Return ``True`` if ``body`` should be treated as an ICS envelope."""
    if content_type is not None:
        mime = content_type.split(";", 1)[0].strip().lower()
        for allowed in DEFAULT_ALLOWED_CONTENT_TYPES:
            if mime == allowed:
                return True
    stripped = body.lstrip()
    return stripped[:15] == b"BEGIN:VCALENDAR"


# ---------------------------------------------------------------------------
# Cadence helpers
# ---------------------------------------------------------------------------


# §04 default cadence is ``*/15 * * * *`` (fifteen minutes). The poll
# tick fires every 15 min and we treat ``last_polled_at + 15 min`` as
# the floor — earlier ticks skip the feed, later ticks fire it. A
# richer cron parse (croniter) is a follow-up; today we treat any
# non-default cadence string as "use the default" rather than
# silently misinterpret a custom expression.
_DEFAULT_CADENCE_SECONDS: Final[int] = 15 * 60


def _next_due(
    feed: IcalFeed,
    *,
    cadence_seconds: int = _DEFAULT_CADENCE_SECONDS,
) -> datetime | None:
    """Return the earliest ``now`` at which ``feed`` is due, or ``None``.

    ``None`` means "never polled — due immediately". A non-null
    ``last_polled_at`` adds the cadence window. The result is
    timezone-aware UTC.
    """
    if feed.last_polled_at is None:
        return None
    last = _ensure_utc(feed.last_polled_at)
    return last + timedelta(seconds=cadence_seconds)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def poll_ical(
    ctx: WorkspaceContext,
    *,
    session: Session,
    envelope: EnvelopeEncryptor,
    now: datetime | None = None,
    clock: Clock | None = None,
    fetcher: Fetcher | None = None,
    resolver: Resolver | None = None,
    event_bus: EventBus | None = None,
    rate_limit_seconds: float = DEFAULT_PER_HOST_RATE_LIMIT_SECONDS,
    poll_timeout_seconds: float = DEFAULT_POLL_FETCH_TIMEOUT_SECONDS,
    max_body_bytes: int = DEFAULT_PROBE_BODY_BYTES,
    allow_private_addresses: bool = False,
    feed_ids: frozenset[str] | None = None,
    force: bool = False,
    allow_self_signed_resolver: Callable[[IcalFeed], bool] | None = None,
) -> PollReport:
    """Run one poll tick for the caller's workspace.

    Walks every enabled :class:`IcalFeed` whose cadence is up,
    fetches + parses + upserts. Returns a :class:`PollReport` with
    the counts the caller's audit / log surface needs.

    ``now`` pins the comparison instant; if omitted it is taken from
    ``clock`` (or :class:`~app.util.clock.SystemClock` if ``clock`` is
    also omitted). ``envelope`` is the
    :class:`~app.adapters.storage.ports.EnvelopeEncryptor` the
    decrypts each feed's stored URL.

    ``allow_private_addresses`` is the §04 SSRF carve-out (cd-xr652);
    the fan-out body reads
    :attr:`app.config.Settings.ical_allow_private_addresses` and
    forwards it here so the worker honours the same gate as
    registration. Default ``False`` — loopback / RFC 1918 /
    link-local feed URLs are rejected at fetch time.

    ``allow_self_signed_resolver`` is the §04 SSRF carve-out
    (cd-t2qtg) for the per-feed ``ical.allow_self_signed`` workspace /
    property setting. The resolver receives one
    :class:`~app.adapters.db.stays.models.IcalFeed` and returns
    whether self-signed certificates are accepted for that feed.
    ``None`` (the default) means the worker treats every feed as
    "verify the chain" — production posture. The fan-out body reads
    the cascade through :func:`app.domain.settings.cascade.concrete_values`
    and passes a closure here so the worker honours each feed's
    workspace + property setting without coupling the worker to the
    cascade module.

    ``feed_ids`` narrows the walk to a specific subset of feed IDs;
    ``None`` (the scheduler default) walks every feed in the workspace.
    The manual ``poll-once`` route (cd-jk6is) passes a single ID so
    the operator can force-ingest one feed without waiting for the
    next tick. ``force=True`` bypasses the per-feed cadence guard so
    a freshly-registered feed (whose ``last_polled_at`` was just
    stamped by the registration probe) ingests immediately. The
    disabled gate is **not** bypassed by ``force`` — operators must
    re-enable a disabled feed explicitly. Per-host rate-limit windows
    are also kept; a single-feed manual call has no peer feeds to
    contend with.

    Does **not** commit the session; the caller's Unit-of-Work owns
    the transaction boundary (§01 "Key runtime invariants" #3).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_now = now if now is not None else resolved_clock.now()
    if resolved_now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime in UTC")
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    tick_started_at = resolved_now

    stmt = (
        select(IcalFeed)
        .where(IcalFeed.workspace_id == ctx.workspace_id)
        .order_by(IcalFeed.id.asc())
    )
    if feed_ids is not None:
        stmt = stmt.where(IcalFeed.id.in_(feed_ids))
    feeds = list(session.scalars(stmt).all())

    # Per-host last-fetched monotonic instant inside this tick. Spec
    # §04 says "Rate-limit per host"; we enforce a soft minimum gap
    # between consecutive HTTPS GETs against the same host within
    # one tick. The monotonic clock keeps the gap robust against
    # wall-clock skew (NTP step in the middle of a tick).
    last_fetch_monotonic: dict[str, float] = {}

    per_feed_results: list[PolledFeedResult] = []
    feeds_polled = 0
    feeds_not_modified = 0
    feeds_rate_limited = 0
    feeds_errored = 0
    feeds_skipped = 0
    reservations_created_total = 0
    reservations_updated_total = 0
    reservations_cancelled_total = 0
    closures_created_total = 0

    for feed in feeds:
        if not feed.enabled:
            per_feed_results.append(
                _skipped_result(feed.id, PollOutcome.SKIPPED_DISABLED)
            )
            feeds_skipped += 1
            continue
        if not force:
            next_due = _next_due(feed)
            if next_due is not None and next_due > resolved_now:
                per_feed_results.append(
                    _skipped_result(feed.id, PollOutcome.SKIPPED_NOT_DUE)
                )
                feeds_skipped += 1
                continue

        # Per-host rate-limit guard — applies inside one tick. The
        # check happens before decrypting the URL so a flood of
        # feeds against the same host short-circuits without the
        # envelope cost.
        try:
            url = ical_service.get_plaintext_url(
                session, ctx, feed_id=feed.id, envelope=envelope
            )
        except Exception as exc:
            # Decrypt failure is the operator's problem (key rotation,
            # ciphertext corruption); record + continue.
            feeds_errored += 1
            _record_feed_error(
                session,
                ctx,
                feed=feed,
                code="ical_url_malformed",
                message=str(exc),
                clock=resolved_clock,
                resolved_now=resolved_now,
            )
            per_feed_results.append(_error_result(feed.id, "ical_url_malformed"))
            continue

        host = (urlsplit(url).hostname or "").lower()
        if host:
            now_monotonic = time.monotonic()
            previous = last_fetch_monotonic.get(host)
            if previous is not None and (now_monotonic - previous) < rate_limit_seconds:
                feeds_rate_limited += 1
                per_feed_results.append(
                    PolledFeedResult(
                        feed_id=feed.id,
                        status=PollOutcome.RATE_LIMITED,
                        error_code="rate_limited",
                        reservations_created=0,
                        reservations_updated=0,
                        reservations_cancelled=0,
                        closures_created=0,
                    )
                )
                continue
            last_fetch_monotonic[host] = now_monotonic

        feed_allow_self_signed = (
            allow_self_signed_resolver(feed)
            if allow_self_signed_resolver is not None
            else False
        )
        outcome = _poll_one_feed(
            session,
            ctx,
            feed=feed,
            url=url,
            fetcher=fetcher,
            resolver=resolver,
            event_bus=resolved_bus,
            clock=resolved_clock,
            resolved_now=resolved_now,
            poll_timeout_seconds=poll_timeout_seconds,
            max_body_bytes=max_body_bytes,
            allow_private_addresses=allow_private_addresses,
            allow_self_signed=feed_allow_self_signed,
        )
        per_feed_results.append(outcome)
        if outcome.status == PollOutcome.POLLED:
            feeds_polled += 1
        elif outcome.status == PollOutcome.NOT_MODIFIED:
            feeds_not_modified += 1
        elif outcome.status == PollOutcome.RATE_LIMITED:
            feeds_rate_limited += 1
        elif outcome.status == PollOutcome.ERROR:
            feeds_errored += 1
        reservations_created_total += outcome.reservations_created
        reservations_updated_total += outcome.reservations_updated
        reservations_cancelled_total += outcome.reservations_cancelled
        closures_created_total += outcome.closures_created

    tick_ended_at = resolved_clock.now()

    _write_poll_tick_audit(
        session,
        ctx,
        feeds_walked=len(feeds),
        feeds_polled=feeds_polled,
        feeds_not_modified=feeds_not_modified,
        feeds_rate_limited=feeds_rate_limited,
        feeds_errored=feeds_errored,
        feeds_skipped=feeds_skipped,
        reservations_created=reservations_created_total,
        reservations_updated=reservations_updated_total,
        reservations_cancelled=reservations_cancelled_total,
        closures_created=closures_created_total,
        tick_started_at=tick_started_at,
        tick_ended_at=tick_ended_at,
        clock=resolved_clock,
    )

    return PollReport(
        feeds_walked=len(feeds),
        feeds_polled=feeds_polled,
        feeds_not_modified=feeds_not_modified,
        feeds_rate_limited=feeds_rate_limited,
        feeds_errored=feeds_errored,
        feeds_skipped=feeds_skipped,
        reservations_created=reservations_created_total,
        reservations_updated=reservations_updated_total,
        reservations_cancelled=reservations_cancelled_total,
        closures_created=closures_created_total,
        tick_started_at=tick_started_at,
        tick_ended_at=tick_ended_at,
        per_feed_results=tuple(per_feed_results),
    )


# ---------------------------------------------------------------------------
# Per-feed body
# ---------------------------------------------------------------------------


def _poll_one_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed: IcalFeed,
    url: str,
    fetcher: Fetcher | None,
    resolver: Resolver | None,
    event_bus: EventBus,
    clock: Clock,
    resolved_now: datetime,
    poll_timeout_seconds: float,
    max_body_bytes: int,
    allow_private_addresses: bool,
    allow_self_signed: bool,
) -> PolledFeedResult:
    """Fetch + parse + upsert one feed.

    Catches every per-feed failure into ``ical_feed.last_error`` and
    returns an :class:`PolledFeedResult`; never raises (the loop in
    :func:`poll_ical` would poison the rest of the tick).
    """
    deadline = time.monotonic() + poll_timeout_seconds
    try:
        polled = fetch_ical_body(
            url,
            last_etag=feed.last_etag,
            deadline=deadline,
            fetcher=fetcher,
            resolver=resolver,
            max_body_bytes=max_body_bytes,
            allow_private_addresses=allow_private_addresses,
            allow_self_signed=allow_self_signed,
        )
    except IcalValidationError as exc:
        _record_feed_error(
            session,
            ctx,
            feed=feed,
            code=exc.code,
            message=str(exc),
            clock=clock,
            resolved_now=resolved_now,
        )
        return _error_result(feed.id, exc.code)
    except Exception as exc:
        # Defensive: an unexpected exception (e.g. socket bug, OS
        # signal mid-fetch) is captured into ``last_error`` so the
        # operator UI can surface it. The loop continues; the
        # exception class name lands as the message body.
        _record_feed_error(
            session,
            ctx,
            feed=feed,
            code="ical_url_unreachable",
            message=f"{type(exc).__name__}: {exc}",
            clock=clock,
            resolved_now=resolved_now,
        )
        return _error_result(feed.id, "ical_url_unreachable")

    if polled.status == 304:
        feed.last_polled_at = resolved_now
        feed.last_error = None
        if polled.etag is not None:
            feed.last_etag = polled.etag
        session.flush()
        return _zero_count_result(feed.id, PollOutcome.NOT_MODIFIED)

    if polled.status == 429:
        # Honor ``Retry-After`` by pushing ``last_polled_at`` *forward*
        # so :func:`_next_due` (which adds the default cadence) returns
        # roughly ``resolved_now + retry_after``. When the upstream
        # asks for a longer wait than the cadence (e.g. Airbnb says
        # "back off 1 h"), this skips the feed for the next several
        # ticks rather than blasting it again 15 min later. When
        # Retry-After is shorter than the cadence (or absent), we
        # simply stamp ``resolved_now`` and the feed waits one full
        # cadence — never less, since the cadence is the spec floor.
        retry_after = polled.retry_after_seconds
        if retry_after is not None and retry_after > _DEFAULT_CADENCE_SECONDS:
            feed.last_polled_at = resolved_now + timedelta(
                seconds=retry_after - _DEFAULT_CADENCE_SECONDS
            )
        else:
            feed.last_polled_at = resolved_now
        feed.last_error = "rate_limited"
        session.flush()
        _log.info(
            "iCal feed polling rate limited",
            extra={
                "event": "worker.poll_ical.rate_limited",
                "feed_id": feed.id,
                "retry_after_seconds": polled.retry_after_seconds,
            },
        )
        return PolledFeedResult(
            feed_id=feed.id,
            status=PollOutcome.RATE_LIMITED,
            error_code="rate_limited",
            reservations_created=0,
            reservations_updated=0,
            reservations_cancelled=0,
            closures_created=0,
        )

    body = polled.body
    if body is None:
        # Unreachable in practice — fetch_ical_body returns None body
        # only on 304/429, both handled above. Belt-and-braces guard
        # so a future status the helper grows isn't silently dropped.
        _record_feed_error(
            session,
            ctx,
            feed=feed,
            code="ical_url_unreachable",
            message=f"unexpected None body on status {polled.status}",
            clock=clock,
            resolved_now=resolved_now,
        )
        return _error_result(feed.id, "ical_url_unreachable")

    try:
        events = _parse_calendar(body)
    except _IcalParseError as exc:
        _record_feed_error(
            session,
            ctx,
            feed=feed,
            code="ical_parse_error",
            message=str(exc),
            clock=clock,
            resolved_now=resolved_now,
        )
        return _error_result(feed.id, "ical_parse_error")

    counts = _apply_events(
        session,
        ctx,
        feed=feed,
        events=events,
        event_bus=event_bus,
        clock=clock,
        resolved_now=resolved_now,
    )
    feed.last_polled_at = resolved_now
    feed.last_error = None
    if polled.etag is not None:
        feed.last_etag = polled.etag
    session.flush()

    _log.info(
        "iCal feed polling tick summary",
        extra={
            "event": "worker.poll_ical.feed.tick",
            "feed_id": feed.id,
            "reservations_created": counts.reservations_created,
            "reservations_updated": counts.reservations_updated,
            "reservations_cancelled": counts.reservations_cancelled,
            "closures_created": counts.closures_created,
            "vevents_parsed": len(events),
        },
    )
    return PolledFeedResult(
        feed_id=feed.id,
        status=PollOutcome.POLLED,
        error_code=None,
        reservations_created=counts.reservations_created,
        reservations_updated=counts.reservations_updated,
        reservations_cancelled=counts.reservations_cancelled,
        closures_created=counts.closures_created,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _IcalParseError(ValueError):
    """Raised inside :func:`_parse_calendar` on malformed input."""


def _parse_calendar(body: bytes) -> list[_ParsedVEvent]:
    """Parse VEVENTs out of a VCALENDAR envelope.

    Skips VEVENTs missing UID / DTSTART / DTEND (warn-and-drop —
    a malformed event must not poison the whole feed). The function
    raises :class:`_IcalParseError` only when the envelope itself
    is unreadable.
    """
    try:
        cal = Calendar.from_ical(body)
    except Exception as exc:
        raise _IcalParseError(f"VCALENDAR envelope parse failed: {exc}") from exc

    out: list[_ParsedVEvent] = []
    for component in cal.walk("VEVENT"):
        uid_raw = _component_get(component, "UID")
        if uid_raw is None:
            continue
        uid = str(uid_raw).strip()
        if not uid or not _UID_RE.match(uid):
            continue
        dtstart_prop = _component_get(component, "DTSTART")
        dtend_prop = _component_get(component, "DTEND")
        if dtstart_prop is None or dtend_prop is None:
            continue
        dtstart_dt = getattr(dtstart_prop, "dt", None)
        dtend_dt = getattr(dtend_prop, "dt", None)
        if dtstart_dt is None or dtend_dt is None:
            continue
        try:
            starts_at = _coerce_to_aware_utc(dtstart_dt)
            ends_at = _coerce_to_aware_utc(dtend_dt)
        except _IcalParseError:
            continue
        if ends_at <= starts_at:
            # Zero-or-negative-length event — skip rather than land a
            # CHECK-violating row downstream (the
            # ``property_closure.ends_after_starts`` constraint would
            # reject it on flush, poisoning the whole tick).
            continue
        summary_raw = _component_get(component, "SUMMARY")
        description_raw = _component_get(component, "DESCRIPTION")
        status_raw = _component_get(component, "STATUS")
        cancelled = status_raw is not None and str(status_raw).upper() == "CANCELLED"
        out.append(
            _ParsedVEvent(
                uid=uid,
                summary=str(summary_raw) if summary_raw is not None else None,
                description=str(description_raw)
                if description_raw is not None
                else None,
                starts_at=starts_at,
                ends_at=ends_at,
                cancelled=cancelled,
            )
        )
    return out


def _component_get(component: object, name: str) -> object:
    """Return ``component.get(name)`` as an opaque object.

    The :mod:`icalendar` library is type-stub-less and exposes
    :meth:`Component.get` as untyped. Wrapping the call in one place
    satisfies ``mypy --strict`` (every caller sees a typed
    ``object``) without scattering ``# type: ignore`` markers across
    the parse loop. Returns ``None`` when the property is absent —
    same surface as the underlying call.
    """
    getter = getattr(component, "get", None)
    if getter is None:
        return None
    value: object = getter(name)
    return value


def _coerce_to_aware_utc(value: object) -> datetime:
    """Coerce an ``icalendar`` DT value to aware UTC.

    Accepts :class:`datetime.datetime` (aware or naive) or
    :class:`datetime.date` (all-day events). All-day events are
    promoted to midnight UTC of the date — coarse but correct for
    Blocked-pattern matching, which is the main consumer of all-day
    rows.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raise _IcalParseError(f"unparseable datetime value: {value!r}")


# ---------------------------------------------------------------------------
# Apply parsed events to DB
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ApplyCounts:
    reservations_created: int = 0
    reservations_updated: int = 0
    reservations_cancelled: int = 0
    closures_created: int = 0


def _apply_events(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed: IcalFeed,
    events: list[_ParsedVEvent],
    event_bus: EventBus,
    clock: Clock,
    resolved_now: datetime,
) -> _ApplyCounts:
    """Upsert each parsed VEVENT and emit events.

    Discriminates Blocked-pattern VEVENTs (closures) from normal
    bookings (reservations). The discrimination is summary-based per
    §04 and intentionally crude — operators get a fallback override
    via the future ``ical_feed.summary_pattern_override`` column
    (cd-d48 follow-up).

    After the per-event loop, sweeps for Reservation rows whose UID
    has *disappeared* from the upstream feed (no VEVENT, no
    ``STATUS:CANCELLED``) and flips their ``status`` to ``cancelled``.
    Spec §04 "iCal feed" §"Polling behavior" calls this out: many
    PMSes signal a cancellation by simply removing the VEVENT rather
    than emitting a tombstone, so the poller must treat absence as a
    cancellation signal. Blocked closures are NOT swept — operators
    may keep the historical record (spec §04 "deleting them manually
    is allowed").
    """
    counts = _ApplyCounts()
    booked_uids_seen: set[str] = set()
    for ev in events:
        if _is_blocked_summary(ev.summary):
            closure_change = _upsert_closure(
                session,
                ctx,
                feed=feed,
                ev=ev,
                event_bus=event_bus,
                clock=clock,
                resolved_now=resolved_now,
            )
            if closure_change == "created":
                counts.closures_created += 1
            continue
        # Track every Booked-pattern UID — including STATUS:CANCELLED
        # tombstones — so the disappearance sweep below doesn't double
        # up on a row the in-band cancel already handled.
        booked_uids_seen.add(ev.uid)
        change_kind = _upsert_reservation(
            session,
            ctx,
            feed=feed,
            ev=ev,
            event_bus=event_bus,
            resolved_now=resolved_now,
        )
        if change_kind == "created":
            counts.reservations_created += 1
        elif change_kind == "updated":
            counts.reservations_updated += 1
        elif change_kind == "cancelled":
            counts.reservations_cancelled += 1
    counts.reservations_cancelled += _cancel_disappeared_reservations(
        session,
        ctx,
        feed=feed,
        seen_uids=booked_uids_seen,
        event_bus=event_bus,
        resolved_now=resolved_now,
    )
    return counts


def _cancel_disappeared_reservations(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed: IcalFeed,
    seen_uids: set[str],
    event_bus: EventBus,
    resolved_now: datetime,
) -> int:
    """Flip-to-cancelled any open Reservation whose UID is gone upstream.

    Scope is narrow on purpose:

    * **Workspace + feed pinned.** Only rows owned by ``ctx.workspace_id``
      and sourced from this exact ``feed.id`` are candidates — never
      another feed's bookings.
    * **iCal-only.** ``source == "ical"`` keeps the sweep from
      cancelling manual / API bookings that happen to share an
      ``external_uid`` (the UNIQUE index already prevents collisions
      on the iCal feed itself, but a manual row with no
      ``ical_feed_id`` would slip the per-feed filter; explicit
      source check makes the contract obvious).
    * **Open lifecycle only.** Skip rows already in
      ``cancelled`` / ``checked_in`` / ``completed`` — those are
      workspace-side facts the poller must never overwrite. A guest
      who has checked in stays checked-in even if Airbnb removes the
      VEVENT post-arrival.
    """
    if not seen_uids:
        # Empty feed (or fully-Blocked feed) is ambiguous — could be a
        # mid-rollout outage that returned an empty calendar rather
        # than a 304. Refuse to sweep in that case; the operator UI
        # surfaces the zero-VEVENT count and they can intervene.
        # NOTE: this also keeps a brand-new feed from cancelling
        # nothing on its first tick (no rows yet) cheaply.
        return 0
    candidates = session.scalars(
        select(Reservation)
        .where(Reservation.workspace_id == ctx.workspace_id)
        .where(Reservation.ical_feed_id == feed.id)
        .where(Reservation.source == "ical")
        .where(Reservation.status == "scheduled")
        .where(Reservation.external_uid.notin_(seen_uids))
    ).all()
    cancelled = 0
    for row in candidates:
        row.status = "cancelled"
        session.flush()
        event_bus.publish(
            ReservationUpserted(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=resolved_now,
                reservation_id=row.id,
                feed_id=feed.id,
                change_kind="cancelled",
            )
        )
        cancelled += 1
    return cancelled


def _is_blocked_summary(summary: str | None) -> bool:
    """Return ``True`` if ``summary`` matches a Blocked-pattern.

    Whole-token (not arbitrary-substring) match against
    :data:`_BLOCKED_SUMMARIES`. The summary, lowercased after strip,
    must either:

    * **Equal** a Blocked token verbatim
      (``"Reserved"`` / ``"Blocked"`` / ``"Not available"``), OR
    * **Contain** a Blocked token followed by a member of
      :data:`_BLOCKED_TOKEN_SEPARATORS`
      (``"CLOSED - Not available"`` → starts with ``"closed - "``,
      ``"Blocked: owner stay"`` → starts with ``"blocked: "``).

    Naked-substring matches like ``"Reserved (Jane Doe)"`` (the
    Airbnb / VRBO booked-reservation shape) are **not** flagged
    Blocked — the parenthesised name is a real-booking signal that
    the substring check would have lost.

    A null / empty summary is **not** treated as Blocked — many real
    bookings ride a feed that doesn't supply a summary and we'd
    otherwise stamp them as closures.
    """
    if summary is None:
        return False
    needle = summary.strip().lower()
    if not needle:
        return False
    for token in _BLOCKED_SUMMARIES:
        if needle == token:
            return True
        # Tokens flagged as exact-only (today: "reserved") never match
        # a non-equal summary — the Airbnb "Reserved (Jane Doe)"
        # shape would otherwise mis-trip the Blocked branch on
        # prefix-match.
        if token in _BLOCKED_EXACT_ONLY_TOKENS:
            continue
        if needle.startswith(token):
            after = needle[len(token) : len(token) + 1]
            if after in _BLOCKED_TOKEN_SEPARATORS:
                return True
        # Match where the Blocked token sits *inside* the summary,
        # bracketed by separators on both sides:
        # "Airbnb (Not available)" → contains "(not available)" with
        # a separator before AND after the token.
        idx = needle.find(token)
        if idx > 0:
            before = needle[idx - 1]
            after_idx = idx + len(token)
            after = needle[after_idx : after_idx + 1] if after_idx < len(needle) else ""
            if before in _BLOCKED_TOKEN_SEPARATORS and (
                after == "" or after in _BLOCKED_TOKEN_SEPARATORS
            ):
                return True
    return False


def _upsert_reservation(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed: IcalFeed,
    ev: _ParsedVEvent,
    event_bus: EventBus,
    resolved_now: datetime,
) -> ReservationChangeKind | None:
    """Upsert one Reservation; return ``"created" | "updated" | "cancelled"``.

    Returns ``None`` when the row was already in the target shape
    (no-op re-poll) — the caller does not count or emit.
    """
    existing = session.scalars(
        select(Reservation)
        .where(Reservation.workspace_id == ctx.workspace_id)
        .where(Reservation.ical_feed_id == feed.id)
        .where(Reservation.external_uid == ev.uid)
    ).one_or_none()

    if ev.cancelled:
        if existing is None:
            # A cancellation for a UID we never ingested is a no-op:
            # there's no row to mark cancelled and creating one would
            # land a phantom "this booking was cancelled" record
            # nobody asked for.
            return None
        if existing.status == "cancelled":
            return None
        existing.status = "cancelled"
        session.flush()
        event_bus.publish(
            ReservationUpserted(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=resolved_now,
                reservation_id=existing.id,
                feed_id=feed.id,
                change_kind="cancelled",
            )
        )
        return "cancelled"

    if existing is None:
        row = Reservation(
            id=new_ulid(),
            workspace_id=ctx.workspace_id,
            property_id=feed.property_id,
            ical_feed_id=feed.id,
            external_uid=ev.uid,
            check_in=ev.starts_at,
            check_out=ev.ends_at,
            guest_name=_guess_guest_name(ev.summary, ev.description),
            guest_count=None,
            status="scheduled",
            source="ical",
            raw_summary=ev.summary,
            raw_description=ev.description,
            created_at=resolved_now,
        )
        session.add(row)
        session.flush()
        event_bus.publish(
            ReservationUpserted(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=resolved_now,
                reservation_id=row.id,
                feed_id=feed.id,
                change_kind="created",
            )
        )
        return "created"

    # Existing row — diff and update only the fields that moved. A
    # no-op re-poll skips the event publish.
    new_check_in = ev.starts_at
    new_check_out = ev.ends_at
    new_guest_name = _guess_guest_name(ev.summary, ev.description)
    existing_check_in = _ensure_utc(existing.check_in)
    existing_check_out = _ensure_utc(existing.check_out)
    if (
        existing_check_in == new_check_in
        and existing_check_out == new_check_out
        and existing.raw_summary == ev.summary
        and existing.raw_description == ev.description
        and existing.guest_name == new_guest_name
        and existing.status == "scheduled"
    ):
        return None
    existing.check_in = new_check_in
    existing.check_out = new_check_out
    existing.raw_summary = ev.summary
    existing.raw_description = ev.description
    existing.guest_name = new_guest_name
    if existing.status == "cancelled":
        # Upstream uncancelled — return to scheduled. Other terminal
        # states (``checked_in``, ``completed``) are workspace-side
        # facts the poller must not overwrite.
        existing.status = "scheduled"
    session.flush()
    event_bus.publish(
        ReservationUpserted(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_now,
            reservation_id=existing.id,
            feed_id=feed.id,
            change_kind="updated",
        )
    )
    return "updated"


def _upsert_closure(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed: IcalFeed,
    ev: _ParsedVEvent,
    event_bus: EventBus,
    clock: Clock,
    resolved_now: datetime,
) -> Literal["created", "updated"] | None:
    """Insert or update an iCal-sourced :class:`PropertyClosure`.

    The source UID is the durable identity. A manually deleted closure
    is a tombstone: repeated polls of the same upstream VEVENT refresh
    ``source_last_seen_at`` but do not reopen the row. If a successful
    poll observes the UID absent and a later poll sees it again, the
    upstream has reasserted the block and the row is reopened.
    """
    row = session.scalars(
        select(PropertyClosure)
        .where(PropertyClosure.property_id == feed.property_id)
        .where(PropertyClosure.source_ical_feed_id == feed.id)
        .where(PropertyClosure.source_external_uid == ev.uid)
    ).first()
    if row is None:
        row = session.scalars(
            select(PropertyClosure)
            .where(PropertyClosure.property_id == feed.property_id)
            .where(PropertyClosure.source_ical_feed_id == feed.id)
            .where(PropertyClosure.source_external_uid.is_(None))
            .where(PropertyClosure.starts_at == ev.starts_at)
            .where(PropertyClosure.ends_at == ev.ends_at)
        ).first()

    if row is not None:
        if row.deleted_at is not None:
            if _deleted_closure_reasserted(row, previous_poll_at=feed.last_polled_at):
                row.unit_id = feed.unit_id
                row.starts_at = ev.starts_at
                row.ends_at = ev.ends_at
                row.reason = "ical_unavailable"
                row.source_external_uid = ev.uid
                row.source_last_seen_at = resolved_now
                row.deleted_at = None
                session.flush()
                event_bus.publish(
                    PropertyClosureCreated(
                        workspace_id=ctx.workspace_id,
                        actor_id=ctx.actor_id,
                        correlation_id=ctx.audit_correlation_id,
                        occurred_at=resolved_now,
                        closure_id=row.id,
                        property_id=feed.property_id,
                        starts_at=ev.starts_at,
                        ends_at=ev.ends_at,
                        reason="ical_unavailable",
                        source_ical_feed_id=feed.id,
                    )
                )
                return "created"
            row.source_external_uid = ev.uid
            row.source_last_seen_at = resolved_now
            session.flush()
            return None

        changed = (
            _ensure_utc(row.starts_at) != ev.starts_at
            or _ensure_utc(row.ends_at) != ev.ends_at
            or row.reason != "ical_unavailable"
            or row.unit_id != feed.unit_id
        )
        row.unit_id = feed.unit_id
        row.starts_at = ev.starts_at
        row.ends_at = ev.ends_at
        row.reason = "ical_unavailable"
        row.source_external_uid = ev.uid
        row.source_last_seen_at = resolved_now
        session.flush()
        if changed:
            event_bus.publish(
                PropertyClosureUpdated(
                    workspace_id=ctx.workspace_id,
                    actor_id=ctx.actor_id,
                    correlation_id=ctx.audit_correlation_id,
                    occurred_at=resolved_now,
                    closure_id=row.id,
                    property_id=feed.property_id,
                    starts_at=ev.starts_at,
                    ends_at=ev.ends_at,
                    reason="ical_unavailable",
                    source_ical_feed_id=feed.id,
                )
            )
            return "updated"
        return None

    row = PropertyClosure(
        id=new_ulid(),
        property_id=feed.property_id,
        unit_id=feed.unit_id,
        starts_at=ev.starts_at,
        ends_at=ev.ends_at,
        reason="ical_unavailable",
        source_ical_feed_id=feed.id,
        source_external_uid=ev.uid,
        source_last_seen_at=resolved_now,
        created_by_user_id=None,
        created_at=resolved_now,
    )
    session.add(row)
    session.flush()
    event_bus.publish(
        PropertyClosureCreated(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_now,
            closure_id=row.id,
            property_id=feed.property_id,
            starts_at=ev.starts_at,
            ends_at=ev.ends_at,
            reason="ical_unavailable",
            source_ical_feed_id=feed.id,
        )
    )
    return "created"


def _deleted_closure_reasserted(
    row: PropertyClosure, *, previous_poll_at: datetime | None
) -> bool:
    if previous_poll_at is None or row.source_last_seen_at is None:
        return False
    if row.deleted_at is None:
        return False
    deleted_at = _ensure_utc(row.deleted_at)
    source_last_seen_at = _ensure_utc(row.source_last_seen_at)
    previous_poll_at = _ensure_utc(previous_poll_at)
    return deleted_at <= previous_poll_at and source_last_seen_at < previous_poll_at


def _guess_guest_name(summary: str | None, description: str | None) -> str | None:
    """Best-effort guest name extraction from VEVENT free text.

    iCal summaries vary by provider — Airbnb uses ``Reserved (Jane
    Doe)``, VRBO embeds the name in DESCRIPTION, Booking.com may
    omit it entirely. We do **not** parse aggressively; the spec
    treats the guest name as an optional hint and the welcome-link
    flow re-collects it from the guest directly.

    Today's heuristic: return the summary verbatim **only** if it
    looks like a real name (alphabetic characters + space, no
    Blocked-pattern tokens). Otherwise ``None``. The richer
    provider-specific extraction is the per-provider parser
    (cd-1ai follow-up).
    """
    if summary is None:
        return None
    cleaned = summary.strip()
    if not cleaned or _is_blocked_summary(cleaned):
        return None
    # Reject summaries that look like template strings ("Confirmed
    # for {name}") or status flags. Heuristic: if the cleaned string
    # contains ``{`` / ``}`` or starts with "Confirmed" without a
    # following name it's not useful — but we deliberately do not
    # try to parse the suffix. Conservative: return the verbatim
    # summary so the manager UI shows whatever the upstream
    # provided, and the operator can refine via the manual edit
    # path.
    return cleaned[:200]  # Match the column's natural cap.


# ---------------------------------------------------------------------------
# Error / status helpers
# ---------------------------------------------------------------------------


def _record_feed_error(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed: IcalFeed,
    code: str,
    message: str,
    clock: Clock,
    resolved_now: datetime,
) -> None:
    """Stamp ``last_error`` + ``last_polled_at`` and audit the failure.

    The caller is the per-feed loop body; the audit row anchors on
    the feed (``entity_kind='ical_feed'``, ``action='poll_failed'``)
    rather than the workspace so operators can grep one feed's
    failure history.
    """
    feed.last_polled_at = resolved_now
    feed.last_error = code
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=feed.id,
        action="poll_failed",
        diff={
            "ok": False,
            "error_code": code,
            # ``message`` may carry diagnostic context (a hostname,
            # a TLS error class) but never the URL itself — the
            # caller is responsible for not leaking the secret.
            "error_message": _redact_message(message),
            "polled_at": resolved_now.isoformat(),
        },
        clock=clock,
    )


def _redact_message(message: str) -> str:
    """Cap the audit message length and strip URL-shaped payloads.

    Belt-and-braces: the audit writer's own redactor handles the
    obvious cases, but a poll failure message can interpolate the
    URL via ``str(exc)``; trim aggressively so a leak through the
    audit feed is bounded.
    """
    if not message:
        return ""
    # Strip ``https://...`` URLs to host-only.
    redacted = re.sub(
        r"https?://([^\s/'\"]+)[^\s'\"]*",
        r"https://\1",
        message,
    )
    return redacted[:512]


def _skipped_result(feed_id: str, status: str) -> PolledFeedResult:
    return _zero_count_result(feed_id, status)


def _zero_count_result(feed_id: str, status: str) -> PolledFeedResult:
    return PolledFeedResult(
        feed_id=feed_id,
        status=status,
        error_code=None,
        reservations_created=0,
        reservations_updated=0,
        reservations_cancelled=0,
        closures_created=0,
    )


def _error_result(feed_id: str, code: str) -> PolledFeedResult:
    return PolledFeedResult(
        feed_id=feed_id,
        status=PollOutcome.ERROR,
        error_code=code,
        reservations_created=0,
        reservations_updated=0,
        reservations_cancelled=0,
        closures_created=0,
    )


def _ensure_utc(value: datetime) -> datetime:
    """Narrow a round-tripped ``DateTime(timezone=True)`` to aware UTC.

    SQLite strips tzinfo off ``DateTime(timezone=True)`` columns on
    read; PostgreSQL preserves it. The column is always written as
    aware UTC, so a naive read is a UTC value that has lost its
    zone. Mirror of the helper in :mod:`app.worker.tasks.overdue`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _write_poll_tick_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feeds_walked: int,
    feeds_polled: int,
    feeds_not_modified: int,
    feeds_rate_limited: int,
    feeds_errored: int,
    feeds_skipped: int,
    reservations_created: int,
    reservations_updated: int,
    reservations_cancelled: int,
    closures_created: int,
    tick_started_at: datetime,
    tick_ended_at: datetime,
    clock: Clock,
) -> None:
    """Record one ``stays.poll_ical_tick`` audit row per workspace per tick.

    Mirror of :func:`app.worker.tasks.overdue._write_overdue_tick_audit`
    — anchored on the workspace so operator dashboards plot per-tick
    rates without joining feed history.
    """
    write_audit(
        session,
        ctx,
        entity_kind="workspace",
        entity_id=ctx.workspace_id,
        action="stays.poll_ical_tick",
        diff={
            "feeds_walked": feeds_walked,
            "feeds_polled": feeds_polled,
            "feeds_not_modified": feeds_not_modified,
            "feeds_rate_limited": feeds_rate_limited,
            "feeds_errored": feeds_errored,
            "feeds_skipped": feeds_skipped,
            "reservations_created": reservations_created,
            "reservations_updated": reservations_updated,
            "reservations_cancelled": reservations_cancelled,
            "closures_created": closures_created,
            "tick_started_at": tick_started_at.isoformat(),
            "tick_ended_at": tick_ended_at.isoformat(),
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public exports for test introspection
# ---------------------------------------------------------------------------


# Test-only re-export: tests want to assert against the canonical
# Blocked-pattern set without re-deriving it. Module-private name
# stays the source of truth.
def blocked_summaries() -> tuple[str, ...]:
    """Return the canonical Blocked-pattern substring set."""
    return _BLOCKED_SUMMARIES


# Re-export so callers (scheduler factory, tests) don't have to dig
# into the private helper name.
def is_blocked_summary(summary: str | None) -> bool:
    """Public alias for the Blocked-pattern detector."""
    return _is_blocked_summary(summary)


# Test-only seam used by integration suites that build a fake fetcher
# emitting custom headers; no production caller imports this name.
_PARSE_RETRY_AFTER: Callable[[str | None], int | None] = _parse_retry_after


# Mapping of registry-shaped status → counter slot, exposed so
# the structured-log dashboard can read the slot name without
# duplicating the closed enum. The Mapping shape keeps the dict
# readonly from caller's POV.
STATUS_COUNTERS: Mapping[str, str] = {
    PollOutcome.POLLED: "feeds_polled",
    PollOutcome.NOT_MODIFIED: "feeds_not_modified",
    PollOutcome.RATE_LIMITED: "feeds_rate_limited",
    PollOutcome.ERROR: "feeds_errored",
    PollOutcome.SKIPPED_DISABLED: "feeds_skipped",
    PollOutcome.SKIPPED_NOT_DUE: "feeds_skipped",
}
