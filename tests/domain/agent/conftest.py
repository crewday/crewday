"""Fixtures for :mod:`app.domain.agent.runtime` tests (cd-nyvm).

Re-exports the integration-layer engine + migration harness so the
``CREWDAY_TEST_DB={sqlite,postgres}`` shard selector reaches this
package; mirrors ``tests/domain/llm/conftest.py``.

Provides:

* a tenant-filtered session against the migrated schema;
* a frozen clock pinned to a deterministic instant;
* a fresh in-process :class:`EventBus` per test;
* a fake :class:`ToolDispatcher` + :class:`TokenFactory` so the
  runtime tests never reach over the network or hash an argon2id
  password (the slowest path in the suite).

See ``docs/specs/11-llm-and-agents.md`` §"Embedded agents",
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.workspace.models import Workspace
from app.adapters.llm.ports import ChatMessage, LLMResponse, LLMUsage
from app.domain.agent.runtime import (
    DelegatedToken,
    GateDecision,
    ToolCall,
    ToolResult,
)
from app.domain.llm import router as router_module
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.registry import Event
from app.tenancy import WorkspaceContext, registry, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

# Re-export integration-layer fixtures so the shared engine /
# migrate_once / db_url machinery reaches this package.
from tests.integration.conftest import (
    db_url as _db_url_fixture,
)
from tests.integration.conftest import (
    engine as _engine_fixture,
)
from tests.integration.conftest import (
    migrate_once as _migrate_once_fixture,
)
from tests.integration.conftest import (
    pytest_collection_modifyitems as pytest_collection_modifyitems,  # re-export
)

db_url = _db_url_fixture
engine = _engine_fixture
migrate_once = _migrate_once_fixture


# Pinned wall clock so ULID + timing assertions stay deterministic.
_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


# Same registry-repair pattern as ``tests/domain/llm/conftest.py``:
# the unit suite's autouse fixture wipes the process-wide tenancy
# registry between modules, which silently drops the tenant filter
# off LLM / messaging queries when the full suite runs.
_REGISTERED_TABLES: tuple[str, ...] = (
    "model_assignment",
    "llm_capability_inheritance",
    "llm_usage",
    "budget_ledger",
    "audit_log",
    "approval_request",
    "agent_token",
    "chat_channel",
    "chat_message",
    "agent_preference",
    "agent_preference_revision",
)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    for table in _REGISTERED_TABLES:
        registry.register(table)


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """Function-scoped tenant-filtered session with SAVEPOINT rollback.

    Mirrors the LLM-suite fixture: outer transaction +
    ``join_transaction_mode='create_savepoint'`` so the runtime
    under test can flush freely without leaking state across cases.
    """
    with engine.connect() as raw_connection:
        outer = raw_connection.begin()
        factory = sessionmaker(
            bind=raw_connection,
            expire_on_commit=False,
            class_=Session,
            join_transaction_mode="create_savepoint",
        )
        install_tenant_filter(factory)
        session = factory()
        try:
            yield session
        finally:
            session.close()
            if outer.is_active:
                outer.rollback()


@pytest.fixture
def clock() -> FrozenClock:
    """Frozen clock pinned to :data:`_PINNED`; tests advance by hand."""
    return FrozenClock(_PINNED)


@pytest.fixture
def bus() -> EventBus:
    """Fresh in-process bus per test.

    Tests assert against captured events without router invalidation
    crosstalk; the production singleton is left to its own
    subscriptions and is reset by :func:`_reset_router_state`.
    """
    return EventBus()


@pytest.fixture(autouse=True)
def _reset_router_state() -> Iterator[None]:
    """Drop the router cache + production-bus subscriptions between cases.

    The :mod:`app.domain.llm.router` cache lives at the module level
    and is shared across tests; without this fixture a primed
    capability resolution from one case would silently satisfy
    another's "no assignment" expectation.
    """
    router_module.invalidate_cache()
    router_module._reset_subscriptions_for_tests()
    default_event_bus._reset_for_tests()
    router_module._subscribe_to_bus(default_event_bus)
    try:
        yield
    finally:
        router_module.invalidate_cache()
        router_module._reset_subscriptions_for_tests()


@pytest.fixture(autouse=True)
def _reset_tenancy_context() -> Iterator[None]:
    """Every test starts without an active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


# ---------------------------------------------------------------------------
# Helpers — concise row factories
# ---------------------------------------------------------------------------


