"""Unit tests for :mod:`app.worker.tasks.poll_ical` (cd-d48).

Mirrors the in-memory SQLite bootstrap in
``tests/unit/test_tasks_overdue.py``: fresh engine per test, load every
sibling ``models`` module onto the shared metadata, run ``create_all``,
drive the service with :class:`FrozenClock`, an in-memory event bus,
and a :class:`FakeEnvelope` so URLs round-trip without crypto.

Covers the cd-d48 acceptance criteria:

* :class:`PollReport` shape: every documented field is populated.
* Happy path: a Booked-pattern VEVENT lands a fresh
  :class:`Reservation`, fires :class:`ReservationUpserted` with
  ``change_kind="created"``, stamps ``feed.last_polled_at`` /
  ``last_etag``, clears ``last_error``.
* Idempotent re-poll: a second tick over the same feed body emits no
  events, mutates no rows.
* Update path: a same-UID VEVENT with a moved DTSTART fires
  :class:`ReservationUpserted` with ``change_kind="updated"``.
* Cancellation: ``STATUS:CANCELLED`` flips a previously-seen UID to
  ``status="cancelled"`` + ``change_kind="cancelled"``; a never-seen
  cancelled UID is a no-op (no phantom row).
* Blocked SUMMARY: every Airbnb / VRBO / Google variant lands a
  :class:`PropertyClosure` with ``reason="ical_unavailable"`` +
  ``source_ical_feed_id`` set, fires
  :class:`PropertyClosureCreated`.
* Closure idempotency: re-poll over the same Blocked window writes
  one row (not two).
* Parse failure: a malformed envelope writes ``last_error =
  'ical_parse_error'``, does not poison the loop, audit row landed.
* 304 Not Modified: body skipped, ``last_polled_at`` bumped,
  ``last_etag`` preserved.
* 429 Rate-Limited: ``last_error = 'rate_limited'``, no body parse.
* Per-host rate-limit gap: two feeds against the same host inside
  one tick — second skips with status ``rate_limited``.
* Disabled feed: skipped without a fetch.
* Cadence guard: a feed polled inside the cadence window is skipped.
* Workspace scoping: a tick on workspace A does not touch workspace
  B's feeds, even if B's feed is due.
* Audit shape: per-tick + per-failure rows landed at the documented
  ``entity_kind`` / ``action`` pair.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed" §"Polling
behavior".
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import SplitResult

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyClosure, Unit
from app.adapters.db.session import make_engine
from app.adapters.db.stays.models import IcalFeed, Reservation
from app.adapters.db.workspace.models import Workspace
from app.adapters.ical.ports import IcalValidationError
from app.adapters.ical.validator import Fetcher, FetchResponse
from app.events.bus import EventBus
from app.events.types import (
    PropertyClosureCreated,
    PropertyClosureUpdated,
    ReservationUpserted,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.poll_ical import (
    DEFAULT_PER_HOST_RATE_LIMIT_SECONDS,
    PollOutcome,
    PollReport,
    fetch_ical_body,
    poll_ical,
)
from tests._fakes.envelope import FakeEnvelope

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_FEED_URL = "https://calendar.example.com/feed.ics"
_OTHER_URL = "https://calendar.example.com/other.ics"
# The validator's :func:`is_public_ip` rejects every documentation /
# private / reserved range. Pick a real-public IP we know is global.
_FAKE_IP = "8.8.8.8"


def _aware(value: datetime | None) -> datetime | None:
    """Promote a SQLite-stripped naive datetime to aware UTC.

    SQLite drops tzinfo on round-trip through
    ``DateTime(timezone=True)``; PostgreSQL preserves it. This helper
    makes assertions backend-agnostic.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Bootstrap (mirrors tests/unit/test_tasks_overdue.py)
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


@pytest.fixture
def envelope() -> FakeEnvelope:
    return FakeEnvelope()


