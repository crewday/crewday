"""Focused provider-webhook tests for ``email_delivery`` updates."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.messaging.models import EmailDelivery
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.messaging.email_delivery_webhooks import (
    EmailDeliveryWebhookEvent,
    EmailDeliveryWebhookHandler,
)
from app.util.ulid import new_ulid

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
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _workspace(session: Session, *, slug: str) -> str:
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
    provider_message_id: str = "msg-1",
    state: str = "sent",
    first_error: str | None = None,
) -> str:
    delivery_id = new_ulid()
    session.add(
        EmailDelivery(
            id=delivery_id,
            workspace_id=workspace_id,
            to_person_id=f"person-{delivery_id}",
            to_email_at_send="recipient@example.com",
            template_key="task_assigned",
            context_snapshot_json={"task_title": "Webhook room"},
            sent_at=_PINNED,
            provider_message_id=provider_message_id,
            delivery_state=state,
            first_error=first_error,
            retry_count=0,
            inbound_linkage=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return delivery_id


def _handler(session: Session) -> EmailDeliveryWebhookHandler:
    return EmailDeliveryWebhookHandler(
        email_deliveries=SqlAlchemyEmailDeliveryRepository(session)
    )


def _row(session: Session, delivery_id: str) -> EmailDelivery:
    return session.scalars(
        select(EmailDelivery).where(EmailDelivery.id == delivery_id)
    ).one()


@pytest.mark.parametrize(
    ("provider_event", "expected_state", "error_text"),
    [
        ("delivered", "delivered", None),
        ("bounce", "bounced", "550 mailbox unavailable"),
        ("complaint", "complaint", "recipient marked spam"),
        ("failed", "failed", "provider rejected message"),
    ],
)
def test_provider_event_maps_to_email_delivery_state(
    session: Session,
    provider_event: str,
    expected_state: str,
    error_text: str | None,
) -> None:
    workspace_id = _workspace(session, slug=f"map-{provider_event}")
    delivery_id = _delivery(session, workspace_id=workspace_id)

    updated = _handler(session).handle(
        EmailDeliveryWebhookEvent(
            workspace_id=workspace_id,
            provider_message_id="msg-1",
            event=provider_event,
            error_text=error_text,
        )
    )

    assert updated is not None
    assert updated.id == delivery_id
    assert updated.delivery_state == expected_state
    assert _row(session, delivery_id).delivery_state == expected_state
    assert _row(session, delivery_id).first_error == error_text


def test_duplicate_webhook_redelivery_is_idempotent(session: Session) -> None:
    workspace_id = _workspace(session, slug="duplicate")
    delivery_id = _delivery(session, workspace_id=workspace_id)
    handler = _handler(session)

    first = handler.handle(
        EmailDeliveryWebhookEvent(
            workspace_id=workspace_id,
            provider_message_id="msg-1",
            event="bounce",
            error_text="first provider reason",
        )
    )
    second = handler.handle(
        EmailDeliveryWebhookEvent(
            workspace_id=workspace_id,
            provider_message_id="msg-1",
            event="bounce",
            error_text="duplicate provider reason",
        )
    )

    assert first is not None
    assert second is not None
    assert second.delivery_state == "bounced"
    row = _row(session, delivery_id)
    assert row.delivery_state == "bounced"
    assert row.first_error == "first provider reason"


def test_first_error_is_not_overwritten(session: Session) -> None:
    workspace_id = _workspace(session, slug="first-error")
    delivery_id = _delivery(
        session,
        workspace_id=workspace_id,
        first_error="smtp timeout on first attempt",
    )

    updated = _handler(session).handle(
        EmailDeliveryWebhookEvent(
            workspace_id=workspace_id,
            provider_message_id="msg-1",
            event="complaint",
            error_text="recipient marked spam",
        )
    )

    assert updated is not None
    assert updated.delivery_state == "complaint"
    assert _row(session, delivery_id).first_error == "smtp timeout on first attempt"


def test_delivered_event_does_not_set_first_error(session: Session) -> None:
    workspace_id = _workspace(session, slug="delivered-no-error")
    delivery_id = _delivery(session, workspace_id=workspace_id)

    updated = _handler(session).handle(
        EmailDeliveryWebhookEvent(
            workspace_id=workspace_id,
            provider_message_id="msg-1",
            event="delivered",
            error_text="provider diagnostic that is not a failure",
        )
    )

    assert updated is not None
    assert updated.delivery_state == "delivered"
    assert _row(session, delivery_id).first_error is None


def test_provider_webhook_lookup_is_workspace_scoped(session: Session) -> None:
    ws_a = _workspace(session, slug="workspace-a")
    ws_b = _workspace(session, slug="workspace-b")
    delivery_a = _delivery(
        session,
        workspace_id=ws_a,
        provider_message_id="shared-provider-id",
    )
    delivery_b = _delivery(
        session,
        workspace_id=ws_b,
        provider_message_id="shared-provider-id",
    )

    updated = _handler(session).handle(
        EmailDeliveryWebhookEvent(
            workspace_id=ws_b,
            provider_message_id="shared-provider-id",
            event="delivered",
        )
    )

    assert updated is not None
    assert updated.id == delivery_b
    assert _row(session, delivery_a).delivery_state == "sent"
    assert _row(session, delivery_b).delivery_state == "delivered"


def test_provider_webhook_does_not_downgrade_terminal_state(
    session: Session,
) -> None:
    workspace_id = _workspace(session, slug="monotonic")
    delivery_id = _delivery(session, workspace_id=workspace_id, state="delivered")

    updated = _handler(session).handle(
        EmailDeliveryWebhookEvent(
            workspace_id=workspace_id,
            provider_message_id="msg-1",
            event="failed",
            error_text="late provider failure",
        )
    )

    assert updated is not None
    assert updated.delivery_state == "delivered"
    row = _row(session, delivery_id)
    assert row.delivery_state == "delivered"
    assert row.first_error is None
