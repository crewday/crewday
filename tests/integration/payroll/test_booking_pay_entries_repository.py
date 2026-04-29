"""Integration tests for booking payroll repository behavior (cd-n0t4)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.payroll.models import Booking, PayPeriod, PayPeriodEntry
from app.adapters.db.payroll.repositories import (
    SqlAlchemyBookingPayRepository,
    SqlAlchemyPayPeriodRepository,
)
from app.adapters.db.workspace.models import WorkEngagement
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_PERIOD_START = datetime(2026, 5, 1, tzinfo=UTC)
_PERIOD_END = datetime(2026, 6, 1, tzinfo=UTC)
_DAY = datetime(2026, 5, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _seed(session: Session) -> tuple[str, str, str, str]:
    tag = new_ulid()[-8:].lower()
    user = bootstrap_user(
        session,
        email=f"booking-pay-{tag}@example.com",
        display_name="Booking Pay",
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"booking-pay-{tag}",
        name="Booking Pay",
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
        state="open",
        created_at=_NOW,
    )
    session.add_all([engagement, period])
    session.flush()
    return workspace.id, user.id, engagement.id, period.id


def _booking(
    *,
    workspace_id: str,
    user_id: str,
    engagement_id: str,
    status: str = "completed",
    start: datetime = _DAY,
    minutes: int = 120,
    actual_minutes_paid: int | None = None,
    pending_amend_minutes: int | None = None,
) -> Booking:
    return Booking(
        id=new_ulid(),
        workspace_id=workspace_id,
        work_engagement_id=engagement_id,
        user_id=user_id,
        property_id=None,
        client_org_id=None,
        status=status,
        kind="work",
        pay_basis="scheduled",
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=minutes),
        actual_minutes=None,
        actual_minutes_paid=actual_minutes_paid
        if actual_minutes_paid is not None
        else minutes,
        break_seconds=0,
        adjusted=status == "adjusted",
        adjustment_reason="manager approved" if status == "adjusted" else None,
        pending_amend_minutes=pending_amend_minutes,
        pending_amend_reason="overrun" if pending_amend_minutes is not None else None,
        cancelled_at=start - timedelta(hours=2)
        if status.startswith("cancelled")
        else None,
        cancellation_window_hours=24,
        cancellation_pay_to_worker=True,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_booking_tables_exist(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())

    assert "booking" in tables
    assert "pay_period_entry" in tables


def test_lists_settled_pay_bearing_bookings_and_filters_pending_amends(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, engagement_id, _period_id = _seed(session)
        included = _booking(
            workspace_id=workspace_id,
            user_id=user_id,
            engagement_id=engagement_id,
        )
        pending = _booking(
            workspace_id=workspace_id,
            user_id=user_id,
            engagement_id=engagement_id,
            pending_amend_minutes=180,
            start=_DAY + timedelta(hours=3),
        )
        scheduled = _booking(
            workspace_id=workspace_id,
            user_id=user_id,
            engagement_id=engagement_id,
            status="scheduled",
            start=_DAY + timedelta(hours=6),
        )
        session.add_all([included, pending, scheduled])
        session.flush()

        repo = SqlAlchemyBookingPayRepository(session)
        rows = repo.list_pay_bearing_bookings(
            workspace_id=workspace_id,
            starts_at=_PERIOD_START,
            ends_at=_PERIOD_END,
            user_id=user_id,
            work_engagement_id=engagement_id,
        )

        assert [row.id for row in rows] == [included.id]
        assert repo.list_unsettled_booking_ids(
            workspace_id=workspace_id,
            starts_at=_PERIOD_START,
            ends_at=_PERIOD_END,
            limit=10,
        ) == [pending.id, scheduled.id]


def test_replace_period_entries_groups_daily_booking_sources(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, engagement_id, period_id = _seed(session)
        first = _booking(
            workspace_id=workspace_id,
            user_id=user_id,
            engagement_id=engagement_id,
            minutes=120,
        )
        second = _booking(
            workspace_id=workspace_id,
            user_id=user_id,
            engagement_id=engagement_id,
            status="adjusted",
            start=_DAY + timedelta(hours=3),
            minutes=60,
            actual_minutes_paid=90,
        )
        session.add_all([first, second])
        session.flush()

        repo = SqlAlchemyBookingPayRepository(session)
        rows = repo.replace_period_entries(
            workspace_id=workspace_id,
            pay_period_id=period_id,
            starts_at=_PERIOD_START,
            ends_at=_PERIOD_END,
            now=_NOW,
        )

        assert len(rows) == 1
        row = rows[0]
        assert row.minutes == 210
        assert row.source_booking_ids == tuple(sorted([first.id, second.id]))
        persisted = session.scalars(select(PayPeriodEntry)).one()
        assert persisted.minutes == 210
        assert len(persisted.source_details_json) == 2


def test_replace_period_entries_rejects_period_from_another_workspace(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, engagement_id, _period_id = _seed(session)
        _other_workspace_id, _other_user_id, _other_engagement_id, other_period_id = (
            _seed(session)
        )
        session.add(
            _booking(
                workspace_id=workspace_id,
                user_id=user_id,
                engagement_id=engagement_id,
            )
        )
        session.flush()

        repo = SqlAlchemyBookingPayRepository(session)

        with pytest.raises(LookupError, match="pay period not found"):
            repo.replace_period_entries(
                workspace_id=workspace_id,
                pay_period_id=other_period_id,
                starts_at=_PERIOD_START,
                ends_at=_PERIOD_END,
                now=_NOW,
            )


def test_pay_period_repository_reports_unsettled_booking_ids(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        workspace_id, user_id, engagement_id, _period_id = _seed(session)
        scheduled = _booking(
            workspace_id=workspace_id,
            user_id=user_id,
            engagement_id=engagement_id,
            status="scheduled",
        )
        session.add(scheduled)
        session.flush()

        repo = SqlAlchemyPayPeriodRepository(session)

        assert repo.list_unsettled_booking_ids(
            workspace_id=workspace_id,
            starts_at=_PERIOD_START,
            ends_at=_PERIOD_END,
            limit=10,
        ) == [scheduled.id]
