"""Unit tests for :mod:`app.worker.jobs.messaging_web_push` (cd-y60x).

Drives the dispatcher against an in-memory SQLite engine plus a fake
``pywebpush`` sender. Covers:

* Successful 2xx delivery flips queue row to ``sent`` and bumps
  ``push_token.last_used_at``.
* HTTP 410 / 404 deletes the matching :class:`PushToken` row, audits
  ``messaging.push.token_purged``, and marks the queue row ``sent``.
* HTTP 5xx schedules a retry per the §10 backoff schedule.
* Other 4xx (e.g. 403) drops the row to ``dead_lettered`` immediately.
* The retry budget caps at 5 attempts — past that, the row dead-letters.
* Concurrent claims: two workers running against the same row land
  exactly one ``sent`` flip (the second worker's CAS update reports
  rowcount=0 and the duplicate send is skipped).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import NotificationPushQueue, PushToken
from app.adapters.db.messaging.repositories import (
    SqlAlchemyPushDeliveryRepository,
)
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.messaging.ports import PushDeliveryRow
from app.domain.messaging.push_tokens import (
    SETTINGS_KEY_VAPID_PRIVATE,
    SETTINGS_KEY_VAPID_SUBJECT,
)
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.jobs import messaging_web_push as worker_module
from app.worker.jobs.messaging_web_push import (
    BACKOFF_SCHEDULE_SECONDS,
    MAX_ATTEMPTS,
    PushSendOutcome,
    _process_row,
    dispatch_due_pushes,
)

_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _strip_tz(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on round-trip; normalise for comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
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
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


def _bootstrap_workspace(s: Session, *, slug: str, with_vapid: bool = True) -> str:
    settings_json: dict[str, str] = {}
    if with_vapid:
        settings_json[SETTINGS_KEY_VAPID_PRIVATE] = "test-private-key"
        settings_json[SETTINGS_KEY_VAPID_SUBJECT] = "mailto:ops@example.com"
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"WS {slug}",
            plan="free",
            quota_json={},
            settings_json=settings_json,
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, email: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=email.split("@")[0],
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _bootstrap_push_token(
    s: Session,
    *,
    workspace_id: str,
    user_id: str,
    suffix: str = "alpha",
) -> str:
    token_id = new_ulid()
    s.add(
        PushToken(
            id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=f"https://fcm.googleapis.com/fcm/send/{suffix}",
            p256dh="p256dh-test",
            auth="auth-test",
            user_agent="UA",
            created_at=_PINNED,
            last_used_at=None,
        )
    )
    s.flush()
    return token_id


def _bootstrap_notification(
    s: Session, *, workspace_id: str, recipient_user_id: str
) -> str:
    from app.adapters.db.messaging.models import Notification

    notification_id = new_ulid()
    s.add(
        Notification(
            id=notification_id,
            workspace_id=workspace_id,
            recipient_user_id=recipient_user_id,
            kind="task_assigned",
            subject="Task assigned",
            body_md="Body",
            read_at=None,
            created_at=_PINNED,
            payload_json={},
        )
    )
    s.flush()
    return notification_id


def _enqueue(
    s: Session,
    *,
    workspace_id: str,
    notification_id: str,
    push_token_id: str,
    next_attempt_at: datetime | None = None,
    attempt: int = 0,
    status: str = "pending",
) -> str:
    delivery_id = new_ulid()
    s.add(
        NotificationPushQueue(
            id=delivery_id,
            workspace_id=workspace_id,
            notification_id=notification_id,
            push_token_id=push_token_id,
            kind="task_assigned",
            body="Hello there",
            payload_json={"task_id": "T1"},
            status=status,
            attempt=attempt,
            next_attempt_at=next_attempt_at if next_attempt_at else _PINNED,
            last_status_code=None,
            last_error=None,
            last_attempted_at=None,
            sent_at=None,
            dead_lettered_at=None,
            created_at=_PINNED,
        )
    )
    s.flush()
    return delivery_id


@dataclass
class FakeSender:
    """Recorded calls + canned outcomes for the pywebpush seam."""

    outcomes: list[PushSendOutcome] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(
        self,
        delivery: PushDeliveryRow,
        token: worker_module._TokenContext,
        vapid: worker_module._VapidConfig,
    ) -> PushSendOutcome:
        self.calls.append((delivery.id, token.endpoint))
        if not self.outcomes:
            return PushSendOutcome(status_code=200, error=None)
        return self.outcomes.pop(0)


@pytest.fixture(autouse=True)
def _patch_make_uow(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Re-route ``make_uow()`` to the in-memory engine for tests.

    The dispatcher opens its own UoW via :func:`app.adapters.db.session.\
make_uow`. We replace that call with a session factory bound to the
    test engine so every per-row UoW commits to the same database.
    """
    from contextlib import contextmanager

    @contextmanager
    def _fake_make_uow() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(worker_module, "make_uow", _fake_make_uow)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestSuccessPath:
    def test_2xx_marks_sent_and_bumps_last_used_at(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="ok")
        user = _bootstrap_user(session, email="alice@example.com")
        token = _bootstrap_push_token(session, workspace_id=ws, user_id=user)
        notif = _bootstrap_notification(
            session, workspace_id=ws, recipient_user_id=user
        )
        delivery = _enqueue(
            session,
            workspace_id=ws,
            notification_id=notif,
            push_token_id=token,
        )
        session.commit()

        sender = FakeSender(outcomes=[PushSendOutcome(status_code=201, error=None)])
        clock = FrozenClock(_PINNED + timedelta(seconds=1))
        report = dispatch_due_pushes(clock=clock, sender=sender)

        assert report.successes == 1
        assert report.processed_count == 1
        assert report.tokens_purged == 0
        assert sender.calls == [(delivery, "https://fcm.googleapis.com/fcm/send/alpha")]

        session.expire_all()
        row = session.scalars(
            select(NotificationPushQueue).where(NotificationPushQueue.id == delivery)
        ).one()
        assert row.status == "sent"
        assert row.attempt == 1
        assert row.last_status_code == 201
        expected_sent_at = (_PINNED + timedelta(seconds=1)).replace(tzinfo=None)
        assert _strip_tz(row.sent_at) == expected_sent_at

        token_row = session.scalars(
            select(PushToken).where(PushToken.id == token)
        ).one()
        assert _strip_tz(token_row.last_used_at) == expected_sent_at


