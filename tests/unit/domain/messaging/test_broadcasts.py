from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

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
from app.adapters.db.workspace.models import UserWorkRole, WorkEngagement, WorkRole
from app.adapters.mail.null import NullMailer
from app.adapters.notifications.ports import NotificationKind
from app.api.messaging.broadcasts import SqlAlchemyBroadcastGateway
from app.api.middleware.approval import InProcessApprovalDispatcher
from app.domain.agent.runtime import DelegatedToken, ToolCall
from app.domain.errors import Conflict, Validation
from app.domain.messaging.broadcasts import (
    audience_token_for_user,
    audience_token_for_work_role,
    audience_token_for_workspace_role,
    execute_broadcast,
    preview_broadcast_audience,
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
    driver_role_id: str
    maid_role_id: str


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
            role_by_key = {
                role.key: role
                for role in session.scalars(
                    select(WorkRole).where(
                        WorkRole.workspace_id == workspace.id,
                        WorkRole.key.in_(("driver", "maid")),
                    )
                )
            }
            missing_keys = {"driver", "maid"} - set(role_by_key)
            for key in missing_keys:
                role = WorkRole(
                    id=new_ulid(),
                    workspace_id=workspace.id,
                    key=key,
                    name=key.title(),
                    created_at=_PINNED,
                )
                session.add(role)
                role_by_key[key] = role
            driver_role_id = role_by_key["driver"].id
            maid_role_id = role_by_key["maid"].id
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
                session.add(
                    WorkEngagement(
                        id=new_ulid(),
                        workspace_id=workspace.id,
                        user_id=worker.id,
                        engagement_kind="payroll",
                        started_on=date(2026, 1, 1),
                        created_at=_PINNED,
                        updated_at=_PINNED,
                    )
                )
                session.add(
                    UserWorkRole(
                        id=new_ulid(),
                        workspace_id=workspace.id,
                        user_id=worker.id,
                        work_role_id=driver_role_id,
                        started_on=date(2026, 1, 1),
                        created_at=_PINNED,
                    )
                )
            session.add(
                UserWorkRole(
                    id=new_ulid(),
                    workspace_id=workspace.id,
                    user_id=worker_ids[0],
                    work_role_id=maid_role_id,
                    started_on=date(2026, 1, 1),
                    created_at=_PINNED,
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
    return _Persona(
        ctx=ctx,
        worker_ids=tuple(worker_ids),
        driver_role_id=driver_role_id,
        maid_role_id=maid_role_id,
    )


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
        gateway = SqlAlchemyBroadcastGateway(session)
        outcome = send_or_queue_broadcast(
            session,
            persona.ctx,
            audience=gateway,
            audience_tokens=[audience_token_for_user(persona.worker_ids[0])],
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
        gateway = SqlAlchemyBroadcastGateway(session)
        outcome = send_or_queue_broadcast(
            session,
            persona.ctx,
            audience=gateway,
            audience_tokens=[
                audience_token_for_work_role(persona.driver_role_id),
                audience_token_for_work_role(persona.maid_role_id),
                audience_token_for_user(persona.worker_ids[0]),
            ],
            confirmed_recipient_count=2,
            subject="Storm watch",
            body_md="Bring patio furniture inside before 16:00.",
            notification_sink=sink,
            approval_queue=gateway,
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


def test_recipient_preview_lists_current_workspace_people_and_groups(
    factory: sessionmaker[Session], persona: _Persona
) -> None:
    with factory() as session:
        preview = preview_broadcast_audience(
            SqlAlchemyBroadcastGateway(session), persona.ctx
        )

    assert {recipient.user_id for recipient in preview.people}.issuperset(
        set(persona.worker_ids)
    )
    groups = {group.token: group for group in preview.groups}
    assert (
        groups[audience_token_for_workspace_role("employees")].resolved_recipient_count
        == 2
    )
    assert groups[audience_token_for_work_role(persona.driver_role_id)].label
    assert (
        groups[audience_token_for_work_role(persona.driver_role_id)].recipient_user_ids
        == persona.worker_ids
    )
    assert groups[
        audience_token_for_work_role(persona.maid_role_id)
    ].recipient_user_ids == (persona.worker_ids[0],)


def test_audience_token_count_mismatch_uses_deduped_resolution(
    factory: sessionmaker[Session], persona: _Persona
) -> None:
    sink = _FakeSink()
    with factory() as session:
        gateway = SqlAlchemyBroadcastGateway(session)
        with pytest.raises(Conflict) as exc:
            send_or_queue_broadcast(
                session,
                persona.ctx,
                audience=gateway,
                audience_tokens=[
                    audience_token_for_work_role(persona.driver_role_id),
                    audience_token_for_work_role(persona.maid_role_id),
                ],
                confirmed_recipient_count=3,
                subject="Storm watch",
                body_md="Bring patio furniture inside before 16:00.",
                notification_sink=sink,
                approval_queue=gateway,
                clock=FrozenClock(_PINNED),
            )
    assert exc.value.extra["error"] == "recipient_count_mismatch"
    assert exc.value.extra["resolved_recipient_count"] == 2


def test_inactive_archived_and_future_users_are_excluded_from_people_and_groups(
    factory: sessionmaker[Session], persona: _Persona
) -> None:
    with factory() as session:
        inactive = bootstrap_user(
            session,
            email="broadcast-inactive@example.com",
            display_name="Broadcast Inactive",
            clock=FrozenClock(_PINNED),
        )
        archived = bootstrap_user(
            session,
            email="broadcast-archived@example.com",
            display_name="Broadcast Archived",
            clock=FrozenClock(_PINNED),
        )
        future_employee = bootstrap_user(
            session,
            email="broadcast-future-employee@example.com",
            display_name="Broadcast Future Employee",
            clock=FrozenClock(_PINNED),
        )
        future_manager = bootstrap_user(
            session,
            email="broadcast-future-manager@example.com",
            display_name="Broadcast Future Manager",
            clock=FrozenClock(_PINNED),
        )
        with tenant_agnostic():
            archived.archived_at = _PINNED
            for user, archived_on in (
                (inactive, date(2026, 1, 31)),
                (archived, None),
            ):
                session.add(
                    WorkEngagement(
                        id=new_ulid(),
                        workspace_id=persona.ctx.workspace_id,
                        user_id=user.id,
                        engagement_kind="payroll",
                        started_on=date(2026, 1, 1),
                        archived_on=archived_on,
                        created_at=_PINNED,
                        updated_at=_PINNED,
                    )
                )
                session.add(
                    UserWorkRole(
                        id=new_ulid(),
                        workspace_id=persona.ctx.workspace_id,
                        user_id=user.id,
                        work_role_id=persona.driver_role_id,
                        started_on=date(2026, 1, 1),
                        created_at=_PINNED,
                    )
                )
            session.add(
                WorkEngagement(
                    id=new_ulid(),
                    workspace_id=persona.ctx.workspace_id,
                    user_id=future_employee.id,
                    engagement_kind="payroll",
                    started_on=date(2999, 1, 1),
                    created_at=_PINNED,
                    updated_at=_PINNED,
                )
            )
            session.add(
                UserWorkRole(
                    id=new_ulid(),
                    workspace_id=persona.ctx.workspace_id,
                    user_id=future_employee.id,
                    work_role_id=persona.driver_role_id,
                    started_on=date(2999, 1, 1),
                    created_at=_PINNED,
                )
            )
            session.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=persona.ctx.workspace_id,
                    user_id=future_manager.id,
                    grant_role="manager",
                    scope_kind="workspace",
                    scope_property_id=None,
                    started_on=date(2999, 1, 1),
                    created_at=_PINNED,
                    created_by_user_id=persona.ctx.actor_id,
                )
            )
        session.commit()

    with factory() as session:
        preview = preview_broadcast_audience(
            SqlAlchemyBroadcastGateway(session), persona.ctx
        )

    person_ids = {person.user_id for person in preview.people}
    driver_group = next(
        group
        for group in preview.groups
        if group.token == audience_token_for_work_role(persona.driver_role_id)
    )
    assert inactive.id not in person_ids
    assert archived.id not in person_ids
    assert future_employee.id not in person_ids
    assert future_manager.id not in person_ids
    assert inactive.id not in driver_group.recipient_user_ids
    assert archived.id not in driver_group.recipient_user_ids
    assert future_employee.id not in driver_group.recipient_user_ids


def test_stale_group_token_is_rejected(
    factory: sessionmaker[Session], persona: _Persona
) -> None:
    sink = _FakeSink()
    with factory() as session, pytest.raises(Validation) as exc:
        send_or_queue_broadcast(
            session,
            persona.ctx,
            audience=SqlAlchemyBroadcastGateway(session),
            audience_tokens=[audience_token_for_work_role("role_missing")],
            confirmed_recipient_count=1,
            subject="Wrong audience",
            body_md="This should not leave the workspace.",
            notification_sink=sink,
            clock=FrozenClock(_PINNED),
        )
    assert exc.value.extra["error"] == "audience_token_not_found"


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
            audience=SqlAlchemyBroadcastGateway(session),
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
            audience=SqlAlchemyBroadcastGateway(session),
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
                audience=SqlAlchemyBroadcastGateway(session),
                subject="Wrong audience",
                body_md="This should not leave the workspace.",
                recipient_user_ids=(outsider.id,),
                notification_sink=service,
                broadcast_id="broadcast_wrong_audience",
                clock=FrozenClock(_PINNED),
            )
