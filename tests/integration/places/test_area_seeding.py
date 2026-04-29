"""Integration tests for property-kind area auto-seeding (cd-a2k)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.places.area_service import list_areas
from app.domain.places.property_service import PropertyCreate, create_property
from app.domain.places.unit_service import UnitCreate, create_unit, list_units
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_EXPECTED = [
    "Entry",
    "Kitchen",
    "Living",
    "Bedroom 1",
    "Bathroom 1",
    "Outdoor",
    "Trash & Laundry",
]
_SLUG_COUNTER = 0


def _next_slug() -> str:
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"ar-seed-{_SLUG_COUNTER:05d}"


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    registry.register("property_workspace")
    registry.register("audit_log")


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLS",
    )


@pytest.fixture
def env(db_session: Session) -> Iterator[tuple[Session, WorkspaceContext]]:
    install_tenant_filter(db_session)
    slug = _next_slug()
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(
        db_session,
        email=f"{slug}@example.com",
        display_name=f"User {slug}",
        clock=clock,
    )
    ws = bootstrap_workspace(
        db_session,
        slug=slug,
        name=f"WS {slug}",
        owner_user_id=user.id,
        clock=clock,
    )
    ctx = _ctx_for(ws.id, ws.slug, user.id)
    token = set_current(ctx)
    try:
        yield db_session, ctx
    finally:
        reset_current(token)


def _property_body(*, kind: str) -> PropertyCreate:
    return PropertyCreate.model_validate(
        {
            "name": "Villa Sud",
            "kind": kind,
            "address": "12 Chemin des Oliviers, Antibes",
            "address_json": {
                "line1": "12 Chemin des Oliviers",
                "city": "Antibes",
                "country": "FR",
            },
            "country": "FR",
            "timezone": "Europe/Paris",
        }
    )


@pytest.mark.parametrize("kind", ["vacation", "str", "mixed"])
def test_seeded_property_kinds_create_default_unit_areas(
    env: tuple[Session, WorkspaceContext],
    kind: str,
) -> None:
    session, ctx = env
    clock = FrozenClock(_PINNED)

    prop = create_property(session, ctx, body=_property_body(kind=kind), clock=clock)
    units = list_units(session, ctx, property_id=prop.id)
    assert len(units) == 1

    areas = list_areas(session, ctx, property_id=prop.id)
    assert [area.name for area in areas] == _EXPECTED
    assert {area.unit_id for area in areas} == {units[0].id}
    assert [area.order_hint for area in areas] == list(range(len(_EXPECTED)))


@pytest.mark.parametrize("kind", ["vacation", "str", "mixed"])
def test_new_unit_on_seeded_property_kind_gets_seeded_areas(
    env: tuple[Session, WorkspaceContext],
    kind: str,
) -> None:
    session, ctx = env
    clock = FrozenClock(_PINNED)
    prop = create_property(session, ctx, body=_property_body(kind=kind), clock=clock)

    unit = create_unit(
        session,
        ctx,
        property_id=prop.id,
        body=UnitCreate.model_validate({"name": "Guest House", "ordinal": 1}),
        clock=clock,
    )

    areas = [
        area
        for area in list_areas(session, ctx, property_id=prop.id)
        if area.unit_id == unit.id
    ]
    assert [area.name for area in areas] == _EXPECTED


def test_residence_property_does_not_seed_areas(
    env: tuple[Session, WorkspaceContext],
) -> None:
    session, ctx = env
    clock = FrozenClock(_PINNED)

    prop = create_property(
        session, ctx, body=_property_body(kind="residence"), clock=clock
    )

    assert list_areas(session, ctx, property_id=prop.id) == []
