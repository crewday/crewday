"""Integration coverage for host-only workspace bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    DeploymentOwner,
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import (
    Invite,
    MagicLinkNonce,
    PasskeyCredential,
    User,
)
from app.adapters.db.places.models import Property, PropertyWorkspace, Unit
from app.adapters.db.workspace.bootstrap import STARTER_WORK_ROLE_KEYS
from app.adapters.db.workspace.models import UserWorkspace, WorkRole, Workspace
from app.admin.init import workspace_bootstrap
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration
_PINNED = datetime(2026, 4, 28, 18, 0, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-workspace-bootstrap-root-key"),
        public_url="https://ops.example.test",
        demo_mode=False,
    )


def test_workspace_bootstrap_creates_workspace_owner_and_cli_audit(
    db_session: Session,
) -> None:
    result = workspace_bootstrap(
        db_session,
        settings=_settings(),
        slug="ops-home",
        name="Ops Home",
        owner_email="Owner@Example.COM",
    )

    assert result.url.startswith("https://ops.example.test/auth/magic/")

    with tenant_agnostic():
        workspace = db_session.get(Workspace, result.workspace_id)
        user = db_session.get(User, result.user_id)
        junction = db_session.get(UserWorkspace, (result.user_id, result.workspace_id))
        owners = db_session.scalar(
            select(PermissionGroup).where(
                PermissionGroup.workspace_id == result.workspace_id,
                PermissionGroup.slug == "owners",
            )
        )
        owner_member = db_session.scalar(
            select(PermissionGroupMember).where(
                PermissionGroupMember.workspace_id == result.workspace_id,
                PermissionGroupMember.user_id == result.user_id,
            )
        )
        owner_grant = db_session.scalar(
            select(RoleGrant).where(
                RoleGrant.workspace_id == result.workspace_id,
                RoleGrant.user_id == result.user_id,
                RoleGrant.grant_role == "manager",
            )
        )
        deployment_owner = db_session.get(DeploymentOwner, result.user_id)
        deployment_grant = db_session.scalar(
            select(RoleGrant).where(
                RoleGrant.workspace_id.is_(None),
                RoleGrant.scope_kind == "deployment",
                RoleGrant.user_id == result.user_id,
            )
        )
        nonce = db_session.scalar(
            select(MagicLinkNonce).where(
                MagicLinkNonce.purpose == "grant_invite",
            )
        )
        invite = db_session.get(
            Invite, nonce.subject_id if nonce is not None else "missing"
        )
        audit = db_session.scalar(
            select(AuditLog).where(AuditLog.action == "admin.workspace_bootstrapped")
        )
        owners_audit = db_session.scalar(
            select(AuditLog).where(AuditLog.action == "owners_bootstrapped")
        )
        properties = db_session.scalars(
            select(Property)
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
            .where(PropertyWorkspace.workspace_id == result.workspace_id)
            .where(Property.deleted_at.is_(None))
        ).all()
        default_property = properties[0] if properties else None
        property_workspace = (
            db_session.get(
                PropertyWorkspace, (default_property.id, result.workspace_id)
            )
            if default_property is not None
            else None
        )
        units = (
            db_session.scalars(
                select(Unit)
                .where(Unit.property_id == default_property.id)
                .where(Unit.deleted_at.is_(None))
            ).all()
            if default_property is not None
            else []
        )
        work_role_keys = db_session.scalars(
            select(WorkRole.key)
            .where(WorkRole.workspace_id == result.workspace_id)
            .order_by(WorkRole.key)
        ).all()

    assert workspace is not None
    assert workspace.slug == "ops-home"
    assert workspace.name == "Ops Home"
    assert user is not None
    assert user.email_lower == "owner@example.com"
    assert junction is not None
    assert owners is not None
    assert owner_member is not None
    assert owner_grant is not None
    assert deployment_owner is not None
    assert deployment_grant is not None
    assert nonce is not None
    assert invite is not None
    assert invite.user_id == result.user_id
    assert invite.workspace_id == result.workspace_id
    assert invite.grants_json == []
    assert invite.group_memberships_json == []
    assert audit is not None
    assert audit.actor_kind == "system"
    assert audit.via == "cli"
    assert owners_audit is not None
    assert owners_audit.actor_kind == "system"
    assert owners_audit.via == "cli"
    assert len(properties) == 1
    assert default_property is not None
    assert default_property.name == "Home"
    assert default_property.kind == "residence"
    assert default_property.timezone == workspace.default_timezone
    assert default_property.default_currency == workspace.default_currency
    assert default_property.country == "XX"
    assert property_workspace is not None
    assert property_workspace.membership_role == "owner_workspace"
    assert property_workspace.status == "active"
    assert len(units) == 1
    assert units[0].name == "Home"
    assert units[0].ordinal == 0
    assert work_role_keys == sorted(STARTER_WORK_ROLE_KEYS)


def test_workspace_bootstrap_duplicate_slug_does_not_seed_second_property(
    db_session: Session,
) -> None:
    result = workspace_bootstrap(
        db_session,
        settings=_settings(),
        slug="ops-home",
        name="Ops Home",
        owner_email="owner@example.com",
    )

    with pytest.raises(ValueError, match="workspace slug 'ops-home' already exists"):
        workspace_bootstrap(
            db_session,
            settings=_settings(),
            slug="ops-home",
            name="Ops Home Retry",
            owner_email="owner@example.com",
        )

    with tenant_agnostic():
        properties = db_session.scalars(
            select(Property)
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
            .where(PropertyWorkspace.workspace_id == result.workspace_id)
            .where(Property.deleted_at.is_(None))
        ).all()

    assert [property_row.name for property_row in properties] == ["Home"]


def test_workspace_bootstrap_existing_owner_gets_non_destructive_invite_link(
    db_session: Session,
) -> None:
    user_id = new_ulid()
    with tenant_agnostic():
        db_session.add(
            User(
                id=user_id,
                email="owner@example.com",
                email_lower="owner@example.com",
                display_name="Existing Owner",
                timezone="UTC",
                created_at=_PINNED,
            )
        )
        db_session.flush()
        db_session.add(
            PasskeyCredential(
                id=b"existing-owner-passkey",
                user_id=user_id,
                public_key=b"test-public-key",
                sign_count=0,
                transports=None,
                backup_eligible=False,
                label="test passkey",
                created_at=_PINNED,
                last_used_at=None,
            )
        )
        db_session.flush()

    result = workspace_bootstrap(
        db_session,
        settings=_settings(),
        slug="ops-existing-owner",
        name="Ops Existing Owner",
        owner_email="owner@example.com",
    )

    with tenant_agnostic():
        invite_nonce = db_session.scalar(
            select(MagicLinkNonce).where(
                MagicLinkNonce.purpose == "grant_invite",
                MagicLinkNonce.subject_id != result.user_id,
            )
        )
        recovery_nonce = db_session.scalar(
            select(MagicLinkNonce).where(
                MagicLinkNonce.purpose == "recover_passkey",
                MagicLinkNonce.subject_id == result.user_id,
            )
        )

    assert result.user_id == user_id
    assert invite_nonce is not None
    assert recovery_nonce is None
