"""Unit tests for deployment-owner membership lookup."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import DeploymentOwner, RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.authz.deployment_owners import (
    add_deployment_owner,
    deployment_owner_count,
    is_deployment_owner,
    remove_deployment_owner,
)
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 30, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
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


def test_deployment_owner_is_explicit_membership(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        owner = _seed_user(s, tag="owner")
        admin_only = _seed_user(s, tag="admin-only")
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=None,
                user_id=admin_only,
                grant_role="manager",
                scope_kind="deployment",
                created_at=_PINNED,
            )
        )
        s.add(
            DeploymentOwner(
                user_id=owner,
                added_at=_PINNED,
                added_by_user_id=None,
            )
        )
        s.commit()

        assert is_deployment_owner(s, user_id=owner) is True
        assert is_deployment_owner(s, user_id=admin_only) is False


def test_add_and_remove_deployment_owner_are_idempotent(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        user_id = _seed_user(s, tag="owner")
        first, first_created = add_deployment_owner(
            s,
            user_id=user_id,
            added_by_user_id=None,
            now=_PINNED,
        )
        second, second_created = add_deployment_owner(
            s,
            user_id=user_id,
            added_by_user_id=None,
            now=_PINNED,
        )

        assert first.user_id == second.user_id == user_id
        assert first_created is True
        assert second_created is False
        assert deployment_owner_count(s) == 1
        assert remove_deployment_owner(s, user_id=user_id) is True
        assert remove_deployment_owner(s, user_id=user_id) is False
        assert deployment_owner_count(s) == 0
