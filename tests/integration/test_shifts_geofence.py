"""Integration tests for geofence enforcement on ``POST /shifts/open``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.time.models import GeofenceSetting, Shift
from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.time import router as time_router
from app.events import ShiftGeofenceWarning, bus
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
_PROPERTY_ID = "01HWA00000000000000000PROP"


def _load_all_models() -> None:
    import importlib
    import pkgutil

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
def api_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(api_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)


@pytest.fixture(autouse=True)
def reset_bus() -> Iterator[None]:
    yield
    bus._reset_for_tests()


def _bootstrap_workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug="geofence-api",
            name="Geofence API",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, workspace_id: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=f"{user_id}@example.com",
            email_lower=canonicalise_email(f"{user_id}@example.com"),
            display_name="Worker",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="worker",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()
    return user_id


def _ctx(*, workspace_id: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="geofence-api",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> FastAPI:
    app = FastAPI()
    app.include_router(time_router)

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return app


@pytest.fixture
def client_env(
    factory: sessionmaker[Session],
) -> tuple[TestClient, sessionmaker[Session], WorkspaceContext]:
    with factory() as s:
        workspace_id = _bootstrap_workspace(s)
        user_id = _bootstrap_user(s, workspace_id=workspace_id)
        s.commit()
    ctx = _ctx(workspace_id=workspace_id, actor_id=user_id)
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, factory, ctx


def _seed_geofence(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    *,
    mode: str,
    enabled: bool = True,
    radius_m: int = 25,
) -> None:
    with factory() as s:
        s.add(
            GeofenceSetting(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                property_id=_PROPERTY_ID,
                lat=43.5804,
                lon=7.1251,
                radius_m=radius_m,
                enabled=enabled,
                mode=mode,
            )
        )
        s.commit()


def _audit_actions(factory: sessionmaker[Session], workspace_id: str) -> list[str]:
    with factory() as s:
        rows = s.scalars(
            select(AuditLog.action)
            .where(AuditLog.workspace_id == workspace_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    return list(rows)


def _shift_count(factory: sessionmaker[Session], workspace_id: str) -> int:
    with factory() as s:
        return (
            s.scalar(
                select(func.count())
                .select_from(Shift)
                .where(Shift.workspace_id == workspace_id)
            )
            or 0
        )


def test_in_fence_open_succeeds(
    client_env: tuple[TestClient, sessionmaker[Session], WorkspaceContext],
) -> None:
    client, factory, ctx = client_env
    _seed_geofence(factory, ctx, mode="enforce")

    resp = client.post(
        "/shifts/open",
        json={
            "property_id": _PROPERTY_ID,
            "client_lat": 43.58041,
            "client_lon": 7.12511,
            "gps_accuracy_m": 3,
        },
    )

    assert resp.status_code == 201, resp.text
    assert _shift_count(factory, ctx.workspace_id) == 1


def test_out_of_fence_enforce_returns_422_and_audits(
    client_env: tuple[TestClient, sessionmaker[Session], WorkspaceContext],
) -> None:
    client, factory, ctx = client_env
    _seed_geofence(factory, ctx, mode="enforce")

    resp = client.post(
        "/shifts/open",
        json={
            "property_id": _PROPERTY_ID,
            "client_lat": 43.5904,
            "client_lon": 7.1251,
            "gps_accuracy_m": 0,
        },
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "geofence_outside"
    assert detail["distance_m"] > detail["radius_m"]
    assert _shift_count(factory, ctx.workspace_id) == 0
    assert _audit_actions(factory, ctx.workspace_id) == ["shift.geofence_rejected"]


def test_out_of_fence_warn_succeeds_audits_and_emits_warning(
    client_env: tuple[TestClient, sessionmaker[Session], WorkspaceContext],
) -> None:
    client, factory, ctx = client_env
    _seed_geofence(factory, ctx, mode="warn")
    captured: list[ShiftGeofenceWarning] = []

    @bus.subscribe(ShiftGeofenceWarning)
    def _capture(event: ShiftGeofenceWarning) -> None:
        captured.append(event)

    resp = client.post(
        "/shifts/open",
        json={
            "property_id": _PROPERTY_ID,
            "client_lat": 43.5904,
            "client_lon": 7.1251,
            "gps_accuracy_m": 0,
        },
    )

    assert resp.status_code == 201, resp.text
    assert _shift_count(factory, ctx.workspace_id) == 1
    assert _audit_actions(factory, ctx.workspace_id) == [
        "open",
        "shift.geofence_warning",
    ]
    assert len(captured) == 1
    assert captured[0].property_id == _PROPERTY_ID
    assert captured[0].distance_m is not None


def test_off_mode_bypasses_missing_fix(
    client_env: tuple[TestClient, sessionmaker[Session], WorkspaceContext],
) -> None:
    client, factory, ctx = client_env
    _seed_geofence(factory, ctx, mode="off")

    resp = client.post("/shifts/open", json={"property_id": _PROPERTY_ID})

    assert resp.status_code == 201, resp.text
    assert _shift_count(factory, ctx.workspace_id) == 1
    assert _audit_actions(factory, ctx.workspace_id) == ["open"]


def test_missing_fix_enforce_returns_422_and_audits(
    client_env: tuple[TestClient, sessionmaker[Session], WorkspaceContext],
) -> None:
    client, factory, ctx = client_env
    _seed_geofence(factory, ctx, mode="enforce")

    resp = client.post("/shifts/open", json={"property_id": _PROPERTY_ID})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "geofence_fix_required"
    assert _shift_count(factory, ctx.workspace_id) == 0
    assert _audit_actions(factory, ctx.workspace_id) == ["shift.geofence_rejected"]


def test_missing_fix_warn_succeeds_with_audit_only(
    client_env: tuple[TestClient, sessionmaker[Session], WorkspaceContext],
) -> None:
    client, factory, ctx = client_env
    _seed_geofence(factory, ctx, mode="warn")
    captured: list[ShiftGeofenceWarning] = []

    @bus.subscribe(ShiftGeofenceWarning)
    def _capture(event: ShiftGeofenceWarning) -> None:
        captured.append(event)

    resp = client.post("/shifts/open", json={"property_id": _PROPERTY_ID})

    assert resp.status_code == 201, resp.text
    assert _shift_count(factory, ctx.workspace_id) == 1
    assert _audit_actions(factory, ctx.workspace_id) == [
        "open",
        "shift.geofence_warning",
    ]
    assert captured == []
