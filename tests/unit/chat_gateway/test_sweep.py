"""Unit tests for the chat-gateway dispatch safety-net sweep (cd-0gaa)."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatGatewayBinding,
    ChatMessage,
)
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.events.bus import EventBus
from app.events.types import ChatMessageReceived
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.chat_gateway_sweep import (
    SWEEP_BATCH_SIZE,
    SWEEP_GRACE_SECONDS,
    sweep_undispatched_messages,
)

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every adapter model so :class:`Base.metadata` is complete.

    The sweep query joins ``chat_message`` to ``chat_channel`` and
    audits to ``audit_log``; the metadata-driven ``create_all`` below
    needs every related table registered before the in-memory engine
    materialises.
    """
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


@pytest.fixture
def patched_uow(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]
) -> Iterator[None]:
    """Redirect :func:`make_uow` (used inside the sweep) to the test session.

    The sweep opens its own UoW via ``make_uow()``; without
    redirection it would reach for whatever default engine the test
    process has wired (or none at all). Patching the symbol the
    sweep imports keeps the sweep talking to the in-memory engine
    the fixture set up above.
    """
    import contextlib

    @contextlib.contextmanager
    def _make_uow() -> Iterator[Session]:
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(
        "app.worker.tasks.chat_gateway_sweep.make_uow",
        _make_uow,
    )
    yield


def _seed_user(s: Session, *, suffix: str = "u") -> str:
    """Mint a minimal :class:`User` row and return its id.

    Used by the predicate tests that need an ``author_user_id`` FK
    target — the SELECT predicate filters on ``author_user_id IS
    NULL`` and ``gateway_binding_id IS NOT NULL``, so the rows the
    sweep should ignore must point at a real user row to satisfy
    the FK during INSERT.
    """
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=f"{suffix}-{user_id[-6:].lower()}@dev.local",
            email_lower=f"{suffix}-{user_id[-6:].lower()}@dev.local",
            display_name="Sweep Test",
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _seed_workspace(s: Session, *, slug_suffix: str = "ws") -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"{slug_suffix}-{workspace_id[-6:].lower()}",
            name="Sweep Test",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _seed_gateway_message(
    s: Session,
    *,
    workspace_id: str,
    created_at: datetime,
    dispatched_to_agent_at: datetime | None = None,
    channel_kind: str = "chat_gateway",
    provider: str = "twilio",
    provider_message_id: str | None = None,
    with_binding: bool = True,
    author_user_id: str | None = None,
) -> str:
    """Seed one channel + binding + message and return the message id.

    Each call mints fresh channel + binding ids so a single workspace
    can host multiple stragglers without colliding on the
    ``(source, provider_message_id)`` unique constraint.

    ``with_binding=False`` mirrors the agent-outbound shape on a
    chat-gateway channel — the row carries no ``gateway_binding_id``
    even though the channel kind matches. ``author_user_id``
    similarly mirrors the agent's reply path (a real user owns the
    delegating actor); legitimate inbound rows leave it ``None``.
    """
    channel_id = new_ulid()
    binding_id = new_ulid()
    message_id = new_ulid()
    pmid = provider_message_id or f"PM-{message_id[-8:]}"
    s.add(
        ChatChannel(
            id=channel_id,
            workspace_id=workspace_id,
            kind=channel_kind,
            source="sms",
            title="SMS",
            created_at=_PINNED,
            archived_at=None,
        )
    )
    if with_binding:
        s.add(
            ChatGatewayBinding(
                id=binding_id,
                workspace_id=workspace_id,
                provider=provider,
                external_contact=f"+1555{message_id[-7:]}",
                channel_id=channel_id,
                display_label="+15551234567",
                provider_metadata_json={},
                created_at=_PINNED,
                last_message_at=_PINNED,
            )
        )
    s.add(
        ChatMessage(
            id=message_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            author_user_id=author_user_id,
            author_label="+15551234567",
            body_md="Need help",
            attachments_json=[],
            source=provider,
            provider_message_id=pmid,
            gateway_binding_id=binding_id if with_binding else None,
            dispatched_to_agent_at=dispatched_to_agent_at,
            created_at=created_at,
        )
    )
    s.flush()
    return message_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_sweep_republishes_stragglers_older_than_grace(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """Three stuck rows produce three ``chat.message.received`` events."""
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    seeded: list[str] = []
    with factory() as s:
        workspace_id = _seed_workspace(s)
        for _ in range(3):
            seeded.append(
                _seed_gateway_message(
                    s,
                    workspace_id=workspace_id,
                    created_at=older_than_grace,
                )
            )
        s.commit()

    bus = EventBus()
    received: list[ChatMessageReceived] = []
    bus.subscribe(ChatMessageReceived)(received.append)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert result.requeued_count == 3
    assert result.failed_count == 0
    assert set(result.processed_ids) == set(seeded)
    assert {event.message_id for event in received} == set(seeded)
    assert all(event.channel_kind == "chat_gateway" for event in received)
    # Every successful re-publish gets one audit row.
    with factory() as s:
        actions = s.scalars(
            select(AuditLog.action).where(AuditLog.action.like("chat_gateway.sweep.%"))
        ).all()
    assert sorted(actions) == ["chat_gateway.sweep.requeued"] * 3


# ---------------------------------------------------------------------------
# Age filter — rows younger than the grace are not touched
# ---------------------------------------------------------------------------


def test_sweep_ignores_rows_younger_than_grace(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """A row inside the 30 s grace must NOT be re-published."""
    fresh_created_at = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS - 1)
    with factory() as s:
        workspace_id = _seed_workspace(s)
        _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=fresh_created_at,
        )
        s.commit()

    bus = EventBus()
    received: list[ChatMessageReceived] = []
    bus.subscribe(ChatMessageReceived)(received.append)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert result.requeued_count == 0
    assert result.failed_count == 0
    assert result.processed_ids == ()
    assert received == []


