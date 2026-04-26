"""Unit tests for :class:`app.api.middleware.request_id.RequestIdMiddleware`.

The middleware:

* Echoes a valid inbound ``X-Request-Id`` (UUID) verbatim.
* Rejects a non-UUID inbound and mints a fresh UUID instead.
* Mints a fresh UUID when no header is present.
* Always responds with the resolved id under ``X-Request-Id``.
* Resets the ContextVar in ``finally`` so the binding does not
  leak across requests.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from app.util.logging import get_request_id


def _build_app() -> FastAPI:
    """Build a minimal FastAPI carrying just the request-id middleware
    so the assertions stay focused (no tenancy, no auth)."""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    captured: dict[str, object] = {}

    @app.get("/echo")
    def echo() -> dict[str, object]:
        captured["request_id"] = get_request_id()
        return {"request_id": get_request_id()}

    app.state.captured = captured
    return app


class TestRequestIdMiddleware:
    """Header passthrough + fallback minting."""

    def test_no_header_mints_uuid(self) -> None:
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/echo")
        assert resp.status_code == 200
        rid = resp.headers[REQUEST_ID_HEADER]
        # Must round-trip cleanly through :class:`uuid.UUID`.
        parsed = uuid.UUID(rid)
        assert str(parsed) == rid
        # The handler observed the same id.
        assert resp.json()["request_id"] == rid

    def test_valid_uuid_inbound_is_echoed(self) -> None:
        app = _build_app()
        client = TestClient(app)
        rid = "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"
        resp = client.get("/echo", headers={REQUEST_ID_HEADER: rid})
        assert resp.headers[REQUEST_ID_HEADER] == rid
        assert resp.json()["request_id"] == rid

    def test_inbound_without_hyphens_is_canonicalised(self) -> None:
        """A bare-hex inbound id is re-rendered through :class:`UUID`
        so downstream log scrapes see a single canonical shape."""
        app = _build_app()
        client = TestClient(app)
        bare = "9b21c6d47f034d8ca26a3f0b5b88d1aa"
        canonical = "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"
        resp = client.get("/echo", headers={REQUEST_ID_HEADER: bare})
        assert resp.headers[REQUEST_ID_HEADER] == canonical

    def test_garbage_inbound_is_replaced_with_fresh_uuid(self) -> None:
        """A non-UUID header value MUST NOT propagate — log
        injection through the request id is a real risk."""
        app = _build_app()
        client = TestClient(app)
        resp = client.get(
            "/echo",
            headers={REQUEST_ID_HEADER: "$(rm -rf /)"},
        )
        rid = resp.headers[REQUEST_ID_HEADER]
        # Fresh UUID, not the injection attempt.
        assert rid != "$(rm -rf /)"
        uuid.UUID(rid)  # parses cleanly

    def test_empty_header_is_replaced_with_fresh_uuid(self) -> None:
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/echo", headers={REQUEST_ID_HEADER: ""})
        rid = resp.headers[REQUEST_ID_HEADER]
        uuid.UUID(rid)

    def test_request_id_does_not_leak_between_requests(self) -> None:
        """The middleware resets the ContextVar in ``finally``."""
        app = _build_app()
        client = TestClient(app)

        rid1 = "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"
        rid2 = "0000bbbb-cccc-dddd-eeee-ffff00001111"

        resp1 = client.get("/echo", headers={REQUEST_ID_HEADER: rid1})
        resp2 = client.get("/echo", headers={REQUEST_ID_HEADER: rid2})

        assert resp1.headers[REQUEST_ID_HEADER] == rid1
        assert resp2.headers[REQUEST_ID_HEADER] == rid2
        # Outside any request, the ContextVar is unbound.
        assert get_request_id() is None
