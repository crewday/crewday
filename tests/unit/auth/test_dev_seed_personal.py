"""Unit tests for :mod:`scripts.dev_seed_personal`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db import session as _session_mod
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.bootstrap import STARTER_WORK_ROLE_KEYS
from app.adapters.db.workspace.models import WorkRole, Workspace
from scripts import dev_seed_personal


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def patched_uow(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(_session_mod, "_default_engine", engine, raising=False)
    monkeypatch.setattr(_session_mod, "_default_sessionmaker_", factory, raising=False)
    yield factory


def test_apply_seed_seeds_starter_work_roles_idempotently(
    patched_uow: sessionmaker[Session],
) -> None:
    payload = {
        "owner": {
            "email": "seed@dev.local",
            "display_name": "Seed Owner",
            "timezone": "UTC",
            "deployment_admin": False,
            "passkeys": [],
        },
        "workspace": {
            "slug": "smoke",
            "name": "Smoke",
            "role": "manager",
        },
        "deployment_settings": {},
    }

    first = dev_seed_personal.apply_seed(payload)
    second = dev_seed_personal.apply_seed(payload)

    assert first["workspace_created"] is True
    assert second["workspace_created"] is False
    with patched_uow() as session:
        workspace = session.scalars(
            select(Workspace).where(Workspace.slug == "smoke")
        ).one()
        roles = session.scalars(
            select(WorkRole)
            .where(WorkRole.workspace_id == workspace.id)
            .order_by(WorkRole.key)
        ).all()
        assert [role.key for role in roles] == sorted(STARTER_WORK_ROLE_KEYS)
        assert len({role.key for role in roles}) == len(STARTER_WORK_ROLE_KEYS)


def test_apply_seed_repairs_existing_workspace_with_empty_role_catalogue(
    patched_uow: sessionmaker[Session],
) -> None:
    payload = {
        "owner": {
            "email": "repair@dev.local",
            "display_name": "Repair Owner",
            "timezone": "UTC",
            "deployment_admin": False,
            "passkeys": [],
        },
        "workspace": {
            "slug": "smoke",
            "name": "Smoke",
            "role": "manager",
        },
        "deployment_settings": {},
    }

    dev_seed_personal.apply_seed(payload)
    with patched_uow() as session:
        workspace = session.scalars(
            select(Workspace).where(Workspace.slug == "smoke")
        ).one()
        for role in session.scalars(
            select(WorkRole).where(WorkRole.workspace_id == workspace.id)
        ):
            session.delete(role)
        session.commit()

    repaired = dev_seed_personal.apply_seed(payload)

    assert repaired["workspace_created"] is False
    with patched_uow() as session:
        workspace = session.scalars(
            select(Workspace).where(Workspace.slug == "smoke")
        ).one()
        roles = session.scalars(
            select(WorkRole)
            .where(WorkRole.workspace_id == workspace.id)
            .order_by(WorkRole.key)
        ).all()
        assert [role.key for role in roles] == sorted(STARTER_WORK_ROLE_KEYS)
