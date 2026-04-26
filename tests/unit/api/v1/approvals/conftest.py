"""Shared fixtures for the approvals HTTP router suite (cd-9ghv).

Mirrors :mod:`tests.unit.api.v1.identity.conftest` — in-memory SQLite
engine with every model loaded, a workspace + owner persona, plus the
seam-specific scaffolding the approvals router needs that the identity
suite does not:

* an :class:`app.tenancy.middleware.ActorIdentity` stamp on the
  request via a tiny FastAPI dependency override (the production
  middleware does this; the unit harness fakes it so tests can
  drive the credential-kind branch by overriding one fixture);
* a fake :class:`~app.domain.agent.runtime.ToolDispatcher` injected
  via :func:`~app.api.v1.approvals.get_tool_dispatcher` so the
  approve route can replay without a real HTTP roundtrip;
* helpers to seed pending :class:`~app.adapters.db.llm.models.ApprovalRequest`
  rows and :class:`~app.adapters.db.identity.models.ApiToken` rows.

The fixtures here do NOT mount the production tenancy middleware —
unit tests pin the :class:`WorkspaceContext` and :class:`ActorIdentity`
directly via FastAPI dependency overrides. End-to-end coverage of the
middleware-stamped path lives in ``tests/integration/api/`` as the
existing identity-suite fan-out does.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.identity.models import ApiToken
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.api.deps import current_workspace_context, db_session
from app.api.errors import add_exception_handlers
from app.api.v1.approvals import (
    APPROVALS_ACT_SCOPE,
    get_tool_dispatcher,
)
from app.api.v1.approvals import (
    router as approvals_router,
)
from app.domain.agent.runtime import (
    APPROVAL_REQUEST_TTL,
    DelegatedToken,
    ToolCall,
    ToolResult,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.middleware import ACTOR_STATE_ATTR, ActorIdentity
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

# Pinned wall clock so any test that asserts on row timestamps stays
# deterministic — matches the identity suite's ``_PINNED`` and the
# domain-suite ``_PINNED``.
_PINNED: datetime = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Engine + session factory
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<ctx>.models`` module.

    Mirrors the identity-suite helper. Without it the
    ``Base.metadata.create_all`` call below would silently skip a
    table whose model module hasn't been imported yet, and the
    seed helpers would crash on a missing-table SQL error mid-test.
    """
    import importlib
    import pkgutil

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


@pytest.fixture(autouse=True)
def _redirect_default_uow_to_test_engine(
    factory: sessionmaker[Session],
) -> Iterator[None]:
    """Redirect ``make_uow`` to the per-test engine.

    Same rationale as ``tests.unit.api.v1.identity.conftest``: the
    approvals router itself does not call :func:`make_uow` (the API
    seam holds its own session via the ``db_session`` dep override),
    but the audit-writer and any future seam that opens its own UoW
    would otherwise hit whatever DB the default factory was last
    built for.
    """
    import app.adapters.db.session as _session_mod

    bound_engine = factory.kw.get("bind")
    assert isinstance(bound_engine, Engine), (
        "approvals conftest factory must be sessionmaker-bound to an Engine; "
        f"got {bound_engine!r}"
    )
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = bound_engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CapturedReplay:
    """One :meth:`FakeToolDispatcher.dispatch` invocation."""

    call: ToolCall
    headers: Mapping[str, str]
    token: DelegatedToken


@dataclass(slots=True)
class FakeToolDispatcher:
    """Minimal :class:`~app.domain.agent.runtime.ToolDispatcher` stub.

    Approval replay only ever calls ``dispatch`` (the gate decision
    was made and recorded at request time — see the runtime gate
    branch). A canned :class:`ToolResult` queue keyed by tool name
    lets a test pre-load distinct outcomes per fixture; the default
    is a 200 OK echo so the happy path lights up without per-test
    setup.
    """

    responses: dict[str, list[ToolResult]] = field(default_factory=dict)
    captured: list[_CapturedReplay] = field(default_factory=list)
    raise_on_dispatch: BaseException | None = None

    def is_gated(self, call: ToolCall) -> Any:  # pragma: no cover - guard
        # The approval consumer never calls ``is_gated`` — the gate
        # decision is replayed off the recorded row. Failing loudly
        # here catches a future regression that adds an unintended
        # call site without a silent stub.
        raise AssertionError("is_gated must not be called during replay")

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        if self.raise_on_dispatch is not None:
            raise self.raise_on_dispatch
        self.captured.append(
            _CapturedReplay(call=call, headers=dict(headers), token=token)
        )
        bucket = self.responses.get(call.name)
        if not bucket:
            return ToolResult(
                call_id=call.id,
                status_code=200,
                body={"echo": dict(call.input)},
                mutated=True,
            )
        return bucket.pop(0)


