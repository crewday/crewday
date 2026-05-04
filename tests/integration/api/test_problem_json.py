"""Integration snapshots for ``HTTPException`` → problem+json spreading.

The RFC 7807 seam (``app/api/errors.py::_handle_http_exception``)
must render native ``HTTPException`` instances through the same
envelope as :class:`DomainError` subclasses. In particular, the
router idiom ``HTTPException(detail={"error": "token_not_found"})``
must surface the error symbol as a **structured JSON field** at the
envelope top level — not as a Python-repr string jammed into
``detail`` (cd-c4nn).

Each case here mounts the full :func:`app.api.factory.create_app`
(same pattern as ``tests/integration/api/test_error_envelope.py``)
and a tiny probe router that raises one shape of ``HTTPException``
per request. Asserting against the composed app exercises the whole
handler chain — middleware, exception handler, envelope builder —
not just the ``_handle_http_exception`` function in isolation
(which the unit tests already cover).

See ``docs/specs/12-rest-api.md`` §"Errors" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.errors import CONTENT_TYPE_PROBLEM_JSON
from app.api.factory import create_app
from app.config import Settings
from app.domain.errors import CANONICAL_TYPE_BASE

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures — composed app + TestClient
# ---------------------------------------------------------------------------


def _pinned_settings(
    db_url: str,
    *,
    profile: Literal["prod", "dev"] = "prod",
) -> Settings:
    """Settings bound to the integration-harness DB URL.

    Mirrors the shape used in
    :mod:`tests.integration.api.test_error_envelope` so the two
    integration tests share a common baseline.
    """
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-problem-json-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile=profile,
        vite_dev_url="http://127.0.0.1:5173",
        demo_mode=False,
        demo_frame_ancestors=None,
        hsts_enabled=False,
        cors_allow_origins=[],
    )


def _build_probe_router() -> APIRouter:
    """Build the synthetic probe router — one route per ``detail`` shape.

    Each route raises a :class:`HTTPException` whose ``detail`` hits a
    different branch of ``_handle_http_exception``. The routes live
    under ``/api/`` so the tenancy middleware's bare-host allow-list
    skips tenant binding — the envelope is what we're testing, not
    workspace routing.
    """
    r = APIRouter()

    @r.get("/api/_probe/http/dict_detail", include_in_schema=False)
    def probe_dict_detail() -> None:
        # The concrete bug from cd-c4nn: a dict-shaped detail was
        # getting ``str()``'d into a Python-repr string inside the
        # ``detail`` field. The envelope must surface ``"error":
        # "token_not_found"`` as a top-level JSON field instead.
        raise HTTPException(
            status_code=404,
            detail={"error": "token_not_found"},
        )

    @r.get("/api/_probe/http/dict_detail_with_message", include_in_schema=False)
    def probe_dict_detail_with_message() -> None:
        # ``message`` inside a dict detail becomes the envelope's
        # ``detail`` field; every other key flows through ``extra``.
        raise HTTPException(
            status_code=422,
            detail={"error": "too_many_tokens", "message": "cap is 5 per user"},
        )

    @r.get("/api/_probe/http/string_detail", include_in_schema=False)
    def probe_string_detail() -> None:
        # Backwards-compat path: plain-string details still render in
        # the ``detail`` field.
        raise HTTPException(status_code=404, detail="missing row")

    @r.get("/api/_probe/http/none_detail", include_in_schema=False)
    def probe_none_detail() -> None:
        # FastAPI defaults ``detail`` to ``HTTPStatus.phrase`` when
        # omitted; the envelope suppresses it to avoid duplicating
        # ``title``. Observationally indistinguishable from a caller
        # who passed ``detail=None`` — both land with no ``detail``
        # field in the envelope.
        raise HTTPException(status_code=404)

    @r.get("/api/_probe/http/list_detail", include_in_schema=False)
    def probe_list_detail() -> None:
        # Fallback path for arbitrary types: ``str(detail)`` so the
        # envelope still carries a non-null ``detail``.
        raise HTTPException(status_code=400, detail=["first", "second"])

    @r.get("/api/_probe/http/dict_detail_reserved_keys", include_in_schema=False)
    def probe_dict_detail_reserved_keys() -> None:
        # A router that put reserved envelope keys in ``detail`` must
        # NOT be able to stomp them on the envelope — the
        # ``problem_response`` ``extra`` guard drops them silently.
        raise HTTPException(
            status_code=404,
            detail={
                "error": "token_not_found",
                "type": "attacker-supplied",
                "status": 200,
                "harmless": "ok",
            },
        )

    @r.get("/api/_probe/http/gone", include_in_schema=False)
    def probe_gone() -> None:
        raise HTTPException(
            status_code=410,
            detail={"error": "welcome_link_expired", "reason": "expired"},
        )

    return r


def _compose_app(db_url: str) -> FastAPI:
    """Compose the real app and splice the probe router in.

    :func:`create_app` registers the SPA catch-all last (the
    ``/{full_path:path}`` matcher would swallow anything mounted after
    it), so we insert the probe routes just before that entry. Mirrors
    the approach in ``tests/integration/api/test_error_envelope.py``
    so any regression in the splicing shows up in both places at once.
    """
    app = create_app(settings=_pinned_settings(db_url))
    probe_router = _build_probe_router()

    routes = app.router.routes
    catch_all_index = next(
        (
            idx
            for idx, route in enumerate(routes)
            if getattr(route, "path", None) == "/{full_path:path}"
        ),
        len(routes),
    )
    for offset, probe_route in enumerate(probe_router.routes):
        routes.insert(catch_all_index + offset, probe_route)
    return app


@pytest.fixture
def composed_client(db_url: str) -> Iterator[TestClient]:
    """TestClient over the full :func:`create_app` with probe routes."""
    app = _compose_app(db_url)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _type_uri(short: str) -> str:
    return f"{CANONICAL_TYPE_BASE}{short}"


# ---------------------------------------------------------------------------
# Dict-detail spreading — the core cd-c4nn fix
# ---------------------------------------------------------------------------


class TestDictDetailSpreadsIntoEnvelope:
    """``HTTPException(detail={...})`` spreads keys into the envelope.

    The regression this guards against: the handler previously did
    ``str(exc.detail)``, producing ``"detail": "{'error': 'token_not_found'}"``
    — a Python-repr string, not a JSON object. Any SDK or CLI built
    against the spec had to string-parse a repr to recover the symbol.
    """

    def test_error_symbol_surfaces_at_envelope_top_level(
        self, composed_client: TestClient
    ) -> None:
        resp = composed_client.get("/api/_probe/http/dict_detail")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
        body = resp.json()

        # Structured JSON field at envelope top level — not a repr string.
        assert body["error"] == "token_not_found"
        assert isinstance(body["error"], str)

        # Standard envelope keys still populated.
        assert body["type"] == _type_uri("not_found")
        assert body["title"] == "Not found"
        assert body["status"] == 404
        assert body["instance"] == "/api/_probe/http/dict_detail"

    def test_detail_field_is_not_a_repr_string(
        self, composed_client: TestClient
    ) -> None:
        """Regression guard: ``detail`` must NOT be ``"{'error': ...}"``."""
        body = composed_client.get("/api/_probe/http/dict_detail").json()
        # Either ``detail`` is absent (dict had no ``message`` key) or
        # it's a real string — never the Python-repr of a dict.
        detail = body.get("detail")
        if detail is not None:
            assert not detail.startswith("{"), (
                f"detail {detail!r} looks like a Python-repr string; "
                "dict-shaped details must spread into envelope fields"
            )

    def test_dict_detail_without_message_omits_detail_field(
        self, composed_client: TestClient
    ) -> None:
        body = composed_client.get("/api/_probe/http/dict_detail").json()
        # The dict had no ``"message"`` key — the envelope's ``detail``
        # field must be absent rather than render as a repr string.
        assert "detail" not in body

    def test_dict_detail_message_becomes_envelope_detail(
        self, composed_client: TestClient
    ) -> None:
        resp = composed_client.get("/api/_probe/http/dict_detail_with_message")
        assert resp.status_code == 422
        body = resp.json()
        assert body["type"] == _type_uri("validation")
        assert body["status"] == 422
        # ``message`` lifted into envelope's ``detail`` field.
        assert body["detail"] == "cap is 5 per user"
        # Other keys flow through as extension fields.
        assert body["error"] == "too_many_tokens"
        # ``message`` is consumed into ``detail`` — it must not also
        # appear as an extension field.
        assert "message" not in body

    def test_dict_detail_cannot_overwrite_reserved_envelope_keys(
        self, composed_client: TestClient
    ) -> None:
        """A router dict that names reserved keys must not stomp them.

        ``problem_response`` already guards ``type``/``status``/etc.;
        this test pins that guard end-to-end for the dict-detail path
        so a future refactor can't quietly open the hole.
        """
        resp = composed_client.get("/api/_probe/http/dict_detail_reserved_keys")
        assert resp.status_code == 404
        body = resp.json()
        # Reserved keys keep the handler-computed values.
        assert body["type"] == _type_uri("not_found")
        assert body["status"] == 404
        # Non-reserved keys pass through.
        assert body["error"] == "token_not_found"
        assert body["harmless"] == "ok"


# ---------------------------------------------------------------------------
# Backwards-compat paths — string, None, and other types
# ---------------------------------------------------------------------------


class TestStringDetailPreserved:
    """Plain-string details still render in the ``detail`` field."""

    def test_string_detail_flows_through(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/http/string_detail")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
        body = resp.json()
        assert body["type"] == _type_uri("not_found")
        assert body["title"] == "Not found"
        assert body["status"] == 404
        assert body["detail"] == "missing row"
        assert body["instance"] == "/api/_probe/http/string_detail"
        # No leftover extension fields from the dict-spread path.
        assert "error" not in body


class TestNoneDetailPreserved:
    """FastAPI's default ``detail`` (``HTTPStatus.phrase``) is suppressed."""

    def test_default_detail_equals_title_is_suppressed(
        self, composed_client: TestClient
    ) -> None:
        resp = composed_client.get("/api/_probe/http/none_detail")
        assert resp.status_code == 404
        body = resp.json()
        # Envelope still shaped correctly — just no redundant ``detail``
        # duplicating ``title``.
        assert body["type"] == _type_uri("not_found")
        assert body["title"] == "Not found"
        assert "detail" not in body


