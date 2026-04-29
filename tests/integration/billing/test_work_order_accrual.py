"""Integration tests for shift-driven work-order accrual."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.billing.models import Organization, RateCard
from app.adapters.db.billing.repositories import SqlAlchemyWorkOrderRepository
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.time.models import Shift
from app.adapters.db.workspace.models import Workspace
from app.domain.billing.work_orders import (
    WorkOrderCreate,
    WorkOrderService,
    handle_shift_ended,
    register_shift_accrual_subscription,
)
from app.domain.time.shifts import close_shift
from app.events import ShiftEnded, bus
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_STARTS = datetime(2026, 4, 29, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _ctx(workspace_id: str, user_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="work-order-accrual",
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _seed(s: Session) -> tuple[str, str, str, str, str]:
    workspace_id = new_ulid()
    user_id = new_ulid()
    org_id = new_ulid()
    property_id = new_ulid()
    rate_card_id = new_ulid()
    email = f"worker-{user_id[-6:]}@example.com"
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"wo-int-{workspace_id[-6:].lower()}",
            name="Work Order Accrual",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency="EUR",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name="worker",
            created_at=_PINNED,
        )
    )
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
    return workspace_id, user_id, org_id, property_id, rate_card_id


def _create_open_work_order(
    s: Session,
    ctx: WorkspaceContext,
    *,
    org_id: str,
    property_id: str,
    rate_card_id: str,
) -> str:
    repo = SqlAlchemyWorkOrderRepository(s)
    service = WorkOrderService(ctx, clock=FrozenClock(_PINNED))
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
    return work_order.id


def test_close_shift_accrues_to_open_work_order_through_event_subscription(
    factory: sessionmaker[Session],
) -> None:
    bus._reset_for_tests()
    try:
        with factory() as s:
            workspace_id, user_id, org_id, property_id, rate_card_id = _seed(s)
            ctx = _ctx(workspace_id, user_id)
            work_order_id = _create_open_work_order(
                s,
                ctx,
                org_id=org_id,
                property_id=property_id,
                rate_card_id=rate_card_id,
            )
            shift_id = new_ulid()
            s.add(
                Shift(
                    id=shift_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    starts_at=_STARTS,
                    ends_at=None,
                    property_id=property_id,
                    source="manual",
                    notes_md=None,
                    approved_by=None,
                    approved_at=None,
                )
            )
            s.flush()

            register_shift_accrual_subscription(
                bus,
                session_provider=lambda _event: (s, ctx),
                repository_factory=SqlAlchemyWorkOrderRepository,
                clock=FrozenClock(_PINNED),
            )
            close_shift(
                s,
                ctx,
                shift_id=shift_id,
                ends_at=_STARTS + timedelta(hours=1, minutes=15),
                clock=FrozenClock(_PINNED),
            )
            accrued = WorkOrderService(ctx).get(
                SqlAlchemyWorkOrderRepository(s),
                work_order_id,
            )

            assert accrued.total_hours_decimal == Decimal("1.25")
            assert accrued.total_cents == 5000
    finally:
        bus._reset_for_tests()


def test_concurrent_shift_end_handlers_increment_cached_totals_exactly(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id, user_id, org_id, property_id, rate_card_id = _seed(s)
        ctx = _ctx(workspace_id, user_id)
        work_order_id = _create_open_work_order(
            s,
            ctx,
            org_id=org_id,
            property_id=property_id,
            rate_card_id=rate_card_id,
        )
        shift_ids = [new_ulid(), new_ulid()]
        for offset, shift_id in enumerate(shift_ids):
            s.add(
                Shift(
                    id=shift_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    starts_at=_STARTS + timedelta(hours=offset * 3),
                    ends_at=_STARTS + timedelta(hours=offset * 3 + 2),
                    property_id=property_id,
                    source="manual",
                    notes_md=None,
                    approved_by=None,
                    approved_at=None,
                )
            )
        s.commit()

    def _run(shift_id: str) -> None:
        with factory() as s:
            ctx = _ctx(workspace_id, user_id)
            shift = s.get(Shift, shift_id)
            assert shift is not None
            ended_at = shift.ends_at
            assert ended_at is not None
            handle_shift_ended(
                ShiftEnded(
                    workspace_id=workspace_id,
                    actor_id=ctx.actor_id,
                    correlation_id=ctx.audit_correlation_id,
                    occurred_at=_PINNED,
                    shift_id=shift_id,
                    ended_at=_as_utc(ended_at),
                ),
                repo=SqlAlchemyWorkOrderRepository(s),
                ctx=ctx,
                clock=FrozenClock(_PINNED),
            )
            s.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_run, shift_ids))

    with factory() as s:
        ctx = _ctx(workspace_id, user_id)
        accrued = WorkOrderService(ctx).get(
            SqlAlchemyWorkOrderRepository(s),
            work_order_id,
        )

    assert accrued.total_hours_decimal == Decimal("4.00")
    assert accrued.total_cents == 16000
