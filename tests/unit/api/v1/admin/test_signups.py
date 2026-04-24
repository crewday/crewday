"""Unit tests for :mod:`app.api.v1.admin.signups` (cd-g1ay).

Covers the placeholder ``GET /admin/signups`` scaffold:

* Authorised (owner) caller gets 200 + the canonical §12 collection
  envelope (``data: []``, ``next_cursor: null``, ``has_more: false``).
* Authorised (manager) caller also gets 200 — managers appear in
  ``audit_log.view.default_allow`` alongside owners.
* Unauthorised (worker) caller gets 403 ``permission_denied`` with
  the canonical error envelope.
* ``?cursor=...&limit=...`` query params are accepted and validated
  against the §12 pagination bounds.

Shape mirrors :mod:`tests.integration.test_shifts_api`'s role-scoped
client fixtures: an in-memory SQLite engine seeded with a workspace,
an actor row, a matching :class:`RoleGrant`, and the owners-group
seed via :func:`bootstrap_workspace`. Tests that need the
``owners``-group default-allow path bind the ctx's ``actor_id`` to
the owner the helper seeded. The worker test uses a second user
that deliberately lacks the owners-group row, so only the
``all_workers`` default-allow fires — which ``audit_log.view``
doesn't include, so the check denies.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations" and ``docs/specs/05-employees-and-roles.md``
§"Action catalog".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.api.deps import current_workspace_context, db_session
from app.api.v1.admin import router as admin_router
from app.tenancy import WorkspaceContext
from app.tenancy.context import ActorGrantRole
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Model loading + engine
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<ctx>.models`` module.

    Mirrors :mod:`tests.integration.test_shifts_api` — ``Base.metadata``
    only knows about tables whose model modules have been imported, so
    an in-memory SQLite engine that skips this walk would miss the
    ``role_grant`` / ``permission_group`` tables the authz gate reads.
    """
    import importlib
    import pkgutil

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
    """Per-test in-memory SQLite engine with every model loaded."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_worker(
    session: Session,
    *,
    workspace_id: str,
    email: str,
    display_name: str,
) -> str:
    """Seed a user + a ``worker`` role grant on ``workspace_id``.

    Does **not** add the user to the ``owners`` group, so the permission
    resolver's default-allow fallback sees the user as ``all_workers``
    only — which is not in ``audit_log.view.default_allow``, so the
    check denies.
    """
    user = bootstrap_user(session, email=email, display_name=display_name)
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user.id,
            grant_role="worker",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()
    return user.id


def _seed_manager(
    session: Session,
    *,
    workspace_id: str,
    email: str,
    display_name: str,
) -> str:
    """Seed a user + a ``manager`` role grant (no owners-group membership).

    ``audit_log.view.default_allow`` lists ``owners, managers``; the
    derived ``managers`` group membership (§02) is the ``role_grant``
    row alone, with no ``permission_group_member`` row required. The
    authz resolver walks the default-allow list after the scope walk
    yields nothing, and ``is_member_of("managers", …)`` returns True
    as soon as it finds this grant.
    """
    user = bootstrap_user(session, email=email, display_name=display_name)
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user.id,
            grant_role="manager",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()
    return user.id


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> FastAPI:
    """Mount :data:`admin_router` behind pinned ctx + db overrides.

    Mirrors the minimal app the other v1 router tests build — the
    router lives under the workspace prefix in production but unit
    tests drop the prefix so the dependency gate runs against a
    straight ``/admin/signups`` path.
    """
    app = FastAPI()
    # Factory mounts each context router at
    # ``/w/{slug}/api/v1/<context>`` — ``admin`` becomes the
    # URL segment. Mount under ``/admin`` here so the handler-
    # relative ``/signups`` path lands at ``/admin/signups`` the
    # way production does.
    app.include_router(admin_router, prefix="/admin")

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return app


def _ctx_for(
    *,
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    grant_role: ActorGrantRole,
    actor_was_owner_member: bool,
) -> WorkspaceContext:
    return build_workspace_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=actor_was_owner_member,
    )


# ---------------------------------------------------------------------------
# Fixtures — one workspace, three personas
# ---------------------------------------------------------------------------


@pytest.fixture
def owner_client(
    factory: sessionmaker[Session],
) -> tuple[TestClient, WorkspaceContext]:
    """Workspace + owner user (seeded via :func:`bootstrap_workspace`)."""
    with factory() as s:
        owner_user = bootstrap_user(s, email="owner@example.com", display_name="Owner")
        ws = bootstrap_workspace(
            s,
            slug="ws-admin-owner",
            name="Admin Owner WS",
            owner_user_id=owner_user.id,
        )
        s.commit()
        owner_id, ws_id, ws_slug = owner_user.id, ws.id, ws.slug
    ctx = _ctx_for(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=owner_id,
        grant_role="manager",
        actor_was_owner_member=True,
    )
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, ctx


@pytest.fixture
def manager_client(
    factory: sessionmaker[Session],
) -> tuple[TestClient, WorkspaceContext]:
    """Workspace with a separate manager — derived ``managers`` membership.

    The owners-group seat is held by a DIFFERENT user so the manager
    caller's default-allow fallback fires through the derived
    ``managers`` group (§02), exercising the path workers can't take.
    """
    with factory() as s:
        owner_user = bootstrap_user(s, email="owner@example.com", display_name="Owner")
        ws = bootstrap_workspace(
            s,
            slug="ws-admin-mgr",
            name="Admin Manager WS",
            owner_user_id=owner_user.id,
        )
        manager_id = _seed_manager(
            s,
            workspace_id=ws.id,
            email="manager@example.com",
            display_name="Manager",
        )
        s.commit()
        ws_id, ws_slug = ws.id, ws.slug
    ctx = _ctx_for(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=manager_id,
        grant_role="manager",
        actor_was_owner_member=False,
    )
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, ctx


@pytest.fixture
def worker_client(
    factory: sessionmaker[Session],
) -> tuple[TestClient, WorkspaceContext]:
    """Workspace with a worker user — only ``all_workers`` membership."""
    with factory() as s:
        owner_user = bootstrap_user(s, email="owner@example.com", display_name="Owner")
        ws = bootstrap_workspace(
            s,
            slug="ws-admin-worker",
            name="Admin Worker WS",
            owner_user_id=owner_user.id,
        )
        worker_id = _seed_worker(
            s,
            workspace_id=ws.id,
            email="worker@example.com",
            display_name="Worker",
        )
        s.commit()
        ws_id, ws_slug = ws.id, ws.slug
    ctx = _ctx_for(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=worker_id,
        grant_role="worker",
        actor_was_owner_member=False,
    )
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, ctx


# ---------------------------------------------------------------------------
# GET /admin/signups — authorisation
# ---------------------------------------------------------------------------


class TestSignupsAuthorisation:
    """The handler is gated on ``audit_log.view`` at workspace scope."""

    def test_owner_gets_200_empty_envelope(
        self,
        owner_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        client, _ctx = owner_client
        resp = client.get("/admin/signups")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # §12 pagination envelope shape — ``data`` / ``next_cursor`` /
        # ``has_more``. Empty payload today; cd-ovt4 populates ``data``.
        assert body == {"data": [], "next_cursor": None, "has_more": False}

    def test_manager_gets_200(
        self,
        manager_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        """A manager (derived ``managers`` group) also passes the gate."""
        client, _ctx = manager_client
        resp = client.get("/admin/signups")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"] == []
        assert body["has_more"] is False

    def test_worker_gets_403_permission_denied(
        self,
        worker_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        """Workers aren't in ``audit_log.view.default_allow`` → 403."""
        client, _ctx = worker_client
        resp = client.get("/admin/signups")
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "permission_denied"
        assert detail["action_key"] == "audit_log.view"


