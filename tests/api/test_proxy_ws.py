"""Unit tests for the dev-profile Vite WebSocket proxy (cd-354g).

Exercises the :func:`register_vite_proxy` websocket seam added on top
of the HTTP-only v1 (cd-q1be). The upstream Vite dev server is never
actually reached — a pure-Python fake satisfies the
:class:`~app.api.proxy.UpstreamWebSocket` protocol and is installed
on ``app.state.vite_ws_connect`` by each test so the suite stays
network-free while exercising the full relay path (path guard,
scheme rewrite, bidirectional forwarding, close semantics).

The same injection seam that keeps the HTTP leg hermetic
(``app.state.vite_client``) is used here — tests drop an
``asyncio.Queue``-backed fake through a new
``app.state.vite_ws_connect`` attribute before making the
:meth:`TestClient.websocket_connect` call.

See ``docs/specs/14-web-frontend.md``, Beads ``cd-354g``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from app.api.proxy import _http_to_ws_url, _is_non_spa_path, _is_sse_path
from app.config import Settings
from app.main import create_app

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _settings(
    *,
    profile: Literal["prod", "dev"] = "dev",
    vite_dev_url: str = "http://127.0.0.1:5173",
) -> Settings:
    """Minimal :class:`Settings` for an in-memory ``dev`` factory build."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-proxy-ws-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        smtp_host=None,
        smtp_port=587,
        smtp_from=None,
        smtp_use_tls=True,
        log_level="INFO",
        cors_allow_origins=[],
        profile=profile,
        vite_dev_url=vite_dev_url,
    )


class _FakeUpstreamWebSocket:
    """Asyncio.Queue-backed fake that satisfies :class:`UpstreamWebSocket`.

    Frames ``send``-ed by the handler land on :attr:`sent`; frames the
    test queues on :attr:`incoming` are what the handler's ``recv``
    returns. ``close`` notes the code + reason the handler emitted so
    assertions can verify the close envelope.

    Shutdown propagates as :class:`ConnectionClosedOK` from ``recv``
    when the test wants to simulate upstream hanging up cleanly, or
    simply by cancelling the pending ``recv`` task when the client
    disconnects first (the relay supervisor drives that path).
    """

    def __init__(self, *, subprotocol: str | None = None) -> None:
        self._subprotocol = subprotocol
        self.incoming: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.closed_code: int | None = None
        self.closed_reason: str | None = None

    @property
    def subprotocol(self) -> str | None:
        return self._subprotocol

    async def recv(self) -> str | bytes:
        frame = await self.incoming.get()
        if frame is None:
            # Sentinel → simulate an upstream close cleanly. The
            # production adapter raises ``ConnectionClosedOK``; using
            # the real exception keeps the relay's ``except
            # ConnectionClosed`` branch on the unit path.
            from websockets.exceptions import ConnectionClosedOK
            from websockets.frames import Close, CloseCode

            raise ConnectionClosedOK(Close(CloseCode.NORMAL_CLOSURE, ""), None, None)
        return frame

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_code = code
        self.closed_reason = reason


def _dev_app_with_ws(
    upstream: _FakeUpstreamWebSocket,
    *,
    captured_urls: list[str] | None = None,
    captured_protocols: list[list[str]] | None = None,
    raise_on_connect: Exception | None = None,
) -> FastAPI:
    """Build a dev-profile app whose WS upstream is ``upstream``.

    ``captured_urls`` / ``captured_protocols`` collect the arguments
    the connector is called with so tests can verify the URL rewrite
    and subprotocol filtering without reaching into handler internals.

    ``raise_on_connect`` is raised from the connector's
    ``__aenter__`` so the upstream-refusal branch can be exercised
    without invoking the real :mod:`websockets` library.
    """
    app = create_app(settings=_settings(profile="dev"))

    @asynccontextmanager
    async def fake_connector(
        url: str, subprotocols: Sequence[str]
    ) -> AsyncIterator[_FakeUpstreamWebSocket]:
        if captured_urls is not None:
            captured_urls.append(url)
        if captured_protocols is not None:
            captured_protocols.append(list(subprotocols))
        if raise_on_connect is not None:
            raise raise_on_connect
        try:
            yield upstream
        finally:
            # Mirror the real adapter: close is a no-op if the relay
            # already tore down the connection.
            pass

    app.state.vite_ws_connect = fake_connector
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Path guard
# ---------------------------------------------------------------------------


