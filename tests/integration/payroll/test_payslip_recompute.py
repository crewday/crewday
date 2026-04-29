"""Integration tests for payslip recomputation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.payroll.models import Booking, PayPeriod, PayRule, Payslip
from app.adapters.db.payroll.repositories import SqlAlchemyPayslipComputeRepository
from app.adapters.db.workspace.models import WorkEngagement
from app.domain.payroll.compute import PayslipComputeConflict, payslip_recompute
from app.events import EventBus
from app.events.types import PayslipComputed
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_PERIOD_START = datetime(2026, 5, 1, tzinfo=UTC)
_PERIOD_END = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="payslip-recompute",
        actor_id="manager",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000COR1",
    )


def _seed(session: Session, *, period_state: str = "locked") -> tuple[str, str, str]:
    tag = new_ulid()[-8:].lower()
    user = bootstrap_user(
        session,
        email=f"payslip-recompute-{tag}@example.com",
        display_name="Payslip Recompute",
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"payslip-recompute-{tag}",
        name="Payslip Recompute",
        owner_user_id=user.id,
    )
    engagement = WorkEngagement(
        id=new_ulid(),
        user_id=user.id,
        workspace_id=workspace.id,
        engagement_kind="payroll",
        supplier_org_id=None,
        started_on=date(2026, 1, 1),
        archived_on=None,
        notes_md="",
        created_at=_NOW,
        updated_at=_NOW,
    )
    period = PayPeriod(
        id=new_ulid(),
        workspace_id=workspace.id,
        starts_at=_PERIOD_START,
        ends_at=_PERIOD_END,
        state=period_state,
        locked_at=_NOW if period_state == "locked" else None,
        locked_by="manager" if period_state == "locked" else None,
        created_at=_NOW,
    )
    rule = PayRule(
        id=new_ulid(),
        workspace_id=workspace.id,
        user_id=user.id,
        currency="USD",
        base_cents_per_hour=1000,
        overtime_multiplier=Decimal("1.5"),
        night_multiplier=Decimal("1"),
        weekend_multiplier=Decimal("1"),
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_to=None,
        created_by="manager",
        created_at=_NOW,
    )
    session.add_all([engagement, period, rule])
    session.flush()

    booking = Booking(
        id=new_ulid(),
        workspace_id=workspace.id,
        work_engagement_id=engagement.id,
        user_id=user.id,
        property_id=None,
        client_org_id=None,
        status="completed",
        kind="work",
        pay_basis="scheduled",
        scheduled_start=datetime(2026, 5, 4, 8, 0, tzinfo=UTC),
        scheduled_end=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        actual_minutes=None,
        actual_minutes_paid=240,
        break_seconds=0,
        adjusted=False,
        adjustment_reason=None,
        pending_amend_minutes=None,
        pending_amend_reason=None,
        cancelled_at=None,
        cancellation_window_hours=24,
        cancellation_pay_to_worker=True,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(booking)
    session.flush()
    return workspace.id, user.id, period.id


def test_recompute_upserts_one_idempotent_payslip_per_period_user(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, period_id = _seed(session)
        repo = SqlAlchemyPayslipComputeRepository(session)
        event_bus = EventBus()
        seen: list[PayslipComputed] = []

        @event_bus.subscribe(PayslipComputed)
        def _remember(event: PayslipComputed) -> None:
            seen.append(event)

        clock = FrozenClock(_NOW)
        first = payslip_recompute(
            repo,
            _ctx(workspace_id),
            period_id=period_id,
            event_bus=event_bus,
            clock=clock,
        )
        second = payslip_recompute(
            repo,
            _ctx(workspace_id),
            period_id=period_id,
            event_bus=event_bus,
            clock=clock,
        )

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].id == second[0].id
        assert first[0].gross_cents == 4000
        assert first[0].net_cents == 4000
        assert first[0].deductions_cents == {}
        assert first[0].components_json == second[0].components_json

        persisted = session.scalars(
            select(Payslip).where(
                Payslip.pay_period_id == period_id,
                Payslip.user_id == user_id,
            )
        ).all()
        assert len(persisted) == 1
        assert persisted[0].components_json["schema_version"] == 1
        assert [event.payslip_id for event in seen] == [first[0].id, first[0].id]


def test_recompute_resets_existing_issued_payslip_to_draft(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, period_id = _seed(session)
        slip = Payslip(
            id=new_ulid(),
            workspace_id=workspace_id,
            pay_period_id=period_id,
            user_id=user_id,
            shift_hours_decimal=Decimal("1.00"),
            overtime_hours_decimal=Decimal("0"),
            gross_cents=100,
            deductions_cents={"advance": 50},
            net_cents=50,
            components_json={"schema_version": 1, "metadata": {"stale": True}},
            status="issued",
            issued_at=_NOW,
            pdf_blob_hash="sha256-stale",
            payout_snapshot_json={"destination_id": "stale"},
            created_at=_NOW,
        )
        session.add(slip)
        session.flush()

        rows = payslip_recompute(
            SqlAlchemyPayslipComputeRepository(session),
            _ctx(workspace_id),
            period_id=period_id,
            clock=FrozenClock(_NOW),
        )

        assert [row.id for row in rows] == [slip.id]
        session.expire_all()
        persisted = session.get(Payslip, slip.id)
        assert persisted is not None
        assert persisted.status == "draft"
        assert persisted.issued_at is None
        assert persisted.paid_at is None
        assert persisted.pdf_blob_hash is None
        assert persisted.payout_snapshot_json is None
        assert persisted.gross_cents == 4000
        assert persisted.net_cents == 4000


def test_recompute_refuses_locked_period_with_paid_payslip(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, period_id = _seed(session)
        slip = Payslip(
            id=new_ulid(),
            workspace_id=workspace_id,
            pay_period_id=period_id,
            user_id=user_id,
            shift_hours_decimal=Decimal("1.00"),
            overtime_hours_decimal=Decimal("0"),
            gross_cents=100,
            deductions_cents={},
            net_cents=100,
            components_json={"schema_version": 1},
            status="paid",
            issued_at=_NOW,
            paid_at=_NOW,
            pdf_blob_hash="sha256-paid",
            created_at=_NOW,
        )
        session.add(slip)
        session.flush()

        with pytest.raises(PayslipComputeConflict, match="paid payslips"):
            payslip_recompute(
                SqlAlchemyPayslipComputeRepository(session),
                _ctx(workspace_id),
                period_id=period_id,
                clock=FrozenClock(_NOW),
            )

        session.expire_all()
        persisted = session.get(Payslip, slip.id)
        assert persisted is not None
        assert persisted.status == "paid"
        assert persisted.paid_at is not None
        assert persisted.gross_cents == 100


def test_recompute_refuses_paid_period(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, _user_id, period_id = _seed(session, period_state="paid")
        repo = SqlAlchemyPayslipComputeRepository(session)

        with pytest.raises(PayslipComputeConflict, match="paid pay periods"):
            payslip_recompute(
                repo,
                _ctx(workspace_id),
                period_id=period_id,
                clock=FrozenClock(_NOW),
            )