def _ctx(
    workspace_id: str,
    *,
    slug: str = "ws",
    role: str = "manager",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=new_ulid(),
        actor_kind="user",
        actor_grant_role=role,  # type: ignore[arg-type]
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def _bootstrap_workspace(session: Session, *, slug: str = "ws") -> str:
    ws_id = new_ulid()
    session.add(
        Workspace(
            id=ws_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return ws_id


def _bootstrap_property(session: Session) -> str:
    pid = new_ulid()
    session.add(
        Property(
            id=pid,
            address="1 Villa Sud Way",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return pid


def _bootstrap_unit(session: Session, *, property_id: str, name: str) -> str:
    uid = new_ulid()
    session.add(
        Unit(
            id=uid,
            property_id=property_id,
            name=name,
            ordinal=0,
            default_checkin_time=None,
            default_checkout_time=None,
            max_guests=None,
            welcome_overrides_json={},
            settings_override_json={},
            notes_md="",
            label=name,
            type=None,
            capacity=1,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    session.flush()
    return uid


def _bootstrap_feed(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    envelope: FakeEnvelope,
    unit_id: str | None = None,
    url: str = _FEED_URL,
    enabled: bool = True,
    last_polled_at: datetime | None = None,
    last_etag: str | None = None,
) -> str:
    """Insert one ``ical_feed`` row with envelope-encrypted URL.

    The poller's only legal plaintext reach is
    :func:`ical_service.get_plaintext_url`, which decrypts the column;
    we mirror the encryption shape here so that path round-trips.
    """
    fid = new_ulid()
    ciphertext = envelope.encrypt(url.encode("utf-8"), purpose="ical-feed-url")
    session.add(
        IcalFeed(
            id=fid,
            workspace_id=workspace_id,
            property_id=property_id,
            unit_id=unit_id,
            url=ciphertext.decode("latin-1"),
            provider="custom",
            poll_cadence="*/15 * * * *",
            last_polled_at=last_polled_at,
            last_etag=last_etag,
            last_error=None,
            enabled=enabled,
            created_at=_PINNED,
        )
    )
    session.flush()
    return fid


# ---------------------------------------------------------------------------
# Fetcher fakes
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedFetcher(Fetcher):
    """Table-driven :class:`Fetcher` stub keyed by URL.

    ``responses`` maps a URL → list of canned :class:`FetchResponse`
    objects consumed in order. Tests that need conditional GET
    behaviour can scrutinise ``calls`` for the inbound URL list.
    """

    responses: dict[str, list[FetchResponse]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def fetch(
        self,
        parsed: SplitResult,
        resolved_ip: str,
        *,
        deadline: float,
        max_body_bytes: int,
    ) -> FetchResponse:
        url = parsed.geturl()
        self.calls.append((url, resolved_ip))
        bucket = self.responses.get(url)
        if not bucket:
            raise AssertionError(f"_ScriptedFetcher: no canned response for {url!r}")
        return bucket.pop(0)


def _fixed_resolver(
    addresses: list[str],
) -> object:
    """Return a callable matching :class:`Resolver` (host, port → list)."""

    def _resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return _resolve


def _ok(
    body: bytes,
    *,
    status: int = 200,
    content_type: str | None = "text/calendar",
    etag: str | None = None,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> FetchResponse:
    """Build a 200 :class:`FetchResponse` carrying ``body``."""
    headers: list[tuple[str, str]] = []
    if content_type is not None:
        headers.append(("Content-Type", content_type))
    if etag is not None:
        headers.append(("ETag", etag))
    headers.extend(extra_headers)
    return FetchResponse(status=status, headers=tuple(headers), body=body)


def _not_modified(*, etag: str | None = None) -> FetchResponse:
    headers: tuple[tuple[str, str], ...] = ()
    if etag is not None:
        headers = (("ETag", etag),)
    return FetchResponse(status=304, headers=headers, body=b"")


def _rate_limited(*, retry_after: int | None = None) -> FetchResponse:
    headers: tuple[tuple[str, str], ...] = ()
    if retry_after is not None:
        headers = (("Retry-After", str(retry_after)),)
    return FetchResponse(status=429, headers=headers, body=b"")


# ---------------------------------------------------------------------------
# VCALENDAR fixtures
# ---------------------------------------------------------------------------


def _vcalendar(*events: str) -> bytes:
    """Wrap one or more VEVENT bodies in a VCALENDAR envelope."""
    inner = "\r\n".join(events)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//crewday-test//EN\r\n"
        f"{inner}\r\n"
        "END:VCALENDAR\r\n"
    ).encode()


def _vevent_booked(
    *,
    uid: str,
    starts: datetime,
    ends: datetime,
    summary: str = "Reserved (Jane Doe)",
    cancelled: bool = False,
) -> str:
    parts = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART:{starts.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{ends.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{summary}",
    ]
    if cancelled:
        parts.append("STATUS:CANCELLED")
    parts.append("END:VEVENT")
    return "\r\n".join(parts)


def _vevent_blocked(
    *,
    uid: str,
    starts: datetime,
    ends: datetime,
    summary: str = "Not available",
) -> str:
    return "\r\n".join(
        [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART:{starts.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{ends.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{summary}",
            "END:VEVENT",
        ]
    )


def _record_reservation(bus: EventBus) -> list[ReservationUpserted]:
    captured: list[ReservationUpserted] = []
    bus.subscribe(ReservationUpserted)(captured.append)
    return captured


def _record_closure(bus: EventBus) -> list[PropertyClosureCreated]:
    captured: list[PropertyClosureCreated] = []
    bus.subscribe(PropertyClosureCreated)(captured.append)
    return captured


def _record_closure_updates(bus: EventBus) -> list[PropertyClosureUpdated]:
    captured: list[PropertyClosureUpdated] = []
    bus.subscribe(PropertyClosureUpdated)(captured.append)
    return captured


# ---------------------------------------------------------------------------
# PollReport shape + happy path
# ---------------------------------------------------------------------------


class TestEmptyTick:
    """A workspace with no feeds returns a zero-shaped report."""

    def test_no_feeds(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=_ScriptedFetcher(),
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        assert isinstance(report, PollReport)
        assert report.feeds_walked == 0
        assert report.feeds_polled == 0
        assert report.reservations_created == 0
        assert report.closures_created == 0
        assert report.per_feed_results == ()


class TestHappyPathBooking:
    """A Booked-pattern VEVENT lands a Reservation + fires the event."""

    def test_creates_reservation(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session, workspace_id=ws, property_id=prop, envelope=envelope
        )
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        body = _vcalendar(
            _vevent_booked(uid="abc-123", starts=starts, ends=ends),
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body, etag='W/"v1"')]},
        )
        captured = _record_reservation(bus)

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.feeds_walked == 1
        assert report.feeds_polled == 1
        assert report.reservations_created == 1
        assert report.reservations_updated == 0
        assert report.closures_created == 0

        rows = list(session.scalars(select(Reservation)).all())
        assert len(rows) == 1
        assert rows[0].external_uid == "abc-123"
        assert rows[0].ical_feed_id == feed_id
        assert rows[0].status == "scheduled"
        assert rows[0].source == "ical"

        feed = session.scalars(select(IcalFeed)).one()
        assert _aware(feed.last_polled_at) == _PINNED
        assert feed.last_etag == 'W/"v1"'
        assert feed.last_error is None

        assert len(captured) == 1
        assert captured[0].change_kind == "created"
        assert captured[0].feed_id == feed_id
        assert captured[0].reservation_id == rows[0].id


class TestIdempotentRepoll:
    """A second tick over the same body emits no events, mutates no rows."""

    def test_idempotent(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        body = _vcalendar(
            _vevent_booked(uid="abc-123", starts=starts, ends=ends),
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body), _ok(body)]},
        )
        captured = _record_reservation(bus)

        # First tick — creates the row.
        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        # Move the clock forward past the cadence window so the second
        # tick is due — the bootstrap stamp on first poll set
        # ``last_polled_at = _PINNED``.
        clock.set(_PINNED + timedelta(minutes=20))

        # Second tick — same body. Should be a no-op.
        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.reservations_created == 0
        assert report.reservations_updated == 0
        assert len(captured) == 1  # still just the first.
        rows = list(session.scalars(select(Reservation)).all())
        assert len(rows) == 1


class TestUpdate:
    """A same-UID VEVENT with moved DTSTART fires ``change_kind='updated'``."""

    def test_updates_on_diff(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        starts_a = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends_a = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        starts_b = datetime(2026, 5, 2, 14, 0, tzinfo=UTC)  # one day later
        ends_b = datetime(2026, 5, 5, 11, 0, tzinfo=UTC)
        body_a = _vcalendar(_vevent_booked(uid="x", starts=starts_a, ends=ends_a))
        body_b = _vcalendar(_vevent_booked(uid="x", starts=starts_b, ends=ends_b))
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body_a), _ok(body_b)]},
        )
        captured = _record_reservation(bus)

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        clock.set(_PINNED + timedelta(minutes=20))
        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.reservations_updated == 1
        assert len(captured) == 2
        assert captured[0].change_kind == "created"
        assert captured[1].change_kind == "updated"


class TestCancellation:
    """``STATUS:CANCELLED`` flips the reservation; never-seen UID is a no-op."""

    def test_cancels_existing(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        body_a = _vcalendar(_vevent_booked(uid="x", starts=starts, ends=ends))
        body_b = _vcalendar(
            _vevent_booked(uid="x", starts=starts, ends=ends, cancelled=True)
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body_a), _ok(body_b)]},
        )
        captured = _record_reservation(bus)

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        clock.set(_PINNED + timedelta(minutes=20))
        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.reservations_cancelled == 1
        rows = list(session.scalars(select(Reservation)).all())
        assert len(rows) == 1
        assert rows[0].status == "cancelled"
        assert captured[-1].change_kind == "cancelled"

    def test_never_seen_cancellation_is_noop(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        body = _vcalendar(
            _vevent_booked(uid="never", starts=starts, ends=ends, cancelled=True)
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body)]},
        )
        captured = _record_reservation(bus)

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.reservations_cancelled == 0
        assert report.reservations_created == 0
        rows = list(session.scalars(select(Reservation)).all())
        assert rows == []
        assert captured == []