# ---------------------------------------------------------------------------
# Age filter — already-dispatched rows are skipped
# ---------------------------------------------------------------------------


def test_sweep_ignores_already_dispatched_rows(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """Rows whose ``dispatched_to_agent_at`` is set must be skipped.

    Idempotency contract: the dispatcher's CAS keeps re-publication
    of a row a primary handler caught up first a no-op. The sweep's
    SELECT predicate makes that even cheaper by never picking the
    row up in the first place — the predicate test would otherwise
    falsely cover for a missing handler-side CAS.
    """
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    with factory() as s:
        workspace_id = _seed_workspace(s)
        _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace,
            dispatched_to_agent_at=_PINNED - timedelta(seconds=1),
        )
        s.commit()

    bus = EventBus()
    received: list[ChatMessageReceived] = []
    bus.subscribe(ChatMessageReceived)(received.append)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert result.requeued_count == 0
    assert received == []


# ---------------------------------------------------------------------------
# Channel kind filter — non-gateway rows are out of scope
# ---------------------------------------------------------------------------


def test_sweep_ignores_non_gateway_channels(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """In-app ``staff``/``manager`` channels must NOT be swept.

    The §23 safety net targets the off-app gateway path only; in-app
    chat runs synchronously inside the agent endpoint per §11 and
    has no ``dispatched_to_agent_at`` to chase.
    """
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    with factory() as s:
        workspace_id = _seed_workspace(s)
        _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace,
            channel_kind="staff",
        )
        s.commit()

    bus = EventBus()
    received: list[ChatMessageReceived] = []
    bus.subscribe(ChatMessageReceived)(received.append)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert result.requeued_count == 0
    assert received == []


# ---------------------------------------------------------------------------
# Direction filter — outbound agent replies on a chat_gateway channel are
# inserted with ``dispatched_to_agent_at = NULL`` and ``gateway_binding_id
# = NULL``. The sweep must NOT pick them up; otherwise re-publishing
# ``chat.message.received`` on an outbound row would loop the agent on
# its own replies. See ``app.domain.agent.runtime._write_chat_reply`` and
# ``app.api.v1.agent`` for the outbound shape.
# ---------------------------------------------------------------------------


