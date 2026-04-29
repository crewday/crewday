"""Unit tests for :mod:`app.domain.integrations.webhooks` (cd-q885).

The dispatcher's HTTP wire bytes + retry-row state machine are
covered here against an in-memory repository fake and an
:class:`httpx.MockTransport`. The integration test under
``tests/integration/integrations/test_webhooks_delivery.py``
exercises the full cipher + SA repository + real HTTP listener
chain.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.domain.integrations.ports import (
    WebhookDeliveryRow,
    WebhookSubscriptionRow,
)
from app.domain.integrations.webhooks import (
    DELIVERY_DEAD_LETTERED,
    DELIVERY_PENDING,
    DELIVERY_SUCCEEDED,
    RETRY_SCHEDULE_SECONDS,
    SIGNATURE_HEADER,
    SUBSCRIPTION_SECRET_PURPOSE,
    create_subscription,
    delete_subscription,
    deliver,
    enqueue,
    list_subscriptions,
    replay_delivery,
    rotate_subscription_secret,
    sign,
    update_subscription,
    verify,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.envelope import FakeEnvelope

_PINNED = datetime(2026, 4, 26, 18, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory repo fake
# ---------------------------------------------------------------------------


@dataclass
class _InMemoryWebhookRepo:
    """Structural :class:`WebhookRepository` fake — no SQL, no flush.

    Implements every method on the Protocol with simple dict storage.
    Tests assert the repository state directly.
    """

    subscriptions: dict[str, WebhookSubscriptionRow] = field(default_factory=dict)
    deliveries: dict[str, WebhookDeliveryRow] = field(default_factory=dict)

    def insert_subscription(
        self,
        *,
        sub_id: str,
        workspace_id: str,
        name: str,
        url: str,
        secret_blob: str,
        secret_last_4: str,
        events: Iterable[str],
        active: bool,
        created_at: datetime,
    ) -> WebhookSubscriptionRow:
        row = WebhookSubscriptionRow(
            id=sub_id,
            workspace_id=workspace_id,
            name=name,
            url=url,
            secret_blob=secret_blob,
            secret_last_4=secret_last_4,
            events=tuple(events),
            active=active,
            created_at=created_at,
            updated_at=created_at,
        )
        self.subscriptions[sub_id] = row
        return row

    def update_subscription(
        self,
        *,
        sub_id: str,
        name: str | None = None,
        url: str | None = None,
        events: Iterable[str] | None = None,
        active: bool | None = None,
        updated_at: datetime,
    ) -> WebhookSubscriptionRow:
        existing = self.subscriptions[sub_id]
        new_events: tuple[str, ...] = (
            tuple(events) if events is not None else existing.events
        )
        new_row = WebhookSubscriptionRow(
            id=existing.id,
            workspace_id=existing.workspace_id,
            name=name if name is not None else existing.name,
            url=url if url is not None else existing.url,
            secret_blob=existing.secret_blob,
            secret_last_4=existing.secret_last_4,
            events=new_events,
            active=active if active is not None else existing.active,
            created_at=existing.created_at,
            updated_at=updated_at,
        )
        self.subscriptions[sub_id] = new_row
        return new_row

    def rotate_subscription_secret(
        self,
        *,
        sub_id: str,
        secret_blob: str,
        secret_last_4: str,
        updated_at: datetime,
    ) -> WebhookSubscriptionRow:
        existing = self.subscriptions[sub_id]
        new_row = WebhookSubscriptionRow(
            id=existing.id,
            workspace_id=existing.workspace_id,
            name=existing.name,
            url=existing.url,
            secret_blob=secret_blob,
            secret_last_4=secret_last_4,
            events=existing.events,
            active=existing.active,
            created_at=existing.created_at,
            updated_at=updated_at,
        )
        self.subscriptions[sub_id] = new_row
        return new_row

    def delete_subscription(self, *, sub_id: str) -> None:
        self.subscriptions.pop(sub_id, None)
        # Cascade — drop matching delivery rows.
        for delivery_id in [
            d.id for d in self.deliveries.values() if d.subscription_id == sub_id
        ]:
            self.deliveries.pop(delivery_id, None)

    def get_subscription(self, *, sub_id: str) -> WebhookSubscriptionRow | None:
        return self.subscriptions.get(sub_id)

    def list_subscriptions(
        self,
        *,
        workspace_id: str,
        active_only: bool = False,
    ) -> tuple[WebhookSubscriptionRow, ...]:
        rows = [
            r for r in self.subscriptions.values() if r.workspace_id == workspace_id
        ]
        if active_only:
            rows = [r for r in rows if r.active]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return tuple(rows)

    def insert_delivery(
        self,
        *,
        delivery_id: str,
        workspace_id: str,
        subscription_id: str,
        event: str,
        payload_json: dict[str, Any],
        status: str,
        attempt: int,
        next_attempt_at: datetime | None,
        replayed_from_id: str | None,
        created_at: datetime,
    ) -> WebhookDeliveryRow:
        row = WebhookDeliveryRow(
            id=delivery_id,
            workspace_id=workspace_id,
            subscription_id=subscription_id,
            event=event,
            payload_json=dict(payload_json),
            status=status,
            attempt=attempt,
            next_attempt_at=next_attempt_at,
            last_status_code=None,
            last_error=None,
            last_attempted_at=None,
            succeeded_at=None,
            dead_lettered_at=None,
            replayed_from_id=replayed_from_id,
            created_at=created_at,
        )
        self.deliveries[delivery_id] = row
        return row

    def get_delivery(self, *, delivery_id: str) -> WebhookDeliveryRow | None:
        return self.deliveries.get(delivery_id)

    def update_delivery_attempt(
        self,
        *,
        delivery_id: str,
        status: str,
        attempt: int,
        next_attempt_at: datetime | None,
        last_status_code: int | None,
        last_error: str | None,
        last_attempted_at: datetime,
        succeeded_at: datetime | None = None,
        dead_lettered_at: datetime | None = None,
    ) -> WebhookDeliveryRow:
        existing = self.deliveries[delivery_id]
        new_row = WebhookDeliveryRow(
            id=existing.id,
            workspace_id=existing.workspace_id,
            subscription_id=existing.subscription_id,
            event=existing.event,
            payload_json=existing.payload_json,
            status=status,
            attempt=attempt,
            next_attempt_at=next_attempt_at,
            last_status_code=last_status_code,
            last_error=last_error,
            last_attempted_at=last_attempted_at,
            succeeded_at=succeeded_at
            if succeeded_at is not None
            else existing.succeeded_at,
            dead_lettered_at=dead_lettered_at
            if dead_lettered_at is not None
            else existing.dead_lettered_at,
            replayed_from_id=existing.replayed_from_id,
            created_at=existing.created_at,
        )
        self.deliveries[delivery_id] = new_row
        return new_row


class _SessionStub:
    """Cheap stand-in for :class:`Session` — :func:`write_audit` only adds.

    The audit writer calls ``session.add(row)`` and the test never
    flushes; we record what landed for assertion.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: object) -> None:
        self.added.append(row)


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id="01HWA00000000000000000WSP1",
        workspace_slug="webhooks",
        actor_id="01HWA00000000000000000USR1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


