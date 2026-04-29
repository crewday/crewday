"""Unit tests for :mod:`app.domain.places.area_service` (cd-a2k)."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Area, Unit
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.places.area_service import (
    AreaCreate,
    AreaNestingTooDeep,
    AreaNotFound,
    AreaReorderItem,
    AreaUpdate,
    create_area,
    delete_area,
    get_area,
    list_areas,
    move_area,
    reorder_areas,
    seed_default_areas_for_unit,
    update_area,
)
from app.domain.places.property_service import (
    PropertyCreate,
    PropertyView,
    create_property,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


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


@pytest.fixture(name="engine_area")
def fixture_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_area")
def fixture_session(engine_area: Engine) -> Iterator[Session]:
    factory = sessionmaker(
        bind=engine_area,
        expire_on_commit=False,
        class_=Session,
    )
    with factory() as s:
        yield s


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _ctx(
    *,
    workspace_id: str,
    slug: str,
    actor_id: str = "01HWA00000000000000000USR",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )


def _make_workspace(session: Session, *, slug: str) -> str:
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


def _create_property(
    session: Session,
    *,
    workspace_id: str,
    slug: str,
    name: str = "Villa Sud",
    kind: str = "residence",
    clock: FrozenClock,
) -> PropertyView:
    body = PropertyCreate.model_validate(
        {
            "name": name,
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
    return create_property(
        session,
        _ctx(workspace_id=workspace_id, slug=slug),
        body=body,
        clock=clock,
    )


def _create_area(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    name: str,
    order_hint: int,
    parent_area_id: str | None = None,
    clock: FrozenClock,
) -> str:
    view = create_area(
        session,
        ctx,
        property_id=property_id,
        body=AreaCreate.model_validate(
            {
                "name": name,
                "kind": "indoor_room",
                "order_hint": order_hint,
                "parent_area_id": parent_area_id,
            }
        ),
        clock=clock,
    )
    return view.id


def _area_audits(session: Session, *, action: str) -> list[AuditLog]:
    return session.scalars(
        select(AuditLog).where(
            AuditLog.entity_kind == "area",
            AuditLog.action == action,
        )
    ).all()


class TestAreaCrud:
    def test_create_update_move_and_list(
        self, session_area: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_area, slug="area-crud")
        ctx = _ctx(workspace_id=ws, slug="area-crud")
        prop = _create_property(
            session_area, workspace_id=ws, slug="area-crud", clock=frozen_clock
        )

        kitchen_id = _create_area(
            session_area,
            ctx,
            property_id=prop.id,
            name="Kitchen",
            order_hint=2,
            clock=frozen_clock,
        )
        entry_id = _create_area(
            session_area,
            ctx,
            property_id=prop.id,
            name="Entry",
            order_hint=1,
            clock=frozen_clock,
        )

        listed = list_areas(session_area, ctx, property_id=prop.id)
        assert [area.name for area in listed] == ["Entry", "Kitchen"]

        updated = update_area(
            session_area,
            ctx,
            area_id=kitchen_id,
            body=AreaUpdate.model_validate(
                {
                    "name": "Chef Kitchen",
                    "kind": "service",
                    "order_hint": 3,
                    "notes_md": "Stocked daily.",
                }
            ),
            clock=frozen_clock,
        )
        assert updated.name == "Chef Kitchen"
        assert updated.kind == "service"
        assert updated.notes_md == "Stocked daily."

        moved = move_area(
            session_area,
            ctx,
            area_id=entry_id,
            parent_area_id=kitchen_id,
            order_hint=4,
            clock=frozen_clock,
        )
        assert moved.parent_area_id == kitchen_id
        assert moved.order_hint == 4

        row = session_area.get(Area, kitchen_id)
        assert row is not None
        assert row.label == "Chef Kitchen"
        assert row.name == "Chef Kitchen"

    def test_delete_soft_deletes_area_and_children(
        self, session_area: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_area, slug="area-delete")
        ctx = _ctx(workspace_id=ws, slug="area-delete")
        prop = _create_property(
            session_area, workspace_id=ws, slug="area-delete", clock=frozen_clock
        )
        parent_id = _create_area(
            session_area,
            ctx,
            property_id=prop.id,
            name="Suite",
            order_hint=1,
            clock=frozen_clock,
        )
        child_id = _create_area(
            session_area,
            ctx,
            property_id=prop.id,
            name="Bath",
            order_hint=2,
            parent_area_id=parent_id,
            clock=frozen_clock,
        )

        deleted = delete_area(session_area, ctx, area_id=parent_id, clock=frozen_clock)

        assert deleted.deleted_at == _PINNED
        assert list_areas(session_area, ctx, property_id=prop.id) == []
        deleted_parent = get_area(
            session_area, ctx, area_id=parent_id, include_deleted=True
        )
        assert deleted_parent.deleted_at == _PINNED.replace(tzinfo=None)
        child = session_area.get(Area, child_id)
        assert child is not None
        assert child.deleted_at == _PINNED.replace(tzinfo=None)
        audits = _area_audits(session_area, action="delete")
        assert len(audits) == 1
        assert audits[0].diff["deleted_child_ids"] == [child_id]

    def test_reorder_is_atomic_and_writes_one_audit(
        self, session_area: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_area, slug="area-reorder")
        ctx = _ctx(workspace_id=ws, slug="area-reorder")
        prop = _create_property(
            session_area, workspace_id=ws, slug="area-reorder", clock=frozen_clock
        )
        first = _create_area(
            session_area,
            ctx,
            property_id=prop.id,
            name="Entry",
            order_hint=1,
            clock=frozen_clock,
        )
        second = _create_area(
            session_area,
            ctx,
            property_id=prop.id,
            name="Kitchen",
            order_hint=2,
            clock=frozen_clock,
        )

        with pytest.raises(AreaNotFound):
            reorder_areas(
                session_area,
                ctx,
                property_id=prop.id,
                orderings=[
                    AreaReorderItem.model_validate(
                        {"area_id": first, "order_hint": 20}
                    ),
                    AreaReorderItem.model_validate(
                        {"area_id": "01HWA000000000000000MISS", "order_hint": 10}
                    ),
                ],
                clock=frozen_clock,
            )
        unchanged = {
            row.id: row.ordering
            for row in session_area.scalars(
                select(Area).where(Area.id.in_([first, second]))
            )
        }
        assert unchanged == {first: 1, second: 2}
        assert _area_audits(session_area, action="reorder") == []

        reordered = reorder_areas(
            session_area,
            ctx,
            property_id=prop.id,
            orderings=[
                AreaReorderItem.model_validate({"area_id": first, "order_hint": 30}),
                AreaReorderItem.model_validate({"area_id": second, "order_hint": 10}),
            ],
            clock=frozen_clock,
        )

        assert [area.id for area in reordered] == [second, first]
        audits = _area_audits(session_area, action="reorder")
        assert len(audits) == 1
        assert audits[0].entity_id == prop.id
        assert audits[0].diff["summary"] == "reordered 2 areas"


class TestAreaDepthLimit:
    def test_rejects_grandchild(
        self, session_area: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_area, slug="area-depth")
        ctx = _ctx(workspace_id=ws, slug="area-depth")
        prop = _create_property(
            session_area, workspace_id=ws, slug="area-depth", clock=frozen_clock
        )
        suite = _create_area(
            session_area,
            ctx,
            property_id=prop.id,
            name="Suite",
            order_hint=1,
            clock=frozen_clock,
        )
        bath = _create_area(
            session_area,
            ctx,
            property_id=prop.id,
            name="Bath",
            order_hint=2,
            parent_area_id=suite,
            clock=frozen_clock,
        )

        with pytest.raises(AreaNestingTooDeep):
            create_area(
                session_area,
                ctx,
                property_id=prop.id,
                body=AreaCreate.model_validate(
                    {
                        "name": "Shower",
                        "kind": "indoor_room",
                        "order_hint": 3,
                        "parent_area_id": bath,
                    }
                ),
                clock=frozen_clock,
            )

        with pytest.raises(AreaNestingTooDeep):
            move_area(
                session_area,
                ctx,
                area_id=suite,
                parent_area_id=bath,
                clock=frozen_clock,
            )


class TestAreaWorkspaceScope:
    def test_cross_workspace_access_is_denied(
        self, session_area: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _make_workspace(session_area, slug="area-a")
        ws_b = _make_workspace(session_area, slug="area-b")
        ctx_a = _ctx(workspace_id=ws_a, slug="area-a")
        ctx_b = _ctx(workspace_id=ws_b, slug="area-b")
        prop = _create_property(
            session_area, workspace_id=ws_a, slug="area-a", clock=frozen_clock
        )
        area_id = _create_area(
            session_area,
            ctx_a,
            property_id=prop.id,
            name="Kitchen",
            order_hint=1,
            clock=frozen_clock,
        )

        with pytest.raises(AreaNotFound):
            get_area(session_area, ctx_b, area_id=area_id)
        with pytest.raises(AreaNotFound):
            list_areas(session_area, ctx_b, property_id=prop.id)
        with pytest.raises(AreaNotFound):
            create_area(
                session_area,
                ctx_b,
                property_id=prop.id,
                body=AreaCreate.model_validate(
                    {"name": "Pool", "kind": "outdoor", "order_hint": 2}
                ),
                clock=frozen_clock,
            )
        with pytest.raises(AreaNotFound):
            update_area(
                session_area,
                ctx_b,
                area_id=area_id,
                body=AreaUpdate.model_validate(
                    {"name": "Galley", "kind": "service", "order_hint": 3}
                ),
                clock=frozen_clock,
            )
        with pytest.raises(AreaNotFound):
            move_area(
                session_area,
                ctx_b,
                area_id=area_id,
                parent_area_id=None,
                order_hint=4,
                clock=frozen_clock,
            )
        with pytest.raises(AreaNotFound):
            reorder_areas(
                session_area,
                ctx_b,
                property_id=prop.id,
                orderings=[
                    AreaReorderItem.model_validate(
                        {"area_id": area_id, "order_hint": 5}
                    )
                ],
                clock=frozen_clock,
            )
        with pytest.raises(AreaNotFound):
            delete_area(session_area, ctx_b, area_id=area_id, clock=frozen_clock)

    def test_seed_helper_is_workspace_scoped(
        self, session_area: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _make_workspace(session_area, slug="area-seed-a")
        ws_b = _make_workspace(session_area, slug="area-seed-b")
        ctx_a = _ctx(workspace_id=ws_a, slug="area-seed-a")
        ctx_b = _ctx(workspace_id=ws_b, slug="area-seed-b")
        prop = _create_property(
            session_area, workspace_id=ws_a, slug="area-seed-a", clock=frozen_clock
        )
        unit_id = session_area.scalars(
            select(Unit.id).where(Unit.property_id == prop.id)
        ).one()

        with pytest.raises(AreaNotFound):
            seed_default_areas_for_unit(
                session_area,
                ctx_b,
                property_id=prop.id,
                unit_id=unit_id,
                now=_PINNED,
                clock=frozen_clock,
            )

        assert list_areas(session_area, ctx_a, property_id=prop.id) == []
