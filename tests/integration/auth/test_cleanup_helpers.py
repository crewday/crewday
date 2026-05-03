"""Regression tests for auth integration cleanup helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration


def test_delete_api_tokens_for_scope_covers_delegate_and_subject_refs(
    engine: Engine,
) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        creator = bootstrap_user(
            s,
            email=f"cleanup-{new_ulid().lower()}@example.com",
            display_name="Cleanup",
        )
        delegate = bootstrap_user(
            s,
            email=f"delegate-{new_ulid().lower()}@example.com",
            display_name="Delegate",
        )
        subject = bootstrap_user(
            s,
            email=f"subject-{new_ulid().lower()}@example.com",
            display_name="Subject",
        )
        token_user = bootstrap_user(
            s,
            email=f"token-user-{new_ulid().lower()}@example.com",
            display_name="Token User",
        )
        token_subject = bootstrap_user(
            s,
            email=f"token-subject-{new_ulid().lower()}@example.com",
            display_name="Token Subject",
        )
        scoped_workspace = bootstrap_workspace(
            s,
            slug=f"cleanup-{new_ulid().lower()}",
            name="Cleanup",
            owner_user_id=creator.id,
        )
        delegated_workspace = bootstrap_workspace(
            s,
            slug=f"cleanup-delegated-{new_ulid().lower()}",
            name="Cleanup Delegated",
            owner_user_id=creator.id,
        )
        now = datetime.now(UTC)
        s.add_all(
            (
                ApiToken(
                    id=new_ulid(),
                    user_id=creator.id,
                    workspace_id=scoped_workspace.id,
                    kind="scoped",
                    delegate_for_user_id=None,
                    subject_user_id=None,
                    label="workspace",
                    scope_json={"tasks.read": True},
                    prefix="workspac",
                    hash=f"hash-{new_ulid()}",
                    expires_at=None,
                    last_used_at=None,
                    revoked_at=None,
                    created_at=now,
                ),
                ApiToken(
                    id=new_ulid(),
                    user_id=creator.id,
                    workspace_id=delegated_workspace.id,
                    kind="delegated",
                    delegate_for_user_id=delegate.id,
                    subject_user_id=None,
                    label="delegated",
                    scope_json={},
                    prefix="delegate",
                    hash=f"hash-{new_ulid()}",
                    expires_at=None,
                    last_used_at=None,
                    revoked_at=None,
                    created_at=now,
                ),
                ApiToken(
                    id=new_ulid(),
                    user_id=creator.id,
                    workspace_id=None,
                    kind="personal",
                    delegate_for_user_id=None,
                    subject_user_id=subject.id,
                    label="subject",
                    scope_json={"me.tasks:read": True},
                    prefix="subject",
                    hash=f"hash-{new_ulid()}",
                    expires_at=None,
                    last_used_at=None,
                    revoked_at=None,
                    created_at=now,
                ),
                ApiToken(
                    id=new_ulid(),
                    user_id=token_user.id,
                    workspace_id=None,
                    kind="personal",
                    delegate_for_user_id=None,
                    subject_user_id=token_subject.id,
                    label="user",
                    scope_json={"me.tasks:read": True},
                    prefix="userid",
                    hash=f"hash-{new_ulid()}",
                    expires_at=None,
                    last_used_at=None,
                    revoked_at=None,
                    created_at=now,
                ),
            )
        )
        creator_id = creator.id
        delegate_id = delegate.id
        subject_id = subject.id
        token_user_id = token_user.id
        token_subject_id = token_subject.id
        workspace_ids = (scoped_workspace.id, delegated_workspace.id)
        s.commit()

    with factory() as s:
        delete_api_tokens_for_scope(
            s,
            workspace_ids=(workspace_ids[0],),
            user_ids=(delegate_id, subject_id, token_user_id),
        )
        assert (
            s.scalar(
                select(ApiToken.id).where(
                    or_(
                        ApiToken.workspace_id == workspace_ids[0],
                        ApiToken.user_id == token_user_id,
                        ApiToken.delegate_for_user_id == delegate_id,
                        ApiToken.subject_user_id == subject_id,
                    )
                )
            )
            is None
        )
        delete_api_tokens_for_scope(
            s,
            workspace_ids=workspace_ids,
            user_ids=(
                creator_id,
                delegate_id,
                subject_id,
                token_user_id,
                token_subject_id,
            ),
        )
        with tenant_agnostic():
            s.execute(
                delete(RoleGrant).where(RoleGrant.workspace_id.in_(workspace_ids))
            )
            s.execute(
                delete(PermissionGroupMember).where(
                    PermissionGroupMember.workspace_id.in_(workspace_ids)
                )
            )
            s.execute(
                delete(PermissionGroup).where(
                    PermissionGroup.workspace_id.in_(workspace_ids)
                )
            )
            s.execute(
                delete(UserWorkspace).where(
                    UserWorkspace.workspace_id.in_(workspace_ids)
                )
            )
            s.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
            s.execute(
                delete(User).where(
                    User.id.in_(
                        (
                            creator_id,
                            delegate_id,
                            subject_id,
                            token_user_id,
                            token_subject_id,
                        )
                    )
                )
            )
        s.commit()