class TestOtherTypeDetailFallsBackToStr:
    """List / tuple / int details fall back to ``str()`` — pre-fix behaviour."""

    def test_list_detail_str_fallback(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/http/list_detail")
        assert resp.status_code == 400
        body = resp.json()
        assert body["type"] == _type_uri("validation")
        # Not a dict and not a string — envelope carries the ``str()``
        # rendering rather than bailing with no ``detail`` at all.
        assert body["detail"] == "['first', 'second']"


class TestGoneDetail:
    """HTTP 410 maps to the canonical ``gone`` problem type."""

    def test_gone_uses_canonical_type(self, composed_client: TestClient) -> None:
        resp = composed_client.get("/api/_probe/http/gone")
        assert resp.status_code == 410
        body = resp.json()
        assert body["type"] == _type_uri("gone")
        assert body["title"] == "Gone"
        assert body["status"] == 410
        assert body["error"] == "welcome_link_expired"
        assert body["reason"] == "expired"


# ---------------------------------------------------------------------------
# Cross-cutting: content-type + type URI invariant on every probe
# ---------------------------------------------------------------------------


_PROBE_PATHS: tuple[str, ...] = (
    "/api/_probe/http/dict_detail",
    "/api/_probe/http/dict_detail_with_message",
    "/api/_probe/http/string_detail",
    "/api/_probe/http/none_detail",
    "/api/_probe/http/list_detail",
    "/api/_probe/http/dict_detail_reserved_keys",
    "/api/_probe/http/gone",
)


class TestEnvelopeInvariants:
    """Every HTTPException-driven response keeps the envelope contract."""

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_content_type_is_problem_json(
        self, composed_client: TestClient, path: str
    ) -> None:
        resp = composed_client.get(path)
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_type_is_full_canonical_uri(
        self, composed_client: TestClient, path: str
    ) -> None:
        body = composed_client.get(path).json()
        assert body["type"].startswith(CANONICAL_TYPE_BASE)

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_instance_equals_request_path(
        self, composed_client: TestClient, path: str
    ) -> None:
        body = composed_client.get(path).json()
        assert body["instance"] == path