# ---------------------------------------------------------------------------
# 410/404 token purge
# ---------------------------------------------------------------------------


class TestTokenPurge:
    def test_410_deletes_token_and_audits(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="gone")
        user = _bootstrap_user(session, email="bob@example.com")
        token = _bootstrap_push_token(session, workspace_id=ws, user_id=user)
        notif = _bootstrap_notification(
            session, workspace_id=ws, recipient_user_id=user
        )
        delivery = _enqueue(
            session,
            workspace_id=ws,
            notification_id=notif,
            push_token_id=token,
        )
        session.commit()

        sender = FakeSender(
            outcomes=[PushSendOutcome(status_code=410, error="http_410")]
        )
        report = dispatch_due_pushes(
            clock=FrozenClock(_PINNED + timedelta(seconds=1)),
            sender=sender,
        )
        assert report.tokens_purged == 1
        assert report.successes == 1  # token-purge counts as a clean drop

        session.expire_all()
        # Token deleted.
        assert (
            session.scalars(
                select(PushToken).where(PushToken.id == token)
            ).one_or_none()
            is None
        )
        # Queue row swept away by the push_token CASCADE FK — the
        # earlier mark_sent flip is observed only via the audit ledger
        # (the queue row's lifecycle is bounded by its push_token).
        assert (
            session.scalars(
                select(NotificationPushQueue).where(
                    NotificationPushQueue.id == delivery
                )
            ).one_or_none()
            is None
        )

        # Audit row written with the canonical action.
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "messaging.push.token_purged")
        ).all()
        assert len(audit) == 1
        assert audit[0].entity_kind == "push_token"
        assert audit[0].entity_id == token
        assert audit[0].diff["user_id"] == user
        assert audit[0].diff["delivery_id"] == delivery
        assert audit[0].diff["reason"] == "http_410"

    def test_404_purges_token_too(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="missing")
        user = _bootstrap_user(session, email="carol@example.com")
        token = _bootstrap_push_token(session, workspace_id=ws, user_id=user)
        notif = _bootstrap_notification(
            session, workspace_id=ws, recipient_user_id=user
        )
        _enqueue(session, workspace_id=ws, notification_id=notif, push_token_id=token)
        session.commit()

        sender = FakeSender(
            outcomes=[PushSendOutcome(status_code=404, error="http_404")]
        )
        report = dispatch_due_pushes(
            clock=FrozenClock(_PINNED + timedelta(seconds=1)),
            sender=sender,
        )
        assert report.tokens_purged == 1

        session.expire_all()
        assert (
            session.scalars(
                select(PushToken).where(PushToken.id == token)
            ).one_or_none()
            is None
        )


