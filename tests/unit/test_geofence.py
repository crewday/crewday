"""Unit tests for clock-in geofence evaluation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.time.models import GeofenceSetting
from app.adapters.db.workspace.models import Workspace
from app.domain.time.geofence import check_geofence
from app.tenancy.context import WorkspaceContext

_PINNED = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)


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
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def ctx(session: Session) -> WorkspaceContext:
    workspace_id = "01HWA00000000000000000WSGF"
    session.add(
        Workspace(
            id=workspace_id,
            slug="geofence-unit",
            name="Geofence Unit",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="geofence-unit",
        actor_id="01HWA00000000000000000USER",
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
        audit_correlation_id="01HWA00000000000000000CORR",
    )


def _setting(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str = "01HWA00000000000000000PROP",
    mode: str = "enforce",
    enabled: bool = True,
    radius_m: int = 100,
) -> None:
    session.add(
        GeofenceSetting(
            id="01HWA00000000000000000GEOF",
            workspace_id=ctx.workspace_id,
            property_id=property_id,
            lat=43.5804,
            lon=7.1251,
            radius_m=radius_m,
            enabled=enabled,
            mode=mode,
        )
    )
    session.flush()


def test_absent_property_or_setting_disables_check(
    session: Session,
    ctx: WorkspaceContext,
) -> None:
    no_property = check_geofence(
        session,
        ctx,
        property_id=None,
        client_lat=None,
        client_lon=None,
        gps_accuracy_m=None,
    )
    assert no_property.status == "disabled"

    no_setting = check_geofence(
        session,
        ctx,
        property_id="01HWA00000000000000000PROP",
        client_lat=None,
        client_lon=None,
        gps_accuracy_m=None,
    )
    assert no_setting.status == "disabled"


@pytest.mark.parametrize(
    ("mode", "enabled"),
    [
        ("off", True),
        ("enforce", False),
    ],
)
def test_off_mode_or_disabled_setting_bypasses_fix(
    session: Session,
    ctx: WorkspaceContext,
    *,
    mode: str,
    enabled: bool,
) -> None:
    _setting(session, ctx, mode=mode, enabled=enabled)

    verdict = check_geofence(
        session,
        ctx,
        property_id="01HWA00000000000000000PROP",
        client_lat=None,
        client_lon=None,
        gps_accuracy_m=None,
    )

    assert verdict.status == "disabled"
    assert verdict.mode == "off"


def test_in_radius_fix_is_ok(session: Session, ctx: WorkspaceContext) -> None:
    _setting(session, ctx, radius_m=20)

    verdict = check_geofence(
        session,
        ctx,
        property_id="01HWA00000000000000000PROP",
        client_lat=43.58045,
        client_lon=7.12515,
        gps_accuracy_m=5,
    )

    assert verdict.status == "ok"
    assert verdict.mode == "enforce"
    assert verdict.distance_m is not None
    assert verdict.distance_m <= 25


def test_accuracy_expands_radius(session: Session, ctx: WorkspaceContext) -> None:
    _setting(session, ctx, radius_m=5)

    verdict = check_geofence(
        session,
        ctx,
        property_id="01HWA00000000000000000PROP",
        client_lat=43.58045,
        client_lon=7.12515,
        gps_accuracy_m=20,
    )

    assert verdict.status == "ok"


def test_outside_radius_reports_distance(
    session: Session, ctx: WorkspaceContext
) -> None:
    _setting(session, ctx, mode="warn", radius_m=10)

    verdict = check_geofence(
        session,
        ctx,
        property_id="01HWA00000000000000000PROP",
        client_lat=43.5904,
        client_lon=7.1251,
        gps_accuracy_m=0,
    )

    assert verdict.status == "outside"
    assert verdict.mode == "warn"
    assert verdict.distance_m is not None
    assert verdict.distance_m > 10


def test_missing_fix_reports_no_fix(session: Session, ctx: WorkspaceContext) -> None:
    _setting(session, ctx)

    verdict = check_geofence(
        session,
        ctx,
        property_id="01HWA00000000000000000PROP",
        client_lat=43.5804,
        client_lon=None,
        gps_accuracy_m=12,
    )

    assert verdict.status == "no_fix"
    assert verdict.radius_m == 100
    assert verdict.gps_accuracy_m == 12
