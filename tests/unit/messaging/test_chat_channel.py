"""Unit tests for chat-channel CRUD and visibility (cd-7ej8)."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import ChatChannel, ChatChannelMember
from app.adapters.db.messaging.repositories import SqlAlchemyChatChannelRepository
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.messaging.channels import (
    ChatChannelCreate,
    ChatChannelInvalid,
    ChatChannelNotFound,
    ChatChannelPermissionDenied,
    ChatChannelService,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

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


def _bootstrap_workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Messaging Test",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(
    s: Session,
    *,
    workspace_id: str,
    email: str,
    role: Literal["manager", "worker", "client", "guest"],
) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=email.split("@", maxsplit=1)[0],
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=role,
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()
    return user_id


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    role: Literal["manager", "worker", "client", "guest"],
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="messaging",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _seed(factory: sessionmaker[Session]) -> tuple[str, str, str]:
    with factory() as s:
        workspace_id = _bootstrap_workspace(s)
        manager_id = _bootstrap_user(
            s,
            workspace_id=workspace_id,
            email="manager@example.com",
            role="manager",
        )
        worker_id = _bootstrap_user(
            s,
            workspace_id=workspace_id,
            email="worker@example.com",
            role="worker",
        )
        s.commit()
    return workspace_id, manager_id, worker_id


class TestChatChannelVisibility:
    def test_worker_lists_staff_only_and_can_post_staff(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        workspace_id, manager_id, worker_id = _seed(factory)
        manager_ctx = _ctx(
            workspace_id=workspace_id,
            actor_id=manager_id,
            role="manager",
        )
        worker_ctx = _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker")

        with factory() as s:
            repo = SqlAlchemyChatChannelRepository(s)
            service = ChatChannelService(manager_ctx, clock=FrozenClock(_PINNED))
            staff = service.create(repo, ChatChannelCreate(kind="staff", title="Staff"))
            manager = service.create(
                repo,
                ChatChannelCreate(kind="manager", title="Managers"),
            )
            gateway = service.create(
                repo,
                ChatChannelCreate(
                    kind="chat_gateway",
                    title="WhatsApp",
                    source="whatsapp",
                    external_ref="wa-1",
                ),
            )

            worker_service = ChatChannelService(worker_ctx, clock=FrozenClock(_PINNED))
            visible = worker_service.list(repo)

            assert [channel.id for channel in visible] == [staff.id]
            assert visible[0].can_post_messages is True
            worker_service.assert_can_post_message(repo, staff.id)
            with pytest.raises(ChatChannelNotFound):
                worker_service.assert_can_post_message(repo, manager.id)
            with pytest.raises(ChatChannelNotFound):
                worker_service.assert_can_post_message(repo, gateway.id)
            with pytest.raises(ChatChannelNotFound):
                worker_service.get(repo, manager.id)
            with pytest.raises(ChatChannelNotFound):
                worker_service.get(repo, gateway.id)

    def test_manager_lists_every_kind(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        workspace_id, manager_id, _worker_id = _seed(factory)
        manager_ctx = _ctx(
            workspace_id=workspace_id,
            actor_id=manager_id,
            role="manager",
        )

        with factory() as s:
            repo = SqlAlchemyChatChannelRepository(s)
            service = ChatChannelService(manager_ctx, clock=FrozenClock(_PINNED))
            service.create(repo, ChatChannelCreate(kind="staff", title="Staff"))
            service.create(repo, ChatChannelCreate(kind="manager", title="Managers"))
            service.create(
                repo,
                ChatChannelCreate(
                    kind="chat_gateway",
                    source="whatsapp",
                    external_ref="wa-1",
                ),
            )

            visible = service.list(repo)

            assert [channel.kind for channel in visible] == [
                "staff",
                "manager",
                "chat_gateway",
            ]
            assert all(channel.can_post_messages for channel in visible)


class TestChatChannelCrud:
    def test_worker_cannot_create_staff_or_manager_channels(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        workspace_id, _manager_id, worker_id = _seed(factory)
        worker_ctx = _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker")

        with factory() as s:
            service = ChatChannelService(worker_ctx, clock=FrozenClock(_PINNED))
            with pytest.raises(ChatChannelPermissionDenied):
                service.create(
                    SqlAlchemyChatChannelRepository(s),
                    ChatChannelCreate(kind="staff", title="Staff"),
                )

    def test_archive_is_soft_and_audited(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        workspace_id, manager_id, _worker_id = _seed(factory)
        manager_ctx = _ctx(
            workspace_id=workspace_id,
            actor_id=manager_id,
            role="manager",
        )

        with factory() as s:
            repo = SqlAlchemyChatChannelRepository(s)
            service = ChatChannelService(manager_ctx, clock=FrozenClock(_PINNED))
            channel = service.create(
                repo,
                ChatChannelCreate(kind="staff", title="Staff"),
            )
            archived = service.archive(repo, channel.id)
            service.archive(repo, channel.id)
            s.commit()

        with factory() as s:
            row = s.get(ChatChannel, channel.id)
            assert row is not None
            # ``UtcDateTime`` (cd-xma93) returns aware UTC on every dialect.
            assert row.archived_at == _PINNED
            assert archived.archived_at == _PINNED
            actions = s.scalars(
                select(AuditLog.action).order_by(AuditLog.created_at.asc())
            ).all()
            assert actions == [
                "messaging.channel.created",
                "messaging.channel.archived",
            ]

            repo = SqlAlchemyChatChannelRepository(s)
            active = ChatChannelService(
                manager_ctx,
                clock=FrozenClock(_PINNED),
            ).list(repo)
            archived_page = ChatChannelService(
                manager_ctx,
                clock=FrozenClock(_PINNED),
            ).list(repo, include_archived=True)
            assert active == []
            assert [channel.id for channel in archived_page] == [channel.id]
            assert archived_page[0].can_post_messages is False
            with pytest.raises(ChatChannelNotFound):
                ChatChannelService(
                    manager_ctx,
                    clock=FrozenClock(_PINNED),
                ).assert_can_post_message(repo, channel.id)

    def test_add_and_remove_member_are_idempotent(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        workspace_id, manager_id, worker_id = _seed(factory)
        manager_ctx = _ctx(
            workspace_id=workspace_id,
            actor_id=manager_id,
            role="manager",
        )

        with factory() as s:
            repo = SqlAlchemyChatChannelRepository(s)
            service = ChatChannelService(manager_ctx, clock=FrozenClock(_PINNED))
            channel = service.create(
                repo,
                ChatChannelCreate(kind="staff", title="Staff"),
            )

            with pytest.raises(ChatChannelInvalid):
                service.add_member(repo, channel.id, new_ulid())

            service.add_member(repo, channel.id, worker_id)
            service.add_member(repo, channel.id, worker_id)
            count = len(s.scalars(select(ChatChannelMember)).all())
            assert count == 1

            service.remove_member(repo, channel.id, worker_id)
            service.remove_member(repo, channel.id, worker_id)
            count = len(s.scalars(select(ChatChannelMember)).all())
            assert count == 0