class TestWebSocketPathGuard:
    """``_is_api_path`` must veto WS upgrades on API-shaped paths.

    Workspace SSE (``/w/<slug>/events``), bare ``/api/...``, and the
    admin tree all must reject the handshake so a future
    API-over-WebSocket handler is not shadowed by the dev proxy.
    """

    def test_bare_api_path_rejected(self) -> None:
        upstream = _FakeUpstreamWebSocket()
        captured: list[str] = []
        app = _dev_app_with_ws(upstream, captured_urls=captured)
        client = _client(app)

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/foo"),
        ):
            raise AssertionError("handshake should have been rejected")

        # Policy-violation (1008) distinguishes this from an upstream
        # refusal (1011) so a future ops dashboard can tell apart
        # "Vite is down" from "the client asked for the wrong path".
        assert excinfo.value.code == 1008
        # No upstream dial ever happened.
        assert captured == []

    def test_admin_api_path_rejected(self) -> None:
        upstream = _FakeUpstreamWebSocket()
        captured: list[str] = []
        app = _dev_app_with_ws(upstream, captured_urls=captured)
        client = _client(app)

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/admin/api/v1/foo"),
        ):
            raise AssertionError("handshake should have been rejected")

        assert excinfo.value.code == 1008
        assert captured == []

    def test_workspace_api_path_rejected(self) -> None:
        upstream = _FakeUpstreamWebSocket()
        captured: list[str] = []
        app = _dev_app_with_ws(upstream, captured_urls=captured)
        client = _client(app)

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/w/demo/api/v1/foo"),
        ):
            raise AssertionError("handshake should have been rejected")

        assert excinfo.value.code == 1008
        assert captured == []

    def test_bare_events_sse_path_rejected(self) -> None:
        """A WS upgrade to ``/events`` must never reach Vite.

        ``/events`` is a real FastAPI SSE surface; proxying a WS
        handshake to Vite would silently shadow any future
        deployment-scoped WS admin stream registered at the same path.
        """
        upstream = _FakeUpstreamWebSocket()
        captured: list[str] = []
        app = _dev_app_with_ws(upstream, captured_urls=captured)
        client = _client(app)

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/events"),
        ):
            raise AssertionError("handshake should have been rejected")

        assert excinfo.value.code == 1008
        assert captured == []

    def test_workspace_events_sse_path_rejected(self) -> None:
        """A WS upgrade to ``/w/<slug>/events`` must never reach Vite.

        The workspace-scoped SSE stream (§14 "SSE-driven invalidation")
        is the primary SSE surface; hijacking it via the dev proxy
        would drop real-time invalidation in the browser.
        """
        upstream = _FakeUpstreamWebSocket()
        captured: list[str] = []
        app = _dev_app_with_ws(upstream, captured_urls=captured)
        client = _client(app)

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/w/demo/events"),
        ):
            raise AssertionError("handshake should have been rejected")

        assert excinfo.value.code == 1008
        assert captured == []

    def test_spa_path_accepted(self) -> None:
        """Non-API paths pass the guard and reach the upstream dial."""
        upstream = _FakeUpstreamWebSocket()
        captured: list[str] = []
        app = _dev_app_with_ws(upstream, captured_urls=captured)
        client = _client(app)

        with client.websocket_connect("/"):
            pass

        # ``with`` block exit triggers downstream close → client→upstream
        # pump sees ``websocket.disconnect`` → relay cancels sibling.
        assert len(captured) == 1
        assert captured[0].startswith("ws://127.0.0.1:5173/")


# ---------------------------------------------------------------------------
# URL rewrite
# ---------------------------------------------------------------------------


class TestSchemeRewrite:
    """``http(s)://`` upstream URLs must flip to ``ws(s)://`` on dial."""

    def test_http_maps_to_ws(self) -> None:
        assert _http_to_ws_url("http://127.0.0.1:5173") == "ws://127.0.0.1:5173"

    def test_https_maps_to_wss(self) -> None:
        assert _http_to_ws_url("https://example.com:8443") == "wss://example.com:8443"

    def test_existing_ws_scheme_unchanged(self) -> None:
        assert _http_to_ws_url("ws://vite.internal") == "ws://vite.internal"

    def test_netloc_containing_http_preserved(self) -> None:
        """The naive ``str.replace('http', 'ws')`` would corrupt this netloc."""
        assert (
            _http_to_ws_url("http://http-proxy.internal:5173/")
            == "ws://http-proxy.internal:5173/"
        )

    def test_dial_url_carries_path_and_query(self) -> None:
        """Deep paths + query strings pass through to the connector."""
        upstream = _FakeUpstreamWebSocket()
        captured: list[str] = []
        app = _dev_app_with_ws(upstream, captured_urls=captured)
        client = _client(app)

        with client.websocket_connect("/@vite/client?token=abc"):
            pass

        assert captured == ["ws://127.0.0.1:5173/@vite/client?token=abc"]


