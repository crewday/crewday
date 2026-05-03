"""Unit tests for :mod:`app.domain.llm.client` (cd-weue).

The wrapper composes router + adapter + budget + recorder into a
single async :meth:`LLMClient.chat` surface; these tests pin the
four §11 "Retryable errors" cases:

* Happy path — first-rung success writes one ``status="ok"`` row,
  returns an :class:`LLMResult` with ``fallback_attempts=0``.
* Retryable-then-success — primary rung raises a transport error,
  secondary rung succeeds. Two ``llm_usage`` rows; the success row
  carries ``fallback_attempts=1``.
* Terminal error — non-retryable :class:`LlmProviderError`
  propagates immediately; only the failing rung's row lands.
* Budget-refused — :func:`check_budget` raises
  :class:`BudgetExceeded` before the first dispatch; no usage row
  is written, the exception propagates verbatim.

The tests use a fresh in-memory SQLite engine per case (not the
shared integration harness) — the wrapper has no migration-shape
dependency beyond the LLM ORM tables, and a per-test engine keeps
the unit suite under the few-minute target the
``docs/specs/17-testing-quality.md`` "Unit" budget pins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.llm.models import (
    BudgetLedger,
    LlmAssignment,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
)
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.adapters.llm.openrouter import (
    LlmContentRefused,
    LlmProviderError,
    LlmRateLimited,
    LlmTransportError,
)
from app.adapters.llm.ports import (
    ChatMessage,
    LLMResponse,
    LLMUsage,
    Tool,
    ToolCall,
)
from app.domain.llm.budget import (
    WINDOW_DAYS,
    BudgetExceeded,
    PricingTable,
)
from app.domain.llm.client import (
    LLMChainExhausted,
    LLMClient,
    LLMResult,
    is_retryable_error,
)
from app.domain.llm.router import CapabilityUnassignedError
from app.domain.llm.usage_recorder import AgentAttribution
from app.tenancy import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.redact import ConsentSet
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_CAPABILITY = "chat.manager"


# ---------------------------------------------------------------------------
# In-memory engine + session — fresh per test
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` package.

    ``Base.metadata.create_all`` only knows about modules already
    imported; the LLM ORM rows live alongside many siblings whose
    foreign keys must resolve at create-table time.
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
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


@pytest.fixture(autouse=True)
def _reset_tenancy() -> Iterator[None]:
    """Every test starts without an active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


# ---------------------------------------------------------------------------
# Seed helpers — terse so tests can focus on behaviour
# ---------------------------------------------------------------------------


