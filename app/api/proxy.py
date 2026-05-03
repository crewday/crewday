"""Dev-profile HTTP + WebSocket proxy to a running Vite dev server.

When ``settings.profile == "dev"`` :func:`app.main.create_app` mounts a
catch-all route that forwards non-API GETs to ``settings.vite_dev_url``
so the Vite HMR loop (tsx → browser refresh) keeps working while an
engineer edits the SPA under ``app/web/src/``. In production
(``profile == "prod"``) this module is not imported — the hot path
stays free of ``httpx``.

Scope for v1 (cd-q1be):

* HTTP forwarding for the documented Vite paths (``/``, ``/@vite/*``,
  ``/src/*``, ``/node_modules/*``, ``/favicon.ico``, everything else
  the SPA references from the dev server). The upstream body is
  buffered (``httpx`` ``.content``) before being handed to Starlette —
  Vite dev modules are small (per-file < a few hundred KB) so the
  simpler shape wins; true chunk-level streaming for large
  source-maps is a v2 follow-up.
* API routes and SSE streams are **not** proxied — they land on the
  real FastAPI routers via the registration-order precedence in
  :func:`app.main.create_app` and the :func:`_is_non_spa_path` guard
  inside both proxy handlers (belt + braces — cd-354g selfreview
  closed the drift between the WS and HTTP guards by sharing this
  helper).
* Hop-by-hop headers (``connection``, ``keep-alive``, ``te``,
  ``trailers``, ``transfer-encoding``, ``upgrade``, ``host``,
  ``content-length``) are stripped on both legs — Starlette will
  re-apply its own framing.

WebSocket HMR (cd-354g):

* Vite's HMR client dials ``ws://<host>/`` with the ``vite-hmr``
  subprotocol. Without the upgrade, ``[vite] server connection lost``
  bubbles to the browser console on every save and module
  replacement silently no-ops — a manual reload picks up changes via
  the HTTP proxy above but the iteration loop is noticeably slower.
* :func:`register_vite_proxy` now installs a
  ``@app.websocket('/{full_path:path}')`` route that:
    - runs the :func:`_is_non_spa_path` guard (``_is_api_path`` plus
      the SSE endpoints ``/events`` and ``/w/<slug>/events``) before
      :meth:`WebSocket.accept` so ``/api/*``, ``/admin/api/*``,
      ``/w/<slug>/api/*`` and the workspace / deployment SSE streams
      never bleed to Vite (closed with ``1008`` policy violation);
    - dials upstream via :mod:`websockets` (``websockets.asyncio.client``)
      through a ``app.state.vite_ws_connect`` seam — tests inject an
      in-memory fake here, the same pattern the HTTP leg uses via
      :class:`httpx.MockTransport`;
    - relays frames verbatim in both directions under a task pair so
      either side closing cancels the other and the upstream connection
      is always closed on the way out;
    - on upstream refusal, closes the downstream socket with
      ``1011`` (INTERNAL_ERROR) so the browser surfaces a clean
      disconnect instead of a 500 stack.

The HTTP leg reuses the same :func:`_is_non_spa_path` helper so the
two surfaces cannot drift — anything the WS route rejects, the HTTP
route also rejects (and vice versa), which is the invariant the
self-review of cd-354g flagged.

See ``docs/specs/14-web-frontend.md`` §"Serving the SPA",
``docs/specs/16-deployment-operations.md`` §"FastAPI static mount",
Beads ``cd-q1be`` and ``cd-354g``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.responses import Response, StreamingResponse
from starlette.websockets import WebSocketState
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.frames import CloseCode

__all__ = ["UpstreamWebSocket", "ViteWebSocketConnect", "register_vite_proxy"]

_log = logging.getLogger(__name__)

# Hop-by-hop headers per RFC 7230 §6.1 plus ``host`` / ``content-length``
# (Starlette / httpx re-synthesise these from the actual transport). We
# strip both on the way out (client → upstream) and on the way back
# (upstream → client) so forwarding never leaks framing state from one
# connection into the other.
_HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "host",
        "content-length",
    }
)

# Request read timeout. Vite dev servers are local (loopback by
# default) and return quickly for every file they serve; 30s is long
# enough to cover a cold-cache source-map on a slow laptop without
# letting a genuinely wedged upstream hang the browser forever.
_UPSTREAM_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(30.0)

# Upstream WebSocket open timeout. Loopback Vite should answer in
# milliseconds; a generous bound still bails out before the downstream
# browser gives up so we can translate the refusal into a clean
# ``1011`` close frame instead of an ASGI-level crash.
_UPSTREAM_WS_OPEN_TIMEOUT: Final[float] = 10.0

# Subprotocols Vite's HMR client may negotiate. Echoing back the one
# the client selected — if any — is how both ends agree on the frame
# grammar; offering an unsupported value silently breaks ``hot
# updated`` messages in the browser. The list is kept in sync with
# Vite's source (``packages/vite/src/node/server/ws.ts``).
_VITE_WS_SUBPROTOCOLS: Final[frozenset[str]] = frozenset({"vite-hmr", "vite-ping"})


def _is_sse_path(path: str) -> bool:
    """Return ``True`` for the bare and workspace-scoped SSE endpoints.

    The SPA opens ``EventSource('/events')`` when no workspace is
    selected and ``EventSource('/w/<slug>/events')`` once one is — both
    served by real FastAPI routers, not by Vite. The HTTP / WS proxy
    must NOT forward these: a WS upgrade to ``/w/<slug>/events`` in
    particular would silently shadow a future WS admin stream.

    ``/w/<slug>/events`` (exactly — no trailing segments) is treated as
    SSE; anything deeper under ``/w/<slug>/`` is either the JSON API
    (caught by :func:`app.main._is_api_path`) or plain SPA chrome.
    """
    if path == "/events" or path.startswith("/events/"):
        return True
    if path.startswith("/w/"):
        segments = [s for s in path.split("/") if s]
        # ['w', slug, 'events'] only — longer sub-paths are SPA chrome.
        return len(segments) == 3 and segments[2] == "events"
    return False


def _is_non_spa_path(path: str) -> bool:
    """Return ``True`` for anything the Vite proxy must NOT handle.

    Union of :func:`app.main._is_api_path` (API + admin API + workspace
    API) and :func:`_is_sse_path` (bare + workspace SSE). Shared by the
    HTTP and WS legs so the two surfaces cannot drift — the selfreview
    of cd-354g flagged a missing SSE guard on the WS side, and the fix
    is to collapse both into a single predicate.

    Imports :func:`_is_api_path` locally so this helper can be imported
    by tests without pulling the factory's transitive router graph.
    """
    from app.main import _is_api_path

    return _is_api_path(path) or _is_sse_path(path)


class UpstreamWebSocket(Protocol):
    """Minimal surface the WS handler needs from a Vite upstream dial.

    A tiny :class:`~typing.Protocol` so tests can hand in a pure-Python
    fake (``asyncio.Queue``-backed) without pulling the real
    :mod:`websockets` machinery into the unit layer, and so the
    production adapter (:class:`websockets.asyncio.client.ClientConnection`)
    can satisfy the contract without an inheritance relationship.

    Mirrors only the four verbs the bidirectional relay actually uses:
    receive one frame, send one frame, close with a code, and look up
    the selected subprotocol to echo back downstream.

    ``subprotocol`` is exposed as a read-only :func:`property` because
    :class:`websockets.asyncio.client.ClientConnection` declares it
    that way; declaring a plain attribute here would demand a settable
    member and break structural conformance.
    """

    @property
    def subprotocol(self) -> str | None: ...

    async def recv(self) -> str | bytes: ...
    async def send(self, message: str | bytes) -> None: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


ViteWebSocketConnect = Callable[
    [str, Sequence[str]],
    "AbstractAsyncContextManager[UpstreamWebSocket]",
]
"""Signature of the ``app.state.vite_ws_connect`` seam.