class TestDisappearedReservationCancelled:
    """A previously-seen Booking VEVENT that vanishes from the feed flips to cancelled.

    Many PMSes (Airbnb pre-2024, generic Booking.com ICS) signal a
    cancellation by *removing* the VEVENT rather than emitting
    ``STATUS:CANCELLED``. The poller must treat absence-of-UID as a
    cancellation signal — see ``docs/specs/04-properties-and-stays.md``
    §"iCal feed" §"Polling behavior".
    """

    def test_missing_uid_flips_status(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        # Tick #1: feed contains the booking. Tick #2: feed contains a
        # different booking; the original UID has disappeared.
        body_a = _vcalendar(_vevent_booked(uid="gone", starts=starts, ends=ends))
        body_b = _vcalendar(_vevent_booked(uid="other", starts=starts, ends=ends))
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body_a), _ok(body_b)]},
        )
        captured = _record_reservation(bus)

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        clock.set(_PINNED + timedelta(minutes=20))
        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        # One created (the new "other"), one cancelled (the gone "gone").
        assert report.reservations_created == 1
        assert report.reservations_cancelled == 1
        rows = {
            r.external_uid: r.status for r in session.scalars(select(Reservation)).all()
        }
        assert rows == {"gone": "cancelled", "other": "scheduled"}

        # Most recent event for "gone" must be the cancellation.
        cancel_events = [e for e in captured if e.change_kind == "cancelled"]
        assert len(cancel_events) == 1

    def test_terminal_lifecycle_states_preserved(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """``checked_in`` / ``completed`` rows are not swept by the disappearance path.

        A guest who has physically arrived stays ``checked_in`` even
        if the upstream feed loses the VEVENT — that's a workspace-side
        fact, not an iCal-side fact.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session, workspace_id=ws, property_id=prop, envelope=envelope
        )
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        # Pre-seed a checked-in row whose UID won't appear in the feed.
        session.add(
            Reservation(
                id=new_ulid(),
                workspace_id=ws,
                property_id=prop,
                ical_feed_id=feed_id,
                external_uid="checked-in-uid",
                check_in=starts,
                check_out=ends,
                guest_name="Already Here",
                status="checked_in",
                source="ical",
                raw_summary=None,
                raw_description=None,
                created_at=_PINNED,
            )
        )
        session.flush()
        body = _vcalendar(_vevent_booked(uid="other", starts=starts, ends=ends))
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)]})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.reservations_cancelled == 0
        kept = session.scalars(
            select(Reservation).where(Reservation.external_uid == "checked-in-uid")
        ).one()
        assert kept.status == "checked_in"

    def test_empty_feed_is_not_a_mass_cancel(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """A 200 with zero VEVENTs does not sweep — could be an upstream outage."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        body_a = _vcalendar(_vevent_booked(uid="keep", starts=starts, ends=ends))
        body_empty = _vcalendar()  # zero VEVENTs
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body_a), _ok(body_empty)]},
        )

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        clock.set(_PINNED + timedelta(minutes=20))
        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.reservations_cancelled == 0
        kept = session.scalars(
            select(Reservation).where(Reservation.external_uid == "keep")
        ).one()
        assert kept.status == "scheduled"