# ---------------------------------------------------------------------------
# Retry / dead-letter
# ---------------------------------------------------------------------------


class TestRetryAndDeadLetter:
    def test_5xx_schedules_retry_per_backoff_table(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="retry")
        user = _bootstrap_user(session, email="dave@example.com")
        token = _bootstrap_push_token(session, workspace_id=ws, user_id=user)
        notif = _bootstrap_notification(
            session, workspace_id=ws, recipient_user_id=user
        )
        delivery = _enqueue(
            session,
            workspace_id=ws,
            notification_id=notif,
            push_token_id=token,
        )
        session.commit()

        sender = FakeSender(
            outcomes=[PushSendOutcome(status_code=503, error="http_503")]
        )
        now = _PINNED + timedelta(seconds=5)
        dispatch_due_pushes(clock=FrozenClock(now), sender=sender)

        session.expire_all()
        row = session.scalars(
            select(NotificationPushQueue).where(NotificationPushQueue.id == delivery)
        ).one()
        assert row.status == "pending"
        assert row.attempt == 1
        assert row.last_status_code == 503
        assert row.last_error == "http_503"
        expected_next = (now + timedelta(seconds=BACKOFF_SCHEDULE_SECONDS[0])).replace(
            tzinfo=None
        )
        assert _strip_tz(row.next_attempt_at) == expected_next

    def test_4xx_other_than_404_410_dead_letters_immediately(
        self, session: Session
    ) -> None:
        ws = _bootstrap_workspace(session, slug="403")
        user = _bootstrap_user(session, email="erin@example.com")
        token = _bootstrap_push_token(session, workspace_id=ws, user_id=user)
        notif = _bootstrap_notification(
            session, workspace_id=ws, recipient_user_id=user
        )
        delivery = _enqueue(
            session,
            workspace_id=ws,
            notification_id=notif,
            push_token_id=token,
        )
        session.commit()

        sender = FakeSender(
            outcomes=[PushSendOutcome(status_code=403, error="http_403")]
        )
        report = dispatch_due_pushes(
            clock=FrozenClock(_PINNED + timedelta(seconds=1)),
            sender=sender,
        )
        assert report.dead_lettered == 1

        session.expire_all()
        row = session.scalars(
            select(NotificationPushQueue).where(NotificationPushQueue.id == delivery)
        ).one()
        assert row.status == "dead_lettered"
        assert row.last_status_code == 403
        expected_dead = (_PINNED + timedelta(seconds=1)).replace(tzinfo=None)
        assert _strip_tz(row.dead_lettered_at) == expected_dead

        # Token NOT deleted on a non-404/410 4xx.
        assert (
            session.scalars(
                select(PushToken).where(PushToken.id == token)
            ).one_or_none()
            is not None
        )

    def test_drops_after_5_attempts(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="exhaust")
        user = _bootstrap_user(session, email="frank@example.com")
        token = _bootstrap_push_token(session, workspace_id=ws, user_id=user)
        notif = _bootstrap_notification(
            session, workspace_id=ws, recipient_user_id=user
        )
        # Pre-set attempt counter to MAX_ATTEMPTS - 1 so the next try
        # is the 5th and final.
        delivery = _enqueue(
            session,
            workspace_id=ws,
            notification_id=notif,
            push_token_id=token,
            attempt=MAX_ATTEMPTS - 1,
        )
        session.commit()

        sender = FakeSender(
            outcomes=[PushSendOutcome(status_code=502, error="http_502")]
        )
        report = dispatch_due_pushes(
            clock=FrozenClock(_PINNED + timedelta(seconds=1)),
            sender=sender,
        )
        assert report.dead_lettered == 1

        session.expire_all()
        row = session.scalars(
            select(NotificationPushQueue).where(NotificationPushQueue.id == delivery)
        ).one()
        assert row.status == "dead_lettered"
        assert row.attempt == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Locking / two-worker race
