"""HTTP-level tests for ``POST /users/{id}/reinstate`` (cd-pb8p).

Exercises both the workspace-local default and the deployment-wide
reinstate path:

* ``?scope=workspace`` (default) — reinstates the engagement +
  ``user_work_role`` rows in the caller's workspace only.
* ``?scope=deployment`` — gated on ``owners@deployment``; clears
  ``users.archived_at`` AND every engagement + ``user_work_role`` the
  user holds across every workspace.

See ``docs/specs/12-rest-api.md`` §"Users" and
``docs/specs/05-employees-and-roles.md`` §"Archive / reinstate".
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
    WorkRole,
)
from app.api.v1.users import build_users_router
from app.auth._throttle import Throttle
from app.authz.deployment_owners import add_deployment_owner
from app.config import Settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user
from tests.unit.api.v1.identity.conftest import _PINNED, build_client


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-users-reinstate-root-key"),
        public_url="https://test.crew.day",
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


def _client(
    ctx: WorkspaceContext,
    factory: sessionmaker[Session],
    *,
    mailer: InMemoryMailer,
    throttle: Throttle,
    settings: Settings,
) -> TestClient:
    return build_client(
        [
            (
                "",
                build_users_router(
                    mailer=mailer,
                    throttle=throttle,
                    settings=settings,
                    base_url=settings.public_url,
                ),
            )
        ],
        factory,
        ctx,
    )


def _seed_archived_target(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    email: str,
    display_name: str,
    role_key: str,
) -> tuple[str, str, str]:
    """Seed a workspace member with an archived engagement + role.

    Returns ``(user_id, engagement_id, user_work_role_id)``.
    """
    with factory() as s, tenant_agnostic():
        user = bootstrap_user(s, email=email, display_name=display_name)
        s.add(
            UserWorkspace(
                user_id=user.id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        s.flush()
        eng = WorkEngagement(
            id=new_ulid(),
            user_id=user.id,
            workspace_id=workspace_id,
            engagement_kind="payroll",
            supplier_org_id=None,
            pay_destination_id=None,
            reimbursement_destination_id=None,
            started_on=_PINNED.date(),
            archived_on=_PINNED.date(),
            notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        s.add(eng)
        s.flush()
        wrole = WorkRole(
            id=new_ulid(),
            workspace_id=workspace_id,
            key=role_key,
            name=role_key.title(),
            description_md="",
            default_settings_json={},
            icon_name="",
            created_at=_PINNED,
            deleted_at=None,
        )
        s.add(wrole)
        s.flush()
        uwr = UserWorkRole(
            id=new_ulid(),
            user_id=user.id,
            workspace_id=workspace_id,
            work_role_id=wrole.id,
            started_on=_PINNED.date(),
            ended_on=_PINNED.date(),
            pay_rule_id=None,
            created_at=_PINNED,
            deleted_at=_PINNED,
        )
        s.add(uwr)
        s.flush()
        s.commit()
        return user.id, eng.id, uwr.id


class TestReinstateScopeWorkspace:
    """Default ``?scope=workspace`` path mirrors the workspace-local seam."""

    def test_owner_reinstates_engagement_in_caller_workspace(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        user_id, eng_id, uwr_id = _seed_archived_target(
            factory,
            workspace_id=ws_id,
            email="archived@example.com",
            display_name="Archived",
            role_key="maid",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        resp = client.post(f"/users/{user_id}/reinstate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == user_id
        assert body["engagement_archived_on"] is None

        with factory() as s, tenant_agnostic():
            eng = s.get(WorkEngagement, eng_id)
            uwr = s.get(UserWorkRole, uwr_id)
            assert eng is not None
            assert uwr is not None
            assert eng.archived_on is None
            assert uwr.deleted_at is None

    def test_explicit_workspace_query_param_routes_to_workspace_path(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """``?scope=workspace`` does NOT touch ``users.archived_at``."""
        ctx, factory, ws_id = owner_ctx
        user_id, _, _ = _seed_archived_target(
            factory,
            workspace_id=ws_id,
            email="alice@example.com",
            display_name="Alice",
            role_key="cook",
        )
        # Pre-stamp ``users.archived_at`` so we can verify the workspace
        # path leaves it alone.
        with factory() as s, tenant_agnostic():
            row = s.get(User, user_id)
            assert row is not None
            row.archived_at = _PINNED
            s.commit()

        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        resp = client.post(f"/users/{user_id}/reinstate?scope=workspace")
        assert resp.status_code == 200
        with factory() as s, tenant_agnostic():
            row = s.get(User, user_id)
            assert row is not None
            assert row.archived_at == _PINNED  # untouched

    def test_invalid_scope_value_is_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        resp = client.post("/users/01H_FAKE_USER_____/reinstate?scope=galaxy")
        assert resp.status_code == 422


class TestReinstateScopeDeployment:
    """``?scope=deployment`` clears ``users.archived_at`` and every engagement."""

    def test_deployment_owner_clears_identity_row_and_all_workspaces(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        user_id, eng_id, uwr_id = _seed_archived_target(
            factory,
            workspace_id=ws_id,
            email="archived@example.com",
            display_name="Archived",
            role_key="maid",
        )
        # Pre-stamp ``users.archived_at`` + register the caller as a
        # deployment owner.
        with factory() as s, tenant_agnostic():
            row = s.get(User, user_id)
            assert row is not None
            row.archived_at = _PINNED
            add_deployment_owner(
                s, user_id=ctx.actor_id, added_by_user_id=None, now=_PINNED
            )
            s.commit()

        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        resp = client.post(f"/users/{user_id}/reinstate?scope=deployment")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == user_id
        assert body["engagement_archived_on"] is None

        with factory() as s, tenant_agnostic():
            row = s.get(User, user_id)
            eng = s.get(WorkEngagement, eng_id)
            uwr = s.get(UserWorkRole, uwr_id)
            assert row is not None and row.archived_at is None
            assert eng is not None and eng.archived_on is None
            assert uwr is not None and uwr.deleted_at is None

    def test_workspace_owner_without_deployment_owner_is_403(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        user_id, _, _ = _seed_archived_target(
            factory,
            workspace_id=ws_id,
            email="archived@example.com",
            display_name="Archived",
            role_key="maid",
        )
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        resp = client.post(f"/users/{user_id}/reinstate?scope=deployment")
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"

    def test_unknown_user_is_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
        mailer: InMemoryMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        ctx, factory, _ = owner_ctx
        with factory() as s:
            add_deployment_owner(
                s, user_id=ctx.actor_id, added_by_user_id=None, now=_PINNED
            )
            s.commit()
        client = _client(
            ctx, factory, mailer=mailer, throttle=throttle, settings=settings
        )
        resp = client.post("/users/01H_DOES_NOT_EXIST_____/reinstate?scope=deployment")
        assert resp.status_code == 404
        assert resp.json()["error"] == "employee_not_found"


# Silence unused-import warnings for symbols imported only to register
# metadata on :class:`Base`.
_ = (date,)
