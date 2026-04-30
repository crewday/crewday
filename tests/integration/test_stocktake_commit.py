"""Integration tests for stocktake commit semantics."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.inventory.models import Movement, Stocktake, StocktakeLine
from app.adapters.db.session import make_engine
from app.services.inventory import movement_service, stocktake_service
from app.util.clock import FrozenClock
from tests.unit.test_stocktake_service import _seed_item, _seed_scope

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine_stocktake_commit() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine_stocktake_commit: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_stocktake_commit, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def test_commit_writes_nonzero_deltas_with_shared_stocktake_source(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    adjusted = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("5.0000"))
    unchanged = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("10.0000"))
    stocktake = stocktake_service.open(
        session, ctx, property_id=property_id, clock=clock
    )
    stocktake_service.save_line(
        session,
        ctx,
        stocktake_id=stocktake.id,
        item_id=adjusted.id,
        observed=Decimal("6.0000"),
        reason="found",
        clock=clock,
    )
    stocktake_service.save_line(
        session,
        ctx,
        stocktake_id=stocktake.id,
        item_id=unchanged.id,
        observed=Decimal("10.0000"),
        reason="audit_correction",
        clock=clock,
    )

    movement_service.consume(
        session,
        ctx,
        item_id=adjusted.id,
        qty=Decimal("2.0000"),
        note="task completion during count",
        clock=clock,
    )
    movements = stocktake_service.commit(
        session,
        ctx,
        stocktake_id=stocktake.id,
        clock=clock,
    )

    assert len(movements) == 1
    assert movements[0].item_id == adjusted.id
    assert movements[0].delta == Decimal("3.0000")
    assert movements[0].reason == "found"
    assert movements[0].source_stocktake_id == stocktake.id
    assert movements[0].on_hand_after == Decimal("6.0000")

    rows = session.scalars(
        select(Movement).where(Movement.source_stocktake_id == stocktake.id)
    ).all()
    assert [row.id for row in rows] == [movements[0].id]
    assert rows[0].source_task_id is None
    committed = session.get(Stocktake, stocktake.id)
    assert committed is not None
    completed_at = committed.completed_at
    assert completed_at is not None
    assert completed_at.replace(tzinfo=UTC) == clock.now()
    assert session.scalars(select(StocktakeLine)).all() == []


def test_commit_can_only_run_once(session: Session, clock: FrozenClock) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("5.0000"))
    stocktake = stocktake_service.open(
        session, ctx, property_id=property_id, clock=clock
    )
    stocktake_service.save_line(
        session,
        ctx,
        stocktake_id=stocktake.id,
        item_id=item.id,
        observed=Decimal("6.0000"),
        clock=clock,
    )

    stocktake_service.commit(session, ctx, stocktake_id=stocktake.id, clock=clock)

    with pytest.raises(stocktake_service.StocktakeAlreadyCommitted):
        stocktake_service.commit(session, ctx, stocktake_id=stocktake.id, clock=clock)