def build_context(
    workspace_id: str,
    *,
    slug: str = "ws-test",
    actor_id: str | None = None,
) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` for a manager-shaped agent."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id or new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def seed_user(session: Session) -> str:
    """Insert a :class:`User` row tenancy-agnostic and return its id.

    Several tests need a real user id to satisfy
    ``chat_message.author_user_id`` and ``approval_request.
    requester_actor_id`` foreign keys; this helper keeps the
    boilerplate at one site.
    """
    from app.adapters.db.identity.models import User

    user_id = new_ulid()
    # Use the **trailing** chars of the ULID to derive the email
    # local-part. Under :class:`FrozenClock` the leading bytes encode
    # the (pinned) timestamp, so two ``seed_user`` calls inside one
    # test would share the leading slice and collide on the
    # ``user.email_lower`` UNIQUE constraint. The trailing slice is
    # the random/monotonic tail, which stays distinct between calls.
    user = User(
        id=user_id,
        email=f"agent-{user_id.lower()[-12:]}@example.com",
        display_name="Agent User",
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(user)
        session.flush()
    return user_id


def seed_workspace(session: Session, *, slug: str | None = None) -> Workspace:
    """Insert a workspace tenancy-agnostic (bootstrap path)."""
    ws_slug = slug or f"ws-{new_ulid().lower()[:12]}"
    ws = Workspace(
        id=new_ulid(),
        slug=ws_slug,
        name=f"Workspace {ws_slug}",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(ws)
        session.flush()
    return ws


# ---------------------------------------------------------------------------
# Fakes — TokenFactory, ToolDispatcher, LLM client
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeTokenFactory:
    """Returns a deterministic :class:`DelegatedToken` per test.

    Bypasses argon2id (the slowest path in the suite) — the runtime
    only cares that the dispatcher receives a plaintext + id pair;
    it does not verify the token itself.
    """

    plaintext: str = "mip_FAKEKEY_FAKESECRET"
    token_id: str = field(default_factory=new_ulid)
    last_call: tuple[str, datetime] | None = None

    def mint_for(
        self,
        ctx: WorkspaceContext,
        *,
        agent_label: str,
        expires_at: datetime,
    ) -> DelegatedToken:
        self.last_call = (agent_label, expires_at)
        return DelegatedToken(plaintext=self.plaintext, token_id=self.token_id)


@dataclass(slots=True)
class CapturedDispatch:
    """One :meth:`FakeToolDispatcher.dispatch` invocation, captured for assertion."""

    call: ToolCall
    headers: Mapping[str, str]
    token: DelegatedToken


@dataclass(slots=True)
class FakeToolDispatcher:
    """Programmable :class:`ToolDispatcher` for the runtime tests.

    ``responses`` is a queue of canned :class:`ToolResult` instances
    keyed by tool name; the dispatcher pops one per call. ``gates``
    is the gate decision per tool name (default: not gated).
    Tests pre-load the queue and inspect :attr:`captured` after the
    turn returns.
    """

    responses: dict[str, list[ToolResult]] = field(default_factory=dict)
    gates: dict[str, GateDecision] = field(default_factory=dict)
    captured: list[CapturedDispatch] = field(default_factory=list)
    is_gated_calls: list[ToolCall] = field(default_factory=list)

    def is_gated(self, call: ToolCall) -> GateDecision:
        self.is_gated_calls.append(call)
        return self.gates.get(call.name, GateDecision(gated=False))

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        self.captured.append(
            CapturedDispatch(call=call, headers=dict(headers), token=token)
        )
        bucket = self.responses.get(call.name)
        if not bucket:
            # No canned response → safe default: a 200 OK read result
            # so the loop progresses and the test sees the dispatch.
            return ToolResult(
                call_id=call.id,
                status_code=200,
                body={"echo": dict(call.input)},
                mutated=False,
            )
        return bucket.pop(0)


@dataclass(slots=True)
class ScriptedLLMClient:
    """LLM client that walks a pre-canned script of replies.

    Each call to :meth:`chat` pops the next ``LLMResponse`` from
    :attr:`replies` and returns it. Tests pre-load the queue with
    text replies and tool-call invocations to drive each branch of
    the runtime loop.
    """

    replies: list[LLMResponse] = field(default_factory=list)
    chat_calls: int = 0
    last_messages: list[ChatMessage] | None = None

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        raise NotImplementedError("ScriptedLLMClient only supports chat()")

    def chat(
        self,
        *,
        model_id: str,
        messages,  # type: ignore[no-untyped-def]
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.chat_calls += 1
        self.last_messages = list(messages)
        if not self.replies:
            return LLMResponse(
                text="(no scripted reply)",
                usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                model_id=model_id,
                finish_reason="stop",
            )
        return self.replies.pop(0)

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        raise NotImplementedError("ScriptedLLMClient does not support ocr")

    def stream_chat(
        self,
        *,
        model_id: str,
        messages,  # type: ignore[no-untyped-def]
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ):  # type: ignore[no-untyped-def]
        raise NotImplementedError("ScriptedLLMClient does not support stream_chat")


def make_text_response(text: str, *, model_id: str = "fake/model") -> LLMResponse:
    """Build an :class:`LLMResponse` carrying ``text`` and trivial usage."""
    return LLMResponse(
        text=text,
        usage=LLMUsage(
            prompt_tokens=10,
            completion_tokens=max(1, len(text) // 4),
            total_tokens=10 + max(1, len(text) // 4),
        ),
        model_id=model_id,
        finish_reason="stop",
    )


def make_tool_call_response(
    tool_name: str, tool_input: Mapping[str, object], *, model_id: str = "fake/model"
) -> LLMResponse:
    """Build an :class:`LLMResponse` whose text is a single tool-call block."""
    import json as _json

    payload = _json.dumps(dict(tool_input))
    body = f"<tool_call name=\"{tool_name}\" input='{payload}'/>"
    return LLMResponse(
        text=body,
        usage=LLMUsage(
            prompt_tokens=12,
            completion_tokens=max(1, len(body) // 4),
            total_tokens=12 + max(1, len(body) // 4),
        ),
        model_id=model_id,
        finish_reason="tool_calls",
    )


# ---------------------------------------------------------------------------
# Event capture
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CapturedEvents:
    """Collects every event published on the test bus."""

    bus: EventBus
    events: list[Event] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Subscribe a catch-all by registering to each runtime-emitted
        # event individually — the bus dispatches by event class
        # name, so we wire one shim per concrete subclass.
        from app.events.types import (
            AgentActionPending,
            AgentTurnFinished,
            AgentTurnStarted,
        )

        @self.bus.subscribe(AgentTurnStarted)
        def _on_started(event: AgentTurnStarted) -> None:
            self.events.append(event)

        @self.bus.subscribe(AgentTurnFinished)
        def _on_finished(event: AgentTurnFinished) -> None:
            self.events.append(event)

        @self.bus.subscribe(AgentActionPending)
        def _on_pending(event: AgentActionPending) -> None:
            self.events.append(event)

    def names(self) -> list[str]:
        return [type(e).name for e in self.events]


@pytest.fixture
def captured_events(bus: EventBus) -> CapturedEvents:
    """Subscribe to ``agent.turn.*`` on the test bus and capture deliveries."""
    return CapturedEvents(bus=bus)


# ---------------------------------------------------------------------------
# Channel + budget seeding
# ---------------------------------------------------------------------------


def seed_channel(
    session: Session,
    *,
    workspace_id: str,
    kind: Literal["staff", "manager", "chat_gateway"] = "manager",
    external_ref: str | None = None,
) -> str:
    """Insert a :class:`ChatChannel` and return its id."""
    from app.adapters.db.messaging.models import ChatChannel

    row_id = new_ulid()
    row = ChatChannel(
        id=row_id,
        workspace_id=workspace_id,
        kind=kind,
        source="app",
        external_ref=external_ref,
        title=f"channel-{row_id[-6:].lower()}",
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(row)
        session.flush()
    return row_id


def seed_budget_ledger(
    session: Session,
    *,
    workspace_id: str,
    cap_cents: int = 10_000,
    spent_cents: int = 0,
) -> None:
    """Seed a :class:`BudgetLedger` row for the workspace."""
    from datetime import timedelta

    from app.adapters.db.llm.models import BudgetLedger

    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=_PINNED - timedelta(days=30),
        period_end=_PINNED,
        spent_cents=spent_cents,
        cap_cents=cap_cents,
        updated_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(row)
        session.flush()


def seed_assignment(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
    api_model_id: str = "fake/chat-model",
) -> None:
    """Seed a :class:`ModelAssignment` + the registry trio it needs."""
    from app.adapters.db.llm.models import (
        LlmModel,
        LlmProvider,
        LlmProviderModel,
        ModelAssignment,
    )

    pm_id = new_ulid()
    provider = LlmProvider(
        id=new_ulid(),
        name=f"provider-{pm_id[-6:].lower()}",
        provider_type="fake",
        timeout_s=60,
        requests_per_minute=60,
        priority=0,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    model = LlmModel(
        id=new_ulid(),
        canonical_name=api_model_id,
        display_name=api_model_id,
        vendor="other",
        capabilities=["chat"],
        is_active=True,
        price_source="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add_all([provider, model])
    session.flush()
    provider_model = LlmProviderModel(
        id=pm_id,
        provider_id=provider.id,
        model_id=model.id,
        api_model_id=api_model_id,
        supports_system_prompt=True,
        supports_temperature=True,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(provider_model)
    session.flush()
    row = ModelAssignment(
        id=new_ulid(),
        workspace_id=workspace_id,
        capability=capability,
        model_id=pm_id,
        provider="fake",
        priority=0,
        enabled=True,
        max_tokens=None,
        temperature=None,
        extra_api_params={},
        required_capabilities=[],
        created_at=_PINNED,
    )
    session.add(row)
    session.flush()
