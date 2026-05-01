"""End-to-end test for the web-push delivery worker (cd-y60x).

Wires the production :func:`pywebpush.webpush` call against an
in-process stdlib :class:`http.server.HTTPServer` standing in for the
push provider. The fake endpoint responds with HTTP 201 to a single
delivery, and we assert that:

* the worker drove the queue row to ``status='sent'``;
* the matching :class:`PushToken` row's ``last_used_at`` was bumped;
* the fake endpoint received exactly one POST whose headers carry
  the VAPID JWT signature.

The encryption path is exercised end-to-end — pywebpush generates the
ECDH shared secret, derives the AES128-GCM key, and POSTs the
ciphertext. We don't decrypt the body server-side; the receipt of a
``Authorization: vapid t=…, k=…`` header is enough to prove the
signing path went through.
"""

from __future__ import annotations

import base64
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Final

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import (
    Notification,
    NotificationPushQueue,
    PushToken,
)
from app.adapters.db.workspace.models import Workspace
from app.domain.messaging.push_tokens import (
    SETTINGS_KEY_VAPID_PRIVATE,
    SETTINGS_KEY_VAPID_SUBJECT,
)
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.jobs import messaging_web_push as worker_module
from app.worker.jobs.messaging_web_push import dispatch_due_pushes

_PINNED: Final[datetime] = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake provider HTTP server
# ---------------------------------------------------------------------------


class _ProviderState:
    """Captures requests the fake provider received."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []


class _FakeProviderHandler(BaseHTTPRequestHandler):
    state: _ProviderState

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        # Drain the body so the connection can close cleanly. The
        # ciphertext is opaque; we don't decrypt it.
        self.rfile.read(length)
        headers = {k.lower(): v for k, v in self.headers.items()}
        type(self).state.requests.append((self.path, headers))
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Silence the default stderr access log so pytest output stays clean.
        return


@pytest.fixture
def provider() -> Iterator[tuple[str, _ProviderState]]:
    """Run a stdlib ``HTTPServer`` on localhost; yield its URL + state."""
    state = _ProviderState()
    handler_cls = type(
        "Handler",
        (_FakeProviderHandler,),
        {"state": state},
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield f"http://{host}:{port}/push/abc", state
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s

    # Sweep the rows this test commits so sibling integration tests on
    # the same xdist worker see a clean slate. The e2e test commits a
    # workspace + user + push tokens / queue rows directly via the
    # production session factory (not the rollback-wrapping
    # ``db_session``), so SAVEPOINT isolation does not apply.
    # Scope the sweep to the slug ("e2e") and email
    # ("alice@example.com") this test always uses; broader deletes
    # would silently clobber rows committed by other tests sharing the
    # worker DB.
    with session_factory() as s:
        ws_id = s.scalar(select(Workspace.id).where(Workspace.slug == "e2e"))
        if ws_id is not None:
            for table_model in (NotificationPushQueue, Notification, PushToken):
                s.execute(delete(table_model).where(table_model.workspace_id == ws_id))
            s.execute(delete(Workspace).where(Workspace.id == ws_id))
        s.execute(
            delete(User).where(
                User.email_lower == canonicalise_email("alice@example.com")
            )
        )
        s.commit()


@pytest.fixture(autouse=True)
def _patch_make_uow(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
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
# VAPID + subscription crypto helpers
# ---------------------------------------------------------------------------


def _generate_vapid_keypair() -> tuple[str, str]:
    """Return ``(private_b64url, public_b64url)`` for a fresh EC P-256 key.

    pywebpush accepts the private key as a raw 32-byte scalar encoded
    base64url (the format ``py_vapid`` exposes via ``Vapid.from_string``).
    A PEM blob would also work in newer pywebpush releases, but the raw
    form matches what the §10 CLI provisioning tool will write into
    ``workspace.settings_json`` so the test mirrors production.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    raw_priv = private_key.private_numbers().private_value.to_bytes(32, "big")
    private_b64 = base64.urlsafe_b64encode(raw_priv).rstrip(b"=").decode("ascii")
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(public_raw).rstrip(b"=").decode("ascii")
    return private_b64, public_b64


def _generate_subscription_keys() -> tuple[str, str]:
    """Return ``(p256dh_b64url, auth_b64url)`` for a synthetic browser sub."""
    sub_priv = ec.generate_private_key(ec.SECP256R1())
    sub_pub_raw = sub_priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    p256dh = base64.urlsafe_b64encode(sub_pub_raw).rstrip(b"=").decode("ascii")
    auth = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("ascii")
    return p256dh, auth


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


def test_happy_path_delivery_e2e(
    session: Session,
    provider: tuple[str, _ProviderState],
) -> None:
    endpoint_url, state = provider
    private_b64, _public_b64 = _generate_vapid_keypair()
    p256dh, auth = _generate_subscription_keys()

    workspace_id = new_ulid()
    user_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug="e2e",
            name="E2E",
            plan="free",
            quota_json={},
            settings_json={
                SETTINGS_KEY_VAPID_PRIVATE: private_b64,
                SETTINGS_KEY_VAPID_SUBJECT: "mailto:ops@example.com",
            },
            created_at=_PINNED,
        )
    )
    session.add(
        User(
            id=user_id,
            email="alice@example.com",
            email_lower=canonicalise_email("alice@example.com"),
            display_name="Alice",
            created_at=_PINNED,
        )
    )
    session.flush()

    token_id = new_ulid()
    session.add(
        PushToken(
            id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=endpoint_url,
            p256dh=p256dh,
            auth=auth,
            user_agent="UA",
            created_at=_PINNED,
            last_used_at=None,
        )
    )
    notification_id = new_ulid()
    session.add(
        Notification(
            id=notification_id,
            workspace_id=workspace_id,
            recipient_user_id=user_id,
            kind="task_assigned",
            subject="Task",
            body_md="Body",
            read_at=None,
            created_at=_PINNED,
            payload_json={},
        )
    )
    session.flush()

    delivery_id = new_ulid()
    session.add(
        NotificationPushQueue(
            id=delivery_id,
            workspace_id=workspace_id,
            notification_id=notification_id,
            push_token_id=token_id,
            kind="task_assigned",
            body="Hello",
            payload_json={"task_id": "T1"},
            status="pending",
            attempt=0,
            next_attempt_at=_PINNED,
            last_status_code=None,
            last_error=None,
            last_attempted_at=None,
            sent_at=None,
            dead_lettered_at=None,
            created_at=_PINNED,
        )
    )
    session.commit()

    # Production sender — this hits pywebpush + the fake HTTP server.
    report = dispatch_due_pushes(
        clock=FrozenClock(_PINNED + timedelta(seconds=1)),
    )
    assert report.successes == 1
    assert report.processed_count == 1
    assert report.tokens_purged == 0

    session.expire_all()
    row = session.scalars(
        select(NotificationPushQueue).where(NotificationPushQueue.id == delivery_id)
    ).one()
    assert row.status == "sent"
    assert row.attempt == 1
    assert row.last_status_code == 201

    token_row = session.scalars(select(PushToken).where(PushToken.id == token_id)).one()
    assert token_row.last_used_at is not None

    # Fake provider received exactly one POST carrying a VAPID JWT.
    assert len(state.requests) == 1
    path, headers = state.requests[0]
    assert path == "/push/abc"
    auth_header = headers.get("authorization", "")
    assert auth_header.startswith("vapid "), auth_header
    assert "t=" in auth_header and "k=" in auth_header
