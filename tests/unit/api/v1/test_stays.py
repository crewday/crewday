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
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.stays.models import Reservation, StayBundle
from app.adapters.ical.ports import IcalProvider, IcalValidation, IcalValidationError
from app.adapters.ical.validator import Fetcher, FetchResponse, Resolver
from app.api.deps import current_workspace_context, db_session
from app.api.v1.stays import (
    build_stays_public_router,
    build_stays_router,
    get_app_settings,
    get_clock,
    get_envelope,
    get_guest_settings_resolver,
    get_ical_fetcher,
    get_ical_resolver,
    get_ical_validator_builder,
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
    fetcher: Fetcher | None = None,
    resolver: Resolver | None = None,
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
    # Builder factory ignores ``allow_self_signed`` — the test
    # validator is a stub that does not open sockets, so the TLS
    # posture is irrelevant at this layer. Routes still call the
    # builder once per request, exercising the cd-t2qtg cascade
    # lookup the factory wires up.
    app.dependency_overrides[get_ical_validator_builder] = lambda: (
        lambda _allow_self_signed: validator
    )
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
    app.dependency_overrides[get_ical_fetcher] = lambda: fetcher
    app.dependency_overrides[get_ical_resolver] = lambda: resolver
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


# ---------------------------------------------------------------------------
# /poll-once route (cd-jk6is)
# ---------------------------------------------------------------------------


_FAKE_PUBLIC_IP = "8.8.8.8"
_VCALENDAR_BODY = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//crewday-test//EN\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:poll-once-uid-1\r\n"
    b"DTSTART:20260501T140000Z\r\n"
    b"DTEND:20260504T110000Z\r\n"
    b"SUMMARY:Reserved (Sam Test)\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


class _ScriptedFetcher(Fetcher):
    """Test :class:`Fetcher` that returns canned ``FetchResponse`` per URL.

    Mirrors the unit-suite stub in ``tests/unit/worker/test_poll_ical.py``
    but kept inline here so the route file stays self-contained — the
    worker stub class is private to its module.
    """

    def __init__(self, responses: dict[str, list[FetchResponse]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def fetch(
        self,
        parsed: object,
        resolved_ip: str,
        *,
        deadline: float,
        max_body_bytes: int,
    ) -> FetchResponse:
        del deadline, max_body_bytes
        url = parsed.geturl()  # type: ignore[attr-defined]
        self.calls.append((url, resolved_ip))
        bucket = self.responses.get(url)
        if not bucket:
            raise AssertionError(f"_ScriptedFetcher: no canned response for {url!r}")
        return bucket.pop(0)


def _ok_response(body: bytes, *, etag: str | None = None) -> FetchResponse:
    headers: list[tuple[str, str]] = [("Content-Type", "text/calendar")]
    if etag is not None:
        headers.append(("ETag", etag))
    return FetchResponse(status=200, headers=tuple(headers), body=body)


def _fixed_resolver(ip: str) -> Resolver:
    def _resolve(host: str, port: int) -> list[str]:
        del host, port
        return [ip]

    return _resolve


def _register_feed(
    client: TestClient, *, property_id: str, url: str
) -> dict[str, object]:
    created = client.post(
        "/stays/ical-feeds",
        json={"property_id": property_id, "url": url},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert isinstance(body, dict)
    return body


def test_ical_poll_once_ingests_reservation_and_is_idempotent(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    """The new ``/poll-once`` route fetches, parses, and upserts.

    Re-running the same body must NOT duplicate the row (idempotency).
    """
    feed_url = "https://airbnb.example/feed.ics"
    fetcher = _ScriptedFetcher(
        responses={
            feed_url: [
                _ok_response(_VCALENDAR_BODY),
                _ok_response(_VCALENDAR_BODY),
            ]
        }
    )

    with factory() as session:
        ctx, workspace_id, _actor_id = _seed_workspace(session, slug="poll-once")
        property_id = _seed_property(session, workspace_id=workspace_id)
        session.commit()

    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
        fetcher=fetcher,
        resolver=_fixed_resolver(_FAKE_PUBLIC_IP),
    )

    feed = _register_feed(client, property_id=property_id, url=feed_url)
    feed_id = feed["id"]

    first = client.post(f"/stays/ical-feeds/{feed_id}/poll-once")
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["feed_id"] == feed_id
    assert payload["status"] == "polled"
    assert payload["error_code"] is None
    assert payload["reservations_created"] == 1
    assert payload["reservations_updated"] == 0
    assert payload["reservations_cancelled"] == 0

    with factory() as session:
        rows = list(session.scalars(select(Reservation)).all())
        assert len(rows) == 1
        assert rows[0].external_uid == "poll-once-uid-1"
        assert rows[0].source == "ical"
        assert rows[0].ical_feed_id == feed_id
        assert rows[0].workspace_id == workspace_id

    # Second call with identical body — same reservation, no new row.
    second = client.post(f"/stays/ical-feeds/{feed_id}/poll-once")
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["reservations_created"] == 0
    assert second_payload["reservations_updated"] == 0
    assert second_payload["reservations_cancelled"] == 0

    with factory() as session:
        rows = list(session.scalars(select(Reservation)).all())
        assert len(rows) == 1


def test_ical_poll_once_surfaces_fetch_failure(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    """A fetch / parse failure is surfaced as 200 + ``status='error'``.

    The poll path captures every per-feed error into the response
    payload (``error_code`` populated, no rows landed) — same shape
    the worker fan-out records. The HTTP status stays 200 because
    the call itself succeeded; the payload signals the operator
    diagnostic.
    """
    feed_url = "https://airbnb.example/feed.ics"
    # text/html is OUTSIDE the allowed-content-type set; combined with
    # a non-VCALENDAR body the fetcher gate raises
    # ``ical_url_bad_content`` before the parse phase.
    bad_response = FetchResponse(
        status=200,
        headers=(("Content-Type", "text/html"),),
        body=b"<html>not-ics</html>",
    )
    fetcher = _ScriptedFetcher(responses={feed_url: [bad_response]})

    with factory() as session:
        ctx, workspace_id, _actor_id = _seed_workspace(session, slug="poll-fail")
        property_id = _seed_property(session, workspace_id=workspace_id)
        session.commit()

    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
        fetcher=fetcher,
        resolver=_fixed_resolver(_FAKE_PUBLIC_IP),
    )
    feed = _register_feed(client, property_id=property_id, url=feed_url)
    feed_id = feed["id"]

    response = client.post(f"/stays/ical-feeds/{feed_id}/poll-once")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["feed_id"] == feed_id
    assert payload["status"] == "error"
    assert payload["error_code"] == "ical_url_bad_content"
    assert payload["reservations_created"] == 0


def test_ical_poll_once_404_for_unknown_feed(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    with factory() as session:
        ctx, _workspace_id, _actor_id = _seed_workspace(session, slug="poll-404")
        session.commit()
    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
    )

    response = client.post("/stays/ical-feeds/does-not-exist/poll-once")
    assert response.status_code == 404
    assert response.json()["detail"] == {"error": "ical_feed_not_found"}


def test_ical_poll_once_denies_worker_role(
    factory: sessionmaker[Session],
    validator: FakeValidator,
    envelope: FakeEnvelope,
    settings: Settings,
) -> None:
    """``/poll-once`` mirrors ``/poll`` permission scope (``stays.manage``).

    A worker-grade actor is denied 403 before the route body runs.
    """
    with factory() as session:
        _owner_ctx, workspace_id, _owner_id = _seed_workspace(
            session, slug="poll-once-worker"
        )
        worker = bootstrap_user(
            session,
            email="poll-once-worker-staff@example.test",
            display_name="Worker",
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
            workspace_slug="poll-once-worker",
            actor_id=worker.id,
            role="worker",
            owner=False,
        )
        session.commit()
    client = _build_client(
        factory=factory,
        ctx=ctx,
        validator=validator,
        envelope=envelope,
        settings=settings,
    )

    response = client.post("/stays/ical-feeds/anything/poll-once")
    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error": "permission_denied",
        "action_key": "stays.manage",
    }