class TestSignAndVerify:
    def test_sign_round_trip(self) -> None:
        secret = b"secret-bytes-32-chars-long-enough"
        body = b'{"hello":"world"}'
        t = 1_700_000_000
        header = sign(body, secret, t)
        assert header.startswith(f"t={t},v1=")
        assert verify(header, body, secret, now_unix=t) is True

    def test_verify_fails_on_body_tamper(self) -> None:
        secret = b"secret-bytes-32-chars-long-enough"
        t = 1_700_000_000
        header = sign(b"original", secret, t)
        assert verify(header, b"tampered", secret, now_unix=t) is False

    def test_verify_fails_on_wrong_secret(self) -> None:
        t = 1_700_000_000
        header = sign(b"body", b"secret-A-bytes-32-chars-long-en", t)
        assert (
            verify(header, b"body", b"secret-B-bytes-32-chars-long-en", now_unix=t)
            is False
        )

    def test_verify_rejects_replay_outside_window(self) -> None:
        secret = b"secret-bytes-32-chars-long-enough"
        t = 1_700_000_000
        header = sign(b"body", secret, t)
        # 10 minutes after the signature was minted → outside the
        # default 5-minute window.
        assert (
            verify(header, b"body", secret, tolerance_s=300, now_unix=t + 600) is False
        )
        # Within window: still good.
        assert verify(header, b"body", secret, tolerance_s=300, now_unix=t + 60) is True

    def test_verify_handles_malformed_header(self) -> None:
        secret = b"secret-bytes-32-chars-long-enough"
        for header in (
            "",
            "t=garbage,v1=deadbeef",
            "v1=onlyhex",
            "t=123,v2=newscheme",
            "t=123,v1=NOT-HEX",
            "t=123,v1=,extra=oops",
        ):
            assert verify(header, b"body", secret, now_unix=1_700_000_000) is False, (
                header
            )

    def test_sign_constant_time_compare_safe(self) -> None:
        """Two distinct inputs must not produce equal signatures."""
        secret = b"secret-bytes-32-chars-long-enough"
        h1 = sign(b"body-A", secret, 1)
        h2 = sign(b"body-B", secret, 1)
        assert h1 != h2


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------


