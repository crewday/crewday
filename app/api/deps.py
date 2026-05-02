"""Shared FastAPI dependencies.

This module holds the minimal dep wiring the v1 routers need today. The
full app factory (cd-ika7) will formalise the tenancy + session middleware
that populates :func:`app.tenancy.current.get_current`; until then these
helpers read whatever the caller stashed there via
:func:`app.tenancy.current.set_current` so the routers can be exercised
from unit tests with a pinned context.

See ``docs/specs/01-architecture.md`` §"WorkspaceContext" and
``docs/specs/12-rest-api.md``.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.adapters.llm.ports import LLMClient
from app.adapters.storage.ports import MimeSniffer, Storage
from app.domain.errors import ServiceUnavailable, Unauthorized
from app.tenancy import WorkspaceContext
from app.tenancy.current import get_current

__all__ = [
    "current_workspace_context",
    "db_session",
    "get_llm",
    "get_mime_sniffer",
    "get_storage",
]


def current_workspace_context() -> WorkspaceContext:
    """FastAPI dep — return the ambient :class:`WorkspaceContext`.

    Raises :class:`Unauthorized` (401 ``unauthorized``) when no
    context is set. The production middleware (cd-ika7) resolves the
    context from the session cookie + URL slug before the handler
    runs; this dep is the read-side of that contract.
    """
    ctx = get_current()
    if ctx is None:
        raise Unauthorized("Authentication required")
    return ctx


def db_session() -> Iterator[Session]:
    """FastAPI dep — yield a :class:`~sqlalchemy.orm.Session` inside a UoW.

    The :class:`~app.adapters.db.session.UnitOfWorkImpl` commits on a
    clean exit and rolls back on an unhandled exception; the handler
    just operates on the yielded session. The UoW yields the concrete
    SQLAlchemy ``Session`` under its :class:`DbSession` Protocol
    return type; we narrow the annotation here so routers can pass
    the session straight to domain services (which still use
    ``sqlalchemy.orm.Session`` directly, per the existing pattern in
    :mod:`app.domain.identity.role_grants`). Tests override this
    dep via ``app.dependency_overrides[db_session] = …`` to pin
    the engine.
    """
    with make_uow() as session:
        assert isinstance(session, Session)
        yield session


def get_storage(request: Request) -> Storage:
    """FastAPI dep — return the configured :class:`Storage` backend.

    Reads :attr:`app.state.storage`, populated by the app factory at
    boot time from :attr:`Settings.storage_backend`. Raises
    :class:`ServiceUnavailable` (503) when no backend is wired — this
    is a deployment misconfiguration (missing ``CREWDAY_ROOT_KEY`` or
    an incomplete S3 config) rather than a client bug, so the surface
    error is "service not ready" rather than a generic 500.

    Tests override via ``app.dependency_overrides[get_storage] = …``
    to inject :class:`tests._fakes.storage.InMemoryStorage` without
    touching :attr:`app.state.storage`.
    """
    # Read lazily: the factory sets ``app.state.storage`` at boot (see
    # :func:`app.api.factory._wire_services`); tests that hit a router
    # without going through the factory must set the attribute
    # themselves or override this dep.
    storage: Storage | None = getattr(request.app.state, "storage", None)
    if storage is None:
        raise ServiceUnavailable(
            "Storage backend is not configured",
            extra={"upstream": "storage"},
        )
    return storage


def get_mime_sniffer(request: Request) -> MimeSniffer:
    """FastAPI dep — return the configured :class:`MimeSniffer`.

    Reads :attr:`app.state.mime_sniffer`, populated by the app factory
    at boot. The sniffer port is the §15 "Input validation" seam:
    every upload that touches the blob store routes through it so we
    validate the bytes themselves, not the multipart-form header an
    attacker controls. Tests override via
    ``app.dependency_overrides[get_mime_sniffer] = …`` to inject a
    deterministic stub (no library dependency, pinned verdicts).

    Mirrors :func:`get_storage` — read lazily from ``app.state`` so a
    deployment that booted without a sniffer surfaces a 503 at request
    time, not a boot-time crash that takes ``/healthz`` down.
    """
    sniffer: MimeSniffer | None = getattr(request.app.state, "mime_sniffer", None)
    if sniffer is None:
        raise ServiceUnavailable(
            "MIME sniffer is not configured",
            extra={"upstream": "mime_sniffer"},
        )
    return sniffer


def get_llm(request: Request) -> LLMClient:
    """FastAPI dep — return the configured :class:`LLMClient`.

    Reads :attr:`app.state.llm`, populated by the app factory at boot
    based on :attr:`Settings.llm_provider` — ``OpenRouterClient`` when
    a static OpenRouter env key or DB decrypt key is available
    (default), or :class:`~app.adapters.llm.fake.FakeLLMClient` when
    the dev/e2e ``CREWDAY_LLM_PROVIDER=fake`` knob is set. Raises
    :class:`ServiceUnavailable` (503) when no client is wired, or when
    the wired client cannot currently resolve an API key.

    Tests override via ``app.dependency_overrides[get_llm] = …`` to
    inject :class:`tests._fakes.llm.EchoLLMClient` or a stub.
    """
    llm: LLMClient | None = getattr(request.app.state, "llm", None)
    if llm is None:
        raise ServiceUnavailable(
            "LLM client is not configured",
            extra={"upstream": "llm"},
        )
    configured = getattr(llm, "is_configured", None)
    try:
        is_configured = bool(configured()) if callable(configured) else True
    except RuntimeError as exc:
        raise ServiceUnavailable(
            "LLM client is not configured",
            extra={"upstream": "llm"},
        ) from exc
    if not is_configured:
        raise ServiceUnavailable(
            "LLM client is not configured",
            extra={"upstream": "llm"},
        )
    return llm
