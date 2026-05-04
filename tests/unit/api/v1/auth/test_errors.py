"""Unit tests for shared auth-router problem+json helpers."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import CONTENT_TYPE_PROBLEM_JSON, add_exception_handlers
from app.api.v1.auth.errors import auth_conflict, auth_rate_limited
from app.domain.errors import CANONICAL_TYPE_BASE


def _client_raising(exc: Exception) -> TestClient:
    app = FastAPI()

    @app.get("/boom")
    def boom() -> None:
        raise exc

    add_exception_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def _type_uri(short_name: str) -> str:
    return f"{CANONICAL_TYPE_BASE}{short_name}"


def test_auth_conflict_preserves_legacy_error_extension() -> None:
    client = _client_raising(auth_conflict("already_consumed"))

    response = client.get("/boom")

    assert response.status_code == 409
    assert response.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
    assert response.json() == {
        "type": _type_uri("conflict"),
        "title": "Conflict",
        "status": 409,
        "instance": "/boom",
        "error": "already_consumed",
    }


def test_auth_extra_cannot_clobber_symbol_or_reserved_problem_fields() -> None:
    client = _client_raising(
        auth_conflict(
            "already_consumed",
            extra={
                "error": "attacker",
                "status": 200,
                "title": "Wrong",
                "type": "wrong",
                "safe": "ok",
            },
        )
    )

    response = client.get("/boom")

    assert response.status_code == 409
    assert response.json() == {
        "type": _type_uri("conflict"),
        "title": "Conflict",
        "status": 409,
        "instance": "/boom",
        "error": "already_consumed",
        "safe": "ok",
    }


def test_auth_rate_limited_sets_retry_after_header() -> None:
    client = _client_raising(auth_rate_limited("rate_limited", retry_after_seconds=30))

    response = client.get("/boom")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    body = response.json()
    assert body["type"] == _type_uri("rate_limited")
    assert body["error"] == "rate_limited"
    assert body["retry_after_seconds"] == 30
