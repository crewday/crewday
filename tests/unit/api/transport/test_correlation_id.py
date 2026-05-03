"""Unit tests for :class:`app.api.transport.correlation_id.CorrelationIdMiddleware`.

Covers the contract spec §11 "Client abstraction" pins:

* Inbound ``X-Correlation-Id`` is preserved on the response's
  ``X-Correlation-Id-Echo`` header.
* Absent inbound header → server-minted ULID, echoed back.
* Hostile inbound (CRLF / NUL / control bytes / oversize / empty) is
  ignored and replaced with a fresh ULID.
* The resolved id lands on ``request.state.correlation_id`` so
  downstream readers (the tenancy middleware, audit / usage writers)
  share one canonical value.
"""

from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.responses import Response

from app.api.transport.correlation_id import (
    CORRELATION_ID_ECHO_HEADER,
    CORRELATION_ID_HEADER,
    CORRELATION_ID_STATE_ATTR,
    MAX_INBOUND_LENGTH,
    CorrelationIdMiddleware,
)

# ULID is 26 chars of Crockford base32 (0-9 + A-Z minus I, L, O, U).
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _build_app() -> FastAPI:
    """Minimal app carrying just the middleware under test.

    The handler reads ``request.state.correlation_id`` and returns it
    in the body so tests can assert the state binding lands in the
    same logical request as the response header.
    """
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    def echo(request: Request) -> dict[str, object]:
        return {
            "state_id": getattr(request.state, CORRELATION_ID_STATE_ATTR, None),
        }

    return app


class TestInboundEchoed:
    """Header contract: inbound ``X-Correlation-Id`` round-trips."""

    def test_ulid_inbound_is_echoed_verbatim(self) -> None:
        """The canonical case the server itself would mint passes through."""
        client = TestClient(_build_app())
        incoming = "01HXC0RR3LATI0NIDV4LU3X4ZW"
        resp = client.get("/echo", headers={CORRELATION_ID_HEADER: incoming})
        assert resp.status_code == 200
        assert resp.headers[CORRELATION_ID_ECHO_HEADER] == incoming
        assert resp.json()["state_id"] == incoming

    def test_arbitrary_printable_ascii_passes_through(self) -> None:
        """A chained agent SDK may stamp a domain-specific token —
        observability headers are best-effort, so we accept anything
        printable up to the length cap."""
        client = TestClient(_build_app())
        incoming = "conv-abc-123/turn-7"
        resp = client.get("/echo", headers={CORRELATION_ID_HEADER: incoming})
        assert resp.headers[CORRELATION_ID_ECHO_HEADER] == incoming
        assert resp.json()["state_id"] == incoming

    def test_inbound_is_stripped_of_surrounding_whitespace(self) -> None:
        """A lenient upstream that tacks a leading/trailing space onto
        a header value should not derail the echo — strip and accept."""
        client = TestClient(_build_app())
        resp = client.get("/echo", headers={CORRELATION_ID_HEADER: "  trace-1  "})
        assert resp.headers[CORRELATION_ID_ECHO_HEADER] == "trace-1"


class TestFallbackMint:
    """Absent / unsafe inbound → fresh ULID is minted server-side."""

    def test_no_header_mints_ulid(self) -> None:
        client = TestClient(_build_app())
        resp = client.get("/echo")
        assert resp.status_code == 200
        echoed = resp.headers[CORRELATION_ID_ECHO_HEADER]
        assert _ULID_RE.match(echoed), f"not a ULID: {echoed!r}"
        # Same value lands on ``request.state``.
        assert resp.json()["state_id"] == echoed

    def test_empty_header_mints_ulid(self) -> None:
        client = TestClient(_build_app())
        resp = client.get("/echo", headers={CORRELATION_ID_HEADER: ""})
        echoed = resp.headers[CORRELATION_ID_ECHO_HEADER]
        assert _ULID_RE.match(echoed)

    def test_whitespace_only_header_mints_ulid(self) -> None:
        client = TestClient(_build_app())
        resp = client.get("/echo", headers={CORRELATION_ID_HEADER: "   "})
        echoed = resp.headers[CORRELATION_ID_ECHO_HEADER]
        assert _ULID_RE.match(echoed)

    def test_oversize_header_mints_ulid(self) -> None:
        """A header value exceeding :data:`MAX_INBOUND_LENGTH` is
        replaced with a fresh ULID — the cap defends downstream log
        scrapers and audit-row column widths from a malicious bloat."""
        client = TestClient(_build_app())
        oversize = "x" * (MAX_INBOUND_LENGTH + 1)
        resp = client.get("/echo", headers={CORRELATION_ID_HEADER: oversize})
        echoed = resp.headers[CORRELATION_ID_ECHO_HEADER]
        assert echoed != oversize
        assert _ULID_RE.match(echoed)

    def test_non_ascii_header_mints_ulid(self) -> None:
        """Non-ASCII codepoints (printable to humans, opaque to many
        proxies) are rejected to keep the wire safe across hops.

        We exercise the validator directly because :class:`httpx`
        rejects non-ASCII header *values* at the client edge before
        they ever reach the ASGI app — the codepath only fires on
        production traffic, where a lenient upstream proxy might
        forward the byte sequence verbatim.
        """
        from app.api.transport.correlation_id import _coerce_inbound

        assert _coerce_inbound("café") is None


class TestStateBinding:
    """``request.state.correlation_id`` is the canonical seam for
    downstream readers (tenancy middleware, audit writers, recorder)."""

    def test_state_attribute_matches_response_header(self) -> None:
        client = TestClient(_build_app())
        incoming = "trace-from-client"
        resp = client.get("/echo", headers={CORRELATION_ID_HEADER: incoming})
        assert resp.json()["state_id"] == resp.headers[CORRELATION_ID_ECHO_HEADER]

    def test_state_attribute_set_when_inbound_missing(self) -> None:
        client = TestClient(_build_app())
        resp = client.get("/echo")
        # Even with no inbound header, the attribute exists — downstream
        # readers can rely on it always being present.
        assert resp.json()["state_id"] == resp.headers[CORRELATION_ID_ECHO_HEADER]