def test_sweep_ignores_outbound_rows_on_gateway_channel(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """An agent reply on a chat-gateway channel must NOT be swept.

    Pins both halves of the predicate (the SELECT filters on
    ``gateway_binding_id IS NOT NULL`` AND ``author_user_id IS
    NULL``); a regression that drops either side falls back into the
    agent-self-reply infinite loop.
    """
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    with factory() as s:
        workspace_id = _seed_workspace(s)
        author_id = _seed_user(s, suffix="agent")
        # Outbound shape: no binding, has an author user id (the
        # delegating actor that the agent's reply attributes back to
        # per §23 ``chat_message`` "Authored").
        _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace,
            with_binding=False,
            author_user_id=author_id,
        )
        s.commit()

    bus = EventBus()
    received: list[ChatMessageReceived] = []
    bus.subscribe(ChatMessageReceived)(received.append)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert result.requeued_count == 0
    assert result.failed_count == 0
    assert result.processed_ids == ()
    assert received == []


def test_sweep_ignores_in_app_authored_rows_on_gateway_channel(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """A real user authoring on a chat-gateway channel is NOT inbound.

    Belt-and-braces: even with a binding present, an
    ``author_user_id`` set means a workspace member typed on the
    channel through the in-app UI; that path doesn't need a sweep
    re-fire. The predicate filters it out.
    """
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    with factory() as s:
        workspace_id = _seed_workspace(s)
        author_id = _seed_user(s, suffix="member")
        _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace,
            author_user_id=author_id,
        )
        s.commit()

    bus = EventBus()
    received: list[ChatMessageReceived] = []
    bus.subscribe(ChatMessageReceived)(received.append)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert result.requeued_count == 0
    assert received == []


# ---------------------------------------------------------------------------
# Bounded batch size
# ---------------------------------------------------------------------------


def test_sweep_processes_at_most_batch_size_per_tick(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """A backlog of 250 stragglers leaves 50 for the next tick."""
    overshoot = 50
    total = SWEEP_BATCH_SIZE + overshoot
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    with factory() as s:
        workspace_id = _seed_workspace(s)
        for _ in range(total):
            _seed_gateway_message(
                s,
                workspace_id=workspace_id,
                created_at=older_than_grace,
            )
        s.commit()

    bus = EventBus()
    received: list[ChatMessageReceived] = []
    bus.subscribe(ChatMessageReceived)(received.append)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert result.requeued_count == SWEEP_BATCH_SIZE
    assert result.failed_count == 0
    assert len(result.processed_ids) == SWEEP_BATCH_SIZE
    assert len(received) == SWEEP_BATCH_SIZE


# ---------------------------------------------------------------------------
# Per-row failure isolation
# ---------------------------------------------------------------------------


def test_sweep_isolates_per_row_failures(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """One bad row raises; the rest still get re-published + audited."""
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    with factory() as s:
        workspace_id = _seed_workspace(s)
        first = _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace - timedelta(seconds=2),
        )
        second = _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace - timedelta(seconds=1),
        )
        third = _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace,
        )
        s.commit()

    bus = EventBus()
    received: list[str] = []

    def _handle(event: ChatMessageReceived) -> None:
        if event.message_id == second:
            raise RuntimeError("subscriber boom")
        received.append(event.message_id)

    bus.subscribe(ChatMessageReceived)(_handle)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    # First + third re-published successfully; second isolated as a failure.
    assert result.requeued_count == 2
    assert result.failed_count == 1
    assert set(result.processed_ids) == {first, second, third}
    assert set(received) == {first, third}

    with factory() as s:
        rows = s.execute(
            select(AuditLog.action, AuditLog.entity_id)
            .where(AuditLog.action.like("chat_gateway.sweep.%"))
            .order_by(AuditLog.entity_id)
        ).all()
    actions_by_message = {entity_id: action for action, entity_id in rows}
    assert actions_by_message[first] == "chat_gateway.sweep.requeued"
    assert actions_by_message[third] == "chat_gateway.sweep.requeued"
    assert actions_by_message[second] == "chat_gateway.sweep.requeue.failed"


