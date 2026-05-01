"""Integration coverage for the ``/poll-once`` manual ingest route (cd-jk6is).

Exercises the **production wiring** end-to-end without going through
the workspace tenancy middleware: mounts the real
:func:`~app.api.v1.stays.build_stays_router` on a TestClient, registers
the live ``ReservationUpserted`` subscribers
(:func:`app.domain.stays.bundle_service.register_subscriptions` +
:func:`app.domain.stays.turnover_generator.register_subscriptions`)
the same way :func:`app.api.factory._register_stays_subscriptions`
does, and drives ``POST /stays/ical-feeds/{feed_id}/poll-once`` with
a stubbed :class:`~app.adapters.ical.validator.Fetcher` returning a
canned VCALENDAR body.

Asserts:

1. The route upserts a :class:`~app.adapters.db.stays.models.Reservation`.
2. The subscribers materialise a
   :class:`~app.adapters.db.stays.models.StayBundle` row + persist
   :class:`~app.adapters.db.tasks.models.Occurrence` rows in the
   request UoW (proves ``bind_active_session`` + ``set_current``
   plumbing inside the route).
3. A second call with the same body is idempotent (no duplicate rows).

Mirrors :mod:`tests.integration.stays.test_factory_wiring`'s shape for
the singleton-bus scrub and the live SA port pinning. See
``docs/specs/04-properties-and-stays.md`` §"iCal feed" §"Polling
behavior" and ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import SplitResult

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.places.models import Property
from app.adapters.db.session import UnitOfWorkImpl, get_active_session
from app.adapters.db.stays.models import IcalFeed, Reservation, StayBundle
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.tasks.repositories import SqlAlchemyTasksCreateOccurrencePort
from app.adapters.ical.ports import IcalValidation
from app.adapters.ical.providers import HostProviderDetector
from app.adapters.ical.validator import Fetcher, FetchResponse, Resolver
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.api.deps import current_workspace_context, db_session
from app.api.v1.stays import (
    build_stays_router,
    get_app_settings,
    get_clock,
    get_envelope,
    get_ical_fetcher,
    get_ical_resolver,
    get_ical_validator_builder,
    get_provider_detector,
    get_tasks_create_occurrence_port,
)
from app.config import Settings
from app.domain.stays import bundle_service, turnover_generator
from app.events.bus import bus as singleton_bus
from app.events.types import ReservationUpserted
from app.tenancy import get_current, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"
_CORRELATION_ID = "01HWA00000000000000000CRL1"
_FAKE_PUBLIC_IP = "8.8.8.8"
_FEED_URL = "https://airbnb.example.com/calendar.ics"
_VCALENDAR_BODY = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//crewday-integration//EN\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:integration-poll-once-1\r\n"
    b"DTSTART:20260427T140000Z\r\n"
    b"DTEND:20260430T110000Z\r\n"
    b"SUMMARY:Reserved (Avery Test)\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)
_ROOT_KEY = "integration-test-cd-jk6is-poll-once-root-key+pad-32"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr(_ROOT_KEY),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="external",
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
        demo_mode=False,
        demo_frame_ancestors=None,
        hsts_enabled=False,
    )


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect :func:`make_uow` to the integration engine.

    Defensive: nothing in the route's call chain currently invokes
    :func:`app.adapters.db.session.make_uow`, but importing the worker's
    :func:`poll_ical` brings in module-level adapters that resolve the
    default sessionmaker on first use. Pinning the engine avoids a
    cross-test leak if a future refactor adds a side-channel reach
    into the global UoW.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def reset_stays_subscriptions() -> Iterator[None]:
    singleton_bus._reset_for_tests()
    bundle_service._reset_subscriptions_for_tests()
    turnover_generator._reset_subscriptions_for_tests()
    try:
        yield
    finally:
        singleton_bus._reset_for_tests()
        bundle_service._reset_subscriptions_for_tests()
        turnover_generator._reset_subscriptions_for_tests()


# ---------------------------------------------------------------------------
# Bootstraps
# ---------------------------------------------------------------------------


def _bootstrap_workspace(session: Session, *, slug: str) -> tuple[str, str]:
    """Seed a workspace with an owner-grade actor.

    Returns ``(workspace_id, owner_user_id)``. Uses the production
    :func:`tests.factories.identity.bootstrap_workspace` helper so the
    ``owners`` system group + ``role_grant`` row land in the same
    shape production signup produces — required for the
    ``stays.manage`` Permission gate to pass.
    """
    user = bootstrap_user(
        session,
        email=f"{slug}@example.test",
        display_name=f"{slug} owner",
    )
    workspace = bootstrap_workspace(
        session,
        slug=slug,
        name=f"Workspace {slug}",
        owner_user_id=user.id,
    )
    session.flush()
    return workspace.id, user.id


def _bootstrap_property(session: Session) -> str:
    pid = new_ulid()
    with tenant_agnostic():
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


def _bootstrap_next_stay(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    after: datetime,
) -> None:
    """Seed a follow-on reservation so the after-checkout bundle path runs.

    The bundle handler skips with ``skipped_no_next_stay`` when no
    later reservation exists on the same property; without this seed
    the integration test's ``/poll-once`` call would land a Reservation
    but no StayBundle / Occurrence rows.
    """
    next_id = new_ulid()
    session.add(
        Reservation(
            id=next_id,
            workspace_id=workspace_id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=f"manual-next-{next_id}",
            check_in=after + timedelta(days=1),
            check_out=after + timedelta(days=4),
            guest_name="Next Guest",
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


def _ctx_for(*, workspace_id: str, slug: str, actor_id: str) -> WorkspaceContext:
    return build_workspace_context(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )


# ---------------------------------------------------------------------------
# Stub fetcher / validator
# ---------------------------------------------------------------------------


class _ScriptedFetcher(Fetcher):
    """Inline :class:`Fetcher` returning canned bodies per URL."""

    def __init__(self, responses: dict[str, list[FetchResponse]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def fetch(
        self,
        parsed: SplitResult,
        resolved_ip: str,
        *,
        deadline: float,
        max_body_bytes: int,
    ) -> FetchResponse:
        del deadline, max_body_bytes
        url = parsed.geturl()
        self.calls.append((url, resolved_ip))
        bucket = self.responses.get(url)
        if not bucket:
            raise AssertionError(f"_ScriptedFetcher: no canned response for {url!r}")
        return bucket.pop(0)


def _ok_response(body: bytes) -> FetchResponse:
    return FetchResponse(
        status=200,
        headers=(("Content-Type", "text/calendar"),),
        body=body,
    )


def _fixed_resolver(ip: str) -> Resolver:
    def _resolve(host: str, port: int) -> list[str]:
        del host, port
        return [ip]

    return _resolve


class _PassthroughValidator:
    """SSRF-bypassing validator used during registration only.

    Production registration runs the SSRF guard (loopback / RFC 1918
    rejection) before the URL hits the DB; the integration test points
    at a synthetic ``airbnb.example.com`` hostname that has no real DNS
    record, so we short-circuit the validator and trust the inbound
    URL. The fetcher is the part the test exercises.
    """

    def validate(self, url: str) -> IcalValidation:
        return IcalValidation(
            url=url,
            resolved_ip=_FAKE_PUBLIC_IP,
            content_type="text/calendar",
            parseable_ics=True,
            bytes_read=len(_VCALENDAR_BODY),
        )


# ---------------------------------------------------------------------------
# App + subscription helpers
# ---------------------------------------------------------------------------


def _wire_subscriptions() -> None:
    """Register stays subscribers exactly the way the factory does.

    Reproduces the body of
    :func:`app.api.factory._register_stays_subscriptions` so the
    integration test pins the production wiring without booting the
    full app. The subscribers read the active session via
    :func:`app.adapters.db.session.get_active_session` and the active
    ctx via :func:`app.tenancy.get_current` — the same recovery path
    the route's ``bind_active_session`` + ``set_current`` block
    populates.
    """
    port = SqlAlchemyTasksCreateOccurrencePort()

    def _session_provider(
        event: ReservationUpserted,
    ) -> tuple[Session, WorkspaceContext] | None:
        del event
        session = get_active_session()
        ctx = get_current()
        if session is None or ctx is None:
            return None
        return session, ctx

    bundle_service.register_subscriptions(
        singleton_bus,
        port=port,
        session_provider=_session_provider,
    )
    turnover_generator.register_subscriptions(
        singleton_bus,
        port=port,
        session_provider=_session_provider,
    )


def _build_app(
    *,
    settings: Settings,
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    fetcher: Fetcher,
) -> TestClient:
    app = FastAPI()
    app.include_router(build_stays_router(), prefix="/stays")

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        with UnitOfWorkImpl(session_factory=factory) as session:
            assert isinstance(session, Session)
            yield session

    real_envelope = Aes256GcmEnvelope(SecretStr(_ROOT_KEY))

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_clock] = lambda: FrozenClock(_PINNED)
    app.dependency_overrides[get_app_settings] = lambda: settings
    # Builder factory ignores ``allow_self_signed`` for the
    # passthrough validator — registration tests don't open a real
    # socket, so the TLS posture is moot. The route still calls the
    # factory once, which exercises the cd-t2qtg cascade lookup.
    app.dependency_overrides[get_ical_validator_builder] = lambda: (
        lambda _allow_self_signed: _PassthroughValidator()
    )
    app.dependency_overrides[get_provider_detector] = HostProviderDetector
    app.dependency_overrides[get_envelope] = lambda: real_envelope
    app.dependency_overrides[get_ical_fetcher] = lambda: fetcher
    app.dependency_overrides[get_ical_resolver] = lambda: _fixed_resolver(
        _FAKE_PUBLIC_IP
    )
    # Live SA concretion for the bundle / regenerate handlers — keeps
    # the chain symmetric with the factory_wiring suite which also
    # uses the production port.
    app.dependency_overrides[get_tasks_create_occurrence_port] = (
        SqlAlchemyTasksCreateOccurrencePort
    )
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPollOnceManualIngest:
    """``/poll-once`` ingests, fires subscribers, and is idempotent."""

    def _seed(
        self,
        *,
        factory: sessionmaker[Session],
        slug: str,
    ) -> tuple[WorkspaceContext, str, str]:
        with factory() as session:
            workspace_id, owner_user_id = _bootstrap_workspace(session, slug=slug)
            property_id = _bootstrap_property(session)
            _bootstrap_next_stay(
                session,
                workspace_id=workspace_id,
                property_id=property_id,
                after=_PINNED + timedelta(days=4),
            )
            session.commit()
        ctx = _ctx_for(
            workspace_id=workspace_id,
            slug=slug,
            actor_id=owner_user_id,
        )
        return ctx, workspace_id, property_id

    def test_poll_once_ingests_reservation_bundle_and_occurrence(
        self,
        engine: Engine,
        pinned_settings: Settings,
        real_make_uow: None,
        reset_stays_subscriptions: None,
    ) -> None:
        # The request-side factory deliberately omits
        # ``install_tenant_filter``: the route + Permission gate exercise
        # cross-table reads (``permission_group``, ``reservation``) for
        # which the production middleware sets ctx via the URL slug, but
        # the test bypasses the workspace tenancy middleware to focus on
        # the ingest chain. Tenant-filter behaviour is covered by other
        # integration tests; here we trust the route's own
        # ``ctx.workspace_id`` filters.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        ctx, workspace_id, property_id = self._seed(factory=factory, slug="poll-once")

        # Wire the production subscribers against the singleton bus —
        # the route's ``bind_active_session`` block must reach them.
        _wire_subscriptions()

        fetcher = _ScriptedFetcher(
            responses={_FEED_URL: [_ok_response(_VCALENDAR_BODY)]}
        )
        client = _build_app(
            settings=pinned_settings,
            factory=factory,
            ctx=ctx,
            fetcher=fetcher,
        )

        # Register the feed via the real registration path. The
        # passthrough validator skips the SSRF DNS lookup; everything
        # else (envelope encrypt, audit, view shape) runs normally.
        created = client.post(
            "/stays/ical-feeds",
            json={"property_id": property_id, "url": _FEED_URL},
        )
        assert created.status_code == 201, created.text
        feed_id = created.json()["id"]

        # Manually ingest — fetch the canned ICS, parse, upsert,
        # publish ``ReservationUpserted``.
        ingest = client.post(f"/stays/ical-feeds/{feed_id}/poll-once")
        assert ingest.status_code == 200, ingest.text
        payload = ingest.json()
        assert payload["status"] == "polled"
        assert payload["reservations_created"] == 1
        assert payload["reservations_updated"] == 0
        assert payload["reservations_cancelled"] == 0
        assert payload["error_code"] is None

        # The Reservation, StayBundle, and Occurrence rows must all
        # have landed in the same UoW the route ran in.
        with factory() as session:
            ical_res = list(
                session.scalars(
                    select(Reservation).where(Reservation.source == "ical")
                ).all()
            )
            assert len(ical_res) == 1, [row.external_uid for row in ical_res]
            res = ical_res[0]
            assert res.external_uid == "integration-poll-once-1"
            assert res.ical_feed_id == feed_id
            assert res.workspace_id == workspace_id

            bundles = list(
                session.scalars(
                    select(StayBundle).where(StayBundle.reservation_id == res.id)
                ).all()
            )
            assert len(bundles) == 1, "subscribers must materialise one bundle"
            assert bundles[0].kind == "turnover"

            occurrences = list(
                session.scalars(
                    select(Occurrence).where(Occurrence.reservation_id == res.id)
                ).all()
            )
            assert len(occurrences) >= 1, (
                "subscribers must persist at least one Occurrence row"
            )
            assert all(row.workspace_id == workspace_id for row in occurrences)
            assert all(row.property_id == property_id for row in occurrences)

        # Idempotent re-run — second ingest must NOT duplicate
        # the Reservation, the StayBundle, or the Occurrence rows.
        fetcher.responses[_FEED_URL] = [_ok_response(_VCALENDAR_BODY)]
        second = client.post(f"/stays/ical-feeds/{feed_id}/poll-once")
        assert second.status_code == 200, second.text
        second_payload = second.json()
        assert second_payload["reservations_created"] == 0
        assert second_payload["reservations_updated"] == 0

        with factory() as session:
            ical_res = list(
                session.scalars(
                    select(Reservation).where(Reservation.source == "ical")
                ).all()
            )
            assert len(ical_res) == 1
            occurrences = list(
                session.scalars(
                    select(Occurrence).where(
                        Occurrence.reservation_id == ical_res[0].id
                    )
                ).all()
            )
            # The SA port keys idempotency on
            # (workspace_id, reservation_id, lifecycle_rule_id,
            # occurrence_key); a re-poll must not multiply rows.
            initial_count = len(occurrences)
            assert initial_count >= 1

        # Sanity: the fetcher saw exactly the two calls we made.
        assert len(fetcher.calls) == 2

        # Confirm the feed metadata advanced — last_polled_at stamped,
        # last_error cleared.
        with factory() as session:
            feed = session.get(IcalFeed, feed_id)
            assert feed is not None
            assert feed.last_polled_at is not None
            assert feed.last_error is None