# ---------------------------------------------------------------------------
# Closures
# ---------------------------------------------------------------------------


class TestBlockedSummaryToClosure:
    """A Blocked-pattern SUMMARY lands a closure with the source feed link."""

    @pytest.mark.parametrize(
        "summary",
        [
            "Not available",
            "CLOSED - Not available",
            "Blocked",
            "Reserved",  # generic Booking.com / Google
            "Airbnb (Not available)",
            # Mixed-case variants pin the case-insensitive matcher: an
            # upstream that suddenly screams "NOT AVAILABLE" or
            # "blocked" must still trip the closure path.
            "NOT AVAILABLE",
            "blocked",
            "BLOCKED: owner stay",
        ],
    )
    def test_blocked_pattern_lands_closure(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
        summary: str,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session, workspace_id=ws, property_id=prop, envelope=envelope
        )
        starts = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 8, 0, 0, tzinfo=UTC)
        body = _vcalendar(
            _vevent_blocked(
                uid=f"b-{summary}",
                starts=starts,
                ends=ends,
                summary=summary,
            ),
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body)]},
        )
        captured = _record_closure(bus)

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.closures_created == 1
        assert report.reservations_created == 0
        rows = list(session.scalars(select(PropertyClosure)).all())
        assert len(rows) == 1
        row = rows[0]
        assert row.property_id == prop
        assert row.reason == "ical_unavailable"
        assert row.source_ical_feed_id == feed_id
        assert row.source_external_uid == f"b-{summary}"
        assert row.source_last_seen_at is not None
        assert row.source_last_seen_at.replace(tzinfo=UTC) == _PINNED

        assert len(captured) == 1
        assert captured[0].closure_id == row.id
        assert captured[0].source_ical_feed_id == feed_id
        assert captured[0].starts_at == starts
        assert captured[0].ends_at == ends
        assert captured[0].reason == "ical_unavailable"


