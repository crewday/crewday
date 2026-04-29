"""Unit tests for :mod:`app.domain.stays.guest_link_service`.

Covers the four public service entry points with deterministic
fakes — no live key, no socket, no DNS:

* :func:`mint_link` — happy path (ULID id, signed token, audit row,
  default TTL = ``check_out + 1d``, custom TTL override, sets the
  ``reservation.guest_link_id`` back-pointer).
* :func:`revoke_link` — flips ``revoked_at``; idempotent on already-
  revoked rows; clears the ``reservation.guest_link_id`` back-
  pointer when this link was the active one and leaves a sibling
  pointer alone otherwise; audit row written.
* :func:`resolve_link` — happy path (returns
  :class:`ResolvedGuestLink` with workspace_id, link_id, stay_id +
  bundle; checklist filtered to ``guest_visible=True``, equipment
  shown only when setting + visible assets); expired token →
  ``GuestLinkGone(EXPIRED)``; revoked link → ``GuestLinkGone(REVOKED)``;
  tampered token (single-byte flip) → ``None``; welcome merge
  respects spec order (stay > unit > property); equipment hidden
  when setting=false.
* :func:`record_access` — appends a record, hashes the IP prefix
  (``/24`` for v4, ``/64`` for v6), classifies the UA family,
  truncates to the last 10 entries, writes one audit row per call.

Token signing keys are deterministic in tests via
``CREWDAY_ROOT_KEY`` set in the env fixture.

See ``docs/specs/04-properties-and-stays.md`` §"Guest welcome
link" for the rendered contract.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.stays.models import GuestLink, Reservation
from app.adapters.db.workspace.models import Workspace
from app.config import get_settings
from app.domain.stays.guest_link_service import (
    ChecklistItem,
    GuestAsset,
    GuestLinkGone,
    GuestLinkGoneReason,
    GuestLinkNotFound,
    ResolvedGuestLink,
    SettingsResolver,
    WelcomeMergeInput,
    WelcomeResolver,
    mint_link,
    record_access,
    resolve_link,
    revoke_link,
)
from app.security.hmac_signer import HmacSigner, rotate_hmac_key
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"


def _aware_utc_or_none(value: datetime | None) -> datetime | None:
    """Restamp UTC tz on a SQLite-loaded naive datetime.

    Mirrors :func:`app.domain.stays.guest_link_service._aware_utc` for
    use inside tests that assert directly on the SA row (SQLite drops
    the tzinfo on read; Postgres preserves it). Tests should generally
    assert on the DTOs the service returns — the DTO already carries
    aware datetimes — but the few cases that read the row to verify a
    column moved use this helper.
    """
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# A long deterministic value so HKDF-Expand has enough entropy and
# tests cannot flake on an empty / too-short root key.
_TEST_ROOT_KEY = "test-root-key-cd-l0k-deterministic-fixed-32+ chars long for HKDF"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWelcomeResolver:
    """Deterministic :class:`WelcomeResolver` backed by a lookup table.

    Tests register one entry per ``(workspace_id, stay_id)`` pair and
    the fake echoes it back. ``None`` is allowed: simulates a stay
    that was deleted out from under the link, which the service
    collapses to a 410 path.
    """

    def __init__(
        self, table: dict[tuple[str, str], WelcomeMergeInput | None] | None = None
    ) -> None:
        self._table: dict[tuple[str, str], WelcomeMergeInput | None] = (
            dict(table) if table is not None else {}
        )
        self.calls: list[tuple[str, str]] = []

    def set(
        self, *, workspace_id: str, stay_id: str, value: WelcomeMergeInput | None
    ) -> None:
        self._table[(workspace_id, stay_id)] = value

    def fetch(
        self,
        *,
        session: Session,
        workspace_id: str,
        stay_id: str,
    ) -> WelcomeMergeInput | None:
        self.calls.append((workspace_id, stay_id))
        return self._table.get((workspace_id, stay_id))


class FakeSettingsResolver:
    """Deterministic :class:`SettingsResolver` with a flat map.

    Tests set a single bool keyed by setting name; cascade scope
    arguments are accepted but the fake doesn't branch on them
    (the service does not test cascade resolution itself —
    cd-settings-cascade owns that).
    """

    def __init__(self, *, defaults: dict[str, bool] | None = None) -> None:
        self._values: dict[str, bool] = dict(defaults or {})
        self.calls: list[tuple[str, str, str | None, str]] = []

    def set(self, key: str, value: bool) -> None:
        self._values[key] = value

    def resolve_bool(
        self,
        *,
        session: Session,
        workspace_id: str,
        property_id: str,
        unit_id: str | None,
        key: str,
    ) -> bool:
        self.calls.append((workspace_id, property_id, unit_id, key))
        return self._values.get(key, False)


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture(autouse=True)
def fixture_root_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin ``CREWDAY_ROOT_KEY`` so :func:`derive_subkey` is deterministic.

    The signing-secret seam in :mod:`app.config` reads
    ``CREWDAY_ROOT_KEY`` via pydantic-settings; we set it on the
    process and bust the lru-cache so the test inherits the value.
    """
    monkeypatch.setenv("CREWDAY_ROOT_KEY", _TEST_ROOT_KEY)
    # ``CREWDAY_DATABASE_URL`` is required by ``Settings``; the
    # surrounding test process may not have set it. A throwaway
    # SQLite URL keeps the field happy without touching disk —
    # tests use their own in-memory engines via ``make_engine``.
    monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(name="engine_guest")