Takes the upstream URL (``ws://127.0.0.1:5173/…``) and the list of
subprotocols the downstream client offered, and returns an async
context manager whose ``__aenter__`` yields an
:class:`UpstreamWebSocket`-shaped object. Production wires
:func:`_default_vite_ws_connect` which wraps
:func:`websockets.asyncio.client.connect`; tests inject an in-memory
fake so no real socket is needed.
"""


def _filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop + host headers before forwarding to Vite."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Strip hop-by-hop + content-length headers from the Vite response.

    We drop ``content-length`` along with the hop-by-hop set because
    Starlette's :class:`StreamingResponse` sets ``transfer-encoding:
    chunked`` itself; echoing the upstream length would let the two
    disagree on wire framing.
    """
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }


async def _forward_client_to_upstream(
    client: WebSocket,
    upstream: UpstreamWebSocket,
) -> None:
    """Pump frames from the browser to Vite until the client hangs up.

    Split from the upstream → client direction so each loop can keep
    its own narrow exception envelope: a browser disconnect raises
    :class:`WebSocketDisconnect` from :mod:`starlette`, whereas an
    upstream close comes out of :mod:`websockets` as
    :class:`~websockets.exceptions.ConnectionClosed`. Having two
    functions lets the relay supervisor
    (:func:`_relay_ws_frames`) cancel the other side precisely on
    whichever loop finishes first.
    """
    try:
        while True:
            # ``receive()`` returns the raw ASGI message so we can
            # forward ``text`` or ``bytes`` frames verbatim without a
            # lossy encode/decode round-trip. Vite HMR only emits
            # text today, but the relay must stay framing-preserving
            # for future use (``vite-plugin-pwa`` ships binary patches).
            message = await client.receive()
            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                return
            if "text" in message and message["text"] is not None:
                await upstream.send(message["text"])
            elif "bytes" in message and message["bytes"] is not None:
                await upstream.send(message["bytes"])
            # Silently drop anything else — an unexpected message
            # type on a WebSocket is an ASGI protocol bug, not
            # something to escalate onto the wire.
    except WebSocketDisconnect:
        # Normal browser navigation / hot reload — the corresponding
        # upstream close lands on ``_forward_upstream_to_client``.
        return


