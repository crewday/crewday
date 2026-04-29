"""Unit tests for demo scenario fixture seeding."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.demo.models import DemoWorkspace
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.stays.models import Reservation
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import Workspace
from app.demo import (
    SCENARIO_KEYS,
    load_scenario_fixture,
    normalise_start_path,
    resolve_relative_timestamp,
    seed_workspace,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as sess:
        yield sess


class TestResolveRelativeTimestamp:
    def test_resolves_t_offsets_in_utc(self) -> None:
        now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

        assert resolve_relative_timestamp("T-2d", now) == now - timedelta(days=2)
        assert resolve_relative_timestamp("T+3h", now) == now + timedelta(hours=3)
        assert resolve_relative_timestamp("T+15m", now) == now + timedelta(minutes=15)

    def test_resolves_named_anchor_offsets(self) -> None:
        now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
        stay = datetime(2026, 5, 2, 10, 0, tzinfo=UTC)

        assert resolve_relative_timestamp(
            "stay:+7d", now, {"stay": stay}
        ) == stay + timedelta(days=7)

    def test_rejects_unknown_shape(self) -> None:
        with pytest.raises(ValueError, match="unsupported relative timestamp"):
            resolve_relative_timestamp("next tuesday", datetime.now(UTC))


class TestNormaliseStartPath:
    def test_accepts_allowlisted_workspace_relative_paths(self) -> None:
        fixture = load_scenario_fixture("rental-manager")

        assert normalise_start_path(fixture, "/expenses") == "/expenses"

    def test_rejects_workspace_prefixed_and_overlong_paths(self) -> None:
        fixture = load_scenario_fixture("rental-manager")

        assert normalise_start_path(fixture, "/w/other/tasks") == "/tasks"
        assert normalise_start_path(fixture, f"/{'a' * 257}") == "/tasks"


class TestSeedWorkspace:
    def test_all_v1_scenarios_seed(self, session: Session) -> None:
        now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

        for scenario_key in SCENARIO_KEYS:
            result = seed_workspace(session, scenario_key, now=now)
            assert result.scenario_key == scenario_key
            assert result.workspace_slug.startswith(f"demo-{scenario_key}-")
            assert result.persona_user_id in result.user_ids.values()
            assert result.counts["properties"] >= 1
            assert result.counts["tasks"] >= 1

        assert _count(session, Workspace) == 3
        assert _count(session, DemoWorkspace) == 3
        assert _count(session, Property) >= 5
        assert _count(session, Reservation) >= 4
        assert _count(session, Occurrence) >= 8
        assert _count(session, ExpenseClaim) >= 5

    def test_repeated_seed_has_same_shape_not_same_ids(self, session: Session) -> None:
        now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

        first = seed_workspace(session, "rental-manager", now=now)
        second = seed_workspace(session, "rental-manager", now=now)

        assert first.workspace_id != second.workspace_id
        assert first.persona_user_id != second.persona_user_id
        assert first.counts == second.counts

    def test_created_at_values_stay_inside_demo_window(self, session: Session) -> None:
        now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
        seed_workspace(session, "rental-manager", now=now)

        lower = now - timedelta(days=30)
        upper = now + timedelta(days=30)
        values: list[datetime] = []
        values.extend(session.scalars(select(Workspace.created_at)).all())
        values.extend(session.scalars(select(DemoWorkspace.created_at)).all())
        values.extend(session.scalars(select(Occurrence.created_at)).all())
        values.extend(session.scalars(select(ExpenseClaim.created_at)).all())
        assert values
        for value in values:
            assert lower <= _as_utc(value) <= upper


def _count(session: Session, model: type[object]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