def fixture_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_guest")
def fixture_session(engine_guest: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_guest, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(_PINNED)


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
    """Insert a minimal ``property`` row so the FK holds."""
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


def _bootstrap_reservation(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    check_in: datetime,
    check_out: datetime,
) -> str:
    rid = new_ulid()
    session.add(
        Reservation(
            id=rid,
            workspace_id=workspace_id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=f"manual-{rid}",
            check_in=check_in,
            check_out=check_out,
            guest_name="A. Test Guest",
            guest_count=2,
            status="scheduled",
            source="manual",
            raw_summary=None,
            raw_description=None,
            guest_link_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return rid


def _make_input(
    *,
    property_id: str,
    property_defaults: dict[str, object] | None = None,
    unit_overrides: dict[str, object] | None = None,
    stay_overrides: dict[str, object] | None = None,
    stay_wifi: str | None = None,
    checklist: tuple[ChecklistItem, ...] = (),
    assets: tuple[GuestAsset, ...] = (),
) -> WelcomeMergeInput:
    return WelcomeMergeInput(
        property_id=property_id,
        property_name="Villa Sud",
        unit_id="01HWA0UNIT00000000000000UN",
        unit_name="Main suite",
        property_defaults=dict(property_defaults or {}),
        unit_overrides=dict(unit_overrides or {}),
        stay_overrides=dict(stay_overrides or {}),
        stay_wifi_password_override=stay_wifi,
        checklist=checklist,
        assets=assets,
        check_in_at=_PINNED + timedelta(days=1),
        check_out_at=_PINNED + timedelta(days=4),
        guest_name="A. Test Guest",
    )


# ---------------------------------------------------------------------------
# mint_link
# ---------------------------------------------------------------------------


class TestMintLink:
    """Happy path + branch coverage for ``mint_link``."""

    def test_mint_persists_row_and_audit(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session_guest, slug="mint-ok")
        prop = _bootstrap_property(session_guest, ws)
        check_out = _PINNED + timedelta(days=4)
        stay = _bootstrap_reservation(
            session_guest,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        ctx = _ctx(ws, slug="mint-ok")

        link = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )

        # Default TTL is ``check_out + 1d`` per §04.
        assert link.expires_at == check_out + timedelta(days=1)
        assert link.revoked_at is None
        assert link.token  # non-empty signed blob
        assert link.workspace_id == ws

        # One row landed.
        row = session_guest.scalars(select(GuestLink)).one()
        assert row.id == link.id
        assert row.token == link.token
        assert row.access_log_json == []

        # One audit row, no token leak.
        audit = session_guest.scalars(
            select(AuditLog).where(AuditLog.entity_id == link.id)
        ).one()
        assert audit.entity_kind == "guest_link"
        assert audit.action == "minted"
        assert link.token not in repr(audit.diff)
        assert audit.diff["after"]["stay_id"] == stay
        assert audit.diff["after"]["property_id"] == prop

    def test_mint_sets_reservation_back_pointer(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """``mint_link`` writes the soft back-pointer on the parent stay.

        The migration's docstring promises that the manager UI can
        find the active link from a reservation in one read; this
        test pins the contract so a regression doesn't silently
        break that invariant.
        """
        ws = _bootstrap_workspace(session_guest, slug="mint-bp")
        prop = _bootstrap_property(session_guest, ws)
        check_out = _PINNED + timedelta(days=4)
        stay = _bootstrap_reservation(
            session_guest,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug="mint-bp")
        link = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )
        row = session_guest.get(Reservation, stay)
        assert row is not None
        assert row.guest_link_id == link.id

    def test_mint_overwrites_back_pointer_on_re_mint(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """A re-mint without revoking points the back-pointer at the new id."""
        ws = _bootstrap_workspace(session_guest, slug="mint-bp2")
        prop = _bootstrap_property(session_guest, ws)
        check_out = _PINNED + timedelta(days=4)
        stay = _bootstrap_reservation(
            session_guest,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug="mint-bp2")
        first = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )
        frozen_clock.advance(timedelta(seconds=1))
        second = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )
        row = session_guest.get(Reservation, stay)
        assert row is not None
        assert row.guest_link_id == second.id
        assert second.id != first.id

    def test_mint_custom_ttl_overrides_default(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session_guest, slug="mint-ttl")
        prop = _bootstrap_property(session_guest, ws)
        check_out = _PINNED + timedelta(days=2)
        stay = _bootstrap_reservation(
            session_guest,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug="mint-ttl")

        link = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            ttl=timedelta(days=7),
            clock=frozen_clock,
        )
        assert link.expires_at == check_out + timedelta(days=7)


# ---------------------------------------------------------------------------
# revoke_link
# ---------------------------------------------------------------------------


class TestRevokeLink:
    """Branch coverage for ``revoke_link``."""

    def test_revoke_stamps_revoked_at(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session_guest, slug="rev-ok")
        prop = _bootstrap_property(session_guest, ws)
        check_out = _PINNED + timedelta(days=4)
        stay = _bootstrap_reservation(
            session_guest,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug="rev-ok")
        link = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )

        result = revoke_link(session_guest, ctx, link_id=link.id, clock=frozen_clock)

        assert result.revoked_at is not None
        # Pinned to the clock's instant.
        assert result.revoked_at == _PINNED
        row = session_guest.get(GuestLink, link.id)
        assert row is not None
        assert row.revoked_at is not None
        # SQLite returns naive datetimes from ``DateTime(timezone=True)``
        # columns; coerce before comparing with the aware ``_PINNED``.
        assert _aware_utc_or_none(row.revoked_at) == _PINNED
        # Audit row exists.
        audit_rows = list(
            session_guest.scalars(
                select(AuditLog)
                .where(AuditLog.entity_id == link.id)
                .order_by(AuditLog.created_at.asc())
            )
        )
        # mint + revoke → 2 rows.
        assert [r.action for r in audit_rows] == ["minted", "revoked"]

    def test_revoke_idempotent(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """A second revoke leaves the original ``revoked_at`` intact."""
        ws = _bootstrap_workspace(session_guest, slug="rev-ide")
        prop = _bootstrap_property(session_guest, ws)
        check_out = _PINNED + timedelta(days=4)
        stay = _bootstrap_reservation(
            session_guest,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug="rev-ide")
        link = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )
        revoke_link(session_guest, ctx, link_id=link.id, clock=frozen_clock)
        first_revoked = session_guest.get(GuestLink, link.id)
        assert first_revoked is not None
        assert _aware_utc_or_none(first_revoked.revoked_at) == _PINNED

        # Advance the clock and revoke again — original timestamp survives.
        frozen_clock.advance(timedelta(hours=1))
        revoke_link(session_guest, ctx, link_id=link.id, clock=frozen_clock)
        again = session_guest.get(GuestLink, link.id)
        assert again is not None
        assert _aware_utc_or_none(again.revoked_at) == _PINNED

    def test_revoke_clears_reservation_back_pointer(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """Revoking the active link nulls ``reservation.guest_link_id``."""
        ws = _bootstrap_workspace(session_guest, slug="rev-bp")
        prop = _bootstrap_property(session_guest, ws)
        check_out = _PINNED + timedelta(days=4)
        stay = _bootstrap_reservation(
            session_guest,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug="rev-bp")
        link = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )
        # Sanity: pointer is set.
        row = session_guest.get(Reservation, stay)
        assert row is not None
        assert row.guest_link_id == link.id

        revoke_link(session_guest, ctx, link_id=link.id, clock=frozen_clock)

        session_guest.refresh(row)
        assert row.guest_link_id is None

    def test_revoke_does_not_clear_pointer_when_other_link_is_active(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """A revoke of an older sibling link leaves the active pointer alone.

        After mint A → mint B (overwrites pointer to B) → revoke A,
        the pointer must still be B. Otherwise the manager UI would
        lose the active link when a stale sibling is revoked.
        """
        ws = _bootstrap_workspace(session_guest, slug="rev-bp2")
        prop = _bootstrap_property(session_guest, ws)
        check_out = _PINNED + timedelta(days=4)
        stay = _bootstrap_reservation(
            session_guest,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug="rev-bp2")
        first = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )
        frozen_clock.advance(timedelta(seconds=1))
        second = mint_link(
            session_guest,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen_clock,
        )

        # Revoke the older sibling. The pointer to ``second`` must
        # survive.
        revoke_link(session_guest, ctx, link_id=first.id, clock=frozen_clock)

        row = session_guest.get(Reservation, stay)
        assert row is not None
        assert row.guest_link_id == second.id

    def test_revoke_missing_raises(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ws = _bootstrap_workspace(session_guest, slug="rev-404")
        ctx = _ctx(ws, slug="rev-404")
        with pytest.raises(GuestLinkNotFound):
            revoke_link(
                session_guest,
                ctx,
                link_id="01HWAMISSING00000000000000",
                clock=frozen_clock,
            )


# ---------------------------------------------------------------------------
# resolve_link
# ---------------------------------------------------------------------------


class TestResolveLink:
    """Branch coverage for ``resolve_link``."""

    def _setup(
        self,
        session: Session,
        frozen: FrozenClock,
        *,
        slug: str,
        check_out_offset: timedelta = timedelta(days=4),
    ) -> tuple[str, str, str, str]:
        ws = _bootstrap_workspace(session, slug=slug)
        prop = _bootstrap_property(session, ws)
        check_out = _PINNED + check_out_offset
        stay = _bootstrap_reservation(
            session,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug=slug)
        link = mint_link(
            session,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen,
        )
        return ws, prop, stay, link.token

    def test_resolve_happy_path(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ws, prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-ok")
        welcome = FakeWelcomeResolver()
        welcome.set(
            workspace_id=ws,
            stay_id=stay,
            value=_make_input(
                property_id=prop,
                property_defaults={"wifi_ssid": "VillaWifi"},
                checklist=(
                    ChecklistItem(id="ch1", label="Strip beds", guest_visible=True),
                    ChecklistItem(id="ch2", label="Internal note", guest_visible=False),
                ),
            ),
        )
        settings_resolver = FakeSettingsResolver()  # show_guest_assets defaults False

        resolved = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=settings_resolver,
            clock=frozen_clock,
        )

        assert isinstance(resolved, ResolvedGuestLink)
        assert resolved.stay_id == stay
        assert resolved.workspace_id == ws
        assert resolved.bundle.welcome == {"wifi_ssid": "VillaWifi"}
        # Checklist filtered to guest_visible=True only.
        assert tuple(c.id for c in resolved.bundle.checklist) == ("ch1",)
        # No assets requested → empty.
        assert resolved.bundle.assets == ()

    def test_resolve_accepts_token_signed_before_hmac_rotation(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ws, prop, stay, token = self._setup(
            session_guest, frozen_clock, slug="r-rot"
        )
        rotate_hmac_key(
            session_guest,
            "guest-link",
            b"g" * 32,
            purge_after=_PINNED + timedelta(hours=72),
            settings=get_settings(),
            clock=frozen_clock,
        )
        welcome = FakeWelcomeResolver()
        welcome.set(workspace_id=ws, stay_id=stay, value=_make_input(property_id=prop))

        result = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )

        assert isinstance(result, ResolvedGuestLink)
        assert result.stay_id == stay

    def test_resolve_expired_returns_gone_expired(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """A real-but-expired row surfaces as ``GuestLinkGone(EXPIRED)``.

        §04 "Privacy" mandates the route render the spec's
        "This link has expired" copy on natural expiry; the
        resolver must hand the route enough discriminator
        (the :class:`GuestLinkGoneReason` enum) plus enough
        scope (link_id + workspace_id) to log the access without
        re-loading the row.
        """
        ws, prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-exp")
        welcome = FakeWelcomeResolver()
        welcome.set(workspace_id=ws, stay_id=stay, value=_make_input(property_id=prop))

        # Advance past expires_at = check_out + 1d (= +5d total) +1s.
        frozen_clock.advance(timedelta(days=5, seconds=1))

        result = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )
        assert isinstance(result, GuestLinkGone)
        assert result.reason is GuestLinkGoneReason.EXPIRED
        assert result.workspace_id == ws
        assert result.link_id  # non-empty ULID

    def test_resolve_revoked_returns_gone_revoked(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """A revoked row surfaces as ``GuestLinkGone(REVOKED)``.

        §04 "Privacy" pins the user-facing copy
        ("This welcome link has been turned off…") to the revoked
        state specifically; the resolver hands the route the
        discriminator so the right wording renders.
        """
        ws, prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-rev")
        ctx = _ctx(ws, slug="r-rev")
        # Find link by token to grab its id.
        row = session_guest.scalars(
            select(GuestLink).where(GuestLink.token == token)
        ).one()
        revoke_link(session_guest, ctx, link_id=row.id, clock=frozen_clock)

        welcome = FakeWelcomeResolver()
        welcome.set(workspace_id=ws, stay_id=stay, value=_make_input(property_id=prop))
        result = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )
        assert isinstance(result, GuestLinkGone)
        assert result.reason is GuestLinkGoneReason.REVOKED
        assert result.workspace_id == ws
        assert result.link_id == row.id

    def test_resolve_revoked_beats_expired(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """Revoked + expired together → REVOKED wins.

        The user-facing "ask your host" copy is the more
        actionable signal so revocation precedes expiry in the
        rendered page.
        """
        ws, prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-rev-x")
        ctx = _ctx(ws, slug="r-rev-x")
        row = session_guest.scalars(
            select(GuestLink).where(GuestLink.token == token)
        ).one()
        revoke_link(session_guest, ctx, link_id=row.id, clock=frozen_clock)
        # Now advance past expiry too.
        frozen_clock.advance(timedelta(days=5, seconds=1))

        welcome = FakeWelcomeResolver()
        welcome.set(workspace_id=ws, stay_id=stay, value=_make_input(property_id=prop))
        result = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )
        assert isinstance(result, GuestLinkGone)
        assert result.reason is GuestLinkGoneReason.REVOKED

    def test_resolve_tampered_returns_none(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """A tampered signature collapses to ``None``, not a
        :class:`GuestLinkGone`.

        Distinguishing tampered from expired would let a probing
        attacker tell "valid-but-expired" from "garbage" — closing
        that oracle is why ``resolve_link`` returns ``None`` here.
        The route renders the same EXPIRED page either way.
        """
        ws, prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-tam")
        # Flip a single byte in the middle of the signed blob. The
        # signature must reject — itsdangerous raises BadSignature
        # which we collapse to ``None``.
        tampered = list(token)
        # Pick a position deep inside the blob to ensure we hit the
        # signature, not the trailing dot. Index 5 is well within
        # the base64 payload prefix of any URLSafeTimedSerializer
        # output longer than ~20 chars.
        target = 5
        original = tampered[target]
        # Cycle to a *different* base64-friendly char.
        replacement = "A" if original != "A" else "B"
        tampered[target] = replacement
        bad_token = "".join(tampered)
        # Sanity: confirm we actually changed something.
        assert bad_token != token

        welcome = FakeWelcomeResolver()
        welcome.set(workspace_id=ws, stay_id=stay, value=_make_input(property_id=prop))
        result = resolve_link(
            session_guest,
            token=bad_token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )
        assert result is None
        # Welcome resolver was never called — short-circuit on the
        # signature failure before any DB read for the merge.
        assert welcome.calls == []

    def test_resolve_stay_gone_returns_none(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """A deleted stay collapses to ``None``.

        Even though the row's signature is valid and unrevoked,
        the merge inputs are missing — the resolver collapses the
        case to ``None`` so the route renders the generic EXPIRED
        page (no leakage of "your stay is gone").
        """
        ws, _prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-gone")
        # Welcome resolver returns ``None`` to simulate a deleted stay.
        welcome = FakeWelcomeResolver()
        welcome.set(workspace_id=ws, stay_id=stay, value=None)
        result = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )
        assert result is None

    def test_welcome_merge_order_stay_beats_unit_beats_property(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """Spec §04: stay > unit > property for every welcome key."""
        ws, prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-merge")
        welcome = FakeWelcomeResolver()
        welcome.set(
            workspace_id=ws,
            stay_id=stay,
            value=_make_input(
                property_id=prop,
                property_defaults={
                    "wifi_ssid": "VillaWifi",
                    "wifi_password": "property-pwd",
                    "house_rules": "No smoking.",
                },
                unit_overrides={
                    "wifi_password": "unit-pwd",
                    "house_rules": "Unit-specific rules.",
                },
                stay_overrides={
                    "house_rules": "Stay-specific rules.",
                },
                stay_wifi="stay-wifi-pwd",
            ),
        )

        resolved = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )
        assert isinstance(resolved, ResolvedGuestLink)
        merged = resolved.bundle.welcome
        # Property-only key flows through.
        assert merged["wifi_ssid"] == "VillaWifi"
        # Stay wifi override beats both unit and property.
        assert merged["wifi_password"] == "stay-wifi-pwd"
        # Stay overrides beat unit overrides for non-wifi keys.
        assert merged["house_rules"] == "Stay-specific rules."

    def test_welcome_merge_unit_beats_property_when_no_stay(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """Without any stay-level override, unit beats property."""
        ws, prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-merg2")
        welcome = FakeWelcomeResolver()
        welcome.set(
            workspace_id=ws,
            stay_id=stay,
            value=_make_input(
                property_id=prop,
                property_defaults={"wifi_password": "property-pwd"},
                unit_overrides={"wifi_password": "unit-pwd"},
            ),
        )
        resolved = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )
        assert isinstance(resolved, ResolvedGuestLink)
        assert resolved.bundle.welcome["wifi_password"] == "unit-pwd"

    def test_equipment_hidden_when_setting_false(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ws, prop, stay, token = self._setup(
            session_guest, frozen_clock, slug="r-eq-off"
        )
        welcome = FakeWelcomeResolver()
        welcome.set(
            workspace_id=ws,
            stay_id=stay,
            value=_make_input(
                property_id=prop,
                assets=(
                    GuestAsset(
                        id="a1",
                        name="Espresso machine",
                        guest_instructions_md="See manual.",
                        cover_photo_url=None,
                        guest_visible=True,
                    ),
                ),
            ),
        )
        # Setting defaults to False.
        resolved = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=FakeSettingsResolver(),
            clock=frozen_clock,
        )
        assert isinstance(resolved, ResolvedGuestLink)
        assert resolved.bundle.assets == ()

    def test_equipment_visible_when_setting_true_and_visible_assets(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ws, prop, stay, token = self._setup(session_guest, frozen_clock, slug="r-eq-on")
        welcome = FakeWelcomeResolver()
        welcome.set(
            workspace_id=ws,
            stay_id=stay,
            value=_make_input(
                property_id=prop,
                assets=(
                    GuestAsset(
                        id="a1",
                        name="Espresso machine",
                        guest_instructions_md="See manual.",
                        cover_photo_url=None,
                        guest_visible=True,
                    ),
                    GuestAsset(
                        id="a2",
                        name="Locked safe",
                        guest_instructions_md="Staff only.",
                        cover_photo_url=None,
                        guest_visible=False,
                    ),
                ),
            ),
        )
        settings_resolver = FakeSettingsResolver(
            defaults={"assets.show_guest_assets": True}
        )
        resolved = resolve_link(
            session_guest,
            token=token,
            welcome_resolver=welcome,
            settings_resolver=settings_resolver,
            clock=frozen_clock,
        )
        assert isinstance(resolved, ResolvedGuestLink)
        # Defence-in-depth: only ``guest_visible=True`` assets render
        # even though the setting is on.
        assert tuple(a.id for a in resolved.bundle.assets) == ("a1",)


# ---------------------------------------------------------------------------
# record_access
# ---------------------------------------------------------------------------


class TestRecordAccess:
    """Branch coverage for ``record_access``."""

    def _mint(
        self, session: Session, frozen: FrozenClock, *, slug: str
    ) -> tuple[WorkspaceContext, str]:
        ws = _bootstrap_workspace(session, slug=slug)
        prop = _bootstrap_property(session, ws)
        check_out = _PINNED + timedelta(days=4)
        stay = _bootstrap_reservation(
            session,
            workspace_id=ws,
            property_id=prop,
            check_in=_PINNED,
            check_out=check_out,
        )
        ctx = _ctx(ws, slug=slug)
        link = mint_link(
            session,
            ctx,
            stay_id=stay,
            property_id=prop,
            check_out_at=check_out,
            clock=frozen,
        )
        return ctx, link.id

    def test_record_hashes_ipv4_and_classifies_ua(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ctx, link_id = self._mint(session_guest, frozen_clock, slug="acc-ok")
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        rec = record_access(
            session_guest,
            ctx,
            link_id=link_id,
            ip="203.0.113.42",
            user_agent=ua,
            clock=frozen_clock,
        )
        assert rec.ua_family == "chrome"
        assert rec.at == _PINNED
        # The hash never includes the raw IP.
        assert "203.0.113.42" not in rec.ip_prefix_sha256
        assert len(rec.ip_prefix_sha256) == 64  # SHA-256 hex
        # Two callers from the same /24 must hash to the same bucket.
        rec_neighbour = record_access(
            session_guest,
            ctx,
            link_id=link_id,
            ip="203.0.113.99",  # same /24 as .42
            user_agent="Mozilla/5.0 Firefox/120.0",
            clock=frozen_clock,
        )
        assert rec_neighbour.ip_prefix_sha256 == rec.ip_prefix_sha256
        assert rec_neighbour.ua_family == "firefox"

    def test_record_truncates_to_last_ten(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ctx, link_id = self._mint(session_guest, frozen_clock, slug="acc-trim")
        # Push 12 records — the column should keep only the last 10.
        for i in range(12):
            frozen_clock.advance(timedelta(seconds=1))
            record_access(
                session_guest,
                ctx,
                link_id=link_id,
                ip=f"198.51.100.{i + 1}",
                user_agent="Mozilla/5.0 Firefox/120.0",
                clock=frozen_clock,
            )
        row = session_guest.get(GuestLink, link_id)
        assert row is not None
        assert len(row.access_log_json) == 10
        # Buffer keeps the **last** 10, so the first two evict.
        # Index 0 in the kept tail should correspond to ip 3 (since
        # we kept records 3..12). Compare via the hash bucket.
        first_kept_hash = row.access_log_json[0]["ip_prefix_sha256"]
        # Re-compute the expected hash for the i=2 (3rd record's IP).
        from app.domain.stays.guest_link_service import _hash_ip_prefix

        assert first_kept_hash == _hash_ip_prefix("198.51.100.3")

    def test_record_ipv6_uses_64_bit_prefix(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        """IPv6 hash collapses /64-siblings + separates /64-strangers.

        Aligns with the §03 audit-log + §15 aggregation conventions
        — every privacy-prefix bucket is /64 across the codebase so
        the same household maps to the same hash regardless of
        which subsystem wrote it.
        """
        from app.domain.stays.guest_link_service import _hash_ip_prefix

        ctx, link_id = self._mint(session_guest, frozen_clock, slug="acc-v6")
        rec = record_access(
            session_guest,
            ctx,
            link_id=link_id,
            ip="2001:db8::1",
            user_agent="Mozilla/5.0 Firefox/120.0",
            clock=frozen_clock,
        )
        # Same /64 (last 64 bits vary) → same bucket.
        sibling_hash = _hash_ip_prefix("2001:db8:0:0::beef")
        assert rec.ip_prefix_sha256 == sibling_hash
        # Different /64 (third hextet flips) → different bucket.
        stranger_hash = _hash_ip_prefix("2001:db8:0:abcd::beef")
        assert rec.ip_prefix_sha256 != stranger_hash

    def test_record_audit_row(
        self,
        session_guest: Session,
        frozen_clock: FrozenClock,
    ) -> None:
        ctx, link_id = self._mint(session_guest, frozen_clock, slug="acc-aud")
        record_access(
            session_guest,
            ctx,
            link_id=link_id,
            ip="203.0.113.7",
            user_agent="Mozilla/5.0 Firefox/120.0",
            clock=frozen_clock,
        )
        accessed = list(
            session_guest.scalars(
                select(AuditLog).where(
                    AuditLog.entity_id == link_id, AuditLog.action == "accessed"
                )
            )
        )
        assert len(accessed) == 1
        diff = accessed[0].diff
        # No raw IP in the audit diff — only the hash prefix.
        assert "203.0.113.7" not in repr(diff)
        assert diff["after"]["log_length"] == 1


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


def test_token_does_not_carry_workspace_id(
    session_guest: Session,
    frozen_clock: FrozenClock,
) -> None:
    """§04 privacy: the workspace_id is never embedded in the token."""
    ws = _bootstrap_workspace(session_guest, slug="no-ws-token")
    prop = _bootstrap_property(session_guest, ws)
    check_out = _PINNED + timedelta(days=4)
    stay = _bootstrap_reservation(
        session_guest,
        workspace_id=ws,
        property_id=prop,
        check_in=_PINNED,
        check_out=check_out,
    )
    ctx = _ctx(ws, slug="no-ws-token")
    link = mint_link(
        session_guest,
        ctx,
        stay_id=stay,
        property_id=prop,
        check_out_at=check_out,
        clock=frozen_clock,
    )
    # Pull the unsealed payload — itsdangerous serializes JSON as
    # base64url; the workspace id is a 26-char Crockford ULID and
    # would appear verbatim if it were in the payload.
    from itsdangerous import URLSafeTimedSerializer

    keys = HmacSigner(
        session_guest, settings=get_settings(), clock=frozen_clock
    ).verification_keys(purpose="guest-link")
    serializer = URLSafeTimedSerializer(secret_key=keys, salt="guest-link-v1")
    payload = serializer.loads(link.token)
    assert isinstance(payload, dict)
    assert "workspace_id" not in payload
    # ``stay_id`` and ``property_id`` are present per the spec contract.
    assert payload["stay_id"] == stay
    assert payload["property_id"] == prop


def test_protocol_classes_runtime_checkable() -> None:
    """The port classes are mypy-checkable but not necessarily runtime."""
    # Just touching the symbols is enough — this guards against an
    # accidental rename on the Protocol that would break downstream
    # callers.
    assert WelcomeResolver is not None
    assert SettingsResolver is not None


# Ensure the module-level env teardown actually busts the cache for
# downstream tests in the same process. The pytest-monkeypatch
# fixture handles env restoration; we just need to bust ``Settings``.
def teardown_module() -> None:
    """Reset the lru-cached Settings so neighbouring tests start clean."""
    # Restore root-key state if a calling test happened to set it.
    os.environ.pop("CREWDAY_ROOT_KEY", None)
    get_settings.cache_clear()
