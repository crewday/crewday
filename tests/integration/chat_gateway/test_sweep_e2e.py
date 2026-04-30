"""End-to-end test for the chat-gateway sweep through the real EventBus.

Exercises the full safety-net path: the sweep finds a stuck row,
re-publishes ``chat.message.received`` on a real :class:`EventBus`,
the registered dispatcher schedules an :class:`AgentDispatchJob`, and
processing the scheduled job (with a fake ``AgentRuntimeEnqueue``)
stamps ``dispatched_to_agent_at``. A second sweep then no-ops, proving
the dispatcher's CAS keeps re-publication safely idempotent.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatGatewayBinding,
    ChatMessage,
)
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.chat_gateway.dispatcher import (
    AgentDispatchJob,
    AgentDispatchPayload,
    dispatch_inbound_message,
    register_chat_gateway_dispatcher,
)
from app.events.bus import EventBus
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.chat_gateway_sweep import (
    SWEEP_GRACE_SECONDS,
    sweep_undispatched_messages,
)

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
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
def local_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(local_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=local_engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def patched_uow(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]
) -> Iterator[None]:
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


def _seed_stuck_inbound(s: Session) -> str:
    workspace_id = new_ulid()
    channel_id = new_ulid()
    binding_id = new_ulid()
    message_id = new_ulid()
    older_than_grace = _PINNED - timedelta(seconds=SWEEP_GRACE_SECONDS + 5)
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Sweep E2E",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        ChatChannel(
            id=channel_id,
            workspace_id=workspace_id,
            kind="chat_gateway",
            source="sms",
            title="SMS",
            created_at=_PINNED,
            archived_at=None,
        )
    )
    s.add(
        ChatGatewayBinding(
            id=binding_id,
            workspace_id=workspace_id,
            provider="twilio",
            external_contact="+15551234567",
            channel_id=channel_id,
            display_label="+15551234567",
            provider_metadata_json={"language_hint": "en-US"},
            created_at=_PINNED,
            last_message_at=_PINNED,
        )
    )
    s.add(
        ChatMessage(
            id=message_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            author_user_id=None,
            author_label="+15551234567",
            body_md="Need help",
            attachments_json=[],
            source="twilio",
            provider_message_id="SM-stuck",
            gateway_binding_id=binding_id,
            dispatched_to_agent_at=None,
            created_at=older_than_grace,
        )
    )
    s.commit()
    return message_id


def test_sweep_to_dispatcher_to_runtime_end_to_end(
    factory: sessionmaker[Session],
    patched_uow: None,
) -> None:
    """One stuck row flows: sweep → bus → dispatcher → runtime enqueue.

    A second sweep tick after the dispatcher stamps the row is a
    no-op — the SELECT predicate filters out rows whose
    ``dispatched_to_agent_at`` has been set, so the agent runtime
    never sees a duplicate payload.
    """
    with factory() as s:
        message_id = _seed_stuck_inbound(s)

    bus = EventBus()
    scheduled: list[AgentDispatchJob] = []
    register_chat_gateway_dispatcher(bus, schedule=scheduled.append)

    sweep_result = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED),
    )

    assert sweep_result.requeued_count == 1
    assert scheduled == [AgentDispatchJob(message_id=message_id)]

    enqueued: list[AgentDispatchPayload] = []
    with factory() as s:
        dispatch_result = dispatch_inbound_message(
            s,
            scheduled[0],
            enqueue=enqueued.append,
            clock=FrozenClock(_PINNED),
        )
        s.commit()

    assert dispatch_result.status == "enqueued"
    assert [payload.message_id for payload in enqueued] == [message_id]
    assert dispatch_result.dispatched_to_agent_at == _PINNED

    # Second sweep — the row is now stamped, so the predicate filters
    # it out before the bus is touched. ``scheduled`` and ``enqueued``
    # stay at one entry each.
    follow_up = sweep_undispatched_messages(
        event_bus=bus,
        clock=FrozenClock(_PINNED + timedelta(seconds=SWEEP_GRACE_SECONDS + 5)),
    )
    assert follow_up.requeued_count == 0
    assert follow_up.processed_ids == ()
    assert len(scheduled) == 1
    assert len(enqueued) == 1
