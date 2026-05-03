"""Unit tests for :mod:`app.api.transport.admin_sse`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.transport import admin_sse
from app.api.transport.admin_sse import AdminSSEFanOut, _stream_admin_events
from app.api.transport.sse import _ParsedLastEventId
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.events.bus import EventBus
from app.events.registry import Event
from app.events.types import DeploymentAdminSSEEvent
from app.tenancy import DeploymentContext
from tests.unit.api.admin._helpers import (
    build_client,
    engine_fixture,
    issue_session,
    seed_admin,
    seed_user,
    settings_fixture,
)


def _fresh_id() -> _ParsedLastEventId:
    return _ParsedLastEventId(stream_token=None, seq=0)


def _fake_request(disconnected: bool = False) -> MagicMock:
    state = {"disconnected": disconnected}

    async def _is_disc() -> bool:
        return state["disconnected"]

    req = MagicMock()
    req.is_disconnected = _is_disc
    req._state = state
    return req


class _CaptureRelay:
    def __init__(self) -> None:
        self.forwarded: list[Event] = []

    @property
    def worker_id(self) -> str:
        return "relay_test"

    def forward(self, event: Event) -> None:
        self.forwarded.append(event)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


async def _collect_next_frame(
    gen: AsyncIterator[bytes], *, timeout: float = 1.0
) -> bytes:
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


def _parse_frame(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line or line.startswith(":") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.lstrip(" ")
    return out


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("admin-sse")


@pytest.fixture
def engine() -> Iterator[Engine]:
    yield from engine_fixture()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from build_client(settings, session_factory, monkeypatch)


class TestAdminSSEFanOut:
    async def test_publishing_after_subscribe_delivers_frame(self) -> None:
        fanout = AdminSSEFanOut()
        gen = _stream_admin_events(
            request=_fake_request(),
            fanout=fanout,
            user_id="admin_1",
            last_event_id=_fresh_id(),
            heartbeat_interval=10.0,
        )
        retry = await _collect_next_frame(gen)
        assert retry == b"retry: 3000\n\n"

        fanout.publish(kind="admin.settings.updated", payload={"key": "signup_enabled"})

        frame = _parse_frame(await _collect_next_frame(gen))
        assert frame["event"] == "admin.settings.updated"
        assert frame["id"] == f"{fanout.stream_token}-1"
        body = json.loads(frame["data"])
        assert body["kind"] == "admin.settings.updated"
        assert body["key"] == "signup_enabled"
        assert body["invalidates"] == [["admin", "settings"]]
        await gen.aclose()

    async def test_user_scoped_events_only_reach_matching_admin(self) -> None:
        fanout = AdminSSEFanOut()
        sub_a = fanout.subscribe(user_id="admin_a")
        sub_b = fanout.subscribe(user_id="admin_b")
        fanout.publish(
            kind="agent.action.pending",
            payload={"approval_request_id": "apr_1"},
            user_scope="admin_b",
        )

        frame = _parse_frame(await asyncio.wait_for(sub_b.queue.get(), timeout=1.0))
        assert frame["event"] == "agent.action.pending"
        assert sub_a.queue.empty()

        fanout.unsubscribe(subscriber=sub_a)
        fanout.unsubscribe(subscriber=sub_b)

    async def test_replay_honours_last_event_id_and_user_scope(self) -> None:
        fanout = AdminSSEFanOut()
        fanout.publish(kind="admin.audit.appended", payload={"entity_id": "a"})
        fanout.publish(
            kind="agent.message.appended",
            payload={"message_id": "hidden"},
            user_scope="other_admin",
        )
        fanout.publish(kind="admin.admins.updated", payload={"action": "grant"})

        frames = list(
            fanout.replay_since(
                last_event_id=_ParsedLastEventId(
                    stream_token=fanout.stream_token,
                    seq=1,
                ),
                user_id="admin_1",
            )
        )
        parsed = [_parse_frame(frame)["event"] for frame in frames]
        assert parsed == ["admin.admins.updated"]

    async def test_heartbeat_uses_supplied_short_interval(self) -> None:
        fanout = AdminSSEFanOut()
        gen = _stream_admin_events(
            request=_fake_request(),
            fanout=fanout,
            user_id="admin_1",
            last_event_id=_fresh_id(),
            heartbeat_interval=0.01,
        )
        await _collect_next_frame(gen)
        assert await _collect_next_frame(gen) == b": keepalive\n\n"
        await gen.aclose()

    def test_publish_admin_event_uses_bus_relay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        admin_sse._bus_bound = False
        event_bus = EventBus()
        relay = _CaptureRelay()
        event_bus.set_relay(relay)
        published: list[dict[str, object]] = []
        monkeypatch.setattr(admin_sse, "default_event_bus", event_bus)
        monkeypatch.setattr(
            admin_sse.default_admin_fanout,
            "publish",
            lambda **kwargs: published.append(kwargs),
        )
        request = MagicMock()
        request.headers = {"X-Request-Id": "corr_admin"}
        ctx = DeploymentContext(
            principal="session_1",
            user_id="admin_1",
            actor_kind="user",
            deployment_scopes=frozenset({"deployment:admin"}),
        )
        try:
            admin_sse.publish_admin_event(
                kind="admin.settings.updated",
                ctx=ctx,
                request=request,
                payload={"key": "signup_enabled"},
            )
        finally:
            admin_sse._bus_bound = False

        assert [event["kind"] for event in published] == ["admin.settings.updated"]
        assert published[0]["payload"]["key"] == "signup_enabled"
        assert len(relay.forwarded) == 1
        relayed = relay.forwarded[0]
        assert isinstance(relayed, DeploymentAdminSSEEvent)
        assert relayed.admin_kind == "admin.settings.updated"
        assert relayed.workspace_id == "__deployment_admin__"
        assert relayed.actor_id == "admin_1"
        assert relayed.correlation_id == "corr_admin"
        assert relayed.occurred_at.tzinfo is UTC


class TestAdminEventsRoute:
    def test_non_admin_gets_invisible_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            user_id = seed_user(s, email="tenant@example.com", display_name="Tenant")
            s.commit()
        cookie = issue_session(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        resp = client.get("/admin/events")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_admin_can_open_stream(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _finite_stream(**kwargs: object) -> AsyncIterator[bytes]:
            del kwargs
            yield b"retry: 3000\n\n"

        monkeypatch.setattr(admin_sse, "_stream_admin_events", _finite_stream)
        _user_id, cookie = seed_admin(session_factory, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        resp = client.get("/admin/events")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.content == b"retry: 3000\n\n"
