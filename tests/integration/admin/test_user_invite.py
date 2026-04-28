"""Integration coverage for host-only user invite links."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import Invite, MagicLinkNonce, PasskeyCredential
from app.adapters.db.workspace.models import UserWorkspace
from app.admin.init import invite_user, workspace_bootstrap
from app.auth import magic_link
from app.auth._throttle import Throttle
from app.config import Settings
from app.domain.identity import membership
from app.tenancy import tenant_agnostic
from app.util.clock import FrozenClock

pytestmark = pytest.mark.integration
_PINNED = FrozenClock(datetime(2026, 4, 28, 18, 0, 0, tzinfo=UTC))


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-user-invite-root-key"),
        public_url="https://ops.example.test",
        demo_mode=False,
    )


def _token_from_url(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _seed_passkey(session: Session, *, user_id: str) -> None:
    with tenant_agnostic():
        session.add(
            PasskeyCredential(
                id=f"pk-{user_id}".encode(),
                user_id=user_id,
                public_key=b"test-public-key",
                sign_count=0,
                transports=None,
                backup_eligible=False,
                label="test passkey",
                created_at=_PINNED.now(),
                last_used_at=None,
            )
        )
        session.flush()


def test_user_invite_printable_link_creates_invite_nonce_and_cli_audit(
    db_session: Session,
) -> None:
    settings = _settings()
    workspace = workspace_bootstrap(
        db_session,
        settings=settings,
        slug="ops-home",
        name="Ops Home",
        owner_email="owner@example.com",
    )

    result = invite_user(
        db_session,
        settings=settings,
        email="Worker@Example.COM",
        workspace_slug="ops-home",
        role="worker",
    )

    assert result.url.startswith("https://ops.example.test/auth/magic/")
    assert result.workspace_id == workspace.workspace_id
    assert result.role == "worker"

    with tenant_agnostic():
        invite = db_session.get(Invite, result.invite_id)
        nonce = db_session.scalar(
            select(MagicLinkNonce).where(
                MagicLinkNonce.subject_id == result.invite_id,
                MagicLinkNonce.purpose == "grant_invite",
            )
        )
        audit = db_session.scalar(
            select(AuditLog).where(AuditLog.action == "admin.user_invited")
        )
        magic_audit = db_session.scalar(
            select(AuditLog).where(
                AuditLog.action == "magic_link.sent",
                AuditLog.entity_id == nonce.jti if nonce is not None else "",
            )
        )

    assert invite is not None
    assert invite.pending_email_lower == "worker@example.com"
    assert invite.grants_json[0]["grant_role"] == "worker"
    assert nonce is not None
    assert audit is not None
    assert audit.actor_kind == "system"
    assert audit.via == "cli"
    assert magic_audit is not None
    assert magic_audit.via == "cli"


def test_user_invite_printed_link_consumes_once_and_activates_membership(
    db_session: Session,
) -> None:
    settings = _settings()
    workspace = workspace_bootstrap(
        db_session,
        settings=settings,
        slug="ops-home",
        name="Ops Home",
        owner_email="owner@example.com",
    )
    result = invite_user(
        db_session,
        settings=settings,
        email="worker@example.com",
        workspace_slug="ops-home",
        role="worker",
    )
    throttle = Throttle()
    token = _token_from_url(result.url)

    acceptance = membership.consume_invite_token(
        db_session,
        token=token,
        ip="127.0.0.1",
        throttle=throttle,
        settings=settings,
    )
    assert isinstance(acceptance, membership.NewUserAcceptance)
    with pytest.raises(magic_link.AlreadyConsumed):
        membership.consume_invite_token(
            db_session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=settings,
        )

    _seed_passkey(db_session, user_id=acceptance.session.user_id)
    activated_workspace_id = membership.complete_invite(
        db_session,
        invite_id=result.invite_id,
        settings=settings,
    )

    with tenant_agnostic():
        junction = db_session.get(
            UserWorkspace, (acceptance.session.user_id, workspace.workspace_id)
        )
    assert activated_workspace_id == workspace.workspace_id
    assert junction is not None