class TestClosureIdempotency:
    """Re-poll over the same Blocked window writes one row, not two."""

    def test_dedup(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        starts = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 8, 0, 0, tzinfo=UTC)
        body = _vcalendar(
            _vevent_blocked(uid="blocked-x", starts=starts, ends=ends),
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body), _ok(body)]},
        )
        captured = _record_closure(bus)

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        clock.set(_PINNED + timedelta(minutes=20))
        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.closures_created == 0  # second tick is a dedup
        rows = list(session.scalars(select(PropertyClosure)).all())
        assert len(rows) == 1
        assert len(captured) == 1

    def test_manual_delete_is_sticky_until_upstream_reasserts(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        starts = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 8, 0, 0, tzinfo=UTC)
        blocked = _vcalendar(
            _vevent_blocked(uid="blocked-x", starts=starts, ends=ends),
        )
        empty = _vcalendar()
        fetcher = _ScriptedFetcher(
            responses={
                _FEED_URL: [_ok(blocked), _ok(blocked), _ok(empty), _ok(blocked)]
            },
        )
        created = _record_closure(bus)
        updated = _record_closure_updates(bus)

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        row = session.scalars(select(PropertyClosure)).one()
        row.deleted_at = _PINNED + timedelta(minutes=5)
        session.flush()

        clock.set(_PINNED + timedelta(minutes=20))
        suppressed = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert suppressed.closures_created == 0
        assert row.deleted_at is not None
        assert len(created) == 1
        assert updated == []

        clock.set(_PINNED + timedelta(minutes=40))
        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        assert row.deleted_at is not None

        clock.set(_PINNED + timedelta(minutes=60))
        reasserted = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert reasserted.closures_created == 1
        assert row.deleted_at is None
        assert row.source_external_uid == "blocked-x"
        assert len(created) == 2
        assert updated == []

    def test_reasserted_closure_keeps_feed_unit_scope(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        unit_id = _bootstrap_unit(session, property_id=prop, name="Villa Sud")
        _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            unit_id=unit_id,
            envelope=envelope,
        )
        starts = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 8, 0, 0, tzinfo=UTC)
        blocked = _vcalendar(
            _vevent_blocked(uid="blocked-x", starts=starts, ends=ends),
        )
        empty = _vcalendar()
        fetcher = _ScriptedFetcher(
            responses={
                _FEED_URL: [_ok(blocked), _ok(blocked), _ok(empty), _ok(blocked)]
            },
        )

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        row = session.scalars(select(PropertyClosure)).one()
        row.unit_id = None
        row.deleted_at = _PINNED + timedelta(minutes=5)
        session.flush()

        clock.set(_PINNED + timedelta(minutes=20))
        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        clock.set(_PINNED + timedelta(minutes=40))
        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )
        clock.set(_PINNED + timedelta(minutes=60))
        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert row.deleted_at is None
        assert row.unit_id == unit_id


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestParseFailure:
    """A malformed envelope writes ``last_error='ical_parse_error'``."""

    def test_parse_error_writes_last_error(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session, workspace_id=ws, property_id=prop, envelope=envelope
        )
        garbage = b"BEGIN:VCALENDAR\r\nthis is not actually an iCal\r\n"
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(garbage)]},
        )

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.feeds_errored == 1
        assert report.per_feed_results[0].error_code == "ical_parse_error"
        feed = session.scalars(select(IcalFeed).where(IcalFeed.id == feed_id)).one()
        assert feed.last_error == "ical_parse_error"

        # Audit row landed with the documented action vocabulary.
        rows = list(
            session.scalars(
                select(AuditLog).where(AuditLog.entity_kind == "ical_feed")
            ).all()
        )
        assert any(r.action == "poll_failed" for r in rows)


class TestParseFailureDoesNotPoisonLoop:
    """A bad feed records its error; the next feed still polls cleanly."""

    def test_loop_continues(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        bad_id = _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            url=_FEED_URL,
        )
        good_id = _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            url=_OTHER_URL,
        )
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        good_body = _vcalendar(
            _vevent_booked(uid="good-1", starts=starts, ends=ends),
        )
        fetcher = _ScriptedFetcher(
            responses={
                _FEED_URL: [_ok(b"BEGIN:VCALENDAR\r\nbroken")],
                _OTHER_URL: [_ok(good_body)],
            },
        )

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
            # Override the per-host gap so two feeds against the same
            # host inside one tick both fire — the second-feed test
            # covers the gap path.
            rate_limit_seconds=0.0,
        )

        assert report.feeds_errored == 1
        assert report.feeds_polled == 1
        assert report.reservations_created == 1
        feeds = {f.id: f for f in session.scalars(select(IcalFeed)).all()}
        assert feeds[bad_id].last_error == "ical_parse_error"
        assert feeds[good_id].last_error is None


class TestNotModified:
    """A 304 response bumps last_polled_at, preserves last_etag, parses no body."""

    def test_304(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            last_polled_at=_PINNED - timedelta(hours=1),
            last_etag='W/"prev"',
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_not_modified()]},
        )
        captured = _record_reservation(bus)

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.feeds_not_modified == 1
        assert report.feeds_polled == 0
        assert report.reservations_created == 0
        assert captured == []

        feed = session.scalars(select(IcalFeed).where(IcalFeed.id == feed_id)).one()
        # ``last_polled_at`` round-trips through SQLite's
        # ``DateTime(timezone=True)`` as a naive value; compare
        # wall-clocks rather than aware vs. naive.
        assert feed.last_polled_at is not None
        feed_polled_at = feed.last_polled_at
        if feed_polled_at.tzinfo is None:
            feed_polled_at = feed_polled_at.replace(tzinfo=UTC)
        assert feed_polled_at == _PINNED
        assert feed.last_etag == 'W/"prev"'