class TestPerRequestIsolation:
    """Each request gets its own resolved id; nothing leaks between."""

    def test_two_requests_get_distinct_minted_ids(self) -> None:
        client = TestClient(_build_app())
        resp1 = client.get("/echo")
        resp2 = client.get("/echo")
        id1 = resp1.headers[CORRELATION_ID_ECHO_HEADER]
        id2 = resp2.headers[CORRELATION_ID_ECHO_HEADER]
        assert id1 != id2

    def test_inbound_does_not_leak_into_subsequent_request(self) -> None:
        client = TestClient(_build_app())
        first = client.get("/echo", headers={CORRELATION_ID_HEADER: "from-client-1"})
        second = client.get("/echo")
        assert first.headers[CORRELATION_ID_ECHO_HEADER] == "from-client-1"
        # No inbound on the second request → fresh ULID, not the prior value.
        echoed = second.headers[CORRELATION_ID_ECHO_HEADER]
        assert echoed != "from-client-1"
        assert _ULID_RE.match(echoed)


class TestHeaderInjectionDefences:
    """CRLF / NUL / control bytes are rejected at the edge.

    Starlette's HTTP parser already rejects raw CR/LF in inbound
    header values, so the middleware's last-ditch validator only
    fires when a malformed value squeaks past a lenient upstream
    proxy. The middleware test for this branch is the unit test
    here, exercised by feeding the validator directly.
    """

    def test_validator_rejects_control_bytes(self) -> None:
        from app.api.transport.correlation_id import _coerce_inbound

        for value in (
            "abc\r\nX-Evil: injected",
            "abc\nLF-injected",
            "abc\x00NUL",
            "abc\tTAB",
            "abc\x07BEL",
        ):
            assert _coerce_inbound(value) is None, value

    def test_validator_rejects_oversize(self) -> None:
        from app.api.transport.correlation_id import _coerce_inbound

        assert _coerce_inbound("x" * (MAX_INBOUND_LENGTH + 1)) is None

    def test_validator_accepts_printable_ascii(self) -> None:
        from app.api.transport.correlation_id import _coerce_inbound

        for value in (
            "01HXC0RR3LATI0NIDV4LU3X4ZW",
            "conv-1/turn-7",
            "agent:abc#42",
        ):
            assert _coerce_inbound(value) == value


class TestEchoOnNonOkResponses:
    """The echo header lands on every response — 4xx, 5xx, handler-raised
    HTTPException, and a plain ``Response`` returned with a 5xx status.

    Spec §11 "Client abstraction" pins the echo as a per-response
    contract, not a 200-only one: a chained caller correlating across
    failures needs the same id back from every status code. The unhandled
    exception path is a separate question — Starlette's
    ``ServerErrorMiddleware`` is mounted *outside* user middleware, so
    a raw ``raise`` in a handler is converted to a 500 response that
    the user-middleware echo never sees. That gap is documented in the
    middleware module and is parity with the legacy ``X-Request-Id``
    behaviour.
    """

    def test_echo_lands_on_404(self) -> None:
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)
        client = TestClient(app)
        # No route registered -> Starlette's default 404.
        resp = client.get("/missing", headers={CORRELATION_ID_HEADER: "trace-404"})
        assert resp.status_code == 404
        assert resp.headers[CORRELATION_ID_ECHO_HEADER] == "trace-404"

    def test_echo_lands_on_handler_http_exception(self) -> None:
        """``HTTPException`` is converted to a Response by FastAPI's
        inner ``ExceptionMiddleware``, so the user-middleware sees a
        normal Response and stamps the echo header."""
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/forbidden")
        def forbidden() -> None:
            raise HTTPException(status_code=403, detail="nope")

        client = TestClient(app)
        resp = client.get("/forbidden", headers={CORRELATION_ID_HEADER: "trace-403"})
        assert resp.status_code == 403
        assert resp.headers[CORRELATION_ID_ECHO_HEADER] == "trace-403"

    def test_echo_lands_on_explicit_5xx_response(self) -> None:
        """A handler returning a Response with a 5xx status code (rather
        than raising) keeps the echo contract — same as 4xx."""
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/oops")
        def oops() -> Response:
            return Response(content=b"oops", status_code=503, media_type="text/plain")

        client = TestClient(app)
        resp = client.get("/oops", headers={CORRELATION_ID_HEADER: "trace-503"})
        assert resp.status_code == 503
        assert resp.headers[CORRELATION_ID_ECHO_HEADER] == "trace-503"

    def test_unhandled_exception_propagates_without_echo(self) -> None:
        """Document the known limitation: an unhandled exception
        bubbling out of the handler reaches Starlette's outer
        ``ServerErrorMiddleware`` *above* this middleware in the
        request-time stack, so the synthesised 500 response carries
        no ``X-Correlation-Id-Echo``. The legacy ``X-Request-Id``
        plumbing has the same gap; revisiting it would require
        reimplementing the middleware as pure ASGI."""
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/boom")
        def boom() -> None:
            raise RuntimeError("kaboom")

        # ``raise_server_exceptions=True`` (TestClient default) would
        # propagate the exception; flip to False to inspect the 500
        # response itself.
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/boom", headers={CORRELATION_ID_HEADER: "trace-boom"})
        assert resp.status_code == 500
        # Documented gap: the echo is absent on this path.
        assert CORRELATION_ID_ECHO_HEADER not in resp.headers
