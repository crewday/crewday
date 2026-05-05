from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.messaging.models import Notification
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.session import make_engine
from app.adapters.mail.null import NullMailer
from app.adapters.notifications.ports import NotificationKind
from app.api.middleware.approval import InProcessApprovalDispatcher
from app.domain.agent.runtime import DelegatedToken, ToolCall
from app.domain.errors import Validation
from app.domain.messaging.broadcasts import (
    execute_broadcast,
    list_broadcast_recipients,
    send_or_queue_broadcast,
)
from app.domain.messaging.notifications import NotificationService
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)


def _load_all_models() -> None:
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


@dataclass(frozen=True, slots=True)
class _Persona:
    ctx: WorkspaceContext
    worker_ids: tuple[str, ...]


@pytest.fixture
def persona(factory: sessionmaker[Session]) -> _Persona:
    with factory() as session:
        owner = bootstrap_user(
            session,
            email="broadcast-owner@example.com",
            display_name="Broadcast Owner",
            clock=FrozenClock(_PINNED),
        )
        workspace = bootstrap_workspace(
            session,
            slug="broadcasts",
            name="Broadcasts",
            owner_user_id=owner.id,
            clock=FrozenClock(_PINNED),
        )
        worker_ids: list[str] = []
        with tenant_agnostic():
            for idx in range(2):
                worker = bootstrap_user(
                    session,
                    email=f"broadcast-worker-{idx}@example.com",
                    display_name=f"Broadcast Worker {idx}",
                    clock=FrozenClock(_PINNED),
                )
                worker_ids.append(worker.id)
                session.add(
                    RoleGrant(
                        id=new_ulid(),
                        workspace_id=workspace.id,
                        user_id=worker.id,
                        grant_role="worker",
                        scope_kind="workspace",
                        scope_property_id=None,
                        created_at=_PINNED,
                        created_by_user_id=owner.id,
                    )
                )
        session.commit()
        ctx = build_workspace_context(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=owner.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
        )
    return _Persona(ctx=ctx, worker_ids=tuple(worker_ids))


@dataclass(slots=True)
class _FakeSink:
    calls: list[tuple[str, NotificationKind, Mapping[str, object]]] = field(
        default_factory=list
    )

    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str:
        self.calls.append((recipient_user_id, kind, dict(payload)))
        return f"notif_{len(self.calls)}"


def test_single_recipient_broadcast_sends_immediately(
    factory: sessionmaker[Session], persona: _Persona
) -> None:
    sink = _FakeSink()
    with factory() as session:
        outcome = send_or_queue_broadcast(
            session,
            persona.ctx,
            target="selected",
            selected_recipient_user_ids=[persona.worker_ids[0]],
            confirmed_recipient_count=1,
            subject="Pool closed",
            body_md="Please route guests through reception.",
            notification_sink=sink,
            clock=FrozenClock(_PINNED),
        )
        session.commit()

    assert outcome.status == "sent"
    assert outcome.notification_ids == ("notif_1",)
    assert sink.calls == [
        (
            persona.worker_ids[0],
            NotificationKind.AGENT_MESSAGE,
            {
                "broadcast_id": sink.calls[0][2]["broadcast_id"],
                "broadcast_subject": "Pool closed",
                "sender_user_id": persona.ctx.actor_id,
                "preview": "Pool closed",
                "message_body": "Please route guests through reception.",
                "deep_link": "/w/broadcasts/notifications",
            },
        )
    ]

    with factory() as session, tenant_agnostic():
        actions = session.scalars(select(AuditLog.action)).all()
    assert "messaging.broadcast.sent" in actions


def test_multi_recipient_broadcast_queues_approval_without_fanout(
    factory: sessionmaker[Session], persona: _Persona
) -> None:
    sink = _FakeSink()
    with factory() as session:
        outcome = send_or_queue_broadcast(
            session,
            persona.ctx,
            target="selected",
            selected_recipient_user_ids=list(persona.worker_ids),
            confirmed_recipient_count=2,
            subject="Storm watch",
            body_md="Bring patio furniture inside before 16:00.",
            notification_sink=sink,
            clock=FrozenClock(_PINNED),
        )
        session.commit()

    assert outcome.status == "pending_approval"
    assert outcome.recipient_count == 2
    assert outcome.notification_ids == ()
    assert outcome.approval_request_id is not None
    assert sink.calls == []

    with factory() as session, tenant_agnostic():
        row = session.get(ApprovalRequest, outcome.approval_request_id)
        assert row is not None
        assert row.action_json["tool_name"] == "messaging.broadcast"
        assert row.action_json["tool_input"]["recipient_user_ids"] == list(
            persona.worker_ids
        )


