"""Unit tests for :func:`app.api.v1.auth.me._load_available_workspaces`.

Covers the two collapse rules the helper owns:

* multiple :class:`RoleGrant` rows on the same workspace collapse onto
  the highest-privilege grant (``manager > worker > client > guest``),
  and
* owners-permission-group membership without a manager surface grant
  is surfaced as ``manager`` (per §03 governance collapse onto the
  manager surface in v1).

The tests drive the helper directly against an in-memory SQLite engine
so the precedence ladder + owners promotion are verified without HTTP
plumbing. The full HTTP path (cookie validation, schema shape, 401
branches) is exercised by
:mod:`tests.integration.auth.test_auth_me`.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions",
``docs/specs/12-rest-api.md`` §"Auth", and
``docs/specs/14-web-frontend.md`` §"Workspace selector".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

# Import every model-bearing package so :data:`Base.metadata` resolves
# every FK the identity / authz tables reference.
from app.adapters.db import audit, authz, identity, workspace  # noqa: F401
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.v1.auth.me import _load_available_workspaces
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_NOW: datetime = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_workspace(s: Session, *, slug: str, name: str) -> Workspace:
    ws = Workspace(
        id=new_ulid(),
        slug=slug,
        name=name,
        plan="free",
        quota_json={},
        settings_json={},
        created_at=_NOW,
    )
    s.add(ws)
    s.flush()
    return ws


def _add_grant(
    s: Session,
    *,
    workspace_id: str,
    user_id: str,
    grant_role: str,
    binding_org_id: str | None = None,
) -> RoleGrant:
    grant = RoleGrant(
        id=new_ulid(),
        workspace_id=workspace_id,
        user_id=user_id,
        grant_role=grant_role,
        scope_kind="workspace",
        scope_property_id=None,
        binding_org_id=binding_org_id,
        created_at=_NOW,
        created_by_user_id=None,
    )
    s.add(grant)
    s.flush()
    return grant


def _add_owners_membership(s: Session, *, workspace_id: str, user_id: str) -> None:
    """Seed an ``owners`` :class:`PermissionGroup` + a membership row.

    Mirrors the shape :func:`app.adapters.db.authz.bootstrap.seed_owners_system_group`
    materialises in production: a system group with slug ``owners`` plus
    one :class:`PermissionGroupMember` row pinning ``user_id`` to it.
    """
    group = PermissionGroup(
        id=new_ulid(),
        workspace_id=workspace_id,
        slug="owners",
        name="Owners",
        system=True,
        capabilities_json={},
        created_at=_NOW,
    )
    s.add(group)
    s.flush()
    s.add(
        PermissionGroupMember(
            group_id=group.id,
            user_id=user_id,
            workspace_id=workspace_id,
            added_at=_NOW,
            added_by_user_id=None,
        )
    )
    s.flush()


# ---------------------------------------------------------------------------
# Tests — highest-privilege collapse
# ---------------------------------------------------------------------------


class TestHighestPrivilegeCollapse:
    """Multiple grants on one workspace surface the highest privilege."""

    def test_manager_plus_worker_collapses_to_manager(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Same workspace, two grants — ``manager`` outranks ``worker``."""
        with session_factory() as s, tenant_agnostic():
            user = bootstrap_user(s, email="dual@example.com", display_name="Dual")
            ws = _add_workspace(s, slug="ws-dual", name="Dual")
            _add_grant(
                s,
                workspace_id=ws.id,
                user_id=user.id,
                grant_role="worker",
            )
            _add_grant(
                s,
                workspace_id=ws.id,
                user_id=user.id,
                grant_role="manager",
            )
            s.commit()
            user_id = user.id

        with session_factory() as s:
            available = _load_available_workspaces(s, user_id=user_id)

        assert len(available) == 1
        row = available[0]
        assert row.workspace.id == "ws-dual"
        assert row.workspace.name == "Dual"
        assert row.grant_role == "manager"
        assert row.source == "workspace_grant"

    def test_worker_plus_client_collapses_to_worker(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """``worker`` outranks ``client``; the lower-rank row is shadowed."""
        with session_factory() as s, tenant_agnostic():
            user = bootstrap_user(
                s, email="wc@example.com", display_name="Worker Client"
            )
            ws = _add_workspace(s, slug="ws-wc", name="WC")
            _add_grant(s, workspace_id=ws.id, user_id=user.id, grant_role="client")
            _add_grant(s, workspace_id=ws.id, user_id=user.id, grant_role="worker")
            s.commit()
            user_id = user.id

        with session_factory() as s:
            available = _load_available_workspaces(s, user_id=user_id)

        assert len(available) == 1
        assert available[0].grant_role == "worker"


# ---------------------------------------------------------------------------
# Tests — owners-group promotion
# ---------------------------------------------------------------------------


class TestOwnersGroupPromotion:
    """Owners-group membership surfaces as ``manager`` per §03."""

    def test_owners_member_with_worker_grant_surfaces_as_manager(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """An owners-group member whose only surface grant is ``worker``
        is still surfaced as ``manager`` so the SPA routes them to the
        manager landing.
        """
        with session_factory() as s, tenant_agnostic():
            user = bootstrap_user(s, email="own@example.com", display_name="Owner")
            ws = _add_workspace(s, slug="ws-own", name="Own")
            _add_grant(s, workspace_id=ws.id, user_id=user.id, grant_role="worker")
            _add_owners_membership(s, workspace_id=ws.id, user_id=user.id)
            s.commit()
            user_id = user.id

        with session_factory() as s:
            available = _load_available_workspaces(s, user_id=user_id)

        assert len(available) == 1
        # Owners-group membership promotes the grant_role to manager
        # even though the underlying RoleGrant is ``worker``.
        assert available[0].grant_role == "manager"
        assert available[0].workspace.id == "ws-own"