# ---------------------------------------------------------------------------
# Non-SPA path predicate (`_is_sse_path` / `_is_non_spa_path`)
# ---------------------------------------------------------------------------


class TestNonSpaPathPredicate:
    """Unit-level coverage for the shared WS / HTTP guard.

    The selfreview of cd-354g flagged drift between the two legs: the
    WS handler called :func:`app.main._is_api_path` which does not
    cover ``/events`` / ``/w/<slug>/events``. Both legs now route
    through :func:`_is_non_spa_path`; these tests pin the predicate so
    a future refactor can't silently resurrect the gap.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/events",
            "/events/",
            "/events/subtree",
            "/w/demo/events",
            "/w/villa-sud/events",
        ],
    )
    def test_sse_paths_classified_as_sse(self, path: str) -> None:
        assert _is_sse_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/dashboard",
            "/w",
            "/w/",
            "/w/demo",
            "/w/demo/",
            "/w/demo/today",
            "/w/demo/events/subtree",  # only the exact 3-segment form is SSE
            "/eventsNotSse",
            "/api/v1/ping",  # API path — caught elsewhere, not here
        ],
    )
    def test_non_sse_paths_classified_as_non_sse(self, path: str) -> None:
        assert _is_sse_path(path) is False

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/ping",
            "/admin/api/v1/health",
            "/w/demo/api/v1/tasks",
            "/events",
            "/w/demo/events",
        ],
    )
    def test_non_spa_union_covers_api_and_sse(self, path: str) -> None:
        assert _is_non_spa_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/dashboard",
            "/w/demo/today",
            "/@vite/client",
            "/src/main.tsx",
        ],
    )
    def test_non_spa_union_passes_spa_paths(self, path: str) -> None:
        assert _is_non_spa_path(path) is False


# ---------------------------------------------------------------------------
# Forward-then-close (happy path)
# ---------------------------------------------------------------------------


class TestBidirectionalRelay:
    """Frames from either side must reach the other verbatim."""

    def test_upstream_text_reaches_client(self) -> None:
        """An upstream ``str`` frame arrives as text on the browser side."""
        upstream = _FakeUpstreamWebSocket(subprotocol="vite-hmr")
        app = _dev_app_with_ws(upstream)
        client = _client(app)

        # Queue the upstream message BEFORE opening the socket —
        # ``recv`` will pick it up as soon as the relay starts.
        upstream.incoming.put_nowait('{"type":"update"}')
        # Sentinel so ``recv`` returns a ``ConnectionClosedOK`` after
        # the one real frame, which terminates the upstream→client
        # pump cleanly and lets the relay unwind.
        upstream.incoming.put_nowait(None)

        with client.websocket_connect("/", subprotocols=["vite-hmr"]) as ws:
            frame = ws.receive_text()
            assert frame == '{"type":"update"}'

    def test_client_text_reaches_upstream(self) -> None:
        """A frame the browser sends lands on the upstream ``send``."""
        upstream = _FakeUpstreamWebSocket(subprotocol="vite-hmr")
        app = _dev_app_with_ws(upstream)
        client = _client(app)

        with client.websocket_connect("/", subprotocols=["vite-hmr"]) as ws:
            ws.send_text('{"type":"ping"}')
            # Trigger upstream close so the ``with`` exit doesn't hang
            # waiting for the server to drain.
            upstream.incoming.put_nowait(None)

        assert upstream.sent == ['{"type":"ping"}']

    def test_bytes_frames_reach_client(self) -> None:
        """Binary upstream frames arrive as bytes on the browser side."""
        upstream = _FakeUpstreamWebSocket()
        app = _dev_app_with_ws(upstream)
        client = _client(app)

        upstream.incoming.put_nowait(b"\x00\x01\x02")
        upstream.incoming.put_nowait(None)

        with client.websocket_connect("/") as ws:
            frame = ws.receive_bytes()
            assert frame == b"\x00\x01\x02"

    def test_bytes_frames_reach_upstream(self) -> None:
        """Client binary frames land on the upstream ``send`` as bytes.

        Kept separate from the upstream-text test because the two
        relay directions run on different tasks and the test cannot
        rely on ordering between a client ``send_bytes`` and the
        upstream ``None`` sentinel — sending only from the client and
        closing the upstream AFTER the ``with`` block drains the
        disconnect cleanly.
        """
        upstream = _FakeUpstreamWebSocket()
        app = _dev_app_with_ws(upstream)
        client = _client(app)

        with client.websocket_connect("/") as ws:
            ws.send_bytes(b"\xff\xfe")
            # Close the upstream AFTER the send so the c→u pump has
            # already flushed the bytes before the relay unwinds.
            upstream.incoming.put_nowait(None)

        assert upstream.sent == [b"\xff\xfe"]

    def test_subprotocol_echoed_to_client(self) -> None:
        """The upstream-negotiated subprotocol is echoed on ``accept``.

        Without this, the browser's ``vite-hmr`` client sees an empty
        ``Sec-WebSocket-Protocol`` response header and aborts.
        """
        upstream = _FakeUpstreamWebSocket(subprotocol="vite-hmr")
        app = _dev_app_with_ws(upstream)
        client = _client(app)

        upstream.incoming.put_nowait(None)

        with client.websocket_connect("/", subprotocols=["vite-hmr"]) as ws:
            # Starlette's :class:`WebSocketTestSession` captures the
            # ``subprotocol`` value from the ASGI ``websocket.accept``
            # message — that's what the browser receives as the
            # ``Sec-WebSocket-Protocol`` response header.
            assert ws.accepted_subprotocol == "vite-hmr"


# ---------------------------------------------------------------------------
# Subprotocol filtering
# ---------------------------------------------------------------------------


class TestSubprotocolFilter:
    """Only Vite-known subprotocols propagate upstream."""

    def test_unknown_subprotocol_filtered(self) -> None:
        """Offering a spurious subprotocol must not reach upstream.

        A bogus value passed to the real :mod:`websockets` client
        would cause the library to raise during handshake; dropping
        it here keeps the dev loop forgiving when some random
        browser extension injects its own Sec-WebSocket-Protocol.
        """
        upstream = _FakeUpstreamWebSocket()
        protocols: list[list[str]] = []
        app = _dev_app_with_ws(upstream, captured_protocols=protocols)
        client = _client(app)

        upstream.incoming.put_nowait(None)

        with client.websocket_connect("/", subprotocols=["bogus-proto"]):
            pass

        assert protocols == [[]]

    def test_known_subprotocol_forwarded(self) -> None:
        upstream = _FakeUpstreamWebSocket(subprotocol="vite-hmr")
        protocols: list[list[str]] = []
        app = _dev_app_with_ws(upstream, captured_protocols=protocols)
        client = _client(app)

        upstream.incoming.put_nowait(None)

        with client.websocket_connect("/", subprotocols=["vite-hmr"]):
            pass

        assert protocols == [["vite-hmr"]]


# ---------------------------------------------------------------------------
# Upstream connect refused
# ---------------------------------------------------------------------------


class TestUpstreamRefused:
    """A Vite outage must surface as a close, not a 500 stack."""

    def test_connection_refused_closes_cleanly(self) -> None:
        """An ``OSError`` from the connector rejects the handshake.

        The downstream side never gets an ``accept``, so Starlette's
        TestClient raises :class:`WebSocketDisconnect` with the
        1011 (INTERNAL_ERROR) code the handler emitted.
        """
        upstream = _FakeUpstreamWebSocket()
        app = _dev_app_with_ws(
            upstream, raise_on_connect=ConnectionRefusedError("refused")
        )
        client = _client(app)

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/"),
        ):
            raise AssertionError(
                "handshake should have been closed on upstream refusal"
            )

        # 1011 (INTERNAL_ERROR) — not a bare 500 stack. Distinguishable
        # from 1008 (path guard) so observability can separate the two
        # failure modes.
        assert excinfo.value.code == 1011

    def test_websocket_exception_closes_cleanly(self) -> None:
        """A :mod:`websockets` handshake failure is translated too."""
        from websockets.exceptions import InvalidHandshake

        upstream = _FakeUpstreamWebSocket()
        app = _dev_app_with_ws(
            upstream, raise_on_connect=InvalidHandshake("bad handshake")
        )
        client = _client(app)

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/"),
        ):
            raise AssertionError(
                "handshake should have been closed on upstream refusal"
            )

        assert excinfo.value.code == 1011


# ---------------------------------------------------------------------------
# State wiring
# ---------------------------------------------------------------------------


class TestStateWiring:
    """``register_vite_proxy`` stashes the connector on ``app.state``."""

    def test_default_connector_installed(self) -> None:
        app = create_app(settings=_settings(profile="dev"))
        assert callable(app.state.vite_ws_connect)

    def test_prod_profile_skips_registration(self) -> None:
        """The prod path does NOT wire the proxy (no ``httpx`` import)."""
        app = create_app(settings=_settings(profile="prod"))
        assert not hasattr(app.state, "vite_ws_connect")
