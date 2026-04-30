"""Integration tests for abandoned stocktake cleanup."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.inventory.models import Movement, Stocktake, StocktakeLine
from app.adapters.db.session import make_engine
from app.services.inventory import stocktake_service
from app.util.clock import FrozenClock
from tests.unit.test_stocktake_service import _seed_item, _seed_scope

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine_stocktake_gc() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine_stocktake_gc: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_stocktake_gc, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def test_abandon_stale_stocktakes_closes_old_sessions_without_movements(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("5.0000"))
    old = stocktake_service.open(session, ctx, property_id=property_id, clock=clock)
    recent = stocktake_service.open(session, ctx, property_id=property_id, clock=clock)
    stocktake_service.save_line(
        session,
        ctx,
        stocktake_id=old.id,
        item_id=item.id,
        observed=Decimal("1.0000"),
        clock=clock,
    )
    old_row = session.get(Stocktake, old.id)
    assert old_row is not None
    old_row.started_at = clock.now() - timedelta(hours=25)
    session.flush()

    abandoned = stocktake_service.abandon_stale(session, ctx, clock=clock)

    assert abandoned == 1
    session.expire_all()
    old_row = session.get(Stocktake, old.id)
    recent_row = session.get(Stocktake, recent.id)
    assert old_row is not None
    assert recent_row is not None
    assert old_row.completed_at is not None
    assert old_row.completed_at.replace(tzinfo=UTC) == clock.now()
    assert old_row.note_md == stocktake_service.ABANDONED_NOTE
    assert recent_row.completed_at is None
    assert session.scalars(select(StocktakeLine)).all() == []
    assert session.scalars(select(Movement)).all() == []