class TestRateLimited:
    """A 429 stamps ``last_error='rate_limited'``, no parse."""

    def test_429(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session, workspace_id=ws, property_id=prop, envelope=envelope
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_rate_limited(retry_after=60)]},
        )

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.feeds_rate_limited == 1
        assert report.feeds_polled == 0
        feed = session.scalars(select(IcalFeed).where(IcalFeed.id == feed_id)).one()
        assert feed.last_error == "rate_limited"

    def test_retry_after_longer_than_cadence_pushes_last_polled_at(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """A ``Retry-After`` longer than the cadence skips the next tick(s).

        Cadence is 15 min; with ``Retry-After: 3600`` (1 h) the feed
        must wait roughly an hour before the next poll, not the
        default 15 min — otherwise a ``429`` storm just keeps
        re-firing.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session, workspace_id=ws, property_id=prop, envelope=envelope
        )
        cadence_seconds = 15 * 60
        retry_after = 3600  # 1 h, well above cadence
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_rate_limited(retry_after=retry_after)]},
        )

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        feed = session.scalars(select(IcalFeed).where(IcalFeed.id == feed_id)).one()
        # last_polled_at = now + (retry_after - cadence) so that
        # last_polled_at + cadence == now + retry_after.
        expected = _PINNED + timedelta(seconds=retry_after - cadence_seconds)
        assert _aware(feed.last_polled_at) == expected

    def test_retry_after_shorter_than_cadence_keeps_floor(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """``Retry-After: 60`` doesn't *shorten* the cadence floor.

        Cadence is the spec-set minimum gap between polls. A
        cooperatively-short Retry-After is honored implicitly (the
        feed waits one full cadence anyway) — the poller never polls
        more aggressively than the cadence floor.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session, workspace_id=ws, property_id=prop, envelope=envelope
        )
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_rate_limited(retry_after=60)]},
        )

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        feed = session.scalars(select(IcalFeed).where(IcalFeed.id == feed_id)).one()
        assert _aware(feed.last_polled_at) == _PINNED


# ---------------------------------------------------------------------------
# Per-host rate limit
# ---------------------------------------------------------------------------


class TestPerHostRateLimit:
    """Two feeds on the same host inside one tick: second skips."""

    def test_same_host_second_feed_skipped(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            url=_FEED_URL,
        )
        _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            url=_OTHER_URL,
        )
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        good_body = _vcalendar(
            _vevent_booked(uid="r-1", starts=starts, ends=ends),
        )
        fetcher = _ScriptedFetcher(
            responses={
                _FEED_URL: [_ok(good_body)],
                # No second response: if the per-host gap is broken
                # and this fetcher is hit, the assertion in the stub
                # raises "no canned response".
                _OTHER_URL: [],
            },
        )

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
            # Use the production default — no explicit override —
            # so the in-tick gap actually engages.
            rate_limit_seconds=DEFAULT_PER_HOST_RATE_LIMIT_SECONDS,
        )

        # First feed succeeds; second is throttled.
        assert report.feeds_polled == 1
        assert report.feeds_rate_limited == 1
        assert len(fetcher.calls) == 1


# ---------------------------------------------------------------------------
# Disabled / cadence guards
# ---------------------------------------------------------------------------


class TestDisabledFeedSkipped:
    """``enabled=False`` short-circuits before fetch."""

    def test_disabled(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            enabled=False,
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: []})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.feeds_walked == 1
        assert report.feeds_skipped == 1
        assert fetcher.calls == []


class TestCadenceGuard:
    """A feed polled inside the cadence window is skipped."""

    def test_skipped_not_due(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        # Polled 1 minute ago — well inside the 15 min default cadence.
        _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            last_polled_at=_PINNED - timedelta(minutes=1),
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: []})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.feeds_walked == 1
        assert report.feeds_skipped == 1
        assert fetcher.calls == []
        assert report.per_feed_results[0].status == PollOutcome.SKIPPED_NOT_DUE


class TestForceBypassesCadence:
    """``force=True`` (cd-jk6is) bypasses the cadence guard but not the
    disabled gate. Backs the manual ``/poll-once`` route's contract."""

    def test_force_polls_inside_cadence_window(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        # Polled 1 minute ago — without ``force`` the feed would skip.
        _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            last_polled_at=_PINNED - timedelta(minutes=1),
        )
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        body = _vcalendar(_vevent_booked(uid="force-1", starts=starts, ends=ends))
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)]})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
            force=True,
        )

        # The feed polled — cadence was bypassed.
        assert report.feeds_polled == 1
        assert report.feeds_skipped == 0
        assert report.per_feed_results[0].status == PollOutcome.POLLED
        assert len(fetcher.calls) == 1

    def test_force_does_not_bypass_disabled_gate(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            enabled=False,
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: []})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
            force=True,
        )

        # Disabled feed still skips even with ``force``.
        assert report.feeds_skipped == 1
        assert fetcher.calls == []
        assert report.per_feed_results[0].status == PollOutcome.SKIPPED_DISABLED