async def _forward_upstream_to_client(
    upstream: UpstreamWebSocket,
    client: WebSocket,
) -> None:
    """Pump frames from Vite to the browser until upstream closes.

    Preserves ``str`` vs ``bytes`` by dispatching on the runtime
    type :class:`websockets` hands back — the library already returns
    the right Python type based on the wire opcode, so we match it
    to :meth:`WebSocket.send_text` / :meth:`WebSocket.send_bytes`
    without inspecting the frame.
    """
    try:
        while True:
            data = await upstream.recv()
            if isinstance(data, str):
                await client.send_text(data)
            else:
                await client.send_bytes(data)
    except ConnectionClosed:
        return


async def _relay_ws_frames(
    client: WebSocket,
    upstream: UpstreamWebSocket,
    *,
    path: str,
) -> None:
    """Run the two pump tasks under a supervising ``wait(FIRST_COMPLETED)``.

    The two directions run as independent tasks so neither blocks the
    other: a long-running upstream pong without a matching client
    message must still land in the browser. The supervisor waits for
    the *first* task to complete, then cancels the sibling so we
    don't leak a pending receive hanging onto a half-closed socket.

    Either side's clean close is absorbed by the per-direction
    helper; anything else (protocol error, abnormal close) bubbles
    up so the caller can translate it into a close frame.
    """
    c_to_u = asyncio.create_task(
        _forward_client_to_upstream(client, upstream),
        name=f"vite-ws-c2u:{path}",
    )
    u_to_c = asyncio.create_task(
        _forward_upstream_to_client(upstream, client),
        name=f"vite-ws-u2c:{path}",
    )
    try:
        done, _pending = await asyncio.wait(
            {c_to_u, u_to_c}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        # Cancel the surviving task(s) and await them to ensure any
        # cleanup (``finally`` blocks, ``CancelledError`` swallowing
        # in :class:`websockets`) actually runs. Without the
        # ``gather(..., return_exceptions=True)`` a cascading
        # ``CancelledError`` would leak out of the relay.
        for task in (c_to_u, u_to_c):
            if not task.done():
                task.cancel()
        await asyncio.gather(c_to_u, u_to_c, return_exceptions=True)

    # Re-raise the original failure if one of the pumps died to a
    # non-disconnect exception — the caller needs it to pick the
    # right close code. Clean returns are already swallowed by the
    # pump helpers.
    for task in done:
        exc = task.exception()
        if exc is not None:
            raise exc


def _http_to_ws_url(http_url: str) -> str:
    """Rewrite an ``http(s)://…`` URL to the matching ``ws(s)://…``.

    Vite exposes HMR on the same host + port as its HTTP surface, so
    the whole netloc + path + query survives verbatim — only the
    scheme flips. We split with :func:`urllib.parse.urlsplit` instead
    of a ``.replace("http", "ws")`` string hack because a netloc like
    ``http-proxy.internal`` would otherwise get mangled.
    """
    parts = urlsplit(http_url)
    scheme_map = {"http": "ws", "https": "wss"}
    ws_scheme = scheme_map.get(parts.scheme, parts.scheme)
    return urlunsplit(
        (ws_scheme, parts.netloc, parts.path, parts.query, parts.fragment)
    )


@asynccontextmanager
async def _default_vite_ws_connect(
    upstream_url: str,
    subprotocols: Sequence[str],
) -> AsyncIterator[UpstreamWebSocket]:
    """Production adapter wrapping :func:`websockets.asyncio.client.connect`.

    Splits out of :func:`register_vite_proxy` so tests can substitute
    a fake via ``app.state.vite_ws_connect`` without monkeypatching
    the third-party :mod:`websockets` module. The ``subprotocols``
    list is forwarded verbatim: the client library negotiates the
    intersection with the upstream and the selected value lands on
    :attr:`ClientConnection.subprotocol`.

    An empty ``subprotocols`` sequence is passed as ``None`` because
    :func:`websockets.asyncio.client.connect` treats ``[]`` as "offer
    no subprotocols" which is different from "omit the header" — the
    omission is what we want when the downstream client didn't offer
    any either.

    :class:`ClientConnection` satisfies :class:`UpstreamWebSocket` by
    duck typing (``recv`` / ``send`` / ``close`` / ``subprotocol``);
    we annotate the narrower protocol so callers don't latch onto
    methods only the concrete library class exposes.
    """
    offered: list[str] | None = list(subprotocols) if subprotocols else None
    # ``subprotocols`` expects a ``Sequence[Subprotocol]`` (a NewType
    # over ``str``); passing plain ``list[str]`` is the documented
    # idiom but mypy --strict rejects the implicit narrowing.
    async with ws_connect(
        upstream_url,
        subprotocols=offered,  # type: ignore[arg-type]
        open_timeout=_UPSTREAM_WS_OPEN_TIMEOUT,
    ) as client:
        yield client


def register_vite_proxy(app: FastAPI, *, vite_dev_url: str) -> None:
    """Install the dev-profile Vite catch-all on ``app``.

    Call from :func:`app.main.create_app` only when
    ``settings.profile == "dev"``; never in prod. API routes must have
    already been registered so FastAPI's in-order route match lands
    ``/api/*`` / ``/w/{slug}/api/*`` on the real handlers; the
    in-handler :func:`_is_non_spa_path` check defends against a future
    registration-order slip (and also vetoes the SSE endpoints at
    ``/events`` / ``/w/<slug>/events`` which share the catch-all
    pattern but are owned by real FastAPI routers).
    """
    # One long-lived AsyncClient per FastAPI app instance. Pooling +
    # HTTP/1.1 keep-alive matter here: Vite serves dozens of small
    # ``.ts`` modules per page-load and a fresh TCP handshake for each
    # would visibly slow the dev loop. Stashed on ``app.state`` so
    # tests can swap a :class:`httpx.MockTransport`-backed client in
    # without reaching into the handler's closure.
    app.state.vite_client = httpx.AsyncClient(
        base_url=vite_dev_url.rstrip("/"),
        timeout=_UPSTREAM_TIMEOUT,
        follow_redirects=False,
    )
    app.state.vite_dev_url = vite_dev_url
    # Upstream WebSocket connector seam — stashed on app.state so
    # ``test_proxy_ws`` can swap in an in-memory fake (matching the
    # ``vite_client`` pattern used by the HTTP leg). Production runs
    # :func:`_default_vite_ws_connect`, which proxies
    # :func:`websockets.asyncio.client.connect`.
    app.state.vite_ws_connect = _default_vite_ws_connect
    # Lifecycle note: neither the :class:`httpx.AsyncClient` nor the
    # per-upgrade :mod:`websockets` connections are explicitly closed
    # by a lifespan hook (TODO cd-ika7 ties one in for the authz
    # cache). The HTTP pool is reclaimed on process exit; each
    # upstream WS is closed on the way out of its handler via the
    # ``async with`` context — no dangling tasks.

    @app.websocket("/{full_path:path}")
    async def vite_ws_proxy(websocket: WebSocket, full_path: str) -> None:
        """Relay a WebSocket upgrade verbatim to the Vite dev server.

        Vite HMR's client dials the root path with the ``vite-hmr``
        subprotocol; we forward the upgrade to the same path on
        ``app.state.vite_dev_url`` (scheme flipped to ``ws://``) and
        pipe frames in both directions until either side hangs up.

        The :func:`_is_non_spa_path` guard runs before
        :meth:`WebSocket.accept` so blocked paths (``/api/*``,
        ``/admin/api/*``, ``/w/<slug>/api/*``, ``/events``,
        ``/w/<slug>/events``) return an HTTP 403 on the handshake —
        never accepting preserves the registration-order contract for
        future WS API / SSE-over-WS routes without an explicit reject
        layer. Upstream connect refusal closes the (already-accepted)
        downstream with :data:`CloseCode.INTERNAL_ERROR` (1011),
        matching Vite's own behaviour when its backing transport
        crashes.
        """
        path = "/" + full_path
        if _is_non_spa_path(path):
            # ``WebSocket.close`` before ``accept`` translates to ASGI
            # ``websocket.close`` → HTTP 403 on the upgrade, so the
            # browser's HMR client sees a clean rejection rather than
            # a silently-accepted-then-dropped connection.
            await websocket.close(code=CloseCode.POLICY_VIOLATION)
            return

        upstream_http_url: str = websocket.app.state.vite_dev_url
        connector: ViteWebSocketConnect = websocket.app.state.vite_ws_connect

        query = websocket.url.query
        upstream_path = path + (f"?{query}" if query else "")
        upstream_url = _http_to_ws_url(upstream_http_url.rstrip("/")) + upstream_path

        # Only forward subprotocols Vite might actually negotiate.
        # An unknown value handed upstream would cause the client
        # library to reject the handshake before the relay ever
        # starts; filtering here is belt + braces with Vite's own
        # allow-list.
        offered_protocols = websocket.scope.get("subprotocols", [])
        client_protocols = [
            sp for sp in offered_protocols if sp in _VITE_WS_SUBPROTOCOLS
        ]

        try:
            upstream_cm = connector(upstream_url, client_protocols)
        except Exception:
            # Defensive — the default connector only constructs the
            # context manager synchronously and returns it; a fake
            # that raises at call time would otherwise surface as an
            # unhandled ASGI error.
            await websocket.close(code=CloseCode.INTERNAL_ERROR)
            _log.warning(
                "vite ws proxy: connector construction failed",
                extra={
                    "event": "spa_vite_ws_proxy_failed",
                    "path": path,
                    "stage": "construct",
                },
            )
            return

        try:
            async with upstream_cm as upstream:
                # Echo the negotiated subprotocol back so the browser
                # client confirms the handshake. ``upstream.subprotocol``
                # may be ``None`` when neither side offered one — the
                # same ``None`` is accepted by Starlette.
                await websocket.accept(subprotocol=upstream.subprotocol)
                await _relay_ws_frames(websocket, upstream, path=path)
        except (OSError, WebSocketException, TimeoutError) as exc:
            _log.warning(
                "vite ws proxy: upstream unreachable",
                extra={
                    "event": "spa_vite_ws_proxy_failed",
                    "path": path,
                    "error": str(exc),
                    "stage": "connect",
                },
            )
            # Close with 1011 on either the pre- or post-accept state.
            # Starlette drives the correct ASGI message from
            # ``client_state`` — a rejected handshake vs. a close frame
            # on an established connection. Once the socket is already
            # ``DISCONNECTED`` there is nothing left to do.
            if websocket.client_state in (
                WebSocketState.CONNECTING,
                WebSocketState.CONNECTED,
            ):
                await websocket.close(code=CloseCode.INTERNAL_ERROR)

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
        response_model=None,
    )
    async def vite_proxy(full_path: str, request: Request) -> Response:
        """Stream the Vite dev server's response back to the caller.

        ``full_path`` is empty on the root (``/``); we forward that as
        ``/`` so Vite serves its ``index.html``. Query strings are
        preserved verbatim — Vite uses ``?t=<timestamp>`` cache busters
        + ``?v=<hash>`` invalidation and every one of them matters.
        """
        path = "/" + full_path
        if _is_non_spa_path(path):
            # Defensive guard: registration order should have already
            # peeled these off, but a JSON envelope is the correct
            # shape for agent / CLI callers in any case.
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "detail": None},
            )

        query = request.url.query
        upstream_path = path + (f"?{query}" if query else "")

        # Read the client via ``app.state`` on every request so tests
        # can swap it after factory construction. Production traffic
        # pays one attribute lookup per request — trivial next to the
        # network hop we're about to do.
        client: httpx.AsyncClient = request.app.state.vite_client
        upstream_base: str = request.app.state.vite_dev_url

        try:
            upstream = await client.request(
                request.method,
                upstream_path,
                headers=_filter_request_headers(dict(request.headers)),
                content=await request.body(),
            )
        except httpx.RequestError as exc:
            _log.warning(
                "vite proxy: upstream unreachable",
                extra={
                    # Underscores — dot-separated triples are masked as
                    # JWTs by the redaction filter (see app.util.logging).
                    "event": "spa_vite_proxy_failed",
                    "path": path,
                    "error": str(exc),
                },
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": "vite_unreachable",
                    "detail": f"Vite dev server at {upstream_base} did not respond",
                },
            )

        return StreamingResponse(
            content=iter([upstream.content]),
            status_code=upstream.status_code,
            headers=_filter_response_headers(upstream.headers),
            media_type=upstream.headers.get("content-type"),
        )
