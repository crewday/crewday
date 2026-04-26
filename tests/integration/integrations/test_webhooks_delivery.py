"""Integration tests for outbound webhook delivery (cd-q885).

Exercises the full pipeline against:

* the migrated SA schema (``webhook_subscription`` + ``webhook_delivery``
  + ``secret_envelope`` + ``audit_log``);
* the row-backed AES-256-GCM cipher (cd-znv4);
* a real :class:`http.server.ThreadingHTTPServer` listening on
  ``127.0.0.1`` so the dispatcher's ``httpx.Client`` actually opens
  a socket.

Covers the cd-q885 acceptance criteria end-to-end:

* Sign / verify round-trips through the full encrypt / decrypt /
  HMAC chain.
* A 500 from the receiver triggers retries; the 3rd attempt succeeds.
* Six failures dead-letter the row + audit
  ``audit.webhook_delivery.dead_lettered``.
* A 400 dead-letters immediately (no retries).
* Replay re-mints the payload with a new timestamp.
* Subscription secret returned only at create.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.integrations.models import (
    WebhookDelivery,
    WebhookSubscription,
)
from app.adapters.db.integrations.repositories import (
    SqlAlchemyWebhookRepository,
)
from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.secrets.repositories import (
    SqlAlchemySecretEnvelopeRepository,
)
from app.adapters.db.workspace.models import Workspace
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.domain.integrations.webhooks import (
    DELIVERY_DEAD_LETTERED,
    DELIVERY_SUCCEEDED,
    RETRY_SCHEDULE_SECONDS,
    create_subscription,
    deliver,
    enqueue,
    replay_delivery,
    verify,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_KEY = SecretStr("x" * 32)
_PINNED = datetime(2026, 4, 26, 18, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Stub HTTP receiver
# ---------------------------------------------------------------------------


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Replays a per-test sequence of HTTP responses.

    Mutable class-level state — set ``status_codes`` to the
    sequence the test wants and ``captured_requests`` collects the
    headers + body the dispatcher sent. The ``ThreadingHTTPServer``
    creates a fresh handler instance per request, but reading +
    writing from class state lets the test thread inspect what
    landed without threading state through the constructor.
    """

    status_codes: ClassVar[list[int]] = []
    captured_requests: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.captured_requests.append(
            {
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "body": body,
            }
        )
        # Pop one status code per request; default 200 if exhausted.
        code = self.status_codes.pop(0) if self.status_codes else 200
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Silence stderr noise."""


@pytest.fixture(name="receiver")
def fixture_receiver() -> Iterator[ThreadingHTTPServer]:
    """Yield a fresh receiver server bound to ``127.0.0.1`` on a random port.

    The handler's class state is reset per test so a leftover
    sequence from a previous test cannot leak.
    """
    _ScriptedHandler.status_codes = []
    _ScriptedHandler.captured_requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _receiver_url(server: ThreadingHTTPServer) -> str:
    # ``ThreadingHTTPServer`` exposes ``server_port`` as a plain int
    # and we always bind to ``127.0.0.1`` in the fixture. The tuple
    # form ``server_address[0]`` is typed broadly enough that mypy
    # flags the f-string interpolation; ``server_port`` is the
    # narrower public surface and matches the iCal test pattern.
    return f"http://127.0.0.1:{server.server_port}/hook"


# ---------------------------------------------------------------------------
# Workspace bootstrap
# ---------------------------------------------------------------------------


def _ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="webhooks-it",
        actor_id="01HWA00000000000000000USR1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_workspace(db_session: Session) -> str:
    ws_id = new_ulid()
    db_session.add(
        Workspace(
            id=ws_id,
            slug=f"webhooks-it-{ws_id[-6:].lower()}",
            name="webhooks integration",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    db_session.flush()
    return ws_id


def _build_envelope(db_session: Session) -> Aes256GcmEnvelope:
    repo = SqlAlchemySecretEnvelopeRepository(db_session)
    return Aes256GcmEnvelope(_KEY, repository=repo, clock=FrozenClock(_PINNED))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubscriptionCreate:
    def test_create_persists_row_and_envelope(self, db_session: Session) -> None:
        ws_id = _bootstrap_workspace(db_session)
        envelope = _build_envelope(db_session)
        repo = SqlAlchemyWebhookRepository(db_session)

        view = create_subscription(
            db_session,
            _ctx(ws_id),
            repo=repo,
            envelope=envelope,
            name="Hermes",
            url="https://hermes.example.com/hook",
            events=["task.completed"],
            clock=FrozenClock(_PINNED),
        )
        assert view.plaintext_secret is not None

        # Row landed.
        row = db_session.get(WebhookSubscription, view.id)
        assert row is not None
        assert row.workspace_id == ws_id
        # Plaintext doesn't appear in the column.
        plaintext_bytes = view.plaintext_secret.encode("utf-8")
        secret_bytes = row.secret_blob.encode("latin-1")
        assert plaintext_bytes not in secret_bytes

        # Pointer-tagged blob → cd-znv4 row landed.
        envelope_id = row.secret_blob.encode("latin-1")[1:].decode("utf-8")
        secret_row = db_session.get(SecretEnvelope, envelope_id)
        assert secret_row is not None
        assert secret_row.owner_entity_kind == "webhook_subscription"
        assert secret_row.owner_entity_id == view.id

        # Audit row written.
        audits = list(
            db_session.scalars(select(AuditLog).where(AuditLog.entity_id == view.id))
        )
        assert len(audits) == 1
        assert audits[0].action == "created"


class TestEndToEndDelivery:
    def _seed(
        self,
        db_session: Session,
        receiver: ThreadingHTTPServer,
        *,
        events: tuple[str, ...] = ("task.completed",),
    ) -> tuple[str, str, str]:
        """Bootstrap a workspace + subscription; return (ws_id, sub_id, secret)."""
        ws_id = _bootstrap_workspace(db_session)
        envelope = _build_envelope(db_session)
        repo = SqlAlchemyWebhookRepository(db_session)
        view = create_subscription(
            db_session,
            _ctx(ws_id),
            repo=repo,
            envelope=envelope,
            name="receiver",
            url=_receiver_url(receiver),
            events=list(events),
            clock=FrozenClock(_PINNED),
        )
        assert view.plaintext_secret is not None
        return ws_id, view.id, view.plaintext_secret

    def test_500_then_500_then_2xx_succeeds_on_third_attempt(
        self, db_session: Session, receiver: ThreadingHTTPServer
    ) -> None:
        ws_id, _sub_id, secret = self._seed(db_session, receiver)
        envelope = _build_envelope(db_session)
        repo = SqlAlchemyWebhookRepository(db_session)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=ws_id,
            event="task.completed",
            data={"task_id": "T1"},
            clock=clock,
        )
        delivery_id = ids[0]
        _ScriptedHandler.status_codes = [500, 500, 200]

        for _ in range(3):
            deliver(
                db_session,
                delivery_id=delivery_id,
                repo=repo,
                envelope=envelope,
                clock=clock,
            )
            clock.advance(timedelta(seconds=60))

        row = db_session.get(WebhookDelivery, delivery_id)
        assert row is not None
        assert row.status == DELIVERY_SUCCEEDED
        assert row.attempt == 3
        # Three captured requests; each carries a real signature
        # over the request body under the plaintext secret.
        assert len(_ScriptedHandler.captured_requests) == 3
        last = _ScriptedHandler.captured_requests[-1]
        sig = last["headers"]["X-Crewday-Signature"]
        assert verify(
            sig,
            last["body"],
            secret.encode("utf-8"),
            now_unix=int(clock.now().timestamp()),
            tolerance_s=24 * 3600,
        )

    def test_six_failures_dead_letter_with_audit(
        self, db_session: Session, receiver: ThreadingHTTPServer
    ) -> None:
        ws_id, sub_id, _secret = self._seed(db_session, receiver)
        envelope = _build_envelope(db_session)
        repo = SqlAlchemyWebhookRepository(db_session)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=ws_id,
            event="task.completed",
            data={},
            clock=clock,
        )
        delivery_id = ids[0]
        _ScriptedHandler.status_codes = [500] * len(RETRY_SCHEDULE_SECONDS)

        for _ in range(len(RETRY_SCHEDULE_SECONDS)):
            deliver(
                db_session,
                delivery_id=delivery_id,
                repo=repo,
                envelope=envelope,
                clock=clock,
            )
            clock.advance(timedelta(days=2))

        row = db_session.get(WebhookDelivery, delivery_id)
        assert row is not None
        assert row.status == DELIVERY_DEAD_LETTERED
        assert row.dead_lettered_at is not None

        audits = list(
            db_session.scalars(
                select(AuditLog)
                .where(AuditLog.entity_kind == "webhook_delivery")
                .where(AuditLog.entity_id == delivery_id)
            )
        )
        assert len(audits) == 1
        assert audits[0].action == "dead_lettered"

        # Successful deliveries are NOT audited; the only
        # webhook-related audit rows are the create + dead_lettered.
        all_subscription_audits = list(
            db_session.scalars(
                select(AuditLog).where(
                    AuditLog.entity_kind == "webhook_subscription",
                    AuditLog.entity_id == sub_id,
                )
            )
        )
        assert len(all_subscription_audits) == 1  # the create

    def test_400_dead_letters_immediately(
        self, db_session: Session, receiver: ThreadingHTTPServer
    ) -> None:
        ws_id, _sub_id, _secret = self._seed(db_session, receiver)
        envelope = _build_envelope(db_session)
        repo = SqlAlchemyWebhookRepository(db_session)
        clock = FrozenClock(_PINNED)
        ids = enqueue(
            repo=repo,
            workspace_id=ws_id,
            event="task.completed",
            data={},
            clock=clock,
        )
        delivery_id = ids[0]
        _ScriptedHandler.status_codes = [400]

        deliver(
            db_session,
            delivery_id=delivery_id,
            repo=repo,
            envelope=envelope,
            clock=clock,
        )

        row = db_session.get(WebhookDelivery, delivery_id)
        assert row is not None
        assert row.status == DELIVERY_DEAD_LETTERED
        assert row.last_status_code == 400
        # Only one wire request fired.
        assert len(_ScriptedHandler.captured_requests) == 1

    def test_replay_succeeds_with_fresh_signature(
        self, db_session: Session, receiver: ThreadingHTTPServer
    ) -> None:
        ws_id, _sub_id, secret = self._seed(db_session, receiver)
        envelope = _build_envelope(db_session)
        repo = SqlAlchemyWebhookRepository(db_session)
        clock = FrozenClock(_PINNED)

        # Original delivery → permanent failure.
        ids = enqueue(
            repo=repo,
            workspace_id=ws_id,
            event="task.completed",
            data={"task_id": "T1"},
            clock=clock,
        )
        original_id = ids[0]
        _ScriptedHandler.status_codes = [400]
        deliver(
            db_session,
            delivery_id=original_id,
            repo=repo,
            envelope=envelope,
            clock=clock,
        )
        original_row = db_session.get(WebhookDelivery, original_id)
        assert original_row is not None
        assert original_row.status == DELIVERY_DEAD_LETTERED

        # Replay rolls the clock + mints a fresh delivery.
        clock.advance(timedelta(hours=1))
        new_id = replay_delivery(
            _ctx(ws_id), repo=repo, delivery_id=original_id, clock=clock
        )
        # Receiver accepts on first try.
        _ScriptedHandler.status_codes = [200]
        # Reset captured requests so we can isolate the replay's wire bytes.
        _ScriptedHandler.captured_requests.clear()
        deliver(
            db_session,
            delivery_id=new_id,
            repo=repo,
            envelope=envelope,
            clock=clock,
        )
        new_row = db_session.get(WebhookDelivery, new_id)
        assert new_row is not None
        assert new_row.status == DELIVERY_SUCCEEDED
        assert new_row.replayed_from_id == original_id

        # Captured signature carries the new ``t=`` timestamp.
        assert len(_ScriptedHandler.captured_requests) == 1
        sig = _ScriptedHandler.captured_requests[0]["headers"]["X-Crewday-Signature"]
        body = _ScriptedHandler.captured_requests[0]["body"]
        assert verify(
            sig,
            body,
            secret.encode("utf-8"),
            now_unix=int(clock.now().timestamp()),
            tolerance_s=24 * 3600,
        )
        # New ``delivery_id`` rode the body.
        assert new_id.encode() in body