# ---------------------------------------------------------------------------
# Persona seeding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Persona:
    """Bundle of ``(ctx, factory, workspace_id, owner_id)``.

    Returned from :func:`owner_ctx` so the test code reads as
    ``persona.ctx`` / ``persona.workspace_id`` rather than tuple-
    unpacking four values at every call site.
    """

    ctx: WorkspaceContext
    factory: sessionmaker[Session]
    workspace_id: str
    owner_id: str


@pytest.fixture
def owner_ctx(factory: sessionmaker[Session]) -> _Persona:
    """Seed a workspace + owner user; return the persona bundle.

    The owner is wired with ``actor_was_owner_member=True`` and
    ``actor_grant_role='manager'`` so the approvals router (which
    has no role gate of its own — §11 "Approval pipeline" treats any
    workspace member with a session as a valid decider) accepts the
    caller as a session-class credential.
    """
    with factory() as s:
        owner_user = bootstrap_user(s, email="owner@example.com", display_name="Owner")
        ws = bootstrap_workspace(
            s,
            slug="ws-approvals",
            name="Approvals WS",
            owner_user_id=owner_user.id,
        )
        s.commit()
        owner_id = owner_user.id
        ws_id, ws_slug = ws.id, ws.slug
    ctx = build_workspace_context(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=owner_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    return _Persona(ctx=ctx, factory=factory, workspace_id=ws_id, owner_id=owner_id)


# ---------------------------------------------------------------------------
# Row seeders
# ---------------------------------------------------------------------------


def seed_pending(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    requester_actor_id: str,
    for_user_id: str | None = None,
    tool_name: str = "tasks.complete",
    tool_input: Mapping[str, object] | None = None,
    inline_channel: str = "web_owner_sidebar",
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
) -> str:
    """Insert one ``status='pending'`` :class:`ApprovalRequest`.

    Mirrors the runtime's ``_write_approval_request`` shape so the
    consumer reads land on the same key set the production gate
    writer produces. Returns the row id.

    The default ``expires_at = created_at + APPROVAL_REQUEST_TTL``
    follows §11 "TTL"; tests that drive the auto-expiry path pass an
    explicit value past ``now()`` to land on the predicate.
    """
    row_id = new_ulid()
    now = created_at or _PINNED
    row = ApprovalRequest(
        id=row_id,
        workspace_id=workspace_id,
        requester_actor_id=requester_actor_id,
        action_json={
            "tool_name": tool_name,
            "tool_call_id": f"tcall-{row_id[-8:].lower()}",
            "tool_input": dict(tool_input or {"task_id": "tsk_42"}),
            "card_summary": f"call {tool_name}",
            "card_risk": "low",
            "pre_approval_source": "manual",
            "agent_correlation_id": new_ulid(),
        },
        status="pending",
        decided_by=None,
        decided_at=None,
        rationale_md=None,
        decision_note_md=None,
        result_json=None,
        expires_at=(
            expires_at if expires_at is not None else now + APPROVAL_REQUEST_TTL
        ),
        inline_channel=inline_channel,
        for_user_id=for_user_id,
        resolved_user_mode=None,
        created_at=now,
    )
    with factory() as s, tenant_agnostic():
        s.add(row)
        s.commit()
    return row_id


def seed_api_token(
    factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str | None,
    kind: str,
    subject_user_id: str | None = None,
    delegate_for_user_id: str | None = None,
    scope_json: Mapping[str, object] | None = None,
    label: str = "test-token",
) -> str:
    """Insert one :class:`ApiToken` row of the requested kind.

    Returns the token id. The check-constraint (``ck_api_token_kind_shape``)
    requires:

    * ``scoped`` — workspace_id non-null, both ``subject_user_id`` AND
      ``delegate_for_user_id`` stay NULL.
    * ``delegated`` — workspace_id non-null, ``delegate_for_user_id``
      non-null, ``subject_user_id`` NULL.
    * ``personal`` — workspace_id NULL, ``subject_user_id`` non-null,
      ``delegate_for_user_id`` NULL.

    The seeder enforces the same rules so a misshapen call surfaces
    at the helper-level (rather than producing an opaque CHECK
    violation deep inside SQLAlchemy's flush).

    ``user_id`` is the creating user — the table's NOT NULL ``user_id``
    column carries it on every kind. For ``delegated`` / ``personal``
    rows it should match :attr:`delegate_for_user_id` /
    :attr:`subject_user_id` respectively (the production
    :func:`app.auth.tokens.mint` pins the same shape).
    """
    if kind not in ("scoped", "delegated", "personal"):
        raise ValueError(f"unknown api_token kind {kind!r}")
    if kind == "delegated":
        if delegate_for_user_id is None:
            raise ValueError("delegated tokens must carry delegate_for_user_id")
        if workspace_id is None:
            raise ValueError("delegated tokens must carry workspace_id")
    if kind == "personal":
        if subject_user_id is None:
            raise ValueError("personal tokens must carry subject_user_id")
        if workspace_id is not None:
            raise ValueError("personal tokens must not carry workspace_id")
    if kind == "scoped":
        if workspace_id is None:
            raise ValueError("scoped tokens must carry workspace_id")
        if subject_user_id is not None or delegate_for_user_id is not None:
            raise ValueError("scoped tokens must not carry subject/delegate user ids")

    token_id = new_ulid()
    row = ApiToken(
        id=token_id,
        user_id=user_id,
        workspace_id=workspace_id,
        kind=kind,
        subject_user_id=subject_user_id,
        delegate_for_user_id=delegate_for_user_id,
        label=label,
        scope_json=dict(scope_json) if scope_json is not None else {},
        prefix=token_id[:8].lower(),
        hash=f"hash-{token_id.lower()}",
        created_at=_PINNED,
        expires_at=_PINNED + timedelta(days=30),
        last_used_at=None,
        revoked_at=None,
    )
    with factory() as s, tenant_agnostic():
        s.add(row)
        s.commit()
    return token_id


# ---------------------------------------------------------------------------
# Client builder
# ---------------------------------------------------------------------------


def build_client(
    persona: _Persona,
    *,
    actor_identity: ActorIdentity | None = None,
    dispatcher: FakeToolDispatcher | None = None,
) -> TestClient:
    """Assemble a TestClient mounting the approvals router at root.

    The router's production mount path is
    ``/w/{slug}/api/v1/approvals``; the unit harness omits the
    workspace prefix because the ``current_workspace_context`` dep
    is overridden — the slug lookup never runs. Tests POST/GET
    against bare paths like ``/`` and ``/{id}/approve``.

    Parameters
    ----------
    persona:
        The seeded workspace + owner. Drives the
        :class:`WorkspaceContext` override.
    actor_identity:
        Optional :class:`ActorIdentity` to stamp on every inbound
        request (mimics what
        :class:`app.tenancy.middleware.TenancyMiddleware` does in
        production). ``None`` exercises the test-convenience session
        path: :func:`_actor_identity` returns ``None``, which the
        auth-gating dep treats as session-class.
    dispatcher:
        Optional :class:`FakeToolDispatcher`. Defaults to a fresh
        one so each test gets isolated capture state. Override only
        when a test needs to pre-load a queue or a raise.
    """
    app = FastAPI()
    add_exception_handlers(app)
    # Mount under ``/approvals`` so the router's ``@router.get("")``
    # has a non-empty parent path (FastAPI rejects ``include_router``
    # with no prefix when any route declares an empty path). The
    # production factory mounts at ``/w/{slug}/api/v1/approvals``;
    # the unit harness shortens to ``/approvals`` because the
    # ``current_workspace_context`` dep is overridden — the slug
    # lookup never runs, so the prefix is cosmetic.
    app.include_router(approvals_router, prefix="/approvals")

    def _override_ctx() -> WorkspaceContext:
        return persona.ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=persona.factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    eff_dispatcher = dispatcher if dispatcher is not None else FakeToolDispatcher()
    app.state.tool_dispatcher = eff_dispatcher

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db

    if actor_identity is not None:
        # Stamp the identity on every request via a thin middleware-
        # like callback. We can't reach for the production
        # :class:`TenancyMiddleware` here — that drives off cookies +
        # bearer tokens we don't issue in unit tests. The simplest
        # production-faithful seam is a Starlette http middleware
        # that copies the pinned identity onto ``request.state``.

        @app.middleware("http")
        async def _stamp_actor(request: Request, call_next):  # type: ignore[no-untyped-def]
            setattr(request.state, ACTOR_STATE_ATTR, actor_identity)
            return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Convenience constructors for ActorIdentity flavours
# ---------------------------------------------------------------------------


def session_identity(*, user_id: str) -> ActorIdentity:
    """Return an :class:`ActorIdentity` shaped like a session cookie."""
    return ActorIdentity(
        user_id=user_id,
        kind="user",
        workspace_id=None,
        token_id=None,
        session_id=new_ulid(),
    )


def token_identity(*, user_id: str, workspace_id: str, token_id: str) -> ActorIdentity:
    """Return an :class:`ActorIdentity` shaped like a bearer-token caller."""
    return ActorIdentity(
        user_id=user_id,
        kind="user",
        workspace_id=workspace_id,
        token_id=token_id,
        session_id=None,
    )


# ---------------------------------------------------------------------------
# Re-exports for tests
# ---------------------------------------------------------------------------


__all__ = [
    "APPROVALS_ACT_SCOPE",
    "_PINNED",
    "FakeToolDispatcher",
    "_Persona",
    "build_client",
    "seed_api_token",
    "seed_pending",
    "session_identity",
    "token_identity",
]


# Mypy hint: silence the "unused import" complaint for re-exports
# the test modules will reach for via the symbol re-export pattern.
_RE_EXPORT_KEEPALIVE: tuple[object, ...] = (
    Callable,
    get_tool_dispatcher,
    Any,
)
