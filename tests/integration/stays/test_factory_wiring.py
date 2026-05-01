"""Integration coverage of stays subscriber wiring inside the factory.

Pins **production** wiring rather than a hand-rolled bus + handler
combo: a real :func:`~app.api.factory.create_app` build registers
both :func:`app.domain.stays.bundle_service.register_subscriptions`
and :func:`app.domain.stays.turnover_generator.register_subscriptions`
against the singleton :data:`app.events.bus.bus`. Publishing a real
:class:`~app.events.types.ReservationUpserted` then materialises a
:class:`~app.adapters.db.stays.models.StayBundle` row and invokes the
no-op :class:`~app.ports.tasks_create_occurrence.TasksCreateOccurrencePort`
twice — once per subscriber.

The SQLAlchemy concretion of the tasks-side port lands with cd-ncbdb;
until then both subscribers run against the no-op port. The test
asserts the port was *called* (not that an :class:`Occurrence` row
persisted) so the noop scope is honest, per the cd-87u7m intake.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles"
and ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.places.models import Property
from app.adapters.db.session import bind_active_session
from app.adapters.db.stays.models import Reservation, StayBundle
from app.adapters.db.workspace.models import Workspace
from app.config import Settings
from app.domain.stays import bundle_service, turnover_generator
from app.events.bus import bus as singleton_bus
from app.events.types import ReservationUpserted
from app.main import create_app
from app.ports.tasks_create_occurrence import (
    NoopTasksCreateOccurrencePort,
    TurnoverOccurrenceRequest,
    TurnoverOccurrenceResult,
)
from app.tenancy import reset_current, set_current, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"
_CORRELATION_ID = "01HWA00000000000000000CRL1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-stays-factory-wiring-root-key"),
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
    """Redirect the process-wide default UoW to the integration engine."""
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def reset_stays_subscriptions() -> Iterator[None]:
    """Scrub the singleton bus's stays subscribers around the test.

    The singleton bus persists across tests in the process; without
    this scrub a second test (or a test that ran after another test
    that called :func:`create_app`) would observe stale subscribers
    AND its own newly-registered ones, double-firing the handler.
    The dedup sets in :mod:`bundle_service` and
    :mod:`turnover_generator` are also reset so the next factory
    build re-installs cleanly.
    """
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
# Bootstraps (mirror the unit / e2e suites so this file stays standalone)
# ---------------------------------------------------------------------------


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    # justification: workspace seeding for cross-tenant test setup
    with tenant_agnostic():
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


def _bootstrap_property(session: Session) -> str:
    pid = new_ulid()
    # justification: property is not workspace-scoped (multi-belonging)
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


def _make_event(*, ctx: WorkspaceContext, reservation_id: str) -> ReservationUpserted:
    return ReservationUpserted(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id,
        correlation_id=ctx.audit_correlation_id,
        occurred_at=_PINNED,
        reservation_id=reservation_id,
        feed_id=None,
        change_kind="created",
    )


def _ctx_for(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="factory-wiring",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=_CORRELATION_ID,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFactoryWiresStaysSubscriptions:
    """``create_app`` wires both stays subscribers; events drive the handlers."""

    def test_publish_materialises_bundle_and_calls_port(
        self,
        db_session: Session,
        pinned_settings: Settings,
        real_make_uow: None,
        reset_stays_subscriptions: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Record every port call the noop double services. We
        # monkeypatch the class method so the real wiring (which
        # constructs the noop port inside ``_register_stays_subscriptions``)
        # routes through our recorder without us threading a custom
        # port into the factory.
        port_calls: list[TurnoverOccurrenceRequest] = []
        original = NoopTasksCreateOccurrencePort.create_or_patch_turnover_occurrence

        def _recording_call(
            self: NoopTasksCreateOccurrencePort,
            session: Session,
            ctx: WorkspaceContext,
            *,
            request: TurnoverOccurrenceRequest,
            now: datetime,
        ) -> TurnoverOccurrenceResult:
            port_calls.append(request)
            return original(self, session, ctx, request=request, now=now)

        monkeypatch.setattr(
            NoopTasksCreateOccurrencePort,
            "create_or_patch_turnover_occurrence",
            _recording_call,
        )

        # Build the real factory app. The build itself registers both
        # stays subscribers on the singleton bus.
        create_app(settings=pinned_settings)

        # Both subscribers should be present on the singleton bus.
        # Reading the private subscriber map is acceptable in a test
        # whose explicit purpose is to pin the wiring contract; the
        # matching public API would be a count helper we don't want
        # to ship just for this assertion.
        handlers = singleton_bus._subscribers.get(ReservationUpserted.name, [])
        assert len(handlers) == 2, (
            "Expected exactly two ReservationUpserted handlers (bundle_service + "
            f"turnover_generator); got {len(handlers)}."
        )

        # Seed the data the bundle handler will read against.
        workspace_id = _bootstrap_workspace(db_session, slug="factory-wiring")
        prop = _bootstrap_property(db_session)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            db_session,
            workspace_id=workspace_id,
            property_id=prop,
            check_in=_PINNED + timedelta(days=1),
            check_out=check_out,
        )
        # Next stay on the same property bounds the after-checkout
        # window — without it the handler skips with
        # ``skipped_no_next_stay`` and never reaches the port.
        _bootstrap_reservation(
            db_session,
            workspace_id=workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )

        ctx = _ctx_for(workspace_id)

        # Publish through the singleton bus that the factory wired.
        # Bind the active session + workspace ctx the way a real
        # publisher (poll_ical inside ``with make_uow():``) does so
        # the lifespan-installed ``session_provider`` can recover
        # both. The integration test must NOT call
        # ``register_subscriptions`` itself (cd-87u7m intake).
        ctx_token = set_current(ctx)
        try:
            with bind_active_session(db_session):
                singleton_bus.publish(_make_event(ctx=ctx, reservation_id=rid))
        finally:
            reset_current(ctx_token)

        # bundle_service materialised a StayBundle row and recorded
        # the port outcome on it. The noop port returns ``"noop"``
        # without persisting anything.
        bundles = db_session.scalars(
            select(StayBundle).where(StayBundle.reservation_id == rid)
        ).all()
        assert len(bundles) == 1
        bundle = bundles[0]
        assert bundle.kind == "turnover"
        assert any(
            entry.get("port_outcome") == "noop" for entry in bundle.tasks_json
        ), bundle.tasks_json

        # The noop port saw two calls — one from each subscriber.
        # bundle_service's call carries a ``stay_task_bundle_id``;
        # turnover_generator's call does not. Asserting both shapes
        # were observed pins both wirings end-to-end.
        assert len(port_calls) == 2, port_calls
        bundle_call = next(
            (call for call in port_calls if call.stay_task_bundle_id is not None),
            None,
        )
        turnover_call = next(
            (call for call in port_calls if call.stay_task_bundle_id is None),
            None,
        )
        assert bundle_call is not None, port_calls
        assert turnover_call is not None, port_calls
        assert bundle_call.reservation_id == rid
        assert turnover_call.reservation_id == rid

    def test_factory_subscription_is_idempotent(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        reset_stays_subscriptions: None,
    ) -> None:
        # A second ``create_app`` with the same singleton bus must
        # not double-subscribe. The dedup lives inside each module's
        # ``_SUBSCRIBED_BUSES`` set, keyed on bus identity.
        create_app(settings=pinned_settings)
        create_app(settings=pinned_settings)

        handlers = singleton_bus._subscribers.get(ReservationUpserted.name, [])
        assert len(handlers) == 2, (
            "Expected idempotent subscription (one bundle + one turnover handler); "
            f"got {len(handlers)} after a second create_app build."
        )