class TestSubscriptionCrud:
    def test_create_returns_plaintext_secret_only_once(self) -> None:
        repo = _InMemoryWebhookRepo()
        envelope = FakeEnvelope()
        clock = FrozenClock(_PINNED)
        session = _SessionStub()

        view = create_subscription(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            envelope=envelope,
            name="Hermes",
            url="https://hermes.example.com/hook",
            events=["task.completed"],
            clock=clock,
        )
        assert view.plaintext_secret is not None
        assert len(view.plaintext_secret) >= 32
        assert view.secret_last_4 == view.plaintext_secret[-4:]
        # An audit row landed.
        assert len(session.added) == 1

        # Subsequent list reads do not surface plaintext.
        listed = list_subscriptions(_ctx(), repo=repo)
        assert len(listed) == 1
        assert listed[0].plaintext_secret is None
        assert listed[0].secret_last_4 == view.secret_last_4

    def test_create_rejects_invalid_inputs(self) -> None:
        repo = _InMemoryWebhookRepo()
        envelope = FakeEnvelope()
        session = _SessionStub()

        with pytest.raises(ValueError, match="non-blank"):
            create_subscription(
                session,  # type: ignore[arg-type]
                _ctx(),
                repo=repo,
                envelope=envelope,
                name="",
                url="https://x.example.com",
                events=["task.completed"],
            )
        with pytest.raises(ValueError, match="http"):
            create_subscription(
                session,  # type: ignore[arg-type]
                _ctx(),
                repo=repo,
                envelope=envelope,
                name="Hermes",
                url="ftp://nope",
                events=["task.completed"],
            )
        with pytest.raises(ValueError, match="at least one event"):
            create_subscription(
                session,  # type: ignore[arg-type]
                _ctx(),
                repo=repo,
                envelope=envelope,
                name="Hermes",
                url="https://x.example.com",
                events=[],
            )
        with pytest.raises(ValueError, match="non-blank"):
            create_subscription(
                session,  # type: ignore[arg-type]
                _ctx(),
                repo=repo,
                envelope=envelope,
                name="Hermes",
                url="https://x.example.com",
                events=[""],
            )
        with pytest.raises(ValueError, match="at least 16"):
            create_subscription(
                session,  # type: ignore[arg-type]
                _ctx(),
                repo=repo,
                envelope=envelope,
                name="Hermes",
                url="https://x.example.com",
                events=["task.completed"],
                secret="too-short",
            )

    def test_update_audits_only_changed_fields(self) -> None:
        repo = _InMemoryWebhookRepo()
        envelope = FakeEnvelope()
        clock = FrozenClock(_PINNED)
        session = _SessionStub()

        view = create_subscription(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            envelope=envelope,
            name="Hermes",
            url="https://hermes.example.com/hook",
            events=["task.completed"],
            clock=clock,
        )
        # Reset audit log — the create write doesn't matter here.
        session.added.clear()

        # No-op patch: same values, no audit row.
        update_subscription(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            sub_id=view.id,
            name="Hermes",
            clock=clock,
        )
        assert session.added == []

        # Real patch: name changes; one audit row.
        clock.advance(timedelta(seconds=1))
        updated = update_subscription(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            sub_id=view.id,
            name="Hermes-2",
            clock=clock,
        )
        assert updated.name == "Hermes-2"
        assert len(session.added) == 1

    def test_delete_removes_subscription(self) -> None:
        repo = _InMemoryWebhookRepo()
        envelope = FakeEnvelope()
        clock = FrozenClock(_PINNED)
        session = _SessionStub()

        view = create_subscription(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            envelope=envelope,
            name="Hermes",
            url="https://hermes.example.com/hook",
            events=["task.completed"],
            clock=clock,
        )
        delete_subscription(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            sub_id=view.id,
            clock=clock,
        )
        assert repo.subscriptions == {}

    def test_rotate_secret_returns_plaintext_once_and_audits(self) -> None:
        repo = _InMemoryWebhookRepo()
        envelope = FakeEnvelope()
        clock = FrozenClock(_PINNED)
        session = _SessionStub()
        view = create_subscription(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            envelope=envelope,
            name="Hermes",
            url="https://hermes.example.com/hook",
            events=["task.completed"],
            secret="0123456789abcdef",
            clock=clock,
        )
        session.added.clear()
        clock.advance(timedelta(seconds=1))

        rotated = rotate_subscription_secret(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            envelope=envelope,
            sub_id=view.id,
            secret="fedcba9876543210",
            clock=clock,
        )

        assert rotated.plaintext_secret == "fedcba9876543210"
        assert rotated.secret_last_4 == "3210"
        assert (
            envelope.decrypt(
                repo.subscriptions[view.id].secret_blob.encode("latin-1"),
                purpose=SUBSCRIPTION_SECRET_PURPOSE,
            )
            == b"fedcba9876543210"
        )
        assert len(session.added) == 1

    def test_update_refuses_cross_workspace(self) -> None:
        repo = _InMemoryWebhookRepo()
        envelope = FakeEnvelope()
        clock = FrozenClock(_PINNED)
        session = _SessionStub()
        view = create_subscription(
            session,  # type: ignore[arg-type]
            _ctx(),
            repo=repo,
            envelope=envelope,
            name="Hermes",
            url="https://hermes.example.com/hook",
            events=["task.completed"],
            clock=clock,
        )

        # Caller from a different workspace tries to touch the row.
        other_ctx = WorkspaceContext(
            workspace_id="01HWA00000000000000000WSP2",
            workspace_slug="other",
            actor_id="01HWA00000000000000000USR2",
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id="01HWA00000000000000000CRL2",
        )
        with pytest.raises(LookupError):
            update_subscription(
                session,  # type: ignore[arg-type]
                other_ctx,
                repo=repo,
                sub_id=view.id,
                name="hijacked",
            )