class TestFeedIdsScope:
    """``feed_ids=`` (cd-jk6is) narrows the workspace SELECT.

    Used by the manual ``/poll-once`` route to ingest one specific
    feed without walking the rest of the workspace.
    """

    def test_feed_ids_filters_to_subset(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        target_feed_id = _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            url=_FEED_URL,
        )
        other_feed_id = _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
            url=_OTHER_URL,
        )
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        body = _vcalendar(
            _vevent_booked(uid="filter-1", starts=starts, ends=ends),
        )
        # Only the target URL gets a canned response — if the filter
        # leaks, the other-feed fetch raises "no canned response".
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)], _OTHER_URL: []})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
            feed_ids=frozenset({target_feed_id}),
        )

        assert report.feeds_walked == 1
        assert report.feeds_polled == 1
        assert report.per_feed_results[0].feed_id == target_feed_id
        # The other feed is not in the report at all.
        assert all(r.feed_id != other_feed_id for r in report.per_feed_results)
        assert len(fetcher.calls) == 1

    def test_feed_ids_empty_walks_zero_feeds(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """Defensive: an empty ``feed_ids`` set walks no feeds.

        This is the route's behaviour when the pre-check 404 has
        already short-circuited; ``feed_ids=frozenset()`` would not
        match any row. Make sure the walker handles it gracefully.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(
            session,
            workspace_id=ws,
            property_id=prop,
            envelope=envelope,
        )
        fetcher = _ScriptedFetcher(responses={})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
            feed_ids=frozenset(),
        )

        assert report.feeds_walked == 0
        assert report.per_feed_results == ()


# ---------------------------------------------------------------------------
# Workspace scoping
# ---------------------------------------------------------------------------


class TestWorkspaceScoping:
    """A tick on workspace A leaves workspace B's feeds untouched."""

    def test_scoping(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="a")
        ws_b = _bootstrap_workspace(session, slug="b")
        prop_a = _bootstrap_property(session)
        prop_b = _bootstrap_property(session)
        _bootstrap_feed(
            session,
            workspace_id=ws_a,
            property_id=prop_a,
            envelope=envelope,
            url=_FEED_URL,
        )
        feed_b_id = _bootstrap_feed(
            session,
            workspace_id=ws_b,
            property_id=prop_b,
            envelope=envelope,
            url=_OTHER_URL,
        )
        starts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        ends = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        body = _vcalendar(_vevent_booked(uid="x", starts=starts, ends=ends))
        # Only ``_FEED_URL`` should be fetched on the ws_a tick.
        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok(body)], _OTHER_URL: []},
        )

        report = poll_ical(
            _ctx(ws_a, slug="a"),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        assert report.feeds_walked == 1  # only ws_a's feed
        assert report.feeds_polled == 1

        # ws_b's feed must remain untouched.
        feed_b = session.scalars(select(IcalFeed).where(IcalFeed.id == feed_b_id)).one()
        assert feed_b.last_polled_at is None
        assert feed_b.last_error is None


# ---------------------------------------------------------------------------
# Audit shape
# ---------------------------------------------------------------------------


class TestTickAudit:
    """A clean tick lands a workspace-anchored ``stays.poll_ical_tick`` row."""

    def test_per_tick_audit(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        body = _vcalendar(
            _vevent_booked(
                uid="audit-1",
                starts=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                ends=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
            )
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)]})

        poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver([_FAKE_IP]),  # type: ignore[arg-type]
        )

        rows = list(
            session.scalars(
                select(AuditLog).where(AuditLog.action == "stays.poll_ical_tick")
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].entity_kind == "workspace"
        assert rows[0].entity_id == ws


# ---------------------------------------------------------------------------
# allow_private_addresses gate (cd-xr652)
# ---------------------------------------------------------------------------


class TestFetchIcalBodyAllowPrivateAddresses:
    """``fetch_ical_body``'s ``allow_private_addresses`` gate (cd-xr652).

    The validator side of the §04 SSRF carve-out flows through the
    registration handler; the worker side flows here. Without this
    flag threaded into :func:`fetch_ical_body`, a feed that registers
    successfully against a loopback ICS server (because the e2e
    compose stack flips ``CREWDAY_ICAL_ALLOW_PRIVATE_ADDRESSES=1``)
    would still be rejected on the very next poll tick with
    ``ical_url_private_address``, blocking GA journey 3 (cd-zxvk).

    Default ``False`` — loopback / RFC 1918 / link-local raise
    ``ical_url_private_address``. Flipping the gate to ``True`` lets
    those resolutions through; every other guard (scheme, body cap,
    DNS rebind pin) still applies.
    """

    def test_default_rejects_loopback(self) -> None:
        """Knob OFF (default): loopback resolution still rejected."""
        body = _vcalendar(
            _vevent_booked(
                uid="probe",
                starts=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                ends=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
            )
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)]})
        with pytest.raises(IcalValidationError) as exc_info:
            fetch_ical_body(
                _FEED_URL,
                last_etag=None,
                deadline=time.monotonic() + 5.0,
                fetcher=fetcher,
                resolver=_fixed_resolver(["127.0.0.1"]),  # type: ignore[arg-type]
            )
        assert exc_info.value.code == "ical_url_private_address"
        # The fetcher must NOT have been called — the SSRF gate trips
        # before we open a TCP connection.
        assert fetcher.calls == []

    def test_knob_on_accepts_loopback(self) -> None:
        """Knob ON: loopback resolution passes; the fetcher runs."""
        body = _vcalendar(
            _vevent_booked(
                uid="probe",
                starts=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                ends=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
            )
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)]})
        polled = fetch_ical_body(
            _FEED_URL,
            last_etag=None,
            deadline=time.monotonic() + 5.0,
            fetcher=fetcher,
            resolver=_fixed_resolver(["127.0.0.1"]),  # type: ignore[arg-type]
            allow_private_addresses=True,
        )
        assert polled.status == 200
        assert polled.body == body
        # Fetcher saw exactly the loopback IP we resolved to — proves
        # the gate let the address through to the pinned-IP TCP path.
        assert fetcher.calls == [(_FEED_URL, "127.0.0.1")]

    def test_knob_on_accepts_rfc1918(self) -> None:
        """Knob ON also covers the RFC 1918 ranges, not just loopback."""
        body = _vcalendar(
            _vevent_booked(
                uid="probe",
                starts=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                ends=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
            )
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)]})
        polled = fetch_ical_body(
            _FEED_URL,
            last_etag=None,
            deadline=time.monotonic() + 5.0,
            fetcher=fetcher,
            resolver=_fixed_resolver(["10.0.0.5"]),  # type: ignore[arg-type]
            allow_private_addresses=True,
        )
        assert polled.status == 200
        assert fetcher.calls == [(_FEED_URL, "10.0.0.5")]

    def test_knob_on_still_rejects_non_https(self) -> None:
        """Knob ON does NOT loosen the scheme gate."""
        fetcher = _ScriptedFetcher(responses={})
        with pytest.raises(IcalValidationError) as exc_info:
            fetch_ical_body(
                "http://calendar.example.com/feed.ics",
                last_etag=None,
                deadline=time.monotonic() + 5.0,
                fetcher=fetcher,
                resolver=_fixed_resolver(["127.0.0.1"]),  # type: ignore[arg-type]
                allow_private_addresses=True,
            )
        assert exc_info.value.code == "ical_url_insecure_scheme"
        assert fetcher.calls == []


