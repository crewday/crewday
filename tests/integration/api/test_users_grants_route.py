"""HTTP-level coverage for ``DELETE /w/<slug>/api/v1/users/{id}/grants``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.errors import CONTENT_TYPE_PROBLEM_JSON, add_exception_handlers
from app.api.v1.users import build_users_router
from app.auth._throttle import Throttle
from app.config import Settings
from app.domain.errors import CANONICAL_TYPE_BASE
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture(autouse=True)
def _redirect_default_uow(
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[None]:
    import app.adapters.db.session as session_module

    original_engine = session_module._default_engine
    original_factory = session_module._default_sessionmaker_
    session_module._default_engine = engine
    session_module._default_sessionmaker_ = session_factory
    try:
        yield
    finally:
        session_module._default_engine = original_engine
        session_module._default_sessionmaker_ = original_factory


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-users-grants-route-root-key"),
        public_url="https://test.crew.day",
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def seeded(
    session_factory: sessionmaker[Session],
) -> Iterator[tuple[WorkspaceContext, str, str]]:
    tag = new_ulid()[-8:].lower()
    slug = f"grants-{tag}"
    session_id = new_ulid()
    with session_factory() as s:
        owner = bootstrap_user(
            s,
            email=f"owner-{tag}@example.com",
            display_name="Owner",
        )
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name="Grants Route",
            owner_user_id=owner.id,
        )
        s.add(
            SessionRow(
                id=session_id,
                user_id=owner.id,
                workspace_id=ws.id,
                expires_at=_PINNED + timedelta(days=7),
                absolute_expires_at=_PINNED + timedelta(days=90),
                last_seen_at=_PINNED,
                ua_hash="test-ua",
                ip_hash="test-ip",
                fingerprint_hash="test-fingerprint",
                created_at=_PINNED,
                invalidated_at=None,
                invalidation_cause=None,
            )
        )
        s.commit()
        owner_id, ws_id, ws_slug = owner.id, ws.id, ws.slug

    ctx = build_workspace_context(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=owner_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    try:
        yield ctx, owner_id, session_id
    finally:
        with session_factory() as s, tenant_agnostic():
            s.query(AuditLog).filter(AuditLog.workspace_id == ws_id).delete(
                synchronize_session=False
            )
            s.query(SessionRow).filter(SessionRow.workspace_id == ws_id).delete(
                synchronize_session=False
            )
            s.query(PermissionGroupMember).filter(
                PermissionGroupMember.workspace_id == ws_id
            ).delete(synchronize_session=False)
            s.query(RoleGrant).filter(RoleGrant.workspace_id == ws_id).delete(
                synchronize_session=False
            )
            s.query(UserWorkspace).filter(UserWorkspace.workspace_id == ws_id).delete(
                synchronize_session=False
            )
            s.query(PermissionGroup).filter(
                PermissionGroup.workspace_id == ws_id
            ).delete(synchronize_session=False)
            ws_row = s.get(Workspace, ws_id)
            if ws_row is not None:
                s.delete(ws_row)
            owner_row = s.get(User, owner_id)
            if owner_row is not None:
                s.delete(owner_row)
            s.commit()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
    seeded: tuple[WorkspaceContext, str, str],
    settings: Settings,
) -> Iterator[TestClient]:
    ctx, _, _ = seeded
    app = FastAPI()
    app.include_router(
        build_users_router(
            mailer=InMemoryMailer(),
            throttle=Throttle(),
            settings=settings,
            base_url=settings.public_url,
        ),
        prefix="/w/{slug}/api/v1",
    )
    add_exception_handlers(app)

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx

    with TestClient(app) as c:
        yield c


def test_delete_grants_refuses_to_orphan_owners_group(
    client: TestClient,
    session_factory: sessionmaker[Session],
    seeded: tuple[WorkspaceContext, str, str],
) -> None:
    ctx, owner_id, session_id = seeded

    # Local router harness: dependency overrides inject the same owner-backed
    # WorkspaceContext a session-authenticated request would carry, while the
    # HTTP client still exercises the mounted /w/<slug>/api/v1 route path.
    response = client.delete(f"/w/{ctx.workspace_slug}/api/v1/users/{owner_id}/grants")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
    body = response.json()
    assert body["status"] == 422
    assert body["error"] == "would_orphan_owners_group"
    # Spec 12 defines canonical RFC 7807 types under /errors/. The Beads
    # task's older /problems/ wording is intentionally not copied here.
    assert body["type"] == f"{CANONICAL_TYPE_BASE}would_orphan_owners_group"

    with session_factory() as s, tenant_agnostic():
        audits = s.scalars(
            select(AuditLog).where(
                AuditLog.workspace_id == ctx.workspace_id,
                AuditLog.action == "member_remove_rejected",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["reason"] == "would_orphan_owners_group"
        assert audits[0].diff["user_id"] == owner_id

        live_grants = s.scalars(
            select(RoleGrant).where(
                RoleGrant.workspace_id == ctx.workspace_id,
                RoleGrant.user_id == owner_id,
                RoleGrant.revoked_at.is_(None),
            )
        ).all()
        assert [grant.grant_role for grant in live_grants] == ["manager"]

        owners_group_id = s.scalar(
            select(PermissionGroup.id).where(
                PermissionGroup.workspace_id == ctx.workspace_id,
                PermissionGroup.slug == "owners",
                PermissionGroup.system.is_(True),
            )
        )
        assert owners_group_id is not None
        owner_membership = s.get(PermissionGroupMember, (owners_group_id, owner_id))
        assert owner_membership is not None

        live_session = s.get(SessionRow, session_id)
        assert live_session is not None
        assert live_session.user_id == owner_id
        assert live_session.workspace_id == ctx.workspace_id
        assert live_session.invalidated_at is None
