"""Postgres race test for the unit last-live-sibling gate (cd-zfcj)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Property, Unit
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.places.property_service import PropertyCreate, create_property
from app.domain.places.unit_service import (
    LastUnitProtected,
    UnitCreate,
    create_unit,
    soft_delete_unit,
)
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = [pytest.mark.integration, pytest.mark.pg_only]


_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


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


@pytest.fixture
def isolated_engine(db_url: str) -> Iterator[Engine]:
    eng = make_engine(db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRUD",
    )


@dataclass(frozen=True, slots=True)
class _TwoUnitProperty:
    workspace_id: str
    workspace_slug: str
    actor_id: str
    property_id: str
    unit_a_id: str
    unit_b_id: str


@dataclass
class _RaceResult:
    deleted_unit_ids: list[str]
    errors: list[Exception]
    lock: threading.Lock


def _new_race_result() -> _RaceResult:
    return _RaceResult(deleted_unit_ids=[], errors=[], lock=threading.Lock())


def _make_property_body(name: str) -> PropertyCreate:
    return PropertyCreate.model_validate(
        {
            "name": name,
            "kind": "vacation",
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


def _bootstrap_two_unit_property(
    factory: sessionmaker[Session], *, slug: str
) -> _TwoUnitProperty:
    clock = FrozenClock(_PINNED)

    with factory() as session:
        user = bootstrap_user(
            session,
            email=f"{slug}@example.com",
            display_name=f"User {slug}",
            clock=clock,
        )
        workspace = bootstrap_workspace(
            session,
            slug=slug,
            name=f"WS {slug}",
            owner_user_id=user.id,
            clock=clock,
        )
        ctx = _ctx_for(workspace.id, workspace.slug, user.id)
        token = set_current(ctx)
        try:
            prop = create_property(
                session,
                ctx,
                body=_make_property_body("Race Villa"),
                clock=clock,
            )
            units = session.scalars(
                select(Unit).where(
                    Unit.property_id == prop.id,
                    Unit.deleted_at.is_(None),
                )
            ).all()
            assert len(units) == 1
            sibling = create_unit(
                session,
                ctx,
                property_id=prop.id,
                body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
                clock=clock,
            )
            session.commit()
        finally:
            reset_current(token)

    return _TwoUnitProperty(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        property_id=prop.id,
        unit_a_id=units[0].id,
        unit_b_id=sibling.id,
    )


def _delete_worker(
    *,
    factory: sessionmaker[Session],
    seeded: _TwoUnitProperty,
    unit_id: str,
    start: threading.Barrier,
    result: _RaceResult,
) -> None:
    try:
        with factory() as session:
            ctx = _ctx_for(seeded.workspace_id, seeded.workspace_slug, seeded.actor_id)
            token = set_current(ctx)
            try:
                start.wait(timeout=5)
                try:
                    deleted = soft_delete_unit(
                        session,
                        ctx,
                        unit_id=unit_id,
                        clock=FrozenClock(_PINNED),
                    )
                    session.commit()
                    with result.lock:
                        result.deleted_unit_ids.append(deleted.id)
                except LastUnitProtected as exc:
                    session.rollback()
                    with result.lock:
                        result.errors.append(exc)
                except Exception:
                    session.rollback()
                    raise
            finally:
                reset_current(token)
    except Exception as exc:  # pragma: no cover - test harness path
        with result.lock:
            result.errors.append(exc)


def _live_unit_ids(factory: sessionmaker[Session], property_id: str) -> list[str]:
    with factory() as session, tenant_agnostic():
        return list(
            session.scalars(
                select(Unit.id)
                .where(
                    Unit.property_id == property_id,
                    Unit.deleted_at.is_(None),
                )
                .order_by(Unit.id)
            )
        )


def _scrub_seeded_property(
    factory: sessionmaker[Session], seeded: _TwoUnitProperty
) -> None:
    with factory() as session, tenant_agnostic():
        for audit in session.scalars(
            select(AuditLog).where(AuditLog.workspace_id == seeded.workspace_id)
        ).all():
            session.delete(audit)
        for model in (RoleGrant, PermissionGroupMember, PermissionGroup, UserWorkspace):
            rows = session.scalars(
                select(model).where(model.workspace_id == seeded.workspace_id)
            ).all()
            for row in rows:
                session.delete(row)
        prop = session.get(Property, seeded.property_id)
        if prop is not None:
            session.delete(prop)
        ws = session.get(Workspace, seeded.workspace_id)
        if ws is not None:
            session.delete(ws)
        user = session.get(User, seeded.actor_id)
        if user is not None:
            session.delete(user)
        session.commit()


class TestSoftDeleteLastUnitRace:
    """Two sibling deletes cannot both pass the last-unit gate on Postgres."""

    def test_one_delete_refuses_and_one_live_unit_remains(
        self, isolated_engine: Engine
    ) -> None:
        factory = _session_factory(isolated_engine)
        slug = f"unit-race-pg-{new_ulid()[-8:].lower()}"
        seeded = _bootstrap_two_unit_property(factory, slug=slug)

        start = threading.Barrier(2)
        result = _new_race_result()

        t1 = threading.Thread(
            target=_delete_worker,
            daemon=True,
            kwargs={
                "factory": factory,
                "seeded": seeded,
                "unit_id": seeded.unit_a_id,
                "start": start,
                "result": result,
            },
        )
        t2 = threading.Thread(
            target=_delete_worker,
            daemon=True,
            kwargs={
                "factory": factory,
                "seeded": seeded,
                "unit_id": seeded.unit_b_id,
                "start": start,
                "result": result,
            },
        )
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        if t1.is_alive() or t2.is_alive():
            start.abort()
            t1.join(timeout=1)
            t2.join(timeout=1)

        try:
            assert not t1.is_alive()
            assert not t2.is_alive()
            assert len(result.deleted_unit_ids) == 1, result
            assert len(result.errors) == 1, result
            assert isinstance(result.errors[0], LastUnitProtected), result
            live_unit_ids = _live_unit_ids(factory, seeded.property_id)
            assert len(live_unit_ids) == 1
            assert live_unit_ids[0] not in result.deleted_unit_ids
        finally:
            _scrub_seeded_property(factory, seeded)
