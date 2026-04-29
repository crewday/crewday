"""Unit tests for billing work-order CRUD and shift accrual."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.billing.models import (
    Organization,
    RateCard,
    WorkOrderShiftAccrual,
)
from app.adapters.db.billing.repositories import SqlAlchemyWorkOrderRepository
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.time.models import Shift
from app.adapters.db.workspace.models import Workspace
from app.domain.billing.work_orders import (
    WorkOrderCreate,
    WorkOrderInvalid,
    WorkOrderPatch,
    WorkOrderService,
    handle_shift_ended,
)
from app.events import EventBus, ShiftEnded, WorkOrderCompleted
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_STARTS = datetime(2026, 4, 29, 8, 0, 0, tzinfo=UTC)


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
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="work-orders",
        actor_id=new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _seed_billing(s: Session) -> tuple[str, str, str, str, str]:
    workspace_id = new_ulid()
    org_id = new_ulid()
    property_id = new_ulid()
    rate_card_id = new_ulid()
    user_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"wo-{workspace_id[-6:].lower()}",
            name="Work Orders",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency="EUR",
            created_at=_PINNED,
        )
    )
    s.flush()
    email = f"worker-{user_id[-6:]}@example.com"
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name="worker",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        Organization(
            id=org_id,
            workspace_id=workspace_id,
            kind="client",
            display_name="Dupont Family",
            billing_address={},
            tax_id=None,
            default_currency="EUR",
            contact_email=None,
            contact_phone=None,
            notes_md=None,
            created_at=_PINNED,
        )
    )
    s.add(
        Property(
            id=property_id,
            name="Billing Villa",
            kind="vacation",
            address="1 Billing Way",
            address_json={"country": "FR"},
            country="FR",
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            default_currency="EUR",
            client_org_id=org_id,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
    )
    s.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label="Billing Villa",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_PINNED,
        )
    )
    s.add(
        RateCard(
            id=rate_card_id,
            workspace_id=workspace_id,
            organization_id=org_id,
            label="Hourly",
            currency="EUR",
            rates_json={"hourly": 4000},
            active_from=_STARTS.date(),
            active_to=None,
        )
    )
    s.flush()
    return workspace_id, org_id, property_id, rate_card_id, user_id


def _service(ctx: WorkspaceContext, bus: EventBus | None = None) -> WorkOrderService:
    return WorkOrderService(ctx, clock=FrozenClock(_PINNED), event_bus=bus)


def test_create_update_state_transitions_audit_and_completed_event_once(
    factory: sessionmaker[Session],
) -> None:
    events: list[WorkOrderCompleted] = []
    event_bus = EventBus()
    event_bus.subscribe(WorkOrderCompleted)(events.append)
    with factory() as s:
        workspace_id, org_id, property_id, rate_card_id, _user_id = _seed_billing(s)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyWorkOrderRepository(s)
        service = _service(ctx, event_bus)

        created = service.create(
            repo,
            WorkOrderCreate(
                organization_id=org_id,
                property_id=property_id,
                title="Replace pool pump seal",
                starts_at=_STARTS,
                rate_card_id=rate_card_id,
            ),
        )
        patched = service.update(
            repo,
            created.id,
            WorkOrderPatch(fields={"title": "Replace pump seal"}),
        )
        in_progress = service.mark_in_progress(repo, created.id)
        completed = service.complete(repo, created.id)
        completed_again = service.complete(repo, created.id)

        assert patched.title == "Replace pump seal"
        assert in_progress.status == "in_progress"
        assert completed.status == "completed"
        assert completed_again.status == "completed"
        assert [event.work_order_id for event in events] == [created.id]
        assert [row.action for row in s.scalars(select(AuditLog)).all()] == [
            "billing.work_order.created",
            "billing.work_order.updated",
            "billing.work_order.state_changed",
            "billing.work_order.state_changed",
        ]


def test_bad_transition_and_rate_change_after_accrual_are_rejected(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id, org_id, property_id, rate_card_id, user_id = _seed_billing(s)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyWorkOrderRepository(s)
        service = _service(ctx, EventBus())
        work_order = service.create(
            repo,
            WorkOrderCreate(
                organization_id=org_id,
                property_id=property_id,
                title="Hourly repair",
                starts_at=_STARTS,
                rate_card_id=rate_card_id,
            ),
        )

        with pytest.raises(WorkOrderInvalid, match="cannot transition"):
            service.complete(repo, work_order.id)

        service.mark_in_progress(repo, work_order.id)
        shift_id = new_ulid()
        ended_at = _STARTS + timedelta(hours=1, minutes=30)
        s.add(
            Shift(
                id=shift_id,
                workspace_id=workspace_id,
                user_id=user_id,
                starts_at=_STARTS,
                ends_at=ended_at,
                property_id=property_id,
                source="manual",
                notes_md=None,
                approved_by=None,
                approved_at=None,
            )
        )
        s.flush()
        handle_shift_ended(
            ShiftEnded(
                workspace_id=workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=_PINNED,
                shift_id=shift_id,
                ended_at=ended_at,
            ),
            repo=repo,
            ctx=ctx,
            clock=FrozenClock(_PINNED),
            event_bus=EventBus(),
        )

        accrued = service.get(repo, work_order.id)
        assert accrued.total_hours_decimal == Decimal("1.50")
        assert accrued.total_cents == 6000
        assert s.scalars(select(WorkOrderShiftAccrual)).one().shift_id == shift_id

        with pytest.raises(WorkOrderInvalid, match="rate_card_id is locked"):
            service.update(
                repo,
                work_order.id,
                WorkOrderPatch(fields={"rate_card_id": None}),
            )


def test_duplicate_shift_ended_is_idempotent(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        workspace_id, org_id, property_id, rate_card_id, user_id = _seed_billing(s)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyWorkOrderRepository(s)
        service = _service(ctx, EventBus())
        work_order = service.create(
            repo,
            WorkOrderCreate(
                organization_id=org_id,
                property_id=property_id,
                title="Hourly repair",
                starts_at=_STARTS,
                rate_card_id=rate_card_id,
            ),
        )
        service.mark_in_progress(repo, work_order.id)
        shift_id = new_ulid()
        ended_at = _STARTS + timedelta(hours=2)
        s.add(
            Shift(
                id=shift_id,
                workspace_id=workspace_id,
                user_id=user_id,
                starts_at=_STARTS,
                ends_at=ended_at,
                property_id=property_id,
                source="manual",
                notes_md=None,
                approved_by=None,
                approved_at=None,
            )
        )
        s.flush()
        event = ShiftEnded(
            workspace_id=workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=_PINNED,
            shift_id=shift_id,
            ended_at=ended_at,
        )

        first = handle_shift_ended(
            event,
            repo=repo,
            ctx=ctx,
            clock=FrozenClock(_PINNED),
            event_bus=EventBus(),
        )
        second = handle_shift_ended(
            event,
            repo=repo,
            ctx=ctx,
            clock=FrozenClock(_PINNED),
            event_bus=EventBus(),
        )

        assert first is not None
        assert second is None
        accrued = service.get(repo, work_order.id)
        assert accrued.total_hours_decimal == Decimal("2.00")
        assert accrued.total_cents == 8000
