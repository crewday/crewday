"""Domain-seam tests for the :class:`EmailDeliveryRepository` port (cd-8kg7).

Exercises :class:`~app.domain.messaging.notifications.NotificationService`
against an in-memory fake that satisfies the
:class:`~app.domain.messaging.ports.EmailDeliveryRepository` Protocol
surface — no SQLAlchemy involved. Proves the service:

* calls ``insert_queued`` with the spec'd snapshot fields BEFORE
  ``mailer.send`` (so the row exists if the I/O hangs);
* calls ``mark_sent`` with the provider message id after a successful
  send;
* calls ``mark_failed`` with the adapter error after
  :class:`~app.adapters.mail.ports.MailDeliveryError`;
* never touches the repository when the recipient is opted out.

The seam-level test gives the next agent a clear contract to satisfy
when they swap the SA concretion for a different storage backend
(e.g. an outbox writer landing in cd-vtm12 follow-ups).

See ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 and
the project memory entry "Per-context Protocol seams".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import EmailOptOut
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.adapters.mail.ports import MailDeliveryError
from app.domain.messaging.notifications import (
    NotificationKind,
    NotificationService,
)
from app.domain.messaging.ports import EmailDeliveryRow
from app.events.bus import EventBus, bus
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures (mirror the unit-test layout in tests/unit/messaging/)
# ---------------------------------------------------------------------------


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
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture(autouse=True)
def reset_bus() -> Iterator[None]:
    yield
    bus._reset_for_tests()


# ---------------------------------------------------------------------------
# In-memory EmailDeliveryRepository fake
# ---------------------------------------------------------------------------


@dataclass
class InsertQueuedCall:
    delivery_id: str
    workspace_id: str
    to_person_id: str
    to_email_at_send: str
    template_key: str
    context_snapshot_json: dict[str, object]
    created_at: datetime


@dataclass
class MarkSentCall:
    delivery_id: str
    provider_message_id: str
    sent_at: datetime


@dataclass
class MarkFailedCall:
    delivery_id: str
    error_text: str
    now: datetime


class FakeEmailDeliveryRepo:
    """In-memory recorder that satisfies the Protocol structurally."""

    def __init__(self) -> None:
        self.insert_calls: list[InsertQueuedCall] = []
        self.mark_sent_calls: list[MarkSentCall] = []
        self.mark_failed_calls: list[MarkFailedCall] = []
        # Captured rows by delivery_id so the assertion shape mirrors
        # what the SA repo would return.
        self.rows: dict[str, EmailDeliveryRow] = {}

    @property
    def session(self) -> Any:
        # The Protocol surface still exposes ``session`` for the audit
        # writer. The fake does not back any UoW; tests that need a
        # session use the SA concretion instead.
        raise AssertionError("FakeEmailDeliveryRepo.session must not be accessed")

    def insert_queued(
        self,
        *,
        delivery_id: str,
        workspace_id: str,
        to_person_id: str,
        to_email_at_send: str,
        template_key: str,
        context_snapshot_json: dict[str, object],
        created_at: datetime,
    ) -> EmailDeliveryRow:
        call = InsertQueuedCall(
            delivery_id=delivery_id,
            workspace_id=workspace_id,
            to_person_id=to_person_id,
            to_email_at_send=to_email_at_send,
            template_key=template_key,
            context_snapshot_json=dict(context_snapshot_json),
            created_at=created_at,
        )
        self.insert_calls.append(call)
        row = EmailDeliveryRow(
            id=delivery_id,
            workspace_id=workspace_id,
            to_person_id=to_person_id,
            to_email_at_send=to_email_at_send,
            template_key=template_key,
            context_snapshot_json=dict(context_snapshot_json),
            sent_at=None,
            provider_message_id=None,
            delivery_state="queued",
            first_error=None,
            retry_count=0,
            inbound_linkage=None,
            created_at=created_at,
        )
        self.rows[delivery_id] = row
        return row

    def mark_sent(
        self,
        *,
        delivery_id: str,
        provider_message_id: str,
        sent_at: datetime,
    ) -> EmailDeliveryRow:
        self.mark_sent_calls.append(
            MarkSentCall(
                delivery_id=delivery_id,
                provider_message_id=provider_message_id,
                sent_at=sent_at,
            )
        )
        prior = self.rows[delivery_id]
        updated = EmailDeliveryRow(
            id=prior.id,
            workspace_id=prior.workspace_id,
            to_person_id=prior.to_person_id,
            to_email_at_send=prior.to_email_at_send,
            template_key=prior.template_key,
            context_snapshot_json=prior.context_snapshot_json,
            sent_at=sent_at,
            provider_message_id=provider_message_id,
            delivery_state="sent",
            first_error=prior.first_error,
            retry_count=prior.retry_count,
            inbound_linkage=prior.inbound_linkage,
            created_at=prior.created_at,
        )
        self.rows[delivery_id] = updated
        return updated

    def mark_failed(
        self,
        *,
        delivery_id: str,
        error_text: str,
        now: datetime,
    ) -> EmailDeliveryRow:
        self.mark_failed_calls.append(
            MarkFailedCall(
                delivery_id=delivery_id,
                error_text=error_text,
                now=now,
            )
        )
        prior = self.rows[delivery_id]
        first_error = prior.first_error if prior.first_error is not None else error_text
        updated = EmailDeliveryRow(
            id=prior.id,
            workspace_id=prior.workspace_id,
            to_person_id=prior.to_person_id,
            to_email_at_send=prior.to_email_at_send,
            template_key=prior.template_key,
            context_snapshot_json=prior.context_snapshot_json,
            sent_at=prior.sent_at,
            provider_message_id=prior.provider_message_id,
            delivery_state="failed",
            first_error=first_error,
            retry_count=prior.retry_count + 1,
            inbound_linkage=prior.inbound_linkage,
            created_at=prior.created_at,
        )
        self.rows[delivery_id] = updated
        return updated

    def find_by_provider_message_id(
        self,
        *,
        workspace_id: str,
        provider_message_id: str,
    ) -> EmailDeliveryRow | None:
        for row in self.rows.values():
            if (
                row.workspace_id == workspace_id
                and row.provider_message_id == provider_message_id
            ):
                return row
        return None


class _RaisingMailer:
    def __init__(self, *, message: str) -> None:
        self._message = message

    def send(
        self,
        *,
        to: object,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: object = None,
        reply_to: str | None = None,
    ) -> str:
        raise MailDeliveryError(self._message)


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


def _bootstrap_workspace(s: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, email: str, display_name: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=display_name,
            locale=None,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _ctx(*, workspace_id: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="ws",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


@pytest.fixture
def env(session: Session) -> tuple[WorkspaceContext, str, FrozenClock]:
    ws_id = _bootstrap_workspace(session, slug="seam-env")
    recipient_id = _bootstrap_user(
        session, email="recipient@example.com", display_name="R"
    )
    actor_id = _bootstrap_user(session, email="actor@example.com", display_name="A")
    session.commit()
    return (
        _ctx(workspace_id=ws_id, actor_id=actor_id),
        recipient_id,
        FrozenClock(_PINNED),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSeamHappyPath:
    def test_insert_queued_then_mark_sent(
        self,
        session: Session,
        env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, recipient_id, clock = env
        repo = FakeEmailDeliveryRepo()
        mailer = InMemoryMailer()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=EventBus(),
            email_deliveries=repo,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "T"},
        )

        # Insert lands first, exactly once.
        assert len(repo.insert_calls) == 1
        insert = repo.insert_calls[0]
        assert insert.workspace_id == ctx.workspace_id
        assert insert.to_person_id == recipient_id
        assert insert.to_email_at_send == "recipient@example.com"
        assert insert.template_key == "task_assigned"
        assert insert.context_snapshot_json == {"task_title": "T"}

        # Mark sent lands once after the mailer succeeded.
        assert len(repo.mark_sent_calls) == 1
        mark = repo.mark_sent_calls[0]
        assert mark.delivery_id == insert.delivery_id
        assert mark.provider_message_id == "msg-1"
        assert len(repo.mark_failed_calls) == 0


class TestSeamFailurePath:
    def test_mark_failed_called_on_mail_delivery_error(
        self,
        session: Session,
        env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, recipient_id, clock = env
        repo = FakeEmailDeliveryRepo()
        mailer = _RaisingMailer(message="421 try later")
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=EventBus(),
            email_deliveries=repo,
        )
        with pytest.raises(MailDeliveryError):
            service.notify(
                recipient_user_id=recipient_id,
                kind=NotificationKind.TASK_ASSIGNED,
                payload={"task_title": "T"},
            )

        # Insert + mark_failed lands once each; mark_sent never fires.
        assert len(repo.insert_calls) == 1
        assert len(repo.mark_failed_calls) == 1
        assert len(repo.mark_sent_calls) == 0

        failed = repo.mark_failed_calls[0]
        assert failed.delivery_id == repo.insert_calls[0].delivery_id
        assert "421" in failed.error_text


class TestSeamOptOut:
    def test_opt_out_skips_repository_entirely(
        self,
        session: Session,
        env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Opted-out emails never reach the mailer — the seam stays
        untouched (no queued row written for a row that will never be
        sent, per the spec contract)."""
        ctx, recipient_id, clock = env
        session.add(
            EmailOptOut(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                user_id=recipient_id,
                category="task_assigned",
                opted_out_at=_PINNED,
                source="profile",
            )
        )
        session.commit()

        repo = FakeEmailDeliveryRepo()
        mailer = InMemoryMailer()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=EventBus(),
            email_deliveries=repo,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "T"},
        )

        assert mailer.sent == []
        assert repo.insert_calls == []
        assert repo.mark_sent_calls == []
        assert repo.mark_failed_calls == []