# ---------------------------------------------------------------------------
# GET /admin/signups — query parameter validation
# ---------------------------------------------------------------------------


class TestSignupsQueryParams:
    """The placeholder accepts ``cursor`` + ``limit`` today.

    Validation matters because cd-ovt4 wires the real query on top —
    any input the scaffold rejects today must still be rejected once
    the query goes live, and any input it accepts must be parseable
    without a schema bump.
    """

    def test_accepts_cursor_and_limit(
        self,
        owner_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        client, _ctx = owner_client
        resp = client.get("/admin/signups?cursor=abc&limit=25")
        assert resp.status_code == 200, resp.text

    def test_rejects_limit_above_ceiling(
        self,
        owner_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        """§12 pins the cap at 500 — ``limit=501`` should fail validation."""
        client, _ctx = owner_client
        resp = client.get("/admin/signups?limit=501")
        assert resp.status_code == 422

    def test_rejects_non_positive_limit(
        self,
        owner_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        client, _ctx = owner_client
        resp = client.get("/admin/signups?limit=0")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# OpenAPI shape
# ---------------------------------------------------------------------------


class TestSignupsOpenApi:
    """The handler declares an ``operation_id`` + ``x-cli`` per §12.

    Naming is ``workspace_admin.*`` / ``workspace-admin`` deliberately
    — the URL is ``/admin/signups`` (spec §15 verbatim) but the CLI
    group ``admin`` is reserved for host-CLI-only verbs per §13, and
    the OpenAPI tag ``admin`` belongs to the deployment-admin tree
    (:mod:`app.api.admin`). Workspace-scoped admin takes the third
    seat.
    """

    def test_operation_id_is_workspace_admin_signups_list(
        self,
        owner_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        client, _ctx = owner_client
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/admin/signups"]["get"]
        assert op["operationId"] == "workspace_admin.signups.list"
        assert op.get("x-cli", {}).get("group") == "workspace-admin"
        assert op["x-cli"].get("verb") == "signups-list"
        # Spec §12 ``x-cli.mutates`` — read endpoints set it explicitly
        # to false so agent runtime's approval mode executes silently.
        assert op["x-cli"].get("mutates") is False

    def test_operation_id_prefix_not_in_reserved_cli_groups(
        self,
        owner_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        """The operation-id prefix must not collide with §13's
        reserved CLI groups (``admin`` host-only; ``deploy`` deployment).
        """
        client, _ctx = owner_client
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/admin/signups"]["get"]
        prefix = op["operationId"].split(".")[0]
        assert prefix not in {"admin", "deploy"}, (
            f"operation_id prefix {prefix!r} collides with a reserved "
            "CLI group (§13 'crewday admin' / 'crewday deploy')"
        )

    def test_tag_is_workspace_admin_not_admin(
        self,
        owner_client: tuple[TestClient, WorkspaceContext],
    ) -> None:
        """OpenAPI tag must be ``workspace_admin`` — the deployment
        admin tree (:mod:`app.api.admin`) owns the ``admin`` tag.
        """
        client, _ctx = owner_client
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/admin/signups"]["get"]
        tags = op.get("tags", [])
        assert "workspace_admin" in tags
        assert "admin" not in tags
