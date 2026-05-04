"""Focused tests for the ``email_delivery`` retry worker (cd-4dp0f)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.messaging.models import EmailDelivery
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.adapters.mail.ports import MailDeliveryError
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks import email_delivery_retry as retry_module
from app.worker.tasks.email_delivery_retry import (
    BACKOFF_SCHEDULE_SECONDS,
    MAX_ATTEMPTS,
    EmailDeliveryRetryTask,
)
from tests._fakes.mailer import InMemoryMailer

_PINNED = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)


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
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


class _RaisingMailer:
    def __init__(self, *, message: str = "smtp 421 transient") -> None:
        self.message = message
        self.send_calls = 0

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
        del to, subject, body_text, body_html, headers, reply_to
        self.send_calls += 1
        raise MailDeliveryError(self.message)


def _workspace(session: Session, *, slug: str = "retry") -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _delivery(
    session: Session,
    *,
    workspace_id: str,
    state: str = "failed",
    retry_count: int = 1,
    created_at: datetime | None = None,
    first_error: str | None = "first error",
    context: dict[str, object] | None = None,
) -> str:
    delivery_id = new_ulid()
    session.add(
        EmailDelivery(
            id=delivery_id,
            workspace_id=workspace_id,
            to_person_id=f"person-{delivery_id}",
            to_email_at_send="recipient@example.com",
            template_key="task_assigned",
            context_snapshot_json=context or {"task_title": "Retry room"},
            sent_at=None,
            provider_message_id=None,
            delivery_state=state,
            first_error=first_error,
            retry_count=retry_count,
            inbound_linkage=None,
            created_at=created_at or _PINNED,
        )
    )
    session.flush()
    return delivery_id


def _row(session: Session, delivery_id: str) -> EmailDelivery:
    return session.scalars(
        select(EmailDelivery).where(EmailDelivery.id == delivery_id)
    ).one()


def _audit_actions(session: Session, delivery_id: str) -> list[str]:
    return list(
        session.scalars(
            select(AuditLog.action)
            .where(AuditLog.entity_id == delivery_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        )
    )


def test_select_due_for_retry_honours_state_workspace_and_backoff(
    session: Session,
) -> None:
    ws_a = _workspace(session, slug="a")
    ws_b = _workspace(session, slug="b")
    due_queued = _delivery(
        session,
        workspace_id=ws_a,
        state="queued",
        retry_count=0,
        first_error=None,
    )
    due_failed = _delivery(
        session,
        workspace_id=ws_a,
        retry_count=1,
        created_at=_PINNED - timedelta(seconds=31),
    )
    not_due = _delivery(
        session,
        workspace_id=ws_a,
        retry_count=1,
        created_at=_PINNED - timedelta(seconds=29),
    )
    other_workspace = _delivery(
        session,
        workspace_id=ws_b,
        retry_count=1,
        created_at=_PINNED - timedelta(seconds=31),
    )
    sent = _delivery(session, workspace_id=ws_a, state="sent", retry_count=0)
    _row(session, sent).sent_at = _PINNED
    _row(session, sent).provider_message_id = "msg-sent"
    session.flush()

    repo = SqlAlchemyEmailDeliveryRepository(session)
    rows = repo.select_due_for_retry(
        workspace_id=ws_a,
        now=_PINNED,
        backoff_schedule_seconds=BACKOFF_SCHEDULE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        limit=10,
    )

    assert [row.id for row in rows] == [due_failed, due_queued]
    assert not_due not in {row.id for row in rows}
    assert other_workspace not in {row.id for row in rows}
    assert sent not in {row.id for row in rows}


def test_retry_success_rerenders_snapshot_marks_sent_and_audits(
    session: Session,
) -> None:
    workspace_id = _workspace(session)
    delivery_id = _delivery(
        session,
        workspace_id=workspace_id,
        retry_count=1,
        created_at=_PINNED - timedelta(seconds=31),
        context={"task_title": "Retry linen", "recipient_display_name": "Ada"},
    )
    mailer = InMemoryMailer()

    report = EmailDeliveryRetryTask(
        session=session,
        mailer=mailer,
        clock=FrozenClock(_PINNED),
    ).run()
    session.flush()

    row = _row(session, delivery_id)
    assert report.sent == 1
    assert row.delivery_state == "sent"
    assert row.provider_message_id == "msg-1"
    assert row.retry_count == 1
    assert mailer.sent[0].subject == "Task assigned: Retry linen"
    assert "Retry linen" in mailer.sent[0].body_text
    assert _audit_actions(session, delivery_id) == ["messaging.email_delivery.retry"]


def test_retry_failure_marks_failed_increments_and_preserves_first_error(
    session: Session,
) -> None:
    workspace_id = _workspace(session)
    delivery_id = _delivery(
        session,
        workspace_id=workspace_id,
        retry_count=1,
        created_at=_PINNED - timedelta(seconds=31),
        first_error="original smtp failure",
    )
    mailer = _RaisingMailer(message="smtp 421 retry later")

    report = EmailDeliveryRetryTask(
        session=session,
        mailer=mailer,
        clock=FrozenClock(_PINNED),
    ).run()
    session.flush()

    row = _row(session, delivery_id)
    assert report.failed == 1
    assert mailer.send_calls == 1
    assert row.delivery_state == "failed"
    assert row.retry_count == 2
    assert row.first_error == "original smtp failure"
    assert _audit_actions(session, delivery_id) == ["messaging.email_delivery.retry"]


def test_retry_failure_at_budget_dead_letters_with_audit(
    session: Session,
) -> None:
    workspace_id = _workspace(session)
    delivery_id = _delivery(
        session,
        workspace_id=workspace_id,
        retry_count=MAX_ATTEMPTS - 1,
        created_at=_PINNED - timedelta(hours=2),
    )

    report = EmailDeliveryRetryTask(
        session=session,
        mailer=_RaisingMailer(message="smtp still down"),
        clock=FrozenClock(_PINNED),
    ).run()
    session.flush()

    row = _row(session, delivery_id)
    assert report.dead_lettered == 1
    assert row.delivery_state == "failed"
    assert row.retry_count == MAX_ATTEMPTS
    assert _audit_actions(session, delivery_id) == [
        "messaging.email_delivery.retry",
        "messaging.email_delivery.dead_lettered",
    ]


def test_retry_stale_budget_failure_does_not_increment_or_duplicate_audit(
    session: Session,
) -> None:
    workspace_id = _workspace(session)
    delivery_id = _delivery(
        session,
        workspace_id=workspace_id,
        retry_count=MAX_ATTEMPTS - 1,
        created_at=_PINNED - timedelta(hours=2),
    )
    repo = SqlAlchemyEmailDeliveryRepository(session)

    first = repo.mark_retry_failed(
        delivery_id=delivery_id,
        expected_retry_count=MAX_ATTEMPTS - 1,
        error_text="first terminal failure",
        now=_PINNED,
        max_attempts=MAX_ATTEMPTS,
    )
    second = repo.mark_retry_failed(
        delivery_id=delivery_id,
        expected_retry_count=MAX_ATTEMPTS - 1,
        error_text="stale terminal failure",
        now=_PINNED,
        max_attempts=MAX_ATTEMPTS,
    )

    row = _row(session, delivery_id)
    assert first is not None
    assert second is None
    assert row.retry_count == MAX_ATTEMPTS
    assert row.first_error == "first error"


def test_retry_stale_success_does_not_overwrite_sent_row(
    session: Session,
) -> None:
    workspace_id = _workspace(session)
    delivery_id = _delivery(
        session,
        workspace_id=workspace_id,
        retry_count=1,
        created_at=_PINNED - timedelta(seconds=31),
    )
    repo = SqlAlchemyEmailDeliveryRepository(session)

    first = repo.mark_retry_sent(
        delivery_id=delivery_id,
        expected_retry_count=1,
        provider_message_id="msg-first",
        sent_at=_PINNED,
    )
    second = repo.mark_retry_sent(
        delivery_id=delivery_id,
        expected_retry_count=1,
        provider_message_id="msg-stale",
        sent_at=_PINNED + timedelta(seconds=1),
    )

    row = _row(session, delivery_id)
    assert first is not None
    assert second is None
    assert row.delivery_state == "sent"
    assert row.provider_message_id == "msg-first"
    assert row.sent_at == _PINNED


def test_retry_row_error_rolls_back_only_that_delivery(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace(session)
    first_id = _delivery(
        session,
        workspace_id=workspace_id,
        retry_count=1,
        created_at=_PINNED - timedelta(seconds=32),
        context={"task_title": "First"},
    )
    second_id = _delivery(
        session,
        workspace_id=workspace_id,
        state="queued",
        retry_count=0,
        first_error=None,
        created_at=_PINNED - timedelta(seconds=31),
        context={"task_title": "Second"},
    )
    original_write_retry_audit = retry_module._write_retry_audit

    def _raise_for_second(*args: object, **kwargs: object) -> None:
        row = kwargs.get("row")
        if getattr(row, "id", None) == second_id:
            raise RuntimeError("audit insert failed")
        original_write_retry_audit(*args, **kwargs)

    monkeypatch.setattr(retry_module, "_write_retry_audit", _raise_for_second)

    report = EmailDeliveryRetryTask(
        session=session,
        mailer=InMemoryMailer(),
        clock=FrozenClock(_PINNED),
    ).run()
    session.flush()

    assert report.attempted == 2
    assert report.sent == 1
    assert report.failed == 1
    assert _row(session, first_id).delivery_state == "sent"
    assert _row(session, second_id).delivery_state == "queued"
    assert _row(session, second_id).provider_message_id is None


def test_mark_failed_uses_atomic_increment_across_sessions(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as seed:
        workspace_id = _workspace(seed)
        delivery_id = _delivery(
            seed,
            workspace_id=workspace_id,
            state="queued",
            retry_count=0,
            first_error=None,
        )
        seed.commit()

    with session_factory() as s1, session_factory() as s2:
        s1.get(EmailDelivery, delivery_id)
        s2.get(EmailDelivery, delivery_id)
        SqlAlchemyEmailDeliveryRepository(s1).mark_failed(
            delivery_id=delivery_id,
            error_text="first",
            now=_PINNED,
        )
        s1.commit()
        SqlAlchemyEmailDeliveryRepository(s2).mark_failed(
            delivery_id=delivery_id,
            error_text="second",
            now=_PINNED,
        )
        s2.commit()

    with session_factory() as verify:
        row = _row(verify, delivery_id)
        assert row.retry_count == 2
        assert row.first_error == "first"
