"""Unit tests for stay task bundle generation."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.stays.models import Reservation, StayBundle
from app.adapters.db.workspace.models import Workspace
from app.config import get_settings
from app.domain.stays.bundle_service import (
    StayLifecycleRule,
    _reset_subscriptions_for_tests,
    generate_bundles_for_stay,
    reapply_bundles_for_stay,
    register_subscriptions,
)
from app.domain.stays.turnover_generator import StaticReservationContextResolver
from app.events.bus import EventBus
from app.events.types import ReservationChangeKind, ReservationUpserted
from app.ports.tasks_create_occurrence import RecordingTasksCreateOccurrencePort
from app.tenancy import reset_current, set_current
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"
_CORRELATION_ID = "01HWA00000000000000000CRL1"


def _load_all_models() -> None:
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
def fixture_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv(
        "CREWDAY_ROOT_KEY",
        "test-root-key-cd-sdo-deterministic-fixed-32+ chars long for HKDF",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(name="engine_bundle")
def fixture_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_bundle")
def fixture_session(engine_bundle: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_bundle, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture(name="ctx")
def fixture_ctx(session_bundle: Session) -> Iterator[WorkspaceContext]:
    ws = _bootstrap_workspace(session_bundle, slug="ws-bundle")
    ctx = WorkspaceContext(
        workspace_id=ws,
        workspace_slug="ws-bundle",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=_CORRELATION_ID,
    )
    token = set_current(ctx)
    try:
        yield ctx
    finally:
        reset_current(token)


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


def _bootstrap_property(session: Session) -> str:
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


def _make_event(
    *,
    ctx: WorkspaceContext,
    reservation_id: str,
    change_kind: ReservationChangeKind = "created",
) -> ReservationUpserted:
    return ReservationUpserted(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id,
        correlation_id=ctx.audit_correlation_id,
        occurred_at=_PINNED,
        reservation_id=reservation_id,
        feed_id=None,
        change_kind=change_kind,
    )


def _rules() -> tuple[StayLifecycleRule, ...]:
    return (
        StayLifecycleRule(
            id="rule-after",
            trigger="after_checkout",
            duration=timedelta(hours=2),
            kind="turnover",
            guest_kind_filter=("guest",),
        ),
        StayLifecycleRule(
            id="rule-before",
            trigger="before_checkin",
            duration=timedelta(hours=1),
            kind="welcome",
            offset_hours=5,
            guest_kind_filter=("guest",),
        ),
        StayLifecycleRule(
            id="rule-during",
            trigger="during_stay",
            duration=timedelta(minutes=45),
            kind="deep_clean",
            rrule="FREQ=DAILY;COUNT=2",
            guest_kind_filter=("guest",),
        ),
    )


def test_generates_one_bundle_per_rule_and_tags_occurrence_requests(
    session_bundle: Session,
    ctx: WorkspaceContext,
) -> None:
    prop = _bootstrap_property(session_bundle)
    check_in = _PINNED + timedelta(days=1)
    check_out = _PINNED + timedelta(days=5)
    rid = _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=check_in,
        check_out=check_out,
    )
    _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=check_out + timedelta(days=1),
        check_out=check_out + timedelta(days=4),
    )

    port = RecordingTasksCreateOccurrencePort()
    result = generate_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        port=port,
        rules=_rules(),
        now=_PINNED,
    )
    second = generate_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        port=port,
        rules=_rules(),
        now=_PINNED,
    )

    assert [outcome.decision for outcome in result.per_rule] == [
        "materialised",
        "materialised",
        "materialised",
    ]
    assert [outcome.occurrences[0].port_outcome for outcome in second.per_rule] == [
        "noop",
        "noop",
        "noop",
    ]
    bundles = session_bundle.scalars(
        select(StayBundle).where(StayBundle.reservation_id == rid)
    ).all()
    assert len(bundles) == 3
    assert {request.stay_task_bundle_id for request in port.calls} == {
        bundle.id for bundle in bundles
    }
    assert [request.occurrence_key for request in port.calls[:4]] == [
        "after_checkout",
        "before_checkin",
        "during_stay:0",
        "during_stay:1",
    ]


def test_same_trigger_rules_do_not_share_occurrence_idempotency_key(
    session_bundle: Session,
    ctx: WorkspaceContext,
) -> None:
    prop = _bootstrap_property(session_bundle)
    check_out = _PINNED + timedelta(days=4)
    rid = _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=_PINNED + timedelta(days=1),
        check_out=check_out,
    )
    _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=check_out + timedelta(days=1),
        check_out=check_out + timedelta(days=4),
    )
    rules = (
        StayLifecycleRule(
            id="rule-after-fast",
            trigger="after_checkout",
            duration=timedelta(hours=1),
            kind="turnover",
            guest_kind_filter=("guest",),
        ),
        StayLifecycleRule(
            id="rule-after-deep",
            trigger="after_checkout",
            duration=timedelta(hours=3),
            kind="turnover",
            guest_kind_filter=("guest",),
            ordinal=1,
        ),
    )
    port = RecordingTasksCreateOccurrencePort()

    first = generate_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        port=port,
        rules=rules,
        now=_PINNED,
    )
    second = generate_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        port=port,
        rules=rules,
        now=_PINNED,
    )

    assert [outcome.occurrences[0].port_outcome for outcome in first.per_rule] == [
        "created",
        "created",
    ]
    assert [outcome.occurrences[0].port_outcome for outcome in second.per_rule] == [
        "noop",
        "noop",
    ]
    assert {outcome.occurrences[0].occurrence_id for outcome in first.per_rule} == {
        "rec_occ_1",
        "rec_occ_2",
    }


def test_guest_kind_filter_skips_rule(
    session_bundle: Session,
    ctx: WorkspaceContext,
) -> None:
    prop = _bootstrap_property(session_bundle)
    rid = _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=_PINNED + timedelta(days=1),
        check_out=_PINNED + timedelta(days=2),
    )
    _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=_PINNED + timedelta(days=3),
        check_out=_PINNED + timedelta(days=4),
    )
    owner_resolver = StaticReservationContextResolver(guest_kind="owner")
    port = RecordingTasksCreateOccurrencePort()

    result = generate_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        port=port,
        resolver=owner_resolver,
        rules=_rules(),
        now=_PINNED,
    )

    assert {outcome.decision for outcome in result.per_rule} == {"skipped_guest_kind"}
    assert port.calls == []
    assert session_bundle.scalars(select(StayBundle)).all() == []


def test_during_stay_excludes_checkout_boundary_and_uses_stable_keys(
    session_bundle: Session,
    ctx: WorkspaceContext,
) -> None:
    prop = _bootstrap_property(session_bundle)
    original_check_in = _PINNED + timedelta(days=1)
    original_check_out = original_check_in + timedelta(days=2)
    rid = _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=original_check_in,
        check_out=original_check_out,
    )
    rules = (
        StayLifecycleRule(
            id="rule-during-stable",
            trigger="during_stay",
            duration=timedelta(minutes=45),
            kind="deep_clean",
            rrule="FREQ=DAILY;COUNT=3",
            guest_kind_filter=("guest",),
        ),
    )
    port = RecordingTasksCreateOccurrencePort()
    first = generate_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        port=port,
        rules=rules,
        now=_PINNED,
    )

    assert [item.occurrence_key for item in first.per_rule[0].occurrences] == [
        "during_stay:0",
        "during_stay:1",
    ]
    reservation = session_bundle.get(Reservation, rid)
    assert reservation is not None
    reservation.check_in = original_check_in + timedelta(hours=4)
    reservation.check_out = original_check_out + timedelta(hours=4)
    reapplied = reapply_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        previous_check_in=original_check_in,
        previous_check_out=original_check_out,
        port=port,
        rules=rules,
        now=_PINNED,
    )

    assert [item.port_outcome for item in reapplied.per_rule[0].occurrences] == [
        "regenerated",
        "regenerated",
    ]


def test_reapply_patches_small_shift_and_regenerates_large_shift(
    session_bundle: Session,
    ctx: WorkspaceContext,
) -> None:
    prop = _bootstrap_property(session_bundle)
    original_check_in = _PINNED + timedelta(days=1)
    original_check_out = _PINNED + timedelta(days=4)
    rid = _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=original_check_in,
        check_out=original_check_out,
    )
    _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=original_check_out + timedelta(days=2),
        check_out=original_check_out + timedelta(days=5),
    )
    rules = _rules()[:2]
    port = RecordingTasksCreateOccurrencePort()
    generate_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        port=port,
        rules=rules,
        now=_PINNED,
    )

    reservation = session_bundle.get(Reservation, rid)
    assert reservation is not None
    reservation.check_in = original_check_in + timedelta(hours=2)
    reservation.check_out = original_check_out + timedelta(hours=2)
    small = reapply_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        previous_check_in=original_check_in,
        previous_check_out=original_check_out,
        port=port,
        rules=rules,
        now=_PINNED,
    )
    assert [outcome.occurrences[0].port_outcome for outcome in small.per_rule] == [
        "patched",
        "patched",
    ]

    previous_check_in = reservation.check_in
    previous_check_out = reservation.check_out
    reservation.check_in = previous_check_in + timedelta(hours=4)
    reservation.check_out = previous_check_out + timedelta(hours=4)
    large = reapply_bundles_for_stay(
        session_bundle,
        ctx,
        reservation_id=rid,
        previous_check_in=previous_check_in,
        previous_check_out=previous_check_out,
        port=port,
        rules=rules,
        now=_PINNED,
    )

    assert [outcome.occurrences[0].port_outcome for outcome in large.per_rule] == [
        "regenerated",
        "regenerated",
    ]
    bundles = session_bundle.scalars(
        select(StayBundle).where(StayBundle.reservation_id == rid)
    ).all()
    assert len(bundles) == 2
    assert all(
        entry.get("cancellation_reason") == "stay rescheduled"
        for bundle in bundles
        for entry in bundle.tasks_json
    )


def test_register_subscriptions_uses_session_provider(
    session_bundle: Session,
    ctx: WorkspaceContext,
) -> None:
    prop = _bootstrap_property(session_bundle)
    check_out = _PINNED + timedelta(days=4)
    rid = _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=_PINNED + timedelta(days=1),
        check_out=check_out,
    )
    _bootstrap_reservation(
        session_bundle,
        workspace_id=ctx.workspace_id,
        property_id=prop,
        check_in=check_out + timedelta(days=1),
        check_out=check_out + timedelta(days=4),
    )
    bus = EventBus()
    port = RecordingTasksCreateOccurrencePort()
    _reset_subscriptions_for_tests()
    bus._reset_for_tests()

    register_subscriptions(
        bus,
        port=port,
        session_provider=lambda event: (session_bundle, ctx),
        rules=_rules()[:1],
    )
    register_subscriptions(
        bus,
        port=port,
        session_provider=lambda event: (session_bundle, ctx),
        rules=_rules()[:1],
    )
    bus.publish(_make_event(ctx=ctx, reservation_id=rid))

    assert len(port.calls) == 1
    assert port.calls[0].reservation_id == rid