# ---------------------------------------------------------------------------
# Multi-tenant: rows from two workspaces are processed under their own ctx
# ---------------------------------------------------------------------------


def test_sweep_processes_rows_across_workspaces(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """Stragglers in workspace A and B both reach the bus + their own audit."""
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    with factory() as s:
        workspace_a = _seed_workspace(s, slug_suffix="a")
        workspace_b = _seed_workspace(s, slug_suffix="b")
        msg_a = _seed_gateway_message(
            s,
            workspace_id=workspace_a,
            created_at=older_than_grace,
        )
        msg_b = _seed_gateway_message(
            s,
            workspace_id=workspace_b,
            created_at=older_than_grace + timedelta(milliseconds=1),
        )
        s.commit()

    bus = EventBus()
    received: list[ChatMessageReceived] = []
    bus.subscribe(ChatMessageReceived)(received.append)

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert result.requeued_count == 2
    by_workspace = {event.workspace_id: event.message_id for event in received}
    assert by_workspace == {workspace_a: msg_a, workspace_b: msg_b}

    with factory() as s:
        rows = s.execute(
            select(AuditLog.workspace_id, AuditLog.entity_id).where(
                AuditLog.action == "chat_gateway.sweep.requeued"
            )
        ).all()
    audit_by_workspace = {workspace: entity_id for workspace, entity_id in rows}
    assert audit_by_workspace == {workspace_a: msg_a, workspace_b: msg_b}


# ---------------------------------------------------------------------------
# Metric label cardinality — bounded ``job`` + ``outcome`` only.
# ---------------------------------------------------------------------------


def test_sweep_metric_uses_bounded_labels(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """``WORKER_SWEEP_REQUEUED_TOTAL`` carries (job, outcome) only.

    Pins the ``job`` label to the scheduler's job id constant
    (``chat_gateway.agent_dispatch_sweep``) and the ``outcome``
    label to ``requeued`` / ``failed``. Workspace ids and message
    ids must NEVER appear as labels — that would explode the
    metric cardinality.
    """
    from app.observability.metrics import WORKER_SWEEP_REQUEUED_TOTAL

    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ok = _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace,
        )
        boom = _seed_gateway_message(
            s,
            workspace_id=workspace_id,
            created_at=older_than_grace + timedelta(milliseconds=1),
        )
        s.commit()

    bus = EventBus()

    def _maybe_raise(event: ChatMessageReceived) -> None:
        if event.message_id == boom:
            raise RuntimeError("boom")

    bus.subscribe(ChatMessageReceived)(_maybe_raise)

    # Snapshot the counter delta across the sweep so other tests'
    # increments don't pollute the assertion.
    before_requeued = WORKER_SWEEP_REQUEUED_TOTAL.labels(
        job="chat_gateway.agent_dispatch_sweep",
        outcome="requeued",
    )._value.get()
    before_failed = WORKER_SWEEP_REQUEUED_TOTAL.labels(
        job="chat_gateway.agent_dispatch_sweep",
        outcome="failed",
    )._value.get()

    result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    after_requeued = WORKER_SWEEP_REQUEUED_TOTAL.labels(
        job="chat_gateway.agent_dispatch_sweep",
        outcome="requeued",
    )._value.get()
    after_failed = WORKER_SWEEP_REQUEUED_TOTAL.labels(
        job="chat_gateway.agent_dispatch_sweep",
        outcome="failed",
    )._value.get()

    assert result.requeued_count == 1
    assert result.failed_count == 1
    assert {ok, boom} == set(result.processed_ids)
    assert after_requeued - before_requeued == 1
    assert after_failed - before_failed == 1

    # The label set must stay (job, outcome) — no workspace or
    # message id sneaking in.
    assert WORKER_SWEEP_REQUEUED_TOTAL._labelnames == ("job", "outcome")
