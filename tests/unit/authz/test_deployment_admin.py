"""Unit tests for :func:`app.authz.is_deployment_admin` (cd-wchi).

Mirrors the shape of :mod:`tests.unit.authz.test_membership` — an
in-memory SQLite engine seeded with workspace + user rows, and a
``RoleGrant`` row planted in either the workspace or deployment
partition. The helper's contract is "True iff the user holds any
active deployment-scope ``role_grant``"; these tests pin both
directions plus the revocation (hard-delete in v1) path.

See ``docs/specs/12-rest-api.md`` §"Admin surface" and
``docs/specs/02-domain-model.md`` §"role_grants".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.authz.deployment_admin import is_deployment_admin
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with every ORM table created."""
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _seed_user(session: Session, *, tag: str) -> str:
    """Insert a minimal user; return its id."""
    user_id = new_ulid()
    email = f"{tag}@example.com"
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=tag.capitalize(),
            created_at=_PINNED,
        )
    )
    session.flush()
    return user_id


def _seed_workspace(session: Session, *, slug: str) -> str:
    """Insert a minimal workspace; return its id."""
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=slug.title(),
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


class TestDeploymentAdminPositive:
    """A deployment-scope grant lights up the helper."""

    def test_is_deployment_admin_returns_true_when_any_active_deployment_grant(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A single ``scope_kind='deployment'`` row → True."""
        with factory() as s:
            user_id = _seed_user(s, tag="depl-admin")
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=None,
                    user_id=user_id,
                    grant_role="manager",
                    scope_kind="deployment",
                    created_at=_PINNED,
                )
            )
            s.commit()
            assert is_deployment_admin(s, user_id=user_id) is True


class TestDeploymentAdminNegative:
    """Workspace-only or absent grants do not authorise the admin surface."""

    def test_is_deployment_admin_returns_false_for_workspace_only_user(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A workspace-scope manager grant must NOT confer deployment admin.

        This is the spec's hard separation (§12 "Admin surface" — the
        deployment surface is gated by deployment-scope grants only).
        Any workspace-scope grant — even ``manager`` on a workspace
        the user owns — leaves the helper returning False.
        """
        with factory() as s:
            user_id = _seed_user(s, tag="ws-only")
            workspace_id = _seed_workspace(s, slug="ws-only-ws")
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user_id,
                    grant_role="manager",
                    # scope_kind defaults to 'workspace' — pin
                    # explicitly so the assertion's intent is clear.
                    scope_kind="workspace",
                    created_at=_PINNED,
                )
            )
            s.commit()
            assert is_deployment_admin(s, user_id=user_id) is False

    def test_is_deployment_admin_returns_false_for_user_with_no_grants(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A user with no grants at all → False."""
        with factory() as s:
            user_id = _seed_user(s, tag="no-grants")
            s.commit()
            assert is_deployment_admin(s, user_id=user_id) is False


class TestDeploymentAdminExcludesRevoked:
    """A revoked deployment grant must not authorise the admin surface.

    v1 has no ``revoked_at`` column on :class:`RoleGrant` (the slice
    docstring spells this out — cd-79r adds it); revocation today is
    a hard delete. We pin the equivalent contract: deleting the row
    flips the helper from True to False on the next call.
    """

    def test_is_deployment_admin_excludes_revoked(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            user_id = _seed_user(s, tag="depl-revoked")
            grant_id = new_ulid()
            s.add(
                RoleGrant(
                    id=grant_id,
                    workspace_id=None,
                    user_id=user_id,
                    grant_role="manager",
                    scope_kind="deployment",
                    created_at=_PINNED,
                )
            )
            s.commit()
            assert is_deployment_admin(s, user_id=user_id) is True

            grant = s.get(RoleGrant, grant_id)
            assert grant is not None
            s.delete(grant)
            s.commit()
            assert is_deployment_admin(s, user_id=user_id) is False


class TestDeploymentAdminMultipleUsers:
    """The query is keyed on ``user_id`` — other admins do not bleed."""

    def test_returns_per_user(self, factory: sessionmaker[Session]) -> None:
        """One user's deployment grant does not authorise another user."""
        with factory() as s:
            admin_id = _seed_user(s, tag="depl-admin-1")
            other_id = _seed_user(s, tag="depl-not-admin")
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=None,
                    user_id=admin_id,
                    grant_role="manager",
                    scope_kind="deployment",
                    created_at=_PINNED,
                )
            )
            s.commit()
            assert is_deployment_admin(s, user_id=admin_id) is True
            assert is_deployment_admin(s, user_id=other_id) is False
