"""HTTP-level tests for admin passkey revocation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import PasskeyCredential
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.workspace.models import UserWorkspace
from app.api.v1.admin import router as admin_router
from app.auth.session import issue as session_issue
from app.auth.webauthn import bytes_to_base64url
from app.config import Settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client, ctx_for

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-admin-passkey-revoke-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [(f"/w/{ctx.workspace_slug}/api/v1/admin", admin_router)],
        factory,
        ctx,
    )


def _path(ctx: WorkspaceContext, *, user_id: str, credential_id: str) -> str:
    return (
        f"/w/{ctx.workspace_slug}/api/v1/admin/users/{user_id}/passkeys/{credential_id}"
    )


def _seed_member(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    email: str,
    display_name: str,
    grant_role: str = "worker",
) -> str:
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user.id,
                grant_role=grant_role,
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.add(
            UserWorkspace(
                user_id=user.id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        s.commit()
        return user.id


def _add_workspace_membership(
    factory: sessionmaker[Session], *, user_id: str, workspace_id: str
) -> None:
    with factory() as s:
        s.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        s.commit()


def _add_owners_group_member(
    factory: sessionmaker[Session], *, user_id: str, workspace_id: str
) -> None:
    with factory() as s, tenant_agnostic():
        owners_group_id = s.scalar(
            select(PermissionGroup.id).where(
                PermissionGroup.workspace_id == workspace_id,
                PermissionGroup.slug == "owners",
                PermissionGroup.system.is_(True),
            )
        )
        assert owners_group_id is not None
        s.add(
            PermissionGroupMember(
                group_id=owners_group_id,
                user_id=user_id,
                workspace_id=workspace_id,
                added_at=_PINNED,
                added_by_user_id=None,
            )
        )
        s.commit()


def _seed_credential(
    factory: sessionmaker[Session],
    *,
    user_id: str,
    credential_id: bytes,
    label: str | None = None,
    transports: str | None = "internal",
) -> None:
    with factory() as s:
        s.add(
            PasskeyCredential(
                id=credential_id,
                user_id=user_id,
                public_key=b"\xaa" * 64,
                sign_count=0,
                transports=transports,
                backup_eligible=False,
                label=label,
                created_at=_PINNED,
                last_used_at=None,
            )
        )
        s.commit()


def _seed_session(
    factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> None:
    with factory() as s:
        session_issue(
            s,
            user_id=user_id,
            has_owner_grant=False,
            ua="ua",
            ip="ip",
            now=_PINNED,
            settings=settings,
        )
        s.commit()


def _audit_actions(factory: sessionmaker[Session]) -> list[str]:
    with factory() as s, tenant_agnostic():
        return list(
            s.scalars(
                select(AuditLog.action)
                .where(
                    AuditLog.action.in_(
                        ["passkey.admin_revoked", "session.invalidated"]
                    )
                )
                .order_by(AuditLog.id)
            ).all()
        )


class TestAdminPasskeyRevoke:
    def test_owner_revokes_one_credential_and_invalidates_sessions(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="admin-revoke-worker@example.com",
            display_name="Admin Revoke Worker",
        )
        revoked = b"\x11" * 32
        kept = b"\x22" * 32
        _seed_credential(
            factory,
            user_id=target_id,
            credential_id=revoked,
            label="old phone",
            transports="internal,hybrid",
        )
        _seed_credential(factory, user_id=target_id, credential_id=kept)
        _seed_session(factory, user_id=target_id, settings=settings)

        client = _client(ctx, factory)
        response = client.delete(
            _path(
                ctx,
                user_id=target_id,
                credential_id=bytes_to_base64url(revoked),
            )
        )

        assert response.status_code == 204, response.text
        with factory() as s:
            assert s.get(PasskeyCredential, revoked) is None
            assert s.get(PasskeyCredential, kept) is not None
            session_row = s.scalars(
                select(SessionRow).where(SessionRow.user_id == target_id)
            ).one()
            assert session_row.invalidated_at is not None
            assert session_row.invalidation_cause == "passkey_admin_revoked"
        assert _audit_actions(factory) == [
            "passkey.admin_revoked",
            "session.invalidated",
        ]

        with factory() as s, tenant_agnostic():
            audit = s.scalars(
                select(AuditLog).where(AuditLog.action == "passkey.admin_revoked")
            ).one()
            assert audit.entity_kind == "passkey_credential"
            assert audit.entity_id == bytes_to_base64url(revoked)
            assert isinstance(audit.diff, dict)
            assert audit.diff["target_user_id"] == target_id
            assert audit.diff["by_actor_id"] == ctx.actor_id
            assert audit.diff["transports"] == "internal,hybrid"
            assert audit.diff["backup_eligible"] is False
            assert audit.diff["label"] == "old phone"

            invalidated = s.scalars(
                select(AuditLog).where(AuditLog.action == "session.invalidated")
            ).one()
            assert isinstance(invalidated.diff, dict)
            assert invalidated.diff["cause"] == "passkey_admin_revoked"

    def test_token_presented_owner_is_rejected_before_target_lookup(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="admin-revoke-token-target@example.com",
            display_name="Admin Revoke Token Target",
        )
        revoked = b"\x12" * 32
        _seed_credential(factory, user_id=target_id, credential_id=revoked)
        _seed_credential(factory, user_id=target_id, credential_id=b"\x13" * 32)
        token_ctx = replace(ctx, principal_kind="token")

        response = _client(token_ctx, factory).delete(
            _path(
                token_ctx,
                user_id=target_id,
                credential_id=bytes_to_base64url(revoked),
            )
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "session_only_endpoint"
        assert response.headers["www-authenticate"] == 'error="session_only_endpoint"'
        with factory() as s, tenant_agnostic():
            assert s.get(PasskeyCredential, revoked) is not None
            audit_count = s.scalars(
                select(AuditLog).where(AuditLog.action == "passkey.admin_revoked")
            ).all()
            assert audit_count == []

    def test_manager_rejected(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        owner_full_ctx, factory, ws_id = owner_ctx
        del owner_full_ctx
        manager_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="admin-revoke-manager@example.com",
            display_name="Admin Revoke Manager",
            grant_role="manager",
        )
        target_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="admin-revoke-worker-2@example.com",
            display_name="Admin Revoke Worker 2",
        )
        _seed_credential(factory, user_id=target_id, credential_id=b"\x33" * 32)
        _seed_credential(factory, user_id=target_id, credential_id=b"\x44" * 32)
        manager_ctx = ctx_for(
            workspace_id=ws_id,
            workspace_slug="ws-identity",
            actor_id=manager_id,
            grant_role="manager",
            actor_was_owner_member=False,
        )

        response = _client(manager_ctx, factory).delete(
            _path(
                manager_ctx,
                user_id=target_id,
                credential_id=bytes_to_base64url(b"\x33" * 32),
            )
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "permission_denied"

    def test_owner_on_every_target_workspace_can_revoke(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="admin-revoke-all-workspaces@example.com",
            display_name="Admin Revoke All Workspaces",
        )
        with factory() as s:
            sibling_owner = bootstrap_user(
                s,
                email="sibling-all-owner@example.com",
                display_name="Sibling All Owner",
            )
            sibling_ws = bootstrap_workspace(
                s,
                slug="sibling-passkey-revoke-allowed",
                name="Sibling Passkey Revoke Allowed",
                owner_user_id=sibling_owner.id,
            )
            s.commit()
            sibling_ws_id = sibling_ws.id
        _add_workspace_membership(
            factory, user_id=target_id, workspace_id=sibling_ws_id
        )
        _add_owners_group_member(
            factory, user_id=ctx.actor_id, workspace_id=sibling_ws_id
        )
        revoked = b"\x45" * 32
        kept = b"\x46" * 32
        _seed_credential(factory, user_id=target_id, credential_id=revoked)
        _seed_credential(factory, user_id=target_id, credential_id=kept)

        response = _client(ctx, factory).delete(
            _path(
                ctx,
                user_id=target_id,
                credential_id=bytes_to_base64url(revoked),
            )
        )

        assert response.status_code == 204, response.text
        with factory() as s:
            assert s.get(PasskeyCredential, revoked) is None
            assert s.get(PasskeyCredential, kept) is not None

    def test_owner_on_current_workspace_but_not_target_sibling_rejected(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="admin-revoke-multi@example.com",
            display_name="Admin Revoke Multi",
        )
        with factory() as s:
            sibling_owner = bootstrap_user(
                s,
                email="sibling-owner@example.com",
                display_name="Sibling Owner",
            )
            sibling_ws = bootstrap_workspace(
                s,
                slug="sibling-passkey-revoke",
                name="Sibling Passkey Revoke",
                owner_user_id=sibling_owner.id,
            )
            s.commit()
            sibling_ws_id = sibling_ws.id
        _add_workspace_membership(
            factory, user_id=target_id, workspace_id=sibling_ws_id
        )
        _seed_credential(factory, user_id=target_id, credential_id=b"\x55" * 32)
        _seed_credential(factory, user_id=target_id, credential_id=b"\x66" * 32)

        response = _client(ctx, factory).delete(
            _path(
                ctx,
                user_id=target_id,
                credential_id=bytes_to_base64url(b"\x55" * 32),
            )
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "permission_denied"

    def test_cross_workspace_target_collapses_to_employee_not_found(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ws_id = owner_ctx
        with factory() as s:
            sibling_owner = bootstrap_user(
                s,
                email="cross-owner@example.com",
                display_name="Cross Owner",
            )
            sibling_ws = bootstrap_workspace(
                s,
                slug="cross-passkey-revoke",
                name="Cross Passkey Revoke",
                owner_user_id=sibling_owner.id,
            )
            target = bootstrap_user(
                s,
                email="cross-target@example.com",
                display_name="Cross Target",
            )
            s.add(
                UserWorkspace(
                    user_id=target.id,
                    workspace_id=sibling_ws.id,
                    source="workspace_grant",
                    added_at=_PINNED,
                )
            )
            s.commit()
            target_id = target.id

        response = _client(ctx, factory).delete(
            _path(ctx, user_id=target_id, credential_id=bytes_to_base64url(b"z" * 32))
        )

        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"] == "employee_not_found"

    @pytest.mark.parametrize(
        "credential_id", ["not*base64url", bytes_to_base64url(b"\xff" * 32)]
    )
    def test_malformed_or_unknown_credential_collapses_to_passkey_not_found(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        credential_id: str,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="unknown-credential@example.com",
            display_name="Unknown Credential",
        )
        _seed_credential(factory, user_id=target_id, credential_id=b"\x77" * 32)
        _seed_credential(factory, user_id=target_id, credential_id=b"\x88" * 32)

        response = _client(ctx, factory).delete(
            _path(ctx, user_id=target_id, credential_id=credential_id)
        )

        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"] == "passkey_not_found"

    def test_wrong_user_credential_collapses_to_passkey_not_found(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="wrong-user-target@example.com",
            display_name="Wrong User Target",
        )
        other_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="wrong-user-other@example.com",
            display_name="Wrong User Other",
        )
        wrong_user_credential = b"\x99" * 32
        _seed_credential(factory, user_id=target_id, credential_id=b"\xaa" * 32)
        _seed_credential(factory, user_id=target_id, credential_id=b"\xbb" * 32)
        _seed_credential(
            factory,
            user_id=other_id,
            credential_id=wrong_user_credential,
        )

        response = _client(ctx, factory).delete(
            _path(
                ctx,
                user_id=target_id,
                credential_id=bytes_to_base64url(wrong_user_credential),
            )
        )

        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"] == "passkey_not_found"
        with factory() as s:
            assert s.get(PasskeyCredential, wrong_user_credential) is not None

    def test_last_credential_refused(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        target_id = _seed_member(
            factory,
            workspace_id=ws_id,
            email="last-credential@example.com",
            display_name="Last Credential",
        )
        credential_id = b"\xcc" * 32
        _seed_credential(factory, user_id=target_id, credential_id=credential_id)

        response = _client(ctx, factory).delete(
            _path(
                ctx,
                user_id=target_id,
                credential_id=bytes_to_base64url(credential_id),
            )
        )

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error"] == "last_credential"
        with factory() as s:
            assert s.get(PasskeyCredential, credential_id) is not None
