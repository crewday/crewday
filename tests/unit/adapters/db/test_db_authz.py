"""Unit tests for :class:`RoleGrant` (cd-wchi) — deployment-scope grants.

Pure-Python SQLAlchemy round-trip on an in-memory SQLite engine,
covering the cd-wchi schema additions:

* ``scope_kind`` enum (``workspace`` | ``deployment``);
* ``workspace_id`` widened to NULLABLE;
* biconditional CHECK
  ``(scope_kind='deployment' AND workspace_id IS NULL) OR
   (scope_kind='workspace' AND workspace_id IS NOT NULL)``;
* partial UNIQUE on ``(user_id, grant_role) WHERE
  scope_kind='deployment'``.

Integration coverage (Alembic upgrade / downgrade round-trip,
SQLite ↔ Postgres parity, FK cascade in a real DB) lives in
:mod:`tests.integration.test_db_authz` and
:mod:`tests.integration.test_schema_parity`.

See ``docs/specs/02-domain-model.md`` §"role_grants" and
``docs/specs/12-rest-api.md`` §"Admin surface".
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.workspace.models import Workspace

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models``.

    Makes ``Base.metadata.create_all`` resolve cross-package FKs (e.g.
    ``role_grant.workspace_id`` → ``workspace.id``) without depending
    on test-collection import order. Mirrors the sibling helper in
    :mod:`tests.unit.adapters.db.test_property_work_role_assignment`.
    """
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with every ORM table created.

    StaticPool keeps the same underlying SQLite DB across checkouts so
    the fixture's ``create_all`` is visible to the session the test
    opens.
    """
    _load_all_models()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh :class:`Session` bound to the in-memory engine.

    No tenant-filter install — the unit slice exercises the schema
    shape, not the filter wiring (which is covered in
    :mod:`tests.integration.test_db_authz`).
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


def _seed_workspace(session: Session, *, workspace_id: str, slug: str) -> Workspace:
    """Insert a minimal workspace so the FK lands cleanly."""
    ws = Workspace(
        id=workspace_id,
        slug=slug,
        name=slug.title(),
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


def _seed_user(session: Session, *, user_id: str, tag: str) -> User:
    """Insert a minimal user so the FK lands cleanly."""
    email = f"{tag}@example.com"
    user = User(
        id=user_id,
        email=email,
        email_lower=canonicalise_email(email),
        display_name=tag.capitalize(),
        created_at=_PINNED,
    )
    session.add(user)
    session.flush()
    return user


class TestWorkspaceGrantUnchanged:
    """Existing workspace-scoped grants keep round-tripping unchanged."""

    def test_workspace_grant_unchanged(self, session: Session) -> None:
        """A ``scope_kind='workspace'`` row with a workspace_id persists.

        The cd-wchi widening must not break legacy call sites that
        only set ``workspace_id`` — :class:`RoleGrant`'s Python-side
        default ``scope_kind='workspace'`` carries the value.
        """
        _seed_workspace(session, workspace_id="01HWA000000000000000000WSP", slug="ws")
        _seed_user(session, user_id="01HWA000000000000000000USR", tag="ws-grant")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RGR",
                workspace_id="01HWA000000000000000000WSP",
                user_id="01HWA000000000000000000USR",
                grant_role="manager",
                created_at=_PINNED,
            )
        )
        session.commit()
        loaded = session.scalars(
            select(RoleGrant).where(RoleGrant.id == "01HWA000000000000000000RGR")
        ).one()
        assert loaded.scope_kind == "workspace"
        assert loaded.workspace_id == "01HWA000000000000000000WSP"


class TestDeploymentGrantWorkspaceIdNull:
    """Deployment grants land with ``workspace_id IS NULL``."""

    def test_deployment_grant_workspace_id_null(self, session: Session) -> None:
        _seed_user(session, user_id="01HWA000000000000000000ADM", tag="depl-admin")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RGD",
                workspace_id=None,
                user_id="01HWA000000000000000000ADM",
                grant_role="manager",
                scope_kind="deployment",
                created_at=_PINNED,
            )
        )
        session.commit()
        loaded = session.scalars(
            select(RoleGrant).where(RoleGrant.id == "01HWA000000000000000000RGD")
        ).one()
        assert loaded.scope_kind == "deployment"
        assert loaded.workspace_id is None
        assert loaded.user_id == "01HWA000000000000000000ADM"


class TestDeploymentGrantNoWorkspaceIdRejected:
    """A deployment row carrying a workspace_id violates the pairing CHECK."""

    def test_deployment_grant_with_workspace_id_rejected(
        self, session: Session
    ) -> None:
        _seed_workspace(session, workspace_id="01HWA000000000000000000WSP", slug="ws-d")
        _seed_user(session, user_id="01HWA000000000000000000ADM", tag="depl-with-ws")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RGE",
                workspace_id="01HWA000000000000000000WSP",
                user_id="01HWA000000000000000000ADM",
                grant_role="manager",
                scope_kind="deployment",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


class TestWorkspaceGrantNoWorkspaceIdRejected:
    """A workspace row missing its workspace_id violates the pairing CHECK."""

    def test_workspace_grant_without_workspace_id_rejected(
        self, session: Session
    ) -> None:
        _seed_user(session, user_id="01HWA000000000000000000WNW", tag="ws-no-ws")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RGF",
                workspace_id=None,
                user_id="01HWA000000000000000000WNW",
                grant_role="manager",
                scope_kind="workspace",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


class TestPartialUniqueDeploymentUserRole:
    """Only one ``(user, role)`` deployment grant per user."""

    def test_two_deployment_grants_same_user_role_rejected(
        self, session: Session
    ) -> None:
        _seed_user(session, user_id="01HWA000000000000000000DUR", tag="depl-dup")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RG1",
                workspace_id=None,
                user_id="01HWA000000000000000000DUR",
                grant_role="manager",
                scope_kind="deployment",
                created_at=_PINNED,
            )
        )
        session.flush()
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RG2",
                workspace_id=None,
                user_id="01HWA000000000000000000DUR",
                grant_role="manager",
                scope_kind="deployment",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_different_role_same_user_allowed(self, session: Session) -> None:
        """Two deployment grants for the same user with different roles persist.

        Pins that the partial UNIQUE keys on ``(user_id, grant_role)``
        — not just ``user_id`` — so a deployment ``manager`` and a
        deployment ``worker`` for the same user co-exist.
        """
        _seed_user(session, user_id="01HWA000000000000000000DR2", tag="depl-roles")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RGA",
                workspace_id=None,
                user_id="01HWA000000000000000000DR2",
                grant_role="manager",
                scope_kind="deployment",
                created_at=_PINNED,
            )
        )
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RGB",
                workspace_id=None,
                user_id="01HWA000000000000000000DR2",
                grant_role="worker",
                scope_kind="deployment",
                created_at=_PINNED,
            )
        )
        session.commit()
        rows = session.scalars(
            select(RoleGrant).where(RoleGrant.user_id == "01HWA000000000000000000DR2")
        ).all()
        assert {r.grant_role for r in rows} == {"manager", "worker"}


class TestWorkspacePartialUnique:
    """cd-x1xh: workspace-side partial UNIQUE on the live partition.

    Before cd-x1xh the workspace surface had no app-level uniqueness
    on ``(workspace_id, user_id, grant_role)`` — re-grants relied on
    a hard DELETE of the prior row. Per §02 (soft-retire) the
    canonical model now keeps revoked rows for audit and the partial
    UNIQUE bounds the **live** rows: at most one
    ``(workspace_id, user_id, grant_role, COALESCE(scope_property_id,
    ''))`` row with ``revoked_at IS NULL``. ``COALESCE`` is needed to
    defeat SQL NULL-distinct semantics so two workspace-wide grants
    ``(ws, u, role, NULL)`` don't both slip through.
    """

    def test_two_live_workspace_grants_same_triple_rejected(
        self, session: Session
    ) -> None:
        """Two live grants on the same triple violate the partial UNIQUE."""
        _seed_workspace(session, workspace_id="01HWA000000000000000000WSU", slug="ws-u")
        _seed_user(session, user_id="01HWA000000000000000000USU", tag="ws-uniq")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RW1",
                workspace_id="01HWA000000000000000000WSU",
                user_id="01HWA000000000000000000USU",
                grant_role="manager",
                created_at=_PINNED,
            )
        )
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RW2",
                workspace_id="01HWA000000000000000000WSU",
                user_id="01HWA000000000000000000USU",
                grant_role="manager",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_revoked_row_does_not_block_regrant(self, session: Session) -> None:
        """A soft-revoked row sits outside the partial UNIQUE's WHERE.

        The first row carries ``revoked_at`` set; the second row
        (live, same triple) lands without conflict — proving the
        ``revoked_at IS NULL`` filter is active in the index.
        """
        _seed_workspace(session, workspace_id="01HWA000000000000000000WSV", slug="ws-v")
        _seed_user(session, user_id="01HWA000000000000000000USV", tag="ws-regrant")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RV1",
                workspace_id="01HWA000000000000000000WSV",
                user_id="01HWA000000000000000000USV",
                grant_role="manager",
                created_at=_PINNED,
                revoked_at=_PINNED,
                ended_on=_PINNED.date(),
            )
        )
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RV2",
                workspace_id="01HWA000000000000000000WSV",
                user_id="01HWA000000000000000000USV",
                grant_role="manager",
                created_at=_PINNED,
            )
        )
        session.commit()
        rows = session.scalars(
            select(RoleGrant).where(RoleGrant.user_id == "01HWA000000000000000000USV")
        ).all()
        assert {r.id for r in rows} == {
            "01HWA000000000000000000RV1",
            "01HWA000000000000000000RV2",
        }


class TestScopeKindEnum:
    """``scope_kind`` is gated by an enum CHECK."""

    def test_bogus_scope_kind_rejected(self, session: Session) -> None:
        _seed_user(session, user_id="01HWA000000000000000000BSK", tag="bogus-scope")
        session.add(
            RoleGrant(
                id="01HWA000000000000000000RGS",
                workspace_id=None,
                user_id="01HWA000000000000000000BSK",
                grant_role="manager",
                scope_kind="organization",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