class TestPollIcalAllowPrivateAddresses:
    """End-to-end thread-through: ``poll_ical`` → ``fetch_ical_body``.

    Confirms the workspace-level ``poll_ical`` driver forwards the
    gate to the per-feed fetch path. Without this thread-through, the
    fan-out body in ``app/worker/jobs/stays.py`` would read the
    ``Settings.ical_allow_private_addresses`` knob but the value
    would never reach :func:`fetch_ical_body`.
    """

    def test_default_records_private_address_error(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """Knob OFF (default): a loopback feed lands as ``last_error``."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        feed_id = _bootstrap_feed(
            session, workspace_id=ws, property_id=prop, envelope=envelope
        )
        body = _vcalendar(
            _vevent_booked(
                uid="loopback",
                starts=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                ends=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
            )
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)]})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver(["127.0.0.1"]),  # type: ignore[arg-type]
        )
        assert report.feeds_errored == 1
        feed = session.get(IcalFeed, feed_id)
        assert feed is not None
        assert feed.last_error == "ical_url_private_address"
        # No HTTPS GET was issued — the SSRF gate tripped first.
        assert fetcher.calls == []

    def test_knob_on_polls_loopback_feed(
        self,
        session: Session,
        bus: EventBus,
        clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """Knob ON: a loopback feed polls cleanly + creates reservations."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        _bootstrap_feed(session, workspace_id=ws, property_id=prop, envelope=envelope)
        body = _vcalendar(
            _vevent_booked(
                uid="loopback",
                starts=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                ends=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
            )
        )
        fetcher = _ScriptedFetcher(responses={_FEED_URL: [_ok(body)]})

        report = poll_ical(
            _ctx(ws),
            session=session,
            envelope=envelope,
            clock=clock,
            event_bus=bus,
            fetcher=fetcher,
            resolver=_fixed_resolver(["127.0.0.1"]),  # type: ignore[arg-type]
            allow_private_addresses=True,
        )
        assert report.feeds_polled == 1
        assert report.feeds_errored == 0
        assert report.reservations_created == 1
        # Fetcher saw the loopback IP — proves the gate flowed through
        # the whole poll_ical → _poll_one_feed → fetch_ical_body chain.
        assert fetcher.calls == [(_FEED_URL, "127.0.0.1")]