def _seed_workspace(session: Session, *, slug: str = "ws-client") -> Workspace:
    ws = Workspace(
        id=new_ulid(),
        slug=slug,
        name=f"Workspace {slug}",
        plan="free",
        quota_json={},
        settings_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


def _seed_provider_model(
    session: Session,
    *,
    api_model_id: str,
) -> LlmProviderModel:
    provider = LlmProvider(
        id=new_ulid(),
        name=f"prov-{api_model_id}",
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
        canonical_name=f"canonical/{api_model_id}",
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
    pm = LlmProviderModel(
        id=new_ulid(),
        provider_id=provider.id,
        model_id=model.id,
        api_model_id=api_model_id,
        supports_system_prompt=True,
        supports_temperature=True,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(pm)
    session.flush()
    return pm


def _seed_assignment(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
    api_model_id: str,
    priority: int = 0,
) -> LlmAssignment:
    pm = _seed_provider_model(session, api_model_id=api_model_id)
    row = LlmAssignment(
        id=new_ulid(),
        workspace_id=workspace_id,
        capability=capability,
        model_id=pm.id,
        provider="openrouter",
        priority=priority,
        enabled=True,
        max_tokens=None,
        temperature=None,
        extra_api_params={},
        required_capabilities=[],
        created_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def _seed_ledger(
    session: Session,
    *,
    workspace_id: str,
    cap_cents: int,
    spent_cents: int = 0,
) -> BudgetLedger:
    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=_PINNED - timedelta(days=WINDOW_DAYS),
        period_end=_PINNED,
        spent_cents=spent_cents,
        cap_cents=cap_cents,
        updated_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def _build_context(workspace_id: str, *, slug: str = "ws-client") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def _attribution() -> AgentAttribution:
    return AgentAttribution(
        actor_user_id=new_ulid(),
        token_id=None,
        agent_label="manager-chat",
    )


def _fetch_rows(session: Session, *, workspace_id: str) -> list[LlmUsageRow]:
    return list(
        session.execute(
            select(LlmUsageRow)
            .where(LlmUsageRow.workspace_id == workspace_id)
            .order_by(LlmUsageRow.attempt.asc())
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Stub adapter — scripted responses / exceptions per call
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Scripted :class:`LLMClient` adapter.

    ``script`` is a list of ``LLMResponse`` or ``Exception`` items;
    each :meth:`chat` call pops the head and either returns it or
    raises. Exhausting the script raises ``AssertionError`` so a
    test that under-provisions the script fails loudly rather than
    looping forever.
    """

    def __init__(self, script: list[LLMResponse | Exception]) -> None:
        self._script: list[LLMResponse | Exception] = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "model_id": model_id,
                "messages": list(messages),
                "max_tokens": max_tokens,
                "temperature": temperature,
                "tools": list(tools) if tools else None,
                "consents": consents,
            }
        )
        if not self._script:
            raise AssertionError(
                "stub adapter script exhausted; test under-provisioned the script"
            )
        head = self._script.pop(0)
        if isinstance(head, Exception):
            raise head
        return head

    # Unused — present only so the stub satisfies the
    # :class:`~app.adapters.llm.ports.LLMClient` protocol surface.
    def complete(self, **kwargs: object) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    def ocr(self, **kwargs: object) -> str:  # pragma: no cover
        raise NotImplementedError

    def stream_chat(self, **kwargs: object):  # pragma: no cover
        raise NotImplementedError


def _ok_response(text: str = "ok", finish_reason: str = "stop") -> LLMResponse:
    return LLMResponse(
        text=text,
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model_id="stub/model",
        finish_reason=finish_reason,
        tool_calls=(),
    )


# ---------------------------------------------------------------------------
# Pricing — empty so estimate_cost_cents stays at zero
# ---------------------------------------------------------------------------
#
# Free pricing keeps the budget pre-flight trivially passable;
# success rows record ``cost_cents=0`` so the ledger doesn't move
# unless a test explicitly seeds a non-zero pricing table.

_FREE_PRICING: PricingTable = {}


def _run(coro: Any) -> Any:
    """Drive an async coroutine to completion in a sync test body.

    The wrapper's :meth:`chat` is async; using :func:`asyncio.run`
    keeps the test bodies plain ``def`` (no ``pytest-asyncio`` plugin
    requirement) which matches the project convention for the rare
    async surfaces that have unit coverage.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Happy path — first-rung success
# ---------------------------------------------------------------------------


class TestHappyPath:
    """First-rung success: one ``status="ok"`` row, ``fallback_attempts=0``."""

    def test_returns_result_and_writes_one_ok_row(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter([_ok_response("hello world")])
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            result = _run(
                client.chat(
                    session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "hi"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    clock=clock,
                )
            )
            session.flush()
        finally:
            reset_current(token)

        assert isinstance(result, LLMResult)
        assert result.text == "hello world"
        assert result.fallback_attempts == 0
        assert result.model_used == "primary/model"
        assert result.correlation_id == ctx.audit_correlation_id
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 5
        assert result.finish_reason == "stop"

        rows = _fetch_rows(session, workspace_id=ws.id)
        assert len(rows) == 1
        assert rows[0].status == "ok"
        assert rows[0].fallback_attempts == 0
        assert rows[0].finish_reason == "stop"
        assert rows[0].correlation_id == ctx.audit_correlation_id

        # The adapter saw exactly one call with our wire-form model id.
        assert len(adapter.calls) == 1
        assert adapter.calls[0]["model_id"] == "primary/model"

    def test_passes_tool_calls_through_on_success(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """Tool-calls returned by the adapter survive onto the LLMResult."""
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        tool_call = ToolCall(id="t1", name="lookup", arguments={"q": "x"})
        adapter = _StubAdapter(
            [
                LLMResponse(
                    text="",
                    usage=LLMUsage(
                        prompt_tokens=1, completion_tokens=1, total_tokens=2
                    ),
                    model_id="stub/model",
                    finish_reason="tool_calls",
                    tool_calls=(tool_call,),
                )
            ]
        )
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            result = _run(
                client.chat(
                    session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "use a tool"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    clock=clock,
                )
            )
        finally:
            reset_current(token)

        assert result.tool_calls == (tool_call,)
        assert result.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# 2. Retryable-then-success — primary fails, secondary succeeds
# ---------------------------------------------------------------------------


class TestRetryableThenSuccess:
    """Primary rung raises a transport error; secondary rung succeeds."""

    def test_walks_to_next_rung_and_records_both_attempts(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="secondary/model",
            priority=1,
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter(
            [
                LlmTransportError("boom"),
                _ok_response("recovered"),
            ]
        )
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            result = _run(
                client.chat(
                    session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "hi"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    clock=clock,
                )
            )
            session.flush()
        finally:
            reset_current(token)

        assert result.text == "recovered"
        assert result.fallback_attempts == 1
        assert result.model_used == "secondary/model"

        rows = _fetch_rows(session, workspace_id=ws.id)
        assert len(rows) == 2
        # Attempt 0 — primary, error, zero cost.
        assert rows[0].status == "error"
        assert rows[0].cost_cents == 0
        assert rows[0].fallback_attempts == 0
        # Attempt 1 — secondary, ok.
        assert rows[1].status == "ok"
        assert rows[1].fallback_attempts == 1

        # Both rows share the request-scoped correlation id.
        assert rows[0].correlation_id == ctx.audit_correlation_id
        assert rows[1].correlation_id == ctx.audit_correlation_id

    def test_rate_limit_advances_chain_too(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """``LlmRateLimited`` is retryable per §11."""
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="secondary/model",
            priority=1,
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter(
            [
                LlmRateLimited("slow down"),
                _ok_response(),
            ]
        )
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            result = _run(
                client.chat(
                    session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "hi"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    clock=clock,
                )
            )
        finally:
            reset_current(token)

        assert result.fallback_attempts == 1
        assert result.model_used == "secondary/model"

    def test_content_refusal_advances_chain(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """``finish_reason="safety"`` is retryable per §11.

        The first rung returns a 200 body but the model refused on
        content grounds; the wrapper logs an error row and walks to
        the next rung exactly like a transport failure.
        """
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="secondary/model",
            priority=1,
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter(
            [
                _ok_response("refused", finish_reason="safety"),
                _ok_response("clean"),
            ]
        )
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            result = _run(
                client.chat(
                    session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "hi"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    clock=clock,
                )
            )
            session.flush()
        finally:
            reset_current(token)

        assert result.text == "clean"
        assert result.fallback_attempts == 1

        rows = _fetch_rows(session, workspace_id=ws.id)
        # Refused rung records as ``error``; finish_reason carried
        # for the /admin/usage diagnostic surface.
        assert rows[0].status == "error"
        assert rows[0].finish_reason == "safety"
        assert rows[0].cost_cents == 0


# ---------------------------------------------------------------------------
# 3. Terminal error — non-retryable 4xx propagates immediately
# ---------------------------------------------------------------------------


class TestTerminalError:
    """``LlmProviderError`` (4xx) raises through; chain stops at the rung."""

    def test_provider_error_raises_immediately_and_records_one_row(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        # Secondary rung exists; the wrapper must NOT walk to it on
        # a non-retryable 4xx — rerunning the same request without
        # editing the payload will hit the same wall.
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="secondary/model",
            priority=1,
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter([LlmProviderError("400 bad request")])
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            with pytest.raises(LlmProviderError, match="400 bad request"):
                _run(
                    client.chat(
                        session,
                        ctx,
                        capability=_CAPABILITY,
                        messages=[{"role": "user", "content": "hi"}],
                        attribution=_attribution(),
                        consents=ConsentSet.none(),
                        clock=clock,
                    )
                )
            session.flush()
        finally:
            reset_current(token)

        rows = _fetch_rows(session, workspace_id=ws.id)
        # One row for the failing rung; the secondary rung stayed
        # untouched.
        assert len(rows) == 1
        assert rows[0].status == "error"
        assert rows[0].cost_cents == 0
        assert rows[0].fallback_attempts == 0
        # Secondary adapter call should not have happened.
        assert len(adapter.calls) == 1


# ---------------------------------------------------------------------------
# 4. Budget-refused — propagates pre-flight, no usage row
# ---------------------------------------------------------------------------


class TestBudgetRefused:
    """``BudgetExceeded`` raises before any rung dispatches; no rows land."""

    def test_budget_exceeded_propagates_with_no_usage_row(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        # Pricing high enough that the projected cost overflows the cap.
        # ``estimate_cost_cents`` floor-divides, so the values picked
        # below are deliberately above the seeded ``cap_cents=1``.
        pricing: PricingTable = {"primary/model": (10_000_000, 10_000_000)}
        _seed_ledger(session, workspace_id=ws.id, cap_cents=1, spent_cents=0)

        adapter = _StubAdapter([])  # Must NOT be called.
        client = LLMClient(adapter, pricing=pricing)

        token = set_current(ctx)
        try:
            with pytest.raises(BudgetExceeded):
                _run(
                    client.chat(
                        session,
                        ctx,
                        capability=_CAPABILITY,
                        messages=[{"role": "user", "content": "hi"}],
                        attribution=_attribution(),
                        consents=ConsentSet.none(),
                        clock=clock,
                    )
                )
        finally:
            reset_current(token)

        rows = _fetch_rows(session, workspace_id=ws.id)
        assert rows == []
        # The adapter never saw a request.
        assert adapter.calls == []


# ---------------------------------------------------------------------------
# 5. Capability unassigned — empty chain raises before any work
# ---------------------------------------------------------------------------


class TestCapabilityUnassigned:
    """An unassigned capability raises :class:`CapabilityUnassignedError`.

    The wrapper does not invent an empty ``LLMResult`` — the API
    layer is responsible for mapping the exception to the
    ``503 capability_unassigned`` response per §11 "Failure modes".
    """

    def test_empty_chain_raises_capability_unassigned(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)
        # No assignment seeded.

        adapter = _StubAdapter([])
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            with pytest.raises(CapabilityUnassignedError):
                _run(
                    client.chat(
                        session,
                        ctx,
                        capability=_CAPABILITY,
                        messages=[{"role": "user", "content": "hi"}],
                        attribution=_attribution(),
                        consents=ConsentSet.none(),
                        clock=clock,
                    )
                )
        finally:
            reset_current(token)

        rows = _fetch_rows(session, workspace_id=ws.id)
        assert rows == []


# ---------------------------------------------------------------------------
# Helper: error classifier
# ---------------------------------------------------------------------------


class TestIsRetryableError:
    """Pin the closed set of retryable adapter exceptions per §11."""

    def test_transport_error_is_retryable(self) -> None:
        assert is_retryable_error(LlmTransportError("transport"))

    def test_rate_limited_is_retryable(self) -> None:
        assert is_retryable_error(LlmRateLimited("429"))

    def test_provider_error_is_terminal(self) -> None:
        # Non-retryable 4xx; the chain MUST stop.
        assert not is_retryable_error(LlmProviderError("400"))

    def test_budget_exceeded_is_terminal(self) -> None:
        # The whole workspace is paused; no rung will help.
        assert not is_retryable_error(
            BudgetExceeded(capability=_CAPABILITY, workspace_id="ws")
        )

    def test_capability_unassigned_is_terminal(self) -> None:
        assert not is_retryable_error(CapabilityUnassignedError(_CAPABILITY, "ws"))


# ---------------------------------------------------------------------------
# 6. Chain-exhausted — every rung failed retryably
# ---------------------------------------------------------------------------


class TestChainExhausted:
    """When every rung fails retryably the wrapper raises
    :class:`LLMChainExhausted` carrying ``fallback_attempts`` and
    ``correlation_id`` so the API layer can echo the
    ``X-LLM-Fallback-Attempts`` / ``X-Correlation-Id-Echo`` headers
    on the failure path (§11 "Failure modes").
    """

    def test_all_rungs_transport_error_raises_chain_exhausted(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="secondary/model",
            priority=1,
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter(
            [
                LlmTransportError("first boom"),
                LlmTransportError("second boom"),
            ]
        )
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            with pytest.raises(LLMChainExhausted) as exc_info:
                _run(
                    client.chat(
                        session,
                        ctx,
                        capability=_CAPABILITY,
                        messages=[{"role": "user", "content": "hi"}],
                        attribution=_attribution(),
                        consents=ConsentSet.none(),
                        clock=clock,
                    )
                )
            session.flush()
        finally:
            reset_current(token)

        # The wrapper exposes both fields the API needs to fill the
        # response headers without re-querying the recorder rows.
        assert exc_info.value.fallback_attempts == 1
        assert exc_info.value.correlation_id == ctx.audit_correlation_id
        # Underlying transport error preserved both as attribute and
        # as the exception cause (raise … from last_error).
        assert isinstance(exc_info.value.last_error, LlmTransportError)
        assert "second boom" in str(exc_info.value.last_error)
        assert exc_info.value.__cause__ is exc_info.value.last_error

        # Both rungs left telemetry rows behind.
        rows = _fetch_rows(session, workspace_id=ws.id)
        assert len(rows) == 2
        assert all(r.status == "error" for r in rows)


# ---------------------------------------------------------------------------
# 7. Chain-exhausted refusal — every rung refused on safety
# ---------------------------------------------------------------------------


class TestChainExhaustedRefusal:
    """Every rung returns a content-refusal ``finish_reason``."""

    def test_full_chain_refusal_raises_llm_content_refused(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="secondary/model",
            priority=1,
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter(
            [
                _ok_response("refused-1", finish_reason="safety"),
                _ok_response("refused-2", finish_reason="content_filter"),
            ]
        )
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            with pytest.raises(LlmContentRefused) as exc_info:
                _run(
                    client.chat(
                        session,
                        ctx,
                        capability=_CAPABILITY,
                        messages=[{"role": "user", "content": "hi"}],
                        attribution=_attribution(),
                        consents=ConsentSet.none(),
                        clock=clock,
                    )
                )
            session.flush()
        finally:
            reset_current(token)

        # The exception carries the last refusal's finish_reason in
        # the message so /admin/usage operators can pattern-match
        # without re-querying.
        assert "content_filter" in str(exc_info.value)

        # The chain-exhausted refusal also carries
        # ``fallback_attempts`` and ``correlation_id`` so the API
        # layer can fill the ``X-LLM-Fallback-Attempts`` /
        # ``X-Correlation-Id-Echo`` headers regardless of which
        # exception subtype it caught (§11 "Failure modes" — same
        # echo contract as the transport-failure path).
        assert exc_info.value.fallback_attempts == 1
        assert exc_info.value.correlation_id == ctx.audit_correlation_id

        # Both refused rungs landed error rows with the verbatim
        # finish_reason for diagnostic surfacing.
        rows = _fetch_rows(session, workspace_id=ws.id)
        assert len(rows) == 2
        assert rows[0].finish_reason == "safety"
        assert rows[1].finish_reason == "content_filter"
        assert all(r.status == "error" for r in rows)


# ---------------------------------------------------------------------------
# 8. attempt_offset — multiple wrapper calls in one request
# ---------------------------------------------------------------------------


class TestAttemptOffset:
    """A single request can make ≥2 ``client.chat`` calls under one
    ``correlation_id``; ``attempt_offset`` keeps each call's rung
    rows on distinct ``attempt`` slots so the recorder's idempotency
    unique on ``(workspace_id, correlation_id, attempt)`` does not
    silently swallow the second call (§11 "Cost tracking" /
    cd-irng idempotency contract).
    """

    def test_back_to_back_calls_with_offset_land_distinct_rows(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter(
            [
                _ok_response("first call"),
                _ok_response("second call"),
            ]
        )
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            # First call consumes attempt slots [0, len(chain) - 1].
            first = _run(
                client.chat(
                    session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "first"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    attempt_offset=0,
                    clock=clock,
                )
            )
            session.flush()
            # Second call: caller bumps offset past the first call's
            # rung count so the (correlation_id, attempt) pair is
            # fresh.
            second = _run(
                client.chat(
                    session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "second"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    attempt_offset=1,
                    clock=clock,
                )
            )
            session.flush()
        finally:
            reset_current(token)

        # Both successes carry ``fallback_attempts=0`` (per call
        # this is the rung index, not the cross-call offset) — the
        # public LLMResult contract is unchanged.
        assert first.fallback_attempts == 0
        assert second.fallback_attempts == 0

        rows = _fetch_rows(session, workspace_id=ws.id)
        # Two ``status="ok"`` rows, distinct ``attempt`` indices.
        # If the recorder had silently deduped (the bug this
        # fix targets), there would be only one row.
        assert len(rows) == 2
        attempts = sorted(r.attempt for r in rows)
        assert attempts == [0, 1]
        # Both rows share the request-scoped correlation_id.
        assert {r.correlation_id for r in rows} == {ctx.audit_correlation_id}

    def test_default_offset_zero_keeps_existing_callers_unchanged(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """The single-call scenario must keep ``attempt=0`` semantics."""
        ws = _seed_workspace(session)
        ctx = _build_context(ws.id, slug=ws.slug)
        _seed_assignment(
            session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        _seed_ledger(session, workspace_id=ws.id, cap_cents=500)

        adapter = _StubAdapter([_ok_response("only call")])
        client = LLMClient(adapter, pricing=_FREE_PRICING)

        token = set_current(ctx)
        try:
            _run(
                client.chat(
                    session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "hi"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    clock=clock,
                )
            )
            session.flush()
        finally:
            reset_current(token)

        rows = _fetch_rows(session, workspace_id=ws.id)
        assert len(rows) == 1
        assert rows[0].attempt == 0
