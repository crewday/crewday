"""Unit tests for :mod:`app.domain.stays.ical_service`.

Covers the six public service entry points with fake
:class:`IcalValidator`, :class:`ProviderDetector`, and
:class:`EnvelopeEncryptor` ports — no socket, no DNS, no live key:

* :func:`register_feed` — happy path (validation + encryption +
  auto-detect + audit + enabled=True on parseable ICS), disabled-
  on-non-parseable, provider override, invalid URL rejection,
  host-only preview + URL never in audit.
* :func:`update_feed` — URL swap re-validates + re-encrypts +
  re-probes + flips enabled; provider override only skips probe;
  empty body raises.
* :func:`disable_feed` — ``enabled=False``, row survives, audit.
* :func:`delete_feed` — hard delete + audit-before-drop.
* :func:`probe_feed` — success path flips enabled; failure path
  records the §04 error code in the audit diff.
* :func:`list_feeds` — workspace scoping, property filter, preview
  never contains plaintext URL.
* :func:`get_plaintext_url` — only decryption surface; cross-
  workspace access raises ``IcalFeedNotFound``.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed".
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.stays.models import IcalFeed
from app.adapters.db.workspace.models import Workspace
from app.adapters.ical.ports import (
    IcalProvider,
    IcalValidation,
    IcalValidationError,
)
from app.domain.stays.ical_service import (
    IcalFeedCreate,
    IcalFeedNotFound,
    IcalFeedUpdate,
    IcalUrlInvalid,
    delete_feed,
    disable_feed,
    get_plaintext_url,
    list_feeds,
    probe_feed,
    register_feed,
    update_feed,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.envelope import FakeEnvelope

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 24, 13, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeValidator:
    """Deterministic :class:`IcalValidator` backed by a lookup table."""

    def __init__(
        self,
        table: dict[str, IcalValidation | IcalValidationError] | None = None,
        *,
        default_parseable: bool = True,
    ) -> None:
        self._table: dict[str, IcalValidation | IcalValidationError] = (
            dict(table) if table is not None else {}
        )
        self._default_parseable = default_parseable
        self.calls: list[str] = []

    def validate(self, url: str) -> IcalValidation:
        self.calls.append(url)
        if url in self._table:
            result = self._table[url]
            if isinstance(result, IcalValidationError):
                raise result
            return result
        # Default: synthesise a success result with the requested
        # ``parseable_ics`` flag. The resolved IP is a TEST-NET-3
        # placeholder so we never pretend a real public IP.
        return IcalValidation(
            url=url,
            resolved_ip="1.1.1.1",
            content_type="text/calendar",
            parseable_ics=self._default_parseable,
            bytes_read=100,
        )


class FakeDetector:
    """``ProviderDetector`` that echoes a fixed mapping."""

    def __init__(self, slug: IcalProvider = "generic") -> None:
        self._slug: IcalProvider = slug
        self.calls: list[str] = []

    def detect(self, url: str) -> IcalProvider:
        self.calls.append(url)
        return self._slug


# ---------------------------------------------------------------------------
# DB fixtures (mirrors tests/unit/places/test_property_service.py)
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
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


@pytest.fixture(name="engine_stays")
def fixture_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_stays")
def fixture_session(engine_stays: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_stays, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(_PINNED)


@pytest.fixture
def envelope() -> FakeEnvelope:
    return FakeEnvelope()


def _ctx(workspace_id: str, *, slug: str = "ws") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _bootstrap_property(session: Session, workspace_id: str) -> str:
    """Insert a minimal ``property`` row so the FK holds.

    We don't go through the places service because that would pull
    the whole multi-belonging junction; for these tests we just need
    an ID that FK-resolves against ``property.id``.
    """
    from app.adapters.db.places.models import Property

    pid = new_ulid()
    session.add(
        Property(
            id=pid,
            name="Villa Sud",
            kind="str",
            address="12 Chemin des Oliviers",
            address_json={"country": "FR"},
            country="FR",
            locale=None,
            default_currency=None,
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            client_org_id=None,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
    )
    session.flush()
    return pid


# ---------------------------------------------------------------------------
# register_feed
# ---------------------------------------------------------------------------


class TestRegister:
    """Happy-path + branch coverage for ``register_feed``."""

    def test_register_happy_path(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session_stays, slug="ical-ok")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="ical-ok")
        validator = FakeValidator(default_parseable=True)
        detector = FakeDetector("airbnb")

        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://www.airbnb.com/ical/abc.ics",
            ),
            validator=validator,
            detector=detector,
            envelope=envelope,
            clock=frozen_clock,
        )

        assert view.provider == "airbnb"
        assert view.enabled is True
        assert view.url_preview == "https://www.airbnb.com"
        assert "abc.ics" not in view.url_preview  # path stripped
        # One DB row landed, encrypted, not plaintext.
        row = session_stays.scalars(select(IcalFeed)).one()
        assert row.url != "https://www.airbnb.com/ical/abc.ics"
        assert row.url.startswith("fake-envelope::ical-feed-url::")
        assert row.enabled is True
        # Audit row written. The path-stripping happens at the domain
        # layer (``view.url_preview`` is host-only by construction in
        # :mod:`app.domain.stays.ical_service`), and the plaintext URL
        # is encrypted at rest via the envelope. Hostnames are NOT
        # credentials — leaving the host visible in the audit diff is
        # operationally useful (an operator triaging a feed registration
        # can see *which* upstream the row came from without unwrapping
        # the envelope).
        audit = session_stays.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).one()
        assert audit.action == "register"
        assert audit.entity_kind == "ical_feed"
        audit_after = audit.diff["after"]
        # The full path-bearing URL never appears in the diff: only the
        # host-only preview lands there.
        assert "https://www.airbnb.com/ical/abc.ics" not in repr(audit.diff)
        assert "abc.ics" not in repr(audit.diff)
        # The host-only preview IS present — that's the intended audit
        # field (path/query stripped at the domain layer).
        assert audit_after["url_preview"] == "https://www.airbnb.com"

    def test_register_disabled_when_not_parseable(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """A probe that returns non-ICS body → row lands ``enabled=False``."""
        ws = _bootstrap_workspace(session_stays, slug="ical-off")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="ical-off")
        validator = FakeValidator(default_parseable=False)

        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://example.com/feed.ics",
            ),
            validator=validator,
            detector=FakeDetector("generic"),
            envelope=envelope,
            clock=frozen_clock,
        )

        assert view.enabled is False
        # cd-ewd7 widened the CHECK so ``generic`` lands verbatim
        # (was ``custom`` under the v1 ``_to_db_provider`` collapse).
        assert view.provider == "generic"

    def test_register_honours_provider_override(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """``provider_override`` wins over auto-detect."""
        ws = _bootstrap_workspace(session_stays, slug="ical-ovr")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="ical-ovr")
        detector = FakeDetector("airbnb")  # would auto-detect airbnb

        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://www.airbnb.com/ical/abc.ics",
                provider_override="booking",
            ),
            validator=FakeValidator(default_parseable=True),
            detector=detector,
            envelope=envelope,
            clock=frozen_clock,
        )

        # Override wins: provider ``booking`` stored, auto-detect
        # skipped (detector never consulted).
        assert view.provider == "booking"
        assert detector.calls == []

    def test_register_rejects_invalid_url(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """Validator errors surface as :class:`IcalUrlInvalid` with same code."""
        ws = _bootstrap_workspace(session_stays, slug="ical-bad")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="ical-bad")
        bad_url = "http://example.com/feed.ics"
        validator = FakeValidator(
            {bad_url: IcalValidationError("ical_url_insecure_scheme", "https required")}
        )
        with pytest.raises(IcalUrlInvalid) as exc_info:
            register_feed(
                session_stays,
                ctx,
                body=IcalFeedCreate(property_id=prop, url=bad_url),
                validator=validator,
                detector=FakeDetector(),
                envelope=envelope,
                clock=frozen_clock,
            )
        assert exc_info.value.code == "ical_url_insecure_scheme"
        # No row landed on the failure path.
        assert session_stays.scalars(select(IcalFeed)).all() == []

    def test_register_stores_gcal_directly(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """cd-ewd7: detector's ``gcal`` result now lands verbatim.

        Pre-cd-ewd7, the service layer collapsed ``gcal`` to
        ``custom`` because the v1 CHECK only admitted four slugs;
        the widened CHECK plus the removal of ``_to_db_provider``
        means the DB row carries the detector's output directly.
        """
        ws = _bootstrap_workspace(session_stays, slug="ical-gc")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="ical-gc")

        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://calendar.google.com/x/ical.ics",
            ),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("gcal"),
            envelope=envelope,
            clock=frozen_clock,
        )
        assert view.provider == "gcal"
        # The DB row carries the same slug — no stealth collapse.
        row = session_stays.scalars(select(IcalFeed)).one()
        assert row.provider == "gcal"

    def test_register_defaults_poll_cadence(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """§04 default ``*/15 * * * *`` applies when caller omits the field."""
        from app.adapters.db.stays.models import DEFAULT_POLL_CADENCE

        ws = _bootstrap_workspace(session_stays, slug="ical-cadence")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="ical-cadence")

        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://www.airbnb.com/ical/abc.ics",
            ),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("airbnb"),
            envelope=envelope,
            clock=frozen_clock,
        )
        assert view.poll_cadence == DEFAULT_POLL_CADENCE
        assert DEFAULT_POLL_CADENCE == "*/15 * * * *"

    def test_register_honours_poll_cadence_override(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """Operator-supplied cadence overrides the §04 default."""
        ws = _bootstrap_workspace(session_stays, slug="ical-cad-ovr")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="ical-cad-ovr")

        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://www.airbnb.com/ical/abc.ics",
                poll_cadence="*/5 * * * *",
            ),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("airbnb"),
            envelope=envelope,
            clock=frozen_clock,
        )
        assert view.poll_cadence == "*/5 * * * *"

    def test_register_with_unit_id(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """cd-ewd7: feed can target a specific unit via ``unit_id``."""
        from app.adapters.db.places.models import Unit

        ws = _bootstrap_workspace(session_stays, slug="ical-unit")
        prop = _bootstrap_property(session_stays, ws)
        unit_id = new_ulid()
        session_stays.add(
            Unit(
                id=unit_id,
                property_id=prop,
                label="Apt 3B",
                type="apartment",
                capacity=2,
                created_at=_PINNED,
            )
        )
        session_stays.flush()
        ctx = _ctx(ws, slug="ical-unit")

        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                unit_id=unit_id,
                url="https://www.airbnb.com/ical/abc.ics",
            ),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("airbnb"),
            envelope=envelope,
            clock=frozen_clock,
        )
        assert view.unit_id == unit_id
        row = session_stays.scalars(select(IcalFeed)).one()
        assert row.unit_id == unit_id


# ---------------------------------------------------------------------------
# update_feed
# ---------------------------------------------------------------------------


def _seed_feed(
    session: Session,
    ctx: WorkspaceContext,
    prop_id: str,
    envelope: FakeEnvelope,
    clock: FrozenClock,
    *,
    url: str = "https://www.airbnb.com/ical/abc.ics",
    detector_slug: IcalProvider = "airbnb",
) -> str:
    view = register_feed(
        session,
        ctx,
        body=IcalFeedCreate(property_id=prop_id, url=url),
        validator=FakeValidator(default_parseable=True),
        detector=FakeDetector(detector_slug),
        envelope=envelope,
        clock=clock,
    )
    return view.id


class TestUpdate:
    """Branch coverage for ``update_feed``."""

    def test_update_swaps_url_and_reprobes(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session_stays, slug="up-url")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="up-url")
        feed_id = _seed_feed(session_stays, ctx, prop, envelope, frozen_clock)
        new_url = "https://www.vrbo.com/ical/456.ics"

        # Fresh validator / detector so we can observe re-probe.
        validator = FakeValidator(default_parseable=True)
        detector = FakeDetector("vrbo")

        view = update_feed(
            session_stays,
            ctx,
            feed_id=feed_id,
            body=IcalFeedUpdate(url=new_url),
            validator=validator,
            detector=detector,
            envelope=envelope,
            clock=frozen_clock,
        )
        assert view.provider == "vrbo"
        assert view.url_preview == "https://www.vrbo.com"
        assert validator.calls == [new_url]

        # Row's ciphertext now decrypts to the new URL.
        row = session_stays.get(IcalFeed, feed_id)
        assert row is not None
        plain = envelope.decrypt(row.url.encode("latin-1"), purpose="ical-feed-url")
        assert plain == new_url.encode("utf-8")

    def test_update_override_only_skips_probe(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session_stays, slug="up-ovr")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="up-ovr")
        feed_id = _seed_feed(session_stays, ctx, prop, envelope, frozen_clock)
        # Pure metadata flip: validator / detector should never be
        # consulted.
        validator = FakeValidator(default_parseable=True)
        detector = FakeDetector("booking")

        view = update_feed(
            session_stays,
            ctx,
            feed_id=feed_id,
            body=IcalFeedUpdate(provider_override="booking"),
            validator=validator,
            detector=detector,
            envelope=envelope,
            clock=frozen_clock,
        )
        assert view.provider == "booking"
        assert validator.calls == []
        assert detector.calls == []

    def test_update_rejects_empty_body(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session_stays, slug="up-nop")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="up-nop")
        feed_id = _seed_feed(session_stays, ctx, prop, envelope, frozen_clock)
        with pytest.raises(ValueError, match="at least one"):
            update_feed(
                session_stays,
                ctx,
                feed_id=feed_id,
                body=IcalFeedUpdate(),
                validator=FakeValidator(),
                detector=FakeDetector(),
                envelope=envelope,
                clock=frozen_clock,
            )

    def test_update_cross_workspace_raises_not_found(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws_a = _bootstrap_workspace(session_stays, slug="ws-a")
        ws_b = _bootstrap_workspace(session_stays, slug="ws-b")
        prop_a = _bootstrap_property(session_stays, ws_a)
        ctx_a = _ctx(ws_a, slug="ws-a")
        ctx_b = _ctx(ws_b, slug="ws-b")
        feed_id = _seed_feed(session_stays, ctx_a, prop_a, envelope, frozen_clock)
        with pytest.raises(IcalFeedNotFound):
            update_feed(
                session_stays,
                ctx_b,
                feed_id=feed_id,
                body=IcalFeedUpdate(provider_override="booking"),
                validator=FakeValidator(),
                detector=FakeDetector(),
                envelope=envelope,
                clock=frozen_clock,
            )

    def test_update_failed_validation_preserves_old_ciphertext(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """A rejected URL swap must leave the stored ciphertext untouched.

        Regression guard: validate-then-encrypt ordering means a
        validation error raises *before* the row's ``url`` column is
        reassigned. If the implementation ever gets refactored to
        encrypt-first, this test fails loudly.
        """
        ws = _bootstrap_workspace(session_stays, slug="up-rej")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="up-rej")
        original_url = "https://www.airbnb.com/ical/original.ics"
        feed_id = _seed_feed(
            session_stays,
            ctx,
            prop,
            envelope,
            frozen_clock,
            url=original_url,
        )
        row_before = session_stays.get(IcalFeed, feed_id)
        assert row_before is not None
        ciphertext_before = row_before.url

        bad_url = "https://evil.example/feed.ics"
        failing_validator = FakeValidator(
            {
                bad_url: IcalValidationError(
                    "ical_url_private_address",
                    "host resolved to private address",
                )
            }
        )
        with pytest.raises(IcalUrlInvalid) as exc_info:
            update_feed(
                session_stays,
                ctx,
                feed_id=feed_id,
                body=IcalFeedUpdate(url=bad_url),
                validator=failing_validator,
                detector=FakeDetector(),
                envelope=envelope,
                clock=frozen_clock,
            )
        assert exc_info.value.code == "ical_url_private_address"

        # Refresh the row from the session and assert the ciphertext
        # did NOT mutate, and that it still decrypts to the original URL.
        session_stays.expire(row_before)
        row_after = session_stays.get(IcalFeed, feed_id)
        assert row_after is not None
        assert row_after.url == ciphertext_before
        decrypted = envelope.decrypt(
            row_after.url.encode("latin-1"), purpose="ical-feed-url"
        )
        assert decrypted == original_url.encode("utf-8")


# ---------------------------------------------------------------------------
# disable_feed / delete_feed
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_disable_flips_enabled_keeps_row(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session_stays, slug="dis")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="dis")
        feed_id = _seed_feed(session_stays, ctx, prop, envelope, frozen_clock)

        view = disable_feed(
            session_stays,
            ctx,
            feed_id=feed_id,
            clock=frozen_clock,
        )
        assert view.enabled is False
        row = session_stays.get(IcalFeed, feed_id)
        assert row is not None
        assert row.enabled is False
        # Audit row written.
        audits = session_stays.scalars(
            select(AuditLog).where(AuditLog.entity_id == feed_id)
        ).all()
        assert [a.action for a in audits] == ["register", "disable"]

    def test_delete_removes_row(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session_stays, slug="del")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="del")
        feed_id = _seed_feed(session_stays, ctx, prop, envelope, frozen_clock)
        delete_feed(session_stays, ctx, feed_id=feed_id, clock=frozen_clock)
        assert session_stays.get(IcalFeed, feed_id) is None
        audits = session_stays.scalars(
            select(AuditLog).where(AuditLog.entity_id == feed_id)
        ).all()
        assert [a.action for a in audits] == ["register", "delete"]

    def test_delete_preserves_reservations_via_set_null(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """``reservation.ical_feed_id`` is ``SET NULL`` on ``delete_feed``.

        §02 "reservation" says a booking captured from an iCal feed
        outlives the feed's deletion (agency swaps provider, booking
        remains real work). Verify that contract holds end-to-end:
        hard-delete the feed, reservation survives, FK column is
        NULL.
        """
        from app.adapters.db.stays.models import Reservation

        ws = _bootstrap_workspace(session_stays, slug="del-resv")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="del-resv")
        feed_id = _seed_feed(session_stays, ctx, prop, envelope, frozen_clock)
        # Seed a reservation that references the feed. Column names
        # match the v1 ORM shape (``check_in`` / ``check_out``, no
        # ``updated_at``).
        reservation_id = new_ulid()
        session_stays.add(
            Reservation(
                id=reservation_id,
                workspace_id=ws,
                property_id=prop,
                ical_feed_id=feed_id,
                external_uid="uid-del-resv-1",
                source="ical",
                status="scheduled",
                guest_name="G. M.",
                guest_count=2,
                check_in=_PINNED,
                check_out=_LATER,
                raw_summary=None,
                raw_description=None,
                created_at=_PINNED,
            )
        )
        session_stays.flush()

        delete_feed(session_stays, ctx, feed_id=feed_id, clock=frozen_clock)
        # Force the session to re-read from the database — the cascade
        # fires at the DB layer, so we must round-trip to see it.
        session_stays.expire_all()

        assert session_stays.get(IcalFeed, feed_id) is None
        resv_after = session_stays.get(Reservation, reservation_id)
        assert resv_after is not None, "reservation survived the feed hard-delete"
        assert resv_after.ical_feed_id is None, (
            "ON DELETE SET NULL cascade must null the FK"
        )


# ---------------------------------------------------------------------------
# probe_feed
# ---------------------------------------------------------------------------


class TestProbe:
    def test_probe_success_flips_enabled(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """A feed that registered with ``enabled=False`` lights up on probe."""
        ws = _bootstrap_workspace(session_stays, slug="prb-on")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="prb-on")
        # Register with non-parseable probe → enabled=False.
        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://example.com/feed.ics",
            ),
            validator=FakeValidator(default_parseable=False),
            detector=FakeDetector("generic"),
            envelope=envelope,
            clock=frozen_clock,
        )
        assert view.enabled is False

        # Later probe returns a parseable body → flip enabled.
        frozen_clock.set(_LATER)
        result = probe_feed(
            session_stays,
            ctx,
            feed_id=view.id,
            validator=FakeValidator(default_parseable=True),
            envelope=envelope,
            clock=frozen_clock,
        )
        assert result.ok is True
        assert result.parseable_ics is True
        row = session_stays.get(IcalFeed, view.id)
        assert row is not None
        assert row.enabled is True

    def test_probe_failure_persists_last_error(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """cd-ewd7: probe failure stamps ``last_error`` on the feed row.

        Pre-cd-ewd7 the error only surfaced in the audit diff because
        the column didn't exist yet; post-cd-ewd7 the service writes
        the code to the row so the operator UI can render a live-
        vs-stale indicator without tailing the audit stream.
        """
        ws = _bootstrap_workspace(session_stays, slug="prb-err")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="prb-err")
        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://example.com/feed.ics",
            ),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("generic"),
            envelope=envelope,
            clock=frozen_clock,
        )
        # Fresh registration: ``last_error`` starts NULL.
        fresh_row = session_stays.get(IcalFeed, view.id)
        assert fresh_row is not None
        assert fresh_row.last_error is None

        # Next probe fails.
        url = "https://example.com/feed.ics"
        failing_validator = FakeValidator(
            {url: IcalValidationError("ical_url_timeout", "deadline exceeded")}
        )
        frozen_clock.set(_LATER)
        result = probe_feed(
            session_stays,
            ctx,
            feed_id=view.id,
            validator=failing_validator,
            envelope=envelope,
            clock=frozen_clock,
        )
        assert result.ok is False
        assert result.error_code == "ical_url_timeout"
        # Row now carries the §04 code so the UI can pick it up.
        session_stays.expire_all()
        row_after = session_stays.get(IcalFeed, view.id)
        assert row_after is not None
        assert row_after.last_error == "ical_url_timeout"
        # The last probe audit still records the error code.
        audits = session_stays.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .where(AuditLog.action == "probe")
        ).all()
        assert audits[-1].diff["error_code"] == "ical_url_timeout"

    def test_probe_success_clears_last_error(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """A successful probe wipes any prior ``last_error`` stamp.

        Without the clear the column would be stuck at whatever code
        the last failure wrote even after the feed healed, which
        would confuse the operator UI ("why is this feed flagged
        stale when it just polled cleanly?").
        """
        ws = _bootstrap_workspace(session_stays, slug="prb-ok")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="prb-ok")
        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://example.com/feed.ics",
            ),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("generic"),
            envelope=envelope,
            clock=frozen_clock,
        )
        # Fail once to plant a code.
        url = "https://example.com/feed.ics"
        probe_feed(
            session_stays,
            ctx,
            feed_id=view.id,
            validator=FakeValidator(
                {url: IcalValidationError("ical_url_timeout", "nope")}
            ),
            envelope=envelope,
            clock=frozen_clock,
        )
        session_stays.expire_all()
        row = session_stays.get(IcalFeed, view.id)
        assert row is not None
        assert row.last_error == "ical_url_timeout"

        # Now heal.
        frozen_clock.set(_LATER)
        result = probe_feed(
            session_stays,
            ctx,
            feed_id=view.id,
            validator=FakeValidator(default_parseable=True),
            envelope=envelope,
            clock=frozen_clock,
        )
        assert result.ok is True
        session_stays.expire_all()
        row_after = session_stays.get(IcalFeed, view.id)
        assert row_after is not None
        assert row_after.last_error is None

    def test_update_url_clears_stale_last_error(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        """Swapping the URL invalidates the prior probe's verdict.

        ``last_error`` reflects the outcome of the last probe against
        the current URL; a fresh URL has not been judged yet, so
        leaving a stale code on the row would mis-represent the
        feed's health.
        """
        ws = _bootstrap_workspace(session_stays, slug="up-err")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="up-err")
        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(
                property_id=prop,
                url="https://example.com/feed.ics",
            ),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("generic"),
            envelope=envelope,
            clock=frozen_clock,
        )
        # Plant a failure on the row.
        probe_feed(
            session_stays,
            ctx,
            feed_id=view.id,
            validator=FakeValidator(
                {
                    "https://example.com/feed.ics": IcalValidationError(
                        "ical_url_timeout", "nope"
                    )
                }
            ),
            envelope=envelope,
            clock=frozen_clock,
        )
        session_stays.expire_all()
        mid = session_stays.get(IcalFeed, view.id)
        assert mid is not None
        assert mid.last_error == "ical_url_timeout"

        # Swap URL; the update path should clear the stale code.
        from app.domain.stays.ical_service import update_feed

        update_feed(
            session_stays,
            ctx,
            feed_id=view.id,
            body=IcalFeedUpdate(url="https://www.airbnb.com/ical/new.ics"),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("airbnb"),
            envelope=envelope,
            clock=frozen_clock,
        )
        session_stays.expire_all()
        row_after = session_stays.get(IcalFeed, view.id)
        assert row_after is not None
        assert row_after.last_error is None


# ---------------------------------------------------------------------------
# list_feeds / get_plaintext_url
# ---------------------------------------------------------------------------


class TestListAndReveal:
    def test_list_workspace_scoped(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws_a = _bootstrap_workspace(session_stays, slug="ws-a-l")
        ws_b = _bootstrap_workspace(session_stays, slug="ws-b-l")
        prop_a = _bootstrap_property(session_stays, ws_a)
        prop_b = _bootstrap_property(session_stays, ws_b)
        ctx_a = _ctx(ws_a, slug="ws-a-l")
        ctx_b = _ctx(ws_b, slug="ws-b-l")
        url_a = "https://www.airbnb.com/ical/secret-path-a.ics"
        url_b = "https://www.airbnb.com/ical/secret-path-b.ics"
        _seed_feed(session_stays, ctx_a, prop_a, envelope, frozen_clock, url=url_a)
        _seed_feed(
            session_stays,
            ctx_b,
            prop_b,
            envelope,
            frozen_clock,
            url=url_b,
            detector_slug="airbnb",
        )

        list_a = list_feeds(session_stays, ctx_a)
        assert len(list_a) == 1
        # The preview never contains the plaintext path.
        assert list_a[0].url_preview == "(encrypted)"
        # Defence-in-depth: assert the URL's secret path token is not
        # present in *any* stringified field of the view.
        view_repr = repr(list_a[0])
        assert "secret-path-a" not in view_repr
        assert "secret-path-b" not in view_repr
        # Host-filter also honours workspace scoping.
        list_b = list_feeds(session_stays, ctx_b, property_id=prop_b)
        assert len(list_b) == 1
        # Filter on a foreign property id yields empty.
        assert list_feeds(session_stays, ctx_a, property_id=prop_b) == []


# ---------------------------------------------------------------------------
# Cross-cutting: plaintext URL never in audit diffs
# ---------------------------------------------------------------------------


class TestAuditNeverLeaksPlaintext:
    """Every mutation route must keep the URL's secret path out of audit.

    We pick a URL whose path carries a recognisable token
    (``secret-token-xyz``) and assert that token never appears in
    the repr of any ``ical_feed`` audit diff. Covers register +
    update + probe (success) + probe (failure) + disable + delete.
    """

    _TOKEN = "secret-token-xyz"
    _URL = f"https://www.airbnb.com/ical/{_TOKEN}.ics"

    def _assert_no_token_in_audits(self, session: Session, feed_id: str) -> None:
        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == feed_id)
        ).all()
        assert audits, "expected at least one audit row for this feed"
        for audit in audits:
            assert self._TOKEN not in repr(audit.diff), (
                f"audit action {audit.action!r} leaked plaintext URL into diff"
            )

    def test_full_lifecycle_never_leaks_token(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session_stays, slug="aud-leak")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="aud-leak")

        # register
        view = register_feed(
            session_stays,
            ctx,
            body=IcalFeedCreate(property_id=prop, url=self._URL),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("airbnb"),
            envelope=envelope,
            clock=frozen_clock,
        )
        feed_id = view.id
        self._assert_no_token_in_audits(session_stays, feed_id)

        # update (URL swap — also must keep the new token out of audit)
        new_token = "rotated-token-abc"
        new_url = f"https://www.airbnb.com/ical/{new_token}.ics"
        update_feed(
            session_stays,
            ctx,
            feed_id=feed_id,
            body=IcalFeedUpdate(url=new_url),
            validator=FakeValidator(default_parseable=True),
            detector=FakeDetector("airbnb"),
            envelope=envelope,
            clock=frozen_clock,
        )
        # Original token gone; new token must also not appear.
        audits = session_stays.scalars(
            select(AuditLog).where(AuditLog.entity_id == feed_id)
        ).all()
        for audit in audits:
            assert self._TOKEN not in repr(audit.diff)
            assert new_token not in repr(audit.diff)

        # probe (success)
        probe_feed(
            session_stays,
            ctx,
            feed_id=feed_id,
            validator=FakeValidator(default_parseable=True),
            envelope=envelope,
            clock=frozen_clock,
        )
        # probe (failure)
        failing_validator = FakeValidator(
            {new_url: IcalValidationError("ical_url_timeout", "nope")}
        )
        probe_feed(
            session_stays,
            ctx,
            feed_id=feed_id,
            validator=failing_validator,
            envelope=envelope,
            clock=frozen_clock,
        )
        # disable
        disable_feed(session_stays, ctx, feed_id=feed_id, clock=frozen_clock)
        # delete (audit-before-drop still applies)
        delete_feed(session_stays, ctx, feed_id=feed_id, clock=frozen_clock)

        audits_final = session_stays.scalars(
            select(AuditLog).where(AuditLog.entity_id == feed_id)
        ).all()
        for audit in audits_final:
            assert self._TOKEN not in repr(audit.diff), (
                f"{audit.action!r} leaked initial URL token"
            )
            assert new_token not in repr(audit.diff), (
                f"{audit.action!r} leaked rotated URL token"
            )

    def test_get_plaintext_url_decrypts(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws = _bootstrap_workspace(session_stays, slug="rv")
        prop = _bootstrap_property(session_stays, ws)
        ctx = _ctx(ws, slug="rv")
        url = "https://www.airbnb.com/ical/secret-token.ics"
        feed_id = _seed_feed(session_stays, ctx, prop, envelope, frozen_clock, url=url)
        plaintext = get_plaintext_url(
            session_stays, ctx, feed_id=feed_id, envelope=envelope
        )
        assert plaintext == url

    def test_get_plaintext_url_cross_workspace_denied(
        self,
        session_stays: Session,
        frozen_clock: FrozenClock,
        envelope: FakeEnvelope,
    ) -> None:
        ws_a = _bootstrap_workspace(session_stays, slug="rv-a")
        ws_b = _bootstrap_workspace(session_stays, slug="rv-b")
        prop_a = _bootstrap_property(session_stays, ws_a)
        ctx_a = _ctx(ws_a, slug="rv-a")
        ctx_b = _ctx(ws_b, slug="rv-b")
        feed_id = _seed_feed(session_stays, ctx_a, prop_a, envelope, frozen_clock)
        with pytest.raises(IcalFeedNotFound):
            get_plaintext_url(
                session_stays,
                ctx_b,
                feed_id=feed_id,
                envelope=envelope,
            )
