"""End-to-end integration coverage for :mod:`app.agent.dispatcher` (cd-z3b7).

Mounts the real tasks router on a throwaway FastAPI app, builds an
:class:`OpenAPIToolDispatcher` against it, and round-trips:

* a read tool (``list_tasks`` → ``GET /api/v1/tasks``) — asserts
  ``mutated=False`` and the seeded task lands in the response.
* a write tool (``create_task`` → ``POST /api/v1/tasks``) — asserts
  ``mutated=True``, the row landed in the DB.
* the manager-creates-task flow end-to-end through
  :func:`app.domain.agent.runtime.run_turn` + the dispatcher.

The dispatcher's gate decision is exercised against ``llm`` routes
that carry ``x-agent-confirm`` annotations — kept on the unit tier;
this suite focuses on the wire-shape contract that the unit suite
can't observe (real Pydantic validation, tenant filter, audit row).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db import session as db_session_module
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.llm.models import (
    BudgetLedger,
    LlmAssignment,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
)
from app.adapters.db.messaging.models import ChatChannel, ChatMessage
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import FilteredSession, make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import Workspace
from app.adapters.llm.ports import LLMResponse, LLMUsage
from app.agent.dispatcher import make_default_dispatcher
from app.agent.tokens import DelegatedTokenFactory
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.deps import get_llm as _get_llm_dep
from app.api.v1.agent import build_agent_router, get_agent_token_factory
from app.api.v1.tasks import router as tasks_router
from app.auth import tokens as tokens_module
from app.domain.agent.runtime import (
    DelegatedToken,
    ToolCall,
    run_turn,
)
from app.events.bus import EventBus
from app.events.types import AgentMessageAppended, AgentTurnFinished, AgentTurnStarted
from app.tenancy import WorkspaceContext, registry, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _ensure_registered() -> None:
    """Register every table the runtime + dispatcher write to."""
    for table in (
        "llm_assignment",
        "llm_capability_inheritance",
        "llm_usage",
        "budget_ledger",
        "audit_log",
        "approval_request",
        "agent_token",
        "chat_channel",
        "chat_message",
        "occurrence",
        "task_template",
        "property_workspace",
        "role_grant",
        "permission_group",
        "permission_group_member",
    ):
        registry.register(table)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """Tenant-filtered session bound to a per-test SAVEPOINT."""
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


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_workspace_and_user(session: Session) -> tuple[Workspace, User]:
    workspace_id = new_ulid()
    user_id = new_ulid()
    workspace_suffix = workspace_id.lower()[-10:]
    user_suffix = user_id.lower()[-12:]
    workspace = Workspace(
        id=workspace_id,
        slug=f"disp-{workspace_suffix}",
        name="Dispatcher WS",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    user = User(
        id=user_id,
        email=f"manager-{user_suffix}@example.com",
        display_name="Manager",
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add_all([workspace, user])
        session.flush()
    # The dispatcher's caller is the workspace manager — every test
    # in this suite needs the ``managers`` system-group fallback to
    # match (``tasks.create`` defaults to it). Seeded here so each
    # test reads as one helper call.
    grant = RoleGrant(
        id=new_ulid(),
        workspace_id=workspace.id,
        user_id=user.id,
        grant_role="manager",
        scope_property_id=None,
        created_at=_PINNED,
        created_by_user_id=None,
    )
    with tenant_agnostic():
        session.add(grant)
        session.flush()
    return workspace, user


def _seed_property(session: Session, *, workspace_id: str) -> str:
    prop = Property(
        id=new_ulid(),
        address="1 Pool Way",
        timezone="Europe/Paris",
        tags_json=[],
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(prop)
        session.flush()
        # ``property`` itself is workspace-agnostic; the binding to
        # the workspace lives on ``property_workspace`` (§02 "Villa
        # belongs to many workspaces"). Without this row the tenant
        # filter on ``property_workspace`` hides the property and
        # the create-task domain helper can't bind it.
        session.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=workspace_id,
                label="Main",
                membership_role="owner_workspace",
                share_guest_identity=False,
                auto_shift_from_occurrence=False,
                status="active",
                created_at=_PINNED,
            )
        )
        session.flush()
    return str(prop.id)


def _seed_seed_task(
    session: Session, *, workspace_id: str, user_id: str, property_id: str
) -> str:
    """Insert one occurrence so ``list_tasks`` has a row to surface."""
    row = Occurrence(
        id=new_ulid(),
        workspace_id=workspace_id,
        schedule_id=None,
        template_id=None,
        property_id=property_id,
        assignee_user_id=user_id,
        starts_at=_PINNED + timedelta(hours=2),
        ends_at=_PINNED + timedelta(hours=3),
        scheduled_for_local="2026-04-26T14:00",
        originally_scheduled_for="2026-04-26T14:00",
        state="pending",
        cancellation_reason=None,
        title="Pool clean",
        description_md="Weekly",
        priority="normal",
        photo_evidence="disabled",
        duration_minutes=60,
        area_id=None,
        unit_id=None,
        expected_role_id=None,
        linked_instruction_ids=[],
        inventory_consumption_json={},
        is_personal=False,
        created_by_user_id=user_id,
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(row)
        session.flush()
    return str(row.id)


def _seed_llm_assignment(session: Session, *, workspace_id: str) -> None:
    pm_id = new_ulid()
    registry_suffix = workspace_id.lower()[-12:]
    provider = LlmProvider(
        id=new_ulid(),
        name=f"fake-provider-{registry_suffix}",
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
        canonical_name=f"fake/dispatcher-model-{registry_suffix}",
        display_name="fake/dispatcher-model",
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
        api_model_id="fake/dispatcher-model",
        supports_system_prompt=True,
        supports_temperature=True,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(provider_model)
    session.flush()
    assignment = LlmAssignment(
        id=new_ulid(),
        workspace_id=workspace_id,
        capability="chat.manager",
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
    session.add(assignment)
    session.flush()


def _seed_budget_ledger(session: Session, *, workspace_id: str) -> None:
    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=_PINNED - timedelta(days=30),
        period_end=_PINNED,
        spent_cents=0,
        cap_cents=10_000,
        updated_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(row)
        session.flush()


def _seed_channel(session: Session, *, workspace_id: str) -> str:
    row_id = new_ulid()
    channel = ChatChannel(
        id=row_id,
        workspace_id=workspace_id,
        kind="manager",
        source="app",
        external_ref=None,
        title="Manager chat",
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(channel)
        session.flush()
    return str(row_id)


def _build_app(session: Session, ctx: WorkspaceContext) -> FastAPI:
    """Mount the tasks router with the seeded session + ctx pinned."""
    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/v1")

    def _session() -> Iterator[Session]:
        yield session

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx
    return app


def _build_agent_app(
    session: Session,
    ctx: WorkspaceContext,
    *,
    llm: _ScriptedLLM,
    clock: FrozenClock,
    bus: EventBus,
) -> FastAPI:
    app = FastAPI()
    app.include_router(tasks_router, prefix="/w/{slug}/api/v1")
    app.include_router(
        build_agent_router(clock=clock, event_bus=bus), prefix="/w/{slug}/api/v1"
    )

    def _session() -> Iterator[Session]:
        yield session

    def _ctx() -> WorkspaceContext:
        return ctx

    def _llm() -> _ScriptedLLM:
        return llm

    def _token_factory() -> DelegatedTokenFactory:
        return DelegatedTokenFactory(session=session, clock=clock)

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx
    app.dependency_overrides[_get_llm_dep] = _llm
    app.dependency_overrides[get_agent_token_factory] = _token_factory
    return app


def _build_agent_app_with_default_db(
    ctx: WorkspaceContext,
    *,
    llm: _ScriptedLLM,
    clock: FrozenClock,
    bus: EventBus,
) -> FastAPI:
    app = FastAPI()
    app.include_router(tasks_router, prefix="/w/{slug}/api/v1")
    app.include_router(
        build_agent_router(clock=clock, event_bus=bus), prefix="/w/{slug}/api/v1"
    )

    def _ctx() -> WorkspaceContext:
        return ctx

    def _llm() -> _ScriptedLLM:
        return llm

    app.dependency_overrides[current_workspace_context] = _ctx
    app.dependency_overrides[_get_llm_dep] = _llm
    return app


# ---------------------------------------------------------------------------
# 1) Read endpoint round-trip — list_tasks
# ---------------------------------------------------------------------------


def test_dispatch_read_endpoint_round_trips_with_mutated_false(
    db_session: Session,
) -> None:
    workspace, user = _seed_workspace_and_user(db_session)
    ctx = WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    set_current(ctx)
    property_id = _seed_property(db_session, workspace_id=workspace.id)
    seeded_task_id = _seed_seed_task(
        db_session,
        workspace_id=workspace.id,
        user_id=user.id,
        property_id=property_id,
    )

    app = _build_app(db_session, ctx)
    dispatcher = make_default_dispatcher(app, workspace_slug=workspace.slug)

    result = dispatcher.dispatch(
        ToolCall(id="c-read", name="list_tasks", input={}),
        token=DelegatedToken(plaintext="mip_FAKEKEY_FAKESECRET", token_id="tok"),
        headers={
            "X-Agent-Channel": "web_owner_sidebar",
            "X-Agent-Reason": "test.read",
        },
    )
    assert result.status_code == 200, result.body
    assert result.mutated is False
    assert isinstance(result.body, dict)
    ids = [row["id"] for row in result.body["data"]]
    assert seeded_task_id in ids


# ---------------------------------------------------------------------------
# 2) Write endpoint round-trip — create_task
# ---------------------------------------------------------------------------


def test_dispatch_write_endpoint_round_trips_with_mutated_true(
    db_session: Session,
) -> None:
    workspace, user = _seed_workspace_and_user(db_session)
    ctx = WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    set_current(ctx)
    property_id = _seed_property(db_session, workspace_id=workspace.id)

    app = _build_app(db_session, ctx)
    dispatcher = make_default_dispatcher(app, workspace_slug=workspace.slug)

    result = dispatcher.dispatch(
        ToolCall(
            id="c-write",
            name="create_task",
            input={
                "title": "Restock kitchen",
                "property_id": property_id,
                "scheduled_for_local": "2026-04-27T09:00",
            },
        ),
        token=DelegatedToken(plaintext="mip_FAKEKEY_FAKESECRET", token_id="tok"),
        headers={
            "X-Agent-Channel": "web_owner_sidebar",
            "X-Agent-Reason": "test.write",
        },
    )
    assert result.status_code == 201, result.body
    assert result.mutated is True

    # The row must have landed in the DB (the dispatcher invokes the
    # real router, which calls the real domain helper).
    rows = list(
        db_session.scalars(
            select(Occurrence).where(Occurrence.title == "Restock kitchen")
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].workspace_id == workspace.id


# ---------------------------------------------------------------------------
# 3) Unknown tool → 404
# ---------------------------------------------------------------------------


def test_dispatch_unknown_tool_returns_404(db_session: Session) -> None:
    workspace, user = _seed_workspace_and_user(db_session)
    ctx = WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    set_current(ctx)
    app = _build_app(db_session, ctx)
    dispatcher = make_default_dispatcher(app, workspace_slug=workspace.slug)

    result = dispatcher.dispatch(
        ToolCall(id="c-bad", name="not.a.real.tool", input={}),
        token=DelegatedToken(plaintext="mip_FAKEKEY_FAKESECRET", token_id="tok"),
        headers={},
    )
    assert result.status_code == 404
    assert result.mutated is False
    assert result.body == {"detail": "tool not found"}


# ---------------------------------------------------------------------------
# 4) End-to-end manager-creates-task via run_turn + dispatcher
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ScriptedLLM:
    """Minimal scripted client; pops one reply per call."""

    replies: list[LLMResponse]
    messages: list[list[dict[str, Any]]] = field(default_factory=list)

    def complete(self, **_: Any) -> LLMResponse:
        raise NotImplementedError

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.messages.append(list(kwargs["messages"]))
        return self.replies.pop(0)

    def ocr(self, **_: Any) -> str:
        raise NotImplementedError

    def stream_chat(self, **_: Any) -> Iterator[str]:
        raise NotImplementedError


@dataclass(slots=True)
class _FailingChatLLM:
    """LLM client that fails after the runtime has minted its delegated token."""

    messages: list[list[dict[str, Any]]] = field(default_factory=list)

    def complete(self, **_: Any) -> LLMResponse:
        raise NotImplementedError

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.messages.append(list(kwargs["messages"]))
        raise RuntimeError("llm transport failed")

    def ocr(self, **_: Any) -> str:
        raise NotImplementedError

    def stream_chat(self, **_: Any) -> Iterator[str]:
        raise NotImplementedError


@dataclass(slots=True)
class _RealDelegatedTokenFactory:
    """Wraps :func:`app.auth.tokens.mint` so the audit row carries a real id."""

    session: Session

    def mint_for(
        self,
        ctx: WorkspaceContext,
        *,
        agent_label: str,
        expires_at: datetime,
    ) -> DelegatedToken:
        minted = tokens_module.mint(
            self.session,
            ctx,
            user_id=ctx.actor_id,
            label=agent_label,
            scopes={},
            expires_at=expires_at,
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
        )
        return DelegatedToken(plaintext=minted.token, token_id=minted.key_id)


def test_manager_creates_task_end_to_end_via_dispatcher(
    db_session: Session,
) -> None:
    workspace, user = _seed_workspace_and_user(db_session)
    ctx = WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    set_current(ctx)
    _seed_llm_assignment(db_session, workspace_id=workspace.id)
    _seed_budget_ledger(db_session, workspace_id=workspace.id)
    channel_id = _seed_channel(db_session, workspace_id=workspace.id)
    property_id = _seed_property(db_session, workspace_id=workspace.id)

    app = _build_app(db_session, ctx)
    dispatcher = make_default_dispatcher(app, workspace_slug=workspace.slug)
    factory = _RealDelegatedTokenFactory(session=db_session)
    bus = EventBus()
    clock = FrozenClock(_PINNED)

    # The LLM emits a single tool call to ``create_task`` and then a
    # plain-text reply that closes the turn.
    tool_call_text = (
        '<tool_call name="create_task" input=\''
        '{"title":"Restock kitchen",'
        f'"property_id":"{property_id}",'
        '"scheduled_for_local":"2026-04-27T09:00"}\'/>'
    )
    llm = _ScriptedLLM(
        replies=[
            LLMResponse(
                text=tool_call_text,
                usage=LLMUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
                model_id="fake/dispatcher-model",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                text="Created the task.",
                usage=LLMUsage(prompt_tokens=22, completion_tokens=5, total_tokens=27),
                model_id="fake/dispatcher-model",
                finish_reason="stop",
            ),
        ]
    )

    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Please create a task to restock the kitchen.",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=factory,
        agent_label="manager-chat-agent",
        capability="chat.manager",
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.tool_calls_made == 1
    assert outcome.llm_calls_made == 2

    # The task landed.
    rows = list(
        db_session.scalars(
            select(Occurrence).where(Occurrence.title == "Restock kitchen")
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].workspace_id == workspace.id

    # An audit row was attributed to the delegating user with the
    # real delegated token id.
    audit_rows = list(
        db_session.scalars(
            select(AuditLog).where(AuditLog.action == "agent.tool.create_task")
        ).all()
    )
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.actor_id == user.id
    assert audit.actor_kind == "user"
    diff = audit.diff
    assert isinstance(diff, dict)
    assert diff["agent_label"] == "manager-chat-agent"
    assert diff["status_code"] == 201
    assert isinstance(diff["token_id"], str) and diff["token_id"]


def test_agent_message_endpoint_dispatches_live_tool_and_audits(
    db_session: Session,
) -> None:
    workspace, user = _seed_workspace_and_user(db_session)
    ctx = WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    set_current(ctx)
    _seed_llm_assignment(db_session, workspace_id=workspace.id)
    _seed_budget_ledger(db_session, workspace_id=workspace.id)
    property_id = _seed_property(db_session, workspace_id=workspace.id)
    bus = EventBus()
    clock = FrozenClock(_PINNED)

    tool_call_text = (
        '<tool_call name="create_task" input=\''
        '{"title":"Restock towels",'
        f'"property_id":"{property_id}",'
        '"scheduled_for_local":"2026-04-27T09:00"}\'/>'
    )
    llm = _ScriptedLLM(
        replies=[
            LLMResponse(
                text=tool_call_text,
                usage=LLMUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
                model_id="fake/dispatcher-model",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                text="Created the task.",
                usage=LLMUsage(prompt_tokens=22, completion_tokens=5, total_tokens=27),
                model_id="fake/dispatcher-model",
                finish_reason="stop",
            ),
        ]
    )
    app = _build_agent_app(db_session, ctx, llm=llm, clock=clock, bus=bus)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        f"/w/{workspace.slug}/api/v1/agent/manager/message",
        json={"body": "Please create a task to restock towels."},
    )

    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "user"
    assert response.json()["body"] == "Please create a task to restock towels."
    assert llm.replies == []
    assert llm.messages
    first_prompt_text = [
        message["content"] for message in llm.messages[0] if message["role"] == "user"
    ]
    assert first_prompt_text.count("Please create a task to restock towels.") == 1

    rows = list(
        db_session.scalars(
            select(Occurrence).where(Occurrence.title == "Restock towels")
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].workspace_id == workspace.id

    audit_rows = list(
        db_session.scalars(
            select(AuditLog).where(AuditLog.action == "agent.tool.create_task")
        ).all()
    )
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.actor_id == user.id
    assert audit.actor_kind == "user"
    diff = audit.diff
    assert isinstance(diff, dict)
    assert diff["agent_label"] == "manager-chat-agent"
    assert diff["status_code"] == 201
    assert isinstance(diff["token_id"], str) and diff["token_id"]

    with tenant_agnostic():
        token_row = db_session.get(ApiToken, diff["token_id"])
    assert token_row is not None
    assert token_row.kind == "delegated"
    assert token_row.revoked_at == _PINNED


def test_agent_message_then_log_refetch_uses_sqlite_default_uow_without_locking(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific post-send lock regression")

    source_db = engine.url.database
    if source_db is None:
        pytest.skip("SQLite file database required")
    db_path = tmp_path / "agent-refetch-lock.db"
    shutil.copy2(source_db, db_path)
    isolated_engine = make_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(
        bind=isolated_engine,
        expire_on_commit=False,
        class_=FilteredSession,
    )
    monkeypatch.setattr(db_session_module, "_default_engine", isolated_engine)
    monkeypatch.setattr(db_session_module, "_default_sessionmaker_", factory)

    try:
        with factory() as seed:
            workspace, user = _seed_workspace_and_user(seed)
            _seed_llm_assignment(seed, workspace_id=workspace.id)
            _seed_budget_ledger(seed, workspace_id=workspace.id)
            seed.commit()

        ctx = WorkspaceContext(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=user.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        set_current(ctx)
        bus = EventBus()
        clock = FrozenClock(_PINNED)
        attempts = 8
        llm = _ScriptedLLM(
            replies=[
                LLMResponse(
                    text=f"Reply {idx}",
                    usage=LLMUsage(
                        prompt_tokens=10,
                        completion_tokens=3,
                        total_tokens=13,
                    ),
                    model_id="fake/dispatcher-model",
                    finish_reason="stop",
                )
                for idx in range(attempts)
            ]
        )
        app = _build_agent_app_with_default_db(ctx, llm=llm, clock=clock, bus=bus)
        client = TestClient(app, raise_server_exceptions=False)

        for idx in range(attempts):
            response = client.post(
                f"/w/{workspace.slug}/api/v1/agent/manager/message",
                json={"body": f"Hello {idx}"},
            )
            assert response.status_code == 201, response.text

            log_response = client.get(f"/w/{workspace.slug}/api/v1/agent/manager/log")
            assert log_response.status_code == 200, log_response.text
            bodies = [row["body"] for row in log_response.json()]
            assert f"Hello {idx}" in bodies
    finally:
        isolated_engine.dispose()


def test_agent_message_unexpected_turn_failure_revokes_default_uow_token(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific default-UoW regression")

    source_db = engine.url.database
    if source_db is None:
        pytest.skip("SQLite file database required")
    db_path = tmp_path / "agent-turn-failure-token.db"
    shutil.copy2(source_db, db_path)
    isolated_engine = make_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(
        bind=isolated_engine,
        expire_on_commit=False,
        class_=FilteredSession,
    )
    monkeypatch.setattr(db_session_module, "_default_engine", isolated_engine)
    monkeypatch.setattr(db_session_module, "_default_sessionmaker_", factory)

    try:
        with factory() as seed:
            workspace, user = _seed_workspace_and_user(seed)
            _seed_llm_assignment(seed, workspace_id=workspace.id)
            _seed_budget_ledger(seed, workspace_id=workspace.id)
            seed.commit()

        ctx = WorkspaceContext(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=user.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        set_current(ctx)
        bus = EventBus()
        clock = FrozenClock(_PINNED)
        llm = _FailingChatLLM()
        app = _build_agent_app_with_default_db(ctx, llm=llm, clock=clock, bus=bus)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            f"/w/{workspace.slug}/api/v1/agent/manager/message",
            json={"body": "Hello"},
        )
        assert response.status_code == 500, response.text

        log_response = client.get(f"/w/{workspace.slug}/api/v1/agent/manager/log")
        assert log_response.status_code == 200, log_response.text
        assert [row["body"] for row in log_response.json()] == ["Hello"]

        with factory() as check, tenant_agnostic():
            token_rows = list(check.scalars(select(ApiToken)).all())
        assert len(token_rows) == 1
        assert token_rows[0].kind == "delegated"
        assert token_rows[0].revoked_at is not None
    finally:
        isolated_engine.dispose()


def test_agent_message_endpoint_surfaces_unassigned_capability_in_log(
    db_session: Session,
) -> None:
    workspace, user = _seed_workspace_and_user(db_session)
    ctx = WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    set_current(ctx)
    bus = EventBus()
    events: list[object] = []
    bus.subscribe(AgentTurnStarted)(events.append)
    bus.subscribe(AgentMessageAppended)(events.append)
    bus.subscribe(AgentTurnFinished)(events.append)
    clock = FrozenClock(_PINNED)
    llm = _ScriptedLLM(replies=[])
    app = _build_agent_app(db_session, ctx, llm=llm, clock=clock, bus=bus)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        f"/w/{workspace.slug}/api/v1/agent/manager/message",
        json={"body": "Hello"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "user"
    assert response.json()["body"] == "Hello"
    assert llm.messages == []

    log_response = client.get(f"/w/{workspace.slug}/api/v1/agent/manager/log")
    assert log_response.status_code == 200, log_response.text
    rows = log_response.json()
    assert [row["kind"] for row in rows] == ["user", "agent"]
    assert rows[0]["body"] == "Hello"
    assert "not configured" in rows[1]["body"]
    assert "assign a chat model" in rows[1]["body"]

    db_rows = list(
        db_session.scalars(
            select(ChatMessage).order_by(
                ChatMessage.created_at.asc(), ChatMessage.id.asc()
            )
        ).all()
    )
    assert [(row.author_label, row.body_md) for row in db_rows] == [
        ("Manager", "Hello"),
        ("agent", rows[1]["body"]),
    ]

    assert [type(event).name for event in events] == [
        "agent.message.appended",
        "agent.turn.started",
        "agent.message.appended",
        "agent.turn.finished",
    ]
    started = events[1]
    finished = events[3]
    assert isinstance(started, AgentTurnStarted)
    assert isinstance(finished, AgentTurnFinished)
    assert started.correlation_id == finished.correlation_id
    assert finished.outcome == "error"