# ---------------------------------------------------------------------------


class TestConcurrentClaim:
    def test_two_workers_do_not_double_send(
        self,
        session: Session,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Two workers running against the same row → exactly one CAS wins.

        We simulate the race by calling :func:`_process_row` from two
        different sessions before either commits. The first claim
        flips the status to ``in_flight``; the second's CAS sees a
        non-``pending`` status and exits without invoking the sender.
        """
        ws = _bootstrap_workspace(session, slug="race")
        user = _bootstrap_user(session, email="grace@example.com")
        token = _bootstrap_push_token(session, workspace_id=ws, user_id=user)
        notif = _bootstrap_notification(
            session, workspace_id=ws, recipient_user_id=user
        )
        delivery = _enqueue(
            session,
            workspace_id=ws,
            notification_id=notif,
            push_token_id=token,
        )
        session.commit()

        sender = FakeSender(outcomes=[PushSendOutcome(status_code=200, error=None)])
        now = _PINNED + timedelta(seconds=1)

        # Worker A: claim + finish in its own session.
        worker_a = session_factory()
        repo_a = SqlAlchemyPushDeliveryRepository(worker_a)
        snapshot = repo_a.get(delivery_id=delivery)
        assert snapshot is not None
        outcome_a = _process_row(
            row=snapshot,
            repo=repo_a,
            clock=FrozenClock(now),
            sender=sender,
            now=now,
        )
        worker_a.commit()
        worker_a.close()

        # Worker B: same input row (snapshot from before A committed).
        # The CAS now sees a non-pending row and bails out without
        # firing the sender.
        worker_b = session_factory()
        repo_b = SqlAlchemyPushDeliveryRepository(worker_b)
        outcome_b = _process_row(
            row=snapshot,
            repo=repo_b,
            clock=FrozenClock(now + timedelta(seconds=1)),
            sender=sender,
            now=now + timedelta(seconds=1),
        )
        worker_b.commit()
        worker_b.close()

        assert outcome_a == "sent"
        assert outcome_b == "no_op"
        # Sender invoked exactly once across the two workers.
        assert len(sender.calls) == 1


# ---------------------------------------------------------------------------
# VAPID misconfiguration
# ---------------------------------------------------------------------------


class TestVapidMissing:
    def test_missing_private_key_dead_letters_with_signal(
        self, session: Session
    ) -> None:
        ws = _bootstrap_workspace(session, slug="novapid", with_vapid=False)
        user = _bootstrap_user(session, email="hank@example.com")
        token = _bootstrap_push_token(session, workspace_id=ws, user_id=user)
        notif = _bootstrap_notification(
            session, workspace_id=ws, recipient_user_id=user
        )
        delivery = _enqueue(
            session,
            workspace_id=ws,
            notification_id=notif,
            push_token_id=token,
        )
        session.commit()

        sender = FakeSender()
        report = dispatch_due_pushes(
            clock=FrozenClock(_PINNED + timedelta(seconds=1)),
            sender=sender,
        )
        assert report.dead_lettered == 1
        # Sender never reached.
        assert sender.calls == []

        session.expire_all()
        row = session.scalars(
            select(NotificationPushQueue).where(NotificationPushQueue.id == delivery)
        ).one()
        assert row.status == "dead_lettered"
        assert row.last_error == "vapid_missing"