# ---------------------------------------------------------------------------
# Enqueue + delivery flow
# ---------------------------------------------------------------------------


def _seed_subscription(
    repo: _InMemoryWebhookRepo,
    *,
    workspace_id: str = "01HWA00000000000000000WSP1",
    events: tuple[str, ...] = ("task.completed",),
    url: str = "https://hermes.example.com/hook",
    secret_plaintext: str = "secret-bytes-32-chars-long-enough",
) -> WebhookSubscriptionRow:
    sub_id = new_ulid()
    envelope = FakeEnvelope()
    blob = envelope.encrypt(
        secret_plaintext.encode("utf-8"),
        purpose=SUBSCRIPTION_SECRET_PURPOSE,
        owner=None,
    )
    return repo.insert_subscription(
        sub_id=sub_id,
        workspace_id=workspace_id,
        name="hermes",
        url=url,
        secret_blob=blob.decode("latin-1"),
        secret_last_4=secret_plaintext[-4:],
        events=events,
        active=True,
        created_at=_PINNED,
    )


class TestEnqueueAndDeliver:
    def test_enqueue_fans_out_to_active_matching_subscriptions(self) -> None:
        repo = _InMemoryWebhookRepo()
        a = _seed_subscription(repo, events=("task.completed", "approval.pending"))
        b = _seed_subscription(repo, events=("task.completed",))
        c = _seed_subscription(repo, events=("stay.upcoming",))
        # Inactive subscription must not be picked up.
        repo.insert_subscription(
            sub_id=new_ulid(),
            workspace_id=a.workspace_id,
            name="off",
            url="https://off.example.com",
            secret_blob="x" * 27,
            secret_last_4="xxxx",
            events=("task.completed",),
            active=False,
            created_at=_PINNED,
        )

        ids = enqueue(
            repo=repo,
            workspace_id=a.workspace_id,
            event="task.completed",
            data={"task_id": "T1"},
            clock=FrozenClock(_PINNED),
        )
        assert len(ids) == 2
        targeted = {repo.deliveries[d].subscription_id for d in ids}
        assert targeted == {a.id, b.id}
        assert c.id not in targeted

    def test_enqueue_with_subscription_filter(self) -> None:
        repo = _InMemoryWebhookRepo()
        a = _seed_subscription(repo)
        b = _seed_subscription(repo)
        ids = enqueue(
            repo=repo,
            workspace_id=a.workspace_id,
            event="task.completed",
            data={},
            subscription_id=a.id,
            clock=FrozenClock(_PINNED),
        )
        assert len(ids) == 1
        assert repo.deliveries[ids[0]].subscription_id == a.id
        assert b.id not in [repo.deliveries[d].subscription_id for d in ids]

    def test_2xx_marks_succeeded(self) -> None:
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={"task_id": "T1"},
            clock=FrozenClock(_PINNED),
        )
        delivery_id = ids[0]

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            session = _SessionStub()
            report = deliver(
                session,  # type: ignore[arg-type]
                delivery_id=delivery_id,
                repo=repo,
                envelope=FakeEnvelope(),
                http=client,
                clock=FrozenClock(_PINNED),
            )
        finally:
            client.close()

        assert report.dead_lettered is False
        assert report.status == DELIVERY_SUCCEEDED
        row = repo.deliveries[delivery_id]
        assert row.status == DELIVERY_SUCCEEDED
        assert row.last_status_code == 200
        assert row.next_attempt_at is None
        assert row.succeeded_at is not None
        # Successful deliveries do NOT audit.
        assert session.added == []
        # The signed header rode the wire.
        assert SIGNATURE_HEADER in captured[0].headers
        assert captured[0].headers["X-Crewday-Event"] == "task.completed"
        assert captured[0].headers["X-Crewday-Delivery"] == delivery_id

    def test_500_then_500_then_2xx_succeeds_on_third_attempt(self) -> None:
        """A 500 from the receiver triggers the retry schedule;
        delivery succeeds on the 3rd attempt."""
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={"task_id": "T1"},
            clock=clock,
        )
        delivery_id = ids[0]
        # Sequence: 500 → 500 → 200.
        responses = iter([500, 500, 200])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(next(responses))

        client = httpx.Client(transport=httpx.MockTransport(handler))
        session = _SessionStub()
        try:
            for _ in range(3):
                deliver(
                    session,  # type: ignore[arg-type]
                    delivery_id=delivery_id,
                    repo=repo,
                    envelope=FakeEnvelope(),
                    http=client,
                    clock=clock,
                )
                clock.advance(timedelta(seconds=60))
        finally:
            client.close()

        row = repo.deliveries[delivery_id]
        assert row.status == DELIVERY_SUCCEEDED
        # 3 attempts fired (attempts 1..3).
        assert row.attempt == 3
        assert row.last_status_code == 200
        assert row.last_error is None

    def test_six_failures_dead_letter(self) -> None:
        """Six failures dead-letter the row + audit
        ``audit.webhook_delivery.dead_lettered``."""
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={},
            clock=clock,
        )
        delivery_id = ids[0]

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        session = _SessionStub()
        try:
            for _ in range(len(RETRY_SCHEDULE_SECONDS)):
                deliver(
                    session,  # type: ignore[arg-type]
                    delivery_id=delivery_id,
                    repo=repo,
                    envelope=FakeEnvelope(),
                    http=client,
                    clock=clock,
                )
                clock.advance(timedelta(days=2))  # outside the schedule
        finally:
            client.close()

        row = repo.deliveries[delivery_id]
        assert row.status == DELIVERY_DEAD_LETTERED
        assert row.attempt == len(RETRY_SCHEDULE_SECONDS)
        assert row.dead_lettered_at is not None
        # Audit row landed for the dead-letter.
        assert len(session.added) == 1
        audit_row = session.added[0]
        assert audit_row.action == "dead_lettered"
        assert audit_row.entity_kind == "webhook_delivery"

    def test_400_dead_letters_immediately(self) -> None:
        """A 400 (non-408 / 429) dead-letters immediately (no retries)."""
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={},
            clock=clock,
        )
        delivery_id = ids[0]

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        session = _SessionStub()
        try:
            report = deliver(
                session,  # type: ignore[arg-type]
                delivery_id=delivery_id,
                repo=repo,
                envelope=FakeEnvelope(),
                http=client,
                clock=clock,
            )
        finally:
            client.close()

        assert report.dead_lettered is True
        row = repo.deliveries[delivery_id]
        assert row.status == DELIVERY_DEAD_LETTERED
        # Attempt counter recorded the single fired attempt.
        assert row.attempt == 1
        assert row.last_status_code == 400
        # Audit landed.
        assert len(session.added) == 1

    def test_408_and_429_walk_retry_schedule(self) -> None:
        """408 and 429 are transient — they walk the schedule,
        not dead-letter immediately."""
        for retryable in (408, 429):
            repo = _InMemoryWebhookRepo()
            sub = _seed_subscription(repo)
            clock = FrozenClock(_PINNED)
            ids = enqueue(
                repo=repo,
                workspace_id=sub.workspace_id,
                event="task.completed",
                data={},
                clock=clock,
            )
            delivery_id = ids[0]

            def handler(_req: httpx.Request, code: int = retryable) -> httpx.Response:
                return httpx.Response(code)

            client = httpx.Client(transport=httpx.MockTransport(handler))
            session = _SessionStub()
            try:
                report = deliver(
                    session,  # type: ignore[arg-type]
                    delivery_id=delivery_id,
                    repo=repo,
                    envelope=FakeEnvelope(),
                    http=client,
                    clock=clock,
                )
            finally:
                client.close()

            assert report.dead_lettered is False, retryable
            row = repo.deliveries[delivery_id]
            assert row.status == DELIVERY_PENDING
            assert row.next_attempt_at is not None
            # Audit is not written for transient retries.
            assert session.added == []

    def test_replay_mints_new_delivery_with_fresh_timestamp(self) -> None:
        """Replay re-attempts the same payload with a new signature
        timestamp."""
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={"task_id": "T1"},
            clock=clock,
        )
        original_id = ids[0]
        # Pretend the original dead-lettered.
        repo.update_delivery_attempt(
            delivery_id=original_id,
            status=DELIVERY_DEAD_LETTERED,
            attempt=6,
            next_attempt_at=None,
            last_status_code=500,
            last_error="http_500",
            last_attempted_at=clock.now(),
            dead_lettered_at=clock.now(),
        )

        # Roll the clock forward and replay.
        clock.advance(timedelta(hours=1))
        new_id = replay_delivery(
            _ctx(), repo=repo, delivery_id=original_id, clock=clock
        )
        assert new_id != original_id
        new_row = repo.deliveries[new_id]
        assert new_row.replayed_from_id == original_id
        assert new_row.status == DELIVERY_PENDING
        assert new_row.attempt == 0
        # Same data, fresh delivery_id + delivered_at.
        assert new_row.payload_json["data"] == {"task_id": "T1"}
        assert new_row.payload_json["delivery_id"] == new_id
        assert (
            new_row.payload_json["delivered_at"]
            != repo.deliveries[original_id].payload_json["delivered_at"]
        )

    def test_replay_refuses_cross_workspace(self) -> None:
        """A caller from workspace B cannot mint a replay for workspace A.

        ``get_delivery`` runs cross-tenant (the worker tick has no
        ambient WorkspaceContext); the boundary check lands at the
        service layer. Without it, a manager in workspace B who
        guesses or harvests a delivery_id from workspace A could mint
        a fresh delivery in workspace A's queue.
        """
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)  # workspace A
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={"task_id": "T1"},
            clock=FrozenClock(_PINNED),
        )
        original_id = ids[0]

        # Caller is in workspace B.
        other_ctx = WorkspaceContext(
            workspace_id="01HWA00000000000000000WSP2",
            workspace_slug="other",
            actor_id="01HWA00000000000000000USR2",
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id="01HWA00000000000000000CRL2",
        )
        with pytest.raises(LookupError):
            replay_delivery(
                other_ctx,
                repo=repo,
                delivery_id=original_id,
                clock=FrozenClock(_PINNED),
            )
        # No new delivery row landed.
        assert len(repo.deliveries) == 1

    def test_deliver_idempotent_on_terminal_row(self) -> None:
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={},
            clock=FrozenClock(_PINNED),
        )
        delivery_id = ids[0]
        # Drive the row into ``succeeded``.
        repo.update_delivery_attempt(
            delivery_id=delivery_id,
            status=DELIVERY_SUCCEEDED,
            attempt=1,
            next_attempt_at=None,
            last_status_code=200,
            last_error=None,
            last_attempted_at=_PINNED,
            succeeded_at=_PINNED,
        )

        called: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            called.append(req)
            return httpx.Response(200)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        session = _SessionStub()
        try:
            report = deliver(
                session,  # type: ignore[arg-type]
                delivery_id=delivery_id,
                repo=repo,
                envelope=FakeEnvelope(),
                http=client,
                clock=FrozenClock(_PINNED),
            )
        finally:
            client.close()
        # No HTTP call; report mirrors the row.
        assert called == []
        assert report.status == DELIVERY_SUCCEEDED

    def test_network_error_is_transient(self) -> None:
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={},
            clock=clock,
        )
        delivery_id = ids[0]

        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connect refused")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        session = _SessionStub()
        try:
            report = deliver(
                session,  # type: ignore[arg-type]
                delivery_id=delivery_id,
                repo=repo,
                envelope=FakeEnvelope(),
                http=client,
                clock=clock,
            )
        finally:
            client.close()
        assert report.dead_lettered is False
        row = repo.deliveries[delivery_id]
        assert row.status == DELIVERY_PENDING
        assert row.last_status_code is None
        assert row.last_error is not None
        assert row.last_error.startswith("network:")

    def test_timeout_is_transient(self) -> None:
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={},
            clock=clock,
        )
        delivery_id = ids[0]

        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("server too slow")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        session = _SessionStub()
        try:
            report = deliver(
                session,  # type: ignore[arg-type]
                delivery_id=delivery_id,
                repo=repo,
                envelope=FakeEnvelope(),
                http=client,
                clock=clock,
            )
        finally:
            client.close()
        assert report.dead_lettered is False
        row = repo.deliveries[delivery_id]
        assert row.status == DELIVERY_PENDING
        assert row.last_error is not None
        assert row.last_error.startswith("timeout:")


