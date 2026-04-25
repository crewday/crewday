"""Integration tests for ``/me/{schedule,leaves,availability_overrides}`` (cd-6uij).

Exercises the router through :class:`TestClient` against a real DB
engine with the same model surface the production schema ships. Each
test asserts on:

* HTTP boundary: status code, response shape, error envelope.
* Persistence: rows land in ``user_leave`` /
  ``user_availability_override`` tables; the aggregator's reads see
  them through the live ORM tenant filter.
* Cross-workspace isolation through the production tenant filter.

Pattern matches :mod:`tests.integration.identity.test_user_leaves_api`:
a per-test in-memory SQLite engine + ``Base.metadata.create_all``
keeps the fixture cost low without sacrificing the integration-tier
guarantee that the **real** ORM seam fires (tenant filter, FK checks).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.availability.models import (
    UserLeave,
    UserWeeklyAvailability,
)
from app.adapters.db.base import Base
from app.adapters.db.holidays.models import PublicHoliday
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.api.deps import current_workspace_context, db_session
from app.api.v1.me_schedule import build_me_schedule_router
from app.tenancy import WorkspaceContext, registry
from app.tenancy.context import ActorGrantRole
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<ctx>.models`` module."""
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
def api_engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def api_factory(api_engine: Engine) -> sessionmaker[Session]:
    """``sessionmaker`` with the tenant filter installed."""
    factory = sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Register the workspace-scoped tables this module touches."""
    registry.register("user_leave")
    registry.register("user_availability_override")
    registry.register("user_weekly_availability")
    registry.register("public_holiday")
    registry.register("occurrence")
    registry.register("audit_log")
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")


def _ctx(
    *,
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    grant_role: ActorGrantRole = "manager",
    actor_was_owner_member: bool = True,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=actor_was_owner_member,
        audit_correlation_id=new_ulid(),
    )


def _build_app(factory: sessionmaker[Session], ctx: WorkspaceContext) -> FastAPI:
    """Mount the me-schedule router behind pinned ctx + UoW overrides.

    Adds a :class:`~starlette.middleware.base.BaseHTTPMiddleware`
    that mirrors :class:`WorkspaceContextMiddleware`: it calls
    :func:`set_current` on the way in and :func:`reset_current` on
    the way out, so the tenant filter sees a live
    :class:`WorkspaceContext` at SELECT compile time.
    """
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response as StarletteResponse

    from app.tenancy.current import reset_current, set_current

    app = FastAPI()

    class _PinCtxMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> StarletteResponse:
            token = set_current(ctx)
            try:
                response = await call_next(request)
                assert isinstance(response, StarletteResponse)
                return response
            finally:
                reset_current(token)

    app.add_middleware(_PinCtxMiddleware)
    app.include_router(build_me_schedule_router())

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


def _seed_workspace_with_owner(
    factory: sessionmaker[Session], *, slug: str
) -> tuple[str, str, str]:
    """Seed a workspace + an owner user. Returns (ws_id, ws_slug, owner_id)."""
    with factory() as s:
        user = bootstrap_user(
            s, email=f"{slug}-owner@example.com", display_name=f"Owner {slug}"
        )
        ws = bootstrap_workspace(s, slug=slug, name=f"WS {slug}", owner_user_id=user.id)
        s.commit()
        return ws.id, ws.slug, user.id


def _seed_worker(factory: sessionmaker[Session], *, ws_id: str, email: str) -> str:
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=email.split("@")[0])
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=user.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.commit()
        return user.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_worker_creates_leave_and_sees_it_in_schedule(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Worker POSTs ``/me/leaves`` and the row lands in ``/me/schedule.pending``.

        End-to-end through the production tenant filter: persist the
        row via the POST, then read it back via the schedule
        aggregator to confirm both writes and reads honour the
        ``ctx.actor_id`` predicate.
        """
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-1"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-1-w@example.com"
        )
        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )

        post = client.post(
            "/me/leaves",
            json={
                "starts_on": "2026-05-04",
                "ends_on": "2026-05-06",
                "category": "vacation",
                "note_md": "Family trip",
            },
        )
        assert post.status_code == 201, post.text
        leave_id = post.json()["id"]
        assert post.json()["user_id"] == worker_id
        assert post.json()["approved_at"] is None

        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Worker self-submits land pending → they belong in
        # ``pending.leaves``, never in the top-level ``leaves`` list.
        assert body["leaves"] == []
        assert [lv["id"] for lv in body["pending"]["leaves"]] == [leave_id]

    def test_worker_creates_override_and_sees_it_in_schedule(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Worker POSTs ``/me/availability_overrides`` and the resolved state lands.

        Adding hours on an off pattern auto-approves per §06's hybrid
        approval matrix; the aggregator returns the row in the live
        ``overrides`` bucket.
        """
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-2"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-2-w@example.com"
        )
        # Seed an off-pattern row for Monday so the aggregator can
        # walk the §06 approval matrix deterministically.
        with api_factory() as s:
            s.add(
                UserWeeklyAvailability(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker_id,
                    weekday=0,
                    starts_local=None,
                    ends_local=None,
                    updated_at=_PINNED,
                )
            )
            s.commit()
        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )

        post = client.post(
            "/me/availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "13:00:00",
            },
        )
        assert post.status_code == 201, post.text
        override_id = post.json()["id"]
        assert post.json()["user_id"] == worker_id
        assert post.json()["approval_required"] is False
        assert post.json()["approved_at"] is not None

        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        body = resp.json()
        assert [ov["id"] for ov in body["overrides"]] == [override_id]
        assert body["pending"]["overrides"] == []

    def test_holiday_in_window_surfaces_in_schedule(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """A workspace holiday on a covered date appears under ``holidays``."""
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-3"
        )
        with api_factory() as s:
            s.add(
                PublicHoliday(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    name="May Day",
                    date=date(2026, 5, 1),
                    country=None,
                    scheduling_effect="block",
                    reduced_starts_local=None,
                    reduced_ends_local=None,
                    payroll_multiplier=None,
                    recurrence=None,
                    notes_md=None,
                    created_at=_PINNED,
                    updated_at=_PINNED,
                    deleted_at=None,
                )
            )
            s.commit()

        owner_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=owner_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client = TestClient(
            _build_app(api_factory, owner_ctx),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["holidays"]) == 1
        assert body["holidays"][0]["name"] == "May Day"

    def test_cross_workspace_isolation_through_tenant_filter(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """The production tenant filter blocks cross-workspace leaks.

        Seeds a leave in workspace B and queries from workspace A —
        the row must be invisible. This is the integration-tier
        guarantee that both the explicit ``workspace_id`` predicate
        in the aggregator AND the tenant filter installed on the
        ``sessionmaker`` work together to keep the surface tight.
        """
        ws_a_id, ws_a_slug, owner_a_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-a"
        )
        ws_b_id, _ws_b_slug, _owner_b_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-b"
        )
        # Seed a leave for the A owner inside workspace B (same user
        # id, different workspace) so a missing ``workspace_id``
        # predicate would surface it.
        with api_factory() as s:
            s.add(
                UserLeave(
                    id=new_ulid(),
                    workspace_id=ws_b_id,
                    user_id=owner_a_id,
                    starts_on=date(2026, 5, 4),
                    ends_on=date(2026, 5, 5),
                    category="vacation",
                    approved_at=_PINNED,
                    approved_by=owner_a_id,
                    note_md=None,
                    created_at=_PINNED,
                    updated_at=_PINNED,
                    deleted_at=None,
                )
            )
            s.commit()

        ctx_a = _ctx(
            workspace_id=ws_a_id,
            workspace_slug=ws_a_slug,
            actor_id=owner_a_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client = TestClient(
            _build_app(api_factory, ctx_a),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        body = resp.json()
        assert body["leaves"] == []
        assert body["pending"]["leaves"] == []

    def test_explicit_user_id_in_body_rejected(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """A worker passing ``user_id`` in the body collapses to 422.

        End-to-end check that the schema-layer ``extra="forbid"``
        guard fires through the real router (not just the unit-test
        seam).
        """
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-4"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-4-w@example.com"
        )
        other_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-4-other@example.com"
        )
        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )

        resp = client.post(
            "/me/leaves",
            json={
                "user_id": other_id,
                "starts_on": "2026-07-01",
                "ends_on": "2026-07-02",
                "category": "personal",
            },
        )
        assert resp.status_code == 422

        # Confirm no row was persisted to the other user.
        with UnitOfWorkImpl(session_factory=api_factory) as s:
            from app.tenancy.current import reset_current, set_current

            assert isinstance(s, Session)
            t = set_current(worker_ctx)
            try:
                from sqlalchemy import select

                rows = s.scalars(
                    select(UserLeave).where(UserLeave.user_id == other_id)
                ).all()
            finally:
                reset_current(t)
        assert rows == []

    def test_default_window_omitted_params(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Calling ``/me/schedule`` with no params returns the 14-day default window."""
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-5"
        )
        owner_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=owner_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client = TestClient(
            _build_app(api_factory, owner_ctx),
            raise_server_exceptions=False,
        )
        resp = client.get("/me/schedule")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        from_d = date.fromisoformat(body["from"])
        to_d = date.fromisoformat(body["to"])
        # 14-day window per §12.
        assert (to_d - from_d) == timedelta(days=14)
        # Empty payload — no rows seeded.
        assert body["rota"] == []
        assert body["tasks"] == []
        assert body["leaves"] == []
        assert body["overrides"] == []
        assert body["holidays"] == []
        assert body["pending"]["leaves"] == []
        assert body["pending"]["overrides"] == []