def test_recipient_preview_lists_current_workspace_staff(
    factory: sessionmaker[Session], persona: _Persona
) -> None:
    with factory() as session:
        recipients = list_broadcast_recipients(session, persona.ctx)

    assert {recipient.user_id for recipient in recipients}.issuperset(
        set(persona.worker_ids)
    )


def test_approved_broadcast_replay_creates_notification_rows(
    factory: sessionmaker[Session],
    persona: _Persona,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.adapters.db.session as session_mod

    bound_engine = factory.kw.get("bind")
    assert isinstance(bound_engine, Engine)
    monkeypatch.setattr(session_mod, "_default_engine", bound_engine)
    monkeypatch.setattr(session_mod, "_default_sessionmaker_", factory)

    call = ToolCall(
        id="call_broadcast",
        name="messaging.broadcast",
        input={
            "workspace_slug": persona.ctx.workspace_slug,
            "broadcast_id": "broadcast_replay",
            "subject": "Approved broadcast",
            "body_md": "This message was approved.",
            "recipient_user_ids": list(persona.worker_ids),
        },
    )
    result = InProcessApprovalDispatcher().dispatch(
        call,
        token=DelegatedToken(plaintext="replay", token_id="token_replay"),
        headers={
            "X-Crewday-Replay-Actor-Id": persona.ctx.actor_id,
            "X-Crewday-Replay-Actor-Role": "manager",
            "X-Crewday-Replay-Actor-Is-Owner": "1",
        },
    )

    assert result.status_code == 200
    assert result.body["recipient_count"] == 2
    with factory() as session, tenant_agnostic():
        rows = session.scalars(
            select(Notification)
            .where(Notification.kind == "agent_message")
            .order_by(Notification.created_at, Notification.id)
        ).all()
    assert [row.recipient_user_id for row in rows] == list(persona.worker_ids)


def test_broadcast_execution_is_idempotent_by_broadcast_id(
    factory: sessionmaker[Session],
    persona: _Persona,
) -> None:
    with factory() as session:
        service = NotificationService(
            session=session,
            ctx=persona.ctx,
            mailer=NullMailer(),
            email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
        )
        first = execute_broadcast(
            session,
            persona.ctx,
            subject="Approved broadcast",
            body_md="This message was approved.",
            recipient_user_ids=persona.worker_ids,
            notification_sink=service,
            broadcast_id="broadcast_once",
            clock=FrozenClock(_PINNED),
        )
        second = execute_broadcast(
            session,
            persona.ctx,
            subject="Approved broadcast",
            body_md="This message was approved.",
            recipient_user_ids=persona.worker_ids,
            notification_sink=service,
            broadcast_id="broadcast_once",
            clock=FrozenClock(_PINNED),
        )
        session.commit()

    assert second == first
    with factory() as session, tenant_agnostic():
        rows = session.scalars(
            select(Notification).where(Notification.kind == "agent_message")
        ).all()
        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_kind == "messaging_broadcast",
                AuditLog.entity_id == "broadcast_once",
                AuditLog.action == "messaging.broadcast.sent",
            )
        ).all()
    assert (
        len(
            [
                row
                for row in rows
                if row.payload_json["broadcast_id"] == "broadcast_once"
            ]
        )
        == 2
    )
    assert len(audits) == 1


def test_broadcast_execution_rejects_cross_workspace_recipient(
    factory: sessionmaker[Session],
    persona: _Persona,
) -> None:
    with factory() as session:
        outsider = bootstrap_user(
            session,
            email="broadcast-outsider@example.com",
            display_name="Broadcast Outsider",
            clock=FrozenClock(_PINNED),
        )
        session.commit()

    with factory() as session:
        service = NotificationService(
            session=session,
            ctx=persona.ctx,
            mailer=NullMailer(),
            email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
        )
        with pytest.raises(Validation, match="current workspace staff"):
            execute_broadcast(
                session,
                persona.ctx,
                subject="Wrong audience",
                body_md="This should not leave the workspace.",
                recipient_user_ids=(outsider.id,),
                notification_sink=service,
                broadcast_id="broadcast_wrong_audience",
                clock=FrozenClock(_PINNED),
            )