# ---------------------------------------------------------------------------
# Wire format check
# ---------------------------------------------------------------------------


class TestWireFormat:
    """Sanity-check the bytes the receiver actually sees."""

    def test_signature_header_format(self) -> None:
        repo = _InMemoryWebhookRepo()
        sub = _seed_subscription(repo, secret_plaintext="x" * 40)
        ids = enqueue(
            repo=repo,
            workspace_id=sub.workspace_id,
            event="task.completed",
            data={"k": "v"},
            clock=FrozenClock(_PINNED),
        )
        delivery_id = ids[0]

        seen_headers: dict[str, str] = {}
        seen_body: list[bytes] = []

        def handler(req: httpx.Request) -> httpx.Response:
            for k, v in req.headers.items():
                seen_headers[k] = v
            seen_body.append(req.content)
            return httpx.Response(200)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        session = _SessionStub()
        try:
            deliver(
                session,  # type: ignore[arg-type]
                delivery_id=delivery_id,
                repo=repo,
                envelope=FakeEnvelope(),
                http=client,
                clock=FrozenClock(_PINNED),
            )
        finally:
            client.close()

        sig_value = seen_headers.get(SIGNATURE_HEADER.lower())
        assert sig_value is not None
        assert sig_value.startswith("t=")
        assert ",v1=" in sig_value
        # Body verifies under the same plaintext secret.
        assert verify(
            sig_value,
            seen_body[0],
            b"x" * 40,
            now_unix=int(_PINNED.timestamp()),
        )
