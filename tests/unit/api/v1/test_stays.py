"""Focused route tests for the stays API surface (cd-0510)."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.stays.models import Reservation, StayBundle
from app.adapters.ical.ports import IcalProvider, IcalValidation, IcalValidationError
from app.api.deps import current_workspace_context, db_session
from app.api.v1.stays import (
    build_stays_public_router,
    build_stays_router,
    get_app_settings,
    get_clock,
    get_envelope,
    get_guest_settings_resolver,
    get_ical_validator,
    get_provider_detector,
    get_tasks_create_occurrence_port,
    get_welcome_resolver,
)
from app.config import Settings
from app.domain.stays.guest_link_service import mint_link
from app.ports.tasks_create_occurrence import RecordingTasksCreateOccurrencePort
from app.tenancy import WorkspaceContext
from app.tenancy.context import ActorGrantRole
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.envelope import FakeEnvelope
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_ROOT_KEY = "test-root-key-cd-0510-route-suite-fixed-32+ chars"


class FakeValidator:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failures: dict[str, IcalValidationError] = {}

    def validate(self, url: str) -> IcalValidation:
        self.calls.append(url)
        failure = self.failures.get(url)
        if failure is not None:
            raise failure
        return IcalValidation(
            url=url,
            resolved_ip="203.0.113.7",
            content_type="text/calendar",
            parseable_ics=True,
            bytes_read=128,
        )


class FakeDetector:
    def detect(self, url: str) -> IcalProvider:
        if "airbnb" in url:
            return "airbnb"
        return "generic"


class EmptySettingsResolver:
    def resolve_bool(
        self,
        *,
        session: Session,
        workspace_id: str,
        property_id: str,
        unit_id: str | None,
        key: str,
    ) -> bool:
        del session, workspace_id, property_id, unit_id, key
        return False


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
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def validator() -> FakeValidator:
    return FakeValidator()


@pytest.fixture
def envelope() -> FakeEnvelope:
    return FakeEnvelope()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        root_key=SecretStr(_ROOT_KEY),
        public_url="https://app.example.test",
    )


def _ctx(
    *,
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    role: ActorGrantRole = "manager",
    owner: bool = True,
) -> WorkspaceContext:
    return build_workspace_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=owner,
    )


def _seed_workspace(
    session: Session,
    *,
    slug: str = "stays",
    role: ActorGrantRole = "manager",
) -> tuple[WorkspaceContext, str, str]:
    user = bootstrap_user(
        session,
        email=f"{slug}@example.test",
        display_name=f"{slug} user",
    )
    workspace = bootstrap_workspace(
        session,
        slug=slug,
        name=f"{slug} workspace",
        owner_user_id=user.id,
    )
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace.id,
            user_id=user.id,
            grant_role=role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()
    return (
        _ctx(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=user.id,
            role=role,
            owner=role == "manager",
        ),
        workspace.id,
        user.id,
    )


def _seed_property(session: Session, *, workspace_id: str) -> str:
    from app.adapters.db.places.models import Property

    property_id = new_ulid()
    session.add(
        Property(
            id=property_id,
            name="Villa Sud",
            kind="str",
            address="12 Chemin des Oliviers",
            address_json={"country": "FR"},
            country="FR",
            locale=None,
            default_currency=None,
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            client_org_id=None,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={"wifi_ssid": "Crewday", "wifi_password": "secret"},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
    )
    session.flush()
    return property_id


def _seed_reservation(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    check_in: datetime,
    check_out: datetime,
    status: str = "scheduled",
    guest_name: str | None = "Ada Guest",
) -> str:
    reservation_id = new_ulid()
    session.add(
        Reservation(
            id=reservation_id,
            workspace_id=workspace_id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=f"manual-{reservation_id}",
            check_in=check_in,
            check_out=check_out,
            guest_name=guest_name,
            guest_count=2,
            status=status,
            source="manual",
            raw_summary=None,
            raw_description=None,
            guest_link_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return reservation_id


def _seed_bundle(
    session: Session,
    *,
    workspace_id: str,
    reservation_id: str,
) -> str:
    bundle_id = new_ulid()
    session.add(
        StayBundle(
            id=bundle_id,
            workspace_id=workspace_id,
            reservation_id=reservation_id,
            kind="turnover",
            tasks_json=[{"rule_id": "rule_default_after_checkout", "status": "ready"}],
            created_at=_PINNED,
        )
    )
    session.flush()
    return bundle_id


def _build_client(
    *,
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
    task_port: RecordingTasksCreateOccurrencePort | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(build_stays_router(), prefix="/stays")
    app.include_router(build_stays_public_router(), prefix="/api/v1/stays")

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        with UnitOfWorkImpl(session_factory=factory) as session:
            assert isinstance(session, Session)
            yield session

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_ical_validator] = lambda: validator
    app.dependency_overrides[get_provider_detector] = lambda: FakeDetector()
    app.dependency_overrides[get_envelope] = lambda: envelope
    app.dependency_overrides[get_clock] = lambda: FrozenClock(_PINNED)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[get_guest_settings_resolver] = lambda: (
        EmptySettingsResolver()
    )
    app.dependency_overrides[get_tasks_create_occurrence_port] = lambda: (
        task_port if task_port is not None else RecordingTasksCreateOccurrencePort()
    )
    # Use the production minimal resolver for route coverage.
    app.dependency_overrides[get_welcome_resolver] = lambda: get_welcome_resolver()
    return TestClient(app, raise_server_exceptions=False)


def _build_public_client(
    *,
    factory: sessionmaker[Session],
    settings: Settings,
) -> TestClient:
    app = FastAPI()
    app.include_router(build_stays_public_router(), prefix="/api/v1/stays")

    def _override_db() -> Iterator[Session]:
        with UnitOfWorkImpl(session_factory=factory) as session:
            assert isinstance(session, Session)
            yield session

    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_clock] = lambda: FrozenClock(_PINNED)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[get_guest_settings_resolver] = lambda: (
        EmptySettingsResolver()
    )
    app.dependency_overrides[get_welcome_resolver] = lambda: get_welcome_resolver()
    return TestClient(app, raise_server_exceptions=False)


def test_ical_feed_crud_disable_delete_and_manual_poll(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    with factory() as session:
        ctx, workspace_id, _actor_id = _seed_workspace(session, slug="ical")
        property_id = _seed_property(session, workspace_id=workspace_id)
        session.commit()
    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
    )

    created = client.post(
        "/stays/ical-feeds",
        json={"property_id": property_id, "url": "https://airbnb.example/feed.ics"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["provider"] == "airbnb"
    assert body["url_preview"] == "https://airbnb.example"
    assert "feed.ics" not in body["url_preview"]

    listed = client.get("/stays/ical-feeds", params={"property_id": property_id})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]
    assert listed.json()[0]["url_preview"] == "(encrypted)"

    polled = client.post(f"/stays/ical-feeds/{body['id']}/poll")
    assert polled.status_code == 200
    assert polled.json()["ok"] is True

    patched = client.patch(
        f"/stays/ical-feeds/{body['id']}",
        json={"url": "https://calendar.example/new.ics"},
    )
    assert patched.status_code == 200
    assert patched.json()["provider"] == "generic"
    assert patched.json()["url_preview"] == "https://calendar.example"

    disabled = client.post(f"/stays/ical-feeds/{body['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    deleted = client.delete(f"/stays/ical-feeds/{body['id']}")
    assert deleted.status_code == 204
    assert client.get("/stays/ical-feeds").json() == []


def test_reservation_listing_filters_and_cursor_pagination(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    with factory() as session:
        ctx, workspace_id, _actor_id = _seed_workspace(session, slug="reservations")
        property_id = _seed_property(session, workspace_id=workspace_id)
        other_property_id = _seed_property(session, workspace_id=workspace_id)
        first = _seed_reservation(
            session,
            workspace_id=workspace_id,
            property_id=property_id,
            check_in=_PINNED + timedelta(days=1),
            check_out=_PINNED + timedelta(days=3),
        )
        second = _seed_reservation(
            session,
            workspace_id=workspace_id,
            property_id=property_id,
            check_in=_PINNED + timedelta(days=5),
            check_out=_PINNED + timedelta(days=7),
        )
        _seed_reservation(
            session,
            workspace_id=workspace_id,
            property_id=other_property_id,
            check_in=_PINNED + timedelta(days=6),
            check_out=_PINNED + timedelta(days=8),
            status="cancelled",
        )
        session.commit()
    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
    )

    page1 = client.get(
        "/stays/reservations",
        params={"property_id": property_id, "status": "scheduled", "limit": 1},
    )
    assert page1.status_code == 200
    assert [row["id"] for row in page1.json()["data"]] == [first]
    assert page1.json()["has_more"] is True
    assert page1.json()["next_cursor"] is not None

    page2 = client.get(
        "/stays/reservations",
        params={
            "property_id": property_id,
            "status": "scheduled",
            "limit": 1,
            "cursor": page1.json()["next_cursor"],
        },
    )
    assert page2.status_code == 200
    assert [row["id"] for row in page2.json()["data"]] == [second]
    assert page2.json()["has_more"] is False

    future = client.get(
        "/stays/reservations",
        params={
            "check_in_gte": (_PINNED + timedelta(days=4)).isoformat(),
            "status": "scheduled",
        },
    )
    assert future.status_code == 200
    assert {row["id"] for row in future.json()["data"]} == {second}


def test_stay_bundle_list_get_and_regenerate_is_idempotent(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    port = RecordingTasksCreateOccurrencePort()
    with factory() as session:
        ctx, workspace_id, _actor_id = _seed_workspace(session, slug="bundles")
        property_id = _seed_property(session, workspace_id=workspace_id)
        stay_id = _seed_reservation(
            session,
            workspace_id=workspace_id,
            property_id=property_id,
            check_in=_PINNED + timedelta(days=1),
            check_out=_PINNED + timedelta(days=3),
        )
        _seed_reservation(
            session,
            workspace_id=workspace_id,
            property_id=property_id,
            check_in=_PINNED + timedelta(days=4),
            check_out=_PINNED + timedelta(days=6),
        )
        bundle_id = _seed_bundle(
            session,
            workspace_id=workspace_id,
            reservation_id=stay_id,
        )
        session.commit()
    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
        task_port=port,
    )

    listed = client.get("/stays/stay-bundles", params={"stay_id": stay_id})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [bundle_id]

    got = client.get(f"/stays/stay-bundles/{bundle_id}")
    assert got.status_code == 200
    assert got.json()["reservation_id"] == stay_id

    first = client.post(f"/stays/stay-bundles/{bundle_id}/regenerate")
    second = client.post(f"/stays/stay-bundles/{bundle_id}/regenerate")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["bundle"]["id"] == bundle_id
    assert second.json()["bundle"]["id"] == bundle_id
    assert len(port.calls) == 2


def test_guest_token_issue_welcome_read_and_revoke(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    with factory() as session:
        ctx, workspace_id, _actor_id = _seed_workspace(session, slug="guest")
        property_id = _seed_property(session, workspace_id=workspace_id)
        stay_id = _seed_reservation(
            session,
            workspace_id=workspace_id,
            property_id=property_id,
            check_in=_PINNED + timedelta(days=1),
            check_out=_PINNED + timedelta(days=3),
        )
        session.commit()
    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
    )

    issued = client.post(f"/stays/stays/{stay_id}/welcome-link", json={})
    assert issued.status_code == 201
    token = issued.json()["token"]
    assert isinstance(token, str)
    assert token in issued.json()["welcome_url"]
    assert client.get("/stays/guest-links").status_code == 404

    welcome = client.get(f"/api/v1/stays/welcome/{token}")
    assert welcome.status_code == 200
    assert welcome.json()["property_name"] == "Villa Sud"
    assert welcome.json()["welcome"]["wifi_ssid"] == "Crewday"

    bearer = client.get(
        "/api/v1/stays/welcome",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 200
    assert bearer.json()["property_id"] == property_id

    revoked = client.delete(f"/stays/stays/{stay_id}/welcome-link")
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    gone = client.get(f"/api/v1/stays/welcome/{token}")
    assert gone.status_code == 410
    assert gone.json()["detail"]["error"] == "welcome_link_revoked"
    assert client.get("/api/v1/stays/welcome/not-a-real-token").status_code == 410


def test_public_welcome_route_is_anonymous_but_token_gated(
    factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    with factory() as session:
        ctx, workspace_id, _actor_id = _seed_workspace(session, slug="public-guest")
        property_id = _seed_property(session, workspace_id=workspace_id)
        stay_id = _seed_reservation(
            session,
            workspace_id=workspace_id,
            property_id=property_id,
            check_in=_PINNED + timedelta(days=1),
            check_out=_PINNED + timedelta(days=3),
        )
        link = mint_link(
            session,
            ctx,
            stay_id=stay_id,
            property_id=property_id,
            check_out_at=_PINNED + timedelta(days=3),
            settings=settings,
            clock=FrozenClock(_PINNED),
        )
        session.commit()
    client = _build_public_client(factory=factory, settings=settings)

    valid = client.get(f"/api/v1/stays/welcome/{link.token}")
    assert valid.status_code == 200
    assert valid.json()["property_name"] == "Villa Sud"

    missing_bearer = client.get("/api/v1/stays/welcome")
    assert missing_bearer.status_code == 401
    assert missing_bearer.json()["detail"]["error"] == "missing_bearer_token"

    invalid = client.get("/api/v1/stays/welcome/not-a-real-token")
    assert invalid.status_code == 410
    assert invalid.json()["detail"] == {
        "error": "welcome_link_expired",
        "reason": "expired",
    }
    assert "Villa Sud" not in invalid.text


def test_stays_actions_deny_worker_by_default(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    with factory() as session:
        _owner_ctx, workspace_id, _owner_id = _seed_workspace(
            session,
            slug="worker-denied",
        )
        worker = bootstrap_user(
            session,
            email="worker-denied-staff@example.test",
            display_name="Denied worker",
        )
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=worker.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        ctx = _ctx(
            workspace_id=workspace_id,
            workspace_slug="worker-denied",
            actor_id=worker.id,
            role="worker",
            owner=False,
        )
        property_id = _seed_property(session, workspace_id=workspace_id)
        stay_id = _seed_reservation(
            session,
            workspace_id=workspace_id,
            property_id=property_id,
            check_in=_PINNED + timedelta(days=1),
            check_out=_PINNED + timedelta(days=3),
        )
        session.commit()
    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
    )

    read = client.get("/stays/reservations")
    assert read.status_code == 403
    assert read.json()["detail"] == {
        "error": "permission_denied",
        "action_key": "stays.read",
    }

    manage = client.post(f"/stays/stays/{stay_id}/welcome-link", json={})
    assert manage.status_code == 403
    assert manage.json()["detail"] == {
        "error": "permission_denied",
        "action_key": "stays.manage",
    }
