"""Integration test — PAT-reachable ``/w/<slug>/api/v1/me`` data routes (cd-fktzw).

Drives the real :class:`WorkspaceContextMiddleware` in front of the real
:func:`app.api.v1.me_data.build_me_data_router` router, so the assertions
prove the end-to-end PAT surface §03 "Personal access tokens" documents:

* a PAT with ``me.tasks:read`` reads its own assigned tasks **plus**
  unassigned tasks matching its subject's ``user_work_role`` (cd-isllv,
  §03) — another member's assigned task and an unassigned task for a role
  the subject lacks never appear;
* a PAT with ``me.bookings:read`` reads **only its own subject's**
  bookings + payslips (``GET /me/bookings`` → 200; another member's rows
  never appear);
* a PAT cannot exceed its ``me.*`` scope — ``me.tasks:read`` 403s on
  ``GET`` / ``POST /me/expenses`` and on ``GET /me/bookings``, and
  ``me.bookings:read`` 403s on ``GET /me/tasks`` — each with the
  ``insufficient_scope`` envelope + ``WWW-Authenticate`` challenge
  (§03 "Usage");
* the right scope admits the route — a ``me.expenses:read`` PAT reaches
  ``GET /me/expenses`` (200); a ``me.profile:read`` PAT reaches
  ``GET /me/profile`` (200, its own row).

The bare-host cookie ``/me`` surfaces are untouched by this task; the
scope gate's no-op for session principals is pinned by the unit test
``tests/unit/authz/test_require_me_scope.py``.

Mirrors the fixture + seed + sweep harness of
``test_personal_token_confinement.py`` (the sibling cd-yhya8 test).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.payroll.models import Booking, PayPeriod, Payslip
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import UserWorkRole, WorkEngagement, WorkRole
from app.api.errors import add_exception_handlers
from app.api.v1.me_data import build_me_data_router
from app.auth.tokens import mint as mint_token
from app.config import Settings
from app.tenancy.middleware import WorkspaceContextMiddleware
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_SLUG = "me-scoped"


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-me-scoped-routes-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        phase0_stub_enabled=False,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture
def wire_default_uow(
    engine: Engine,
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Redirect ``make_uow`` and the middleware's settings to the test fixtures."""
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = session_factory
    monkeypatch.setattr("app.tenancy.middleware.get_settings", lambda: settings)
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


def _build_app() -> FastAPI:
    """Real middleware in front of the real ``/me`` data router."""
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)
    app.include_router(build_me_data_router(), prefix="/w/{slug}/api/v1")
    # RFC 7807 envelope wiring so a route-level ``InsufficientScope`` /
    # domain error renders as ``application/problem+json`` (§12), matching
    # production; without it FastAPI would emit a plain ``{"detail": ...}``.
    add_exception_handlers(app)
    return app


def _seed_two_members(
    session_factory: sessionmaker[Session],
) -> tuple[str, str, str, str]:
    """Seed a workspace with an owner + two worker members (Alice, Bob).

    Returns ``(workspace_id, owner_id, alice_id, bob_id)``. Each worker
    holds a live ``worker`` grant + a reconciled ``user_workspace`` row so
    their PATs resolve a live ctx.
    """
    from app.adapters.db.authz.models import RoleGrant
    from app.domain.identity.user_workspace_refresh import reconcile_user_workspace

    with session_factory() as s:
        owner = bootstrap_user(
            s, email="owner-me-scoped@example.com", display_name="Owner"
        )
        alice = bootstrap_user(
            s, email="alice-me-scoped@example.com", display_name="Alice"
        )
        bob = bootstrap_user(s, email="bob-me-scoped@example.com", display_name="Bob")
        ws = bootstrap_workspace(
            s, slug=_SLUG, name="Me Scoped", owner_user_id=owner.id
        )
        for member_id in (alice.id, bob.id):
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws.id,
                    user_id=member_id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
        s.commit()
        ws_id, owner_id, alice_id, bob_id = ws.id, owner.id, alice.id, bob.id
    with session_factory() as s:
        reconcile_user_workspace(s, now=_PINNED)
        s.commit()
    return ws_id, owner_id, alice_id, bob_id


def _seed_task(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    assignee_user_id: str | None,
    title: str,
    expected_role_id: str | None = None,
) -> str:
    """Insert one ad-hoc, non-personal occurrence.

    ``assignee_user_id=None`` seeds an **unassigned** task; pair it with
    ``expected_role_id`` to exercise the §03 ``me.tasks:read`` "unassigned
    tasks matching the subject's ``user_work_role``" arm.
    """
    task_id = new_ulid()
    with session_factory() as s:
        s.add(
            Occurrence(
                id=task_id,
                workspace_id=workspace_id,
                assignee_user_id=assignee_user_id,
                expected_role_id=expected_role_id,
                title=title,
                state="pending",
                is_personal=False,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(hours=1),
                created_at=_PINNED,
            )
        )
        s.commit()
    return task_id


def _seed_work_role_for_user(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    key: str,
) -> str:
    """Create a workspace work role and an active user_work_role link.

    Returns the ``work_role_id`` — an unassigned task with a matching
    ``expected_role_id`` is the "eligible pool" the subject should see.
    """
    work_role_id = new_ulid()
    with session_factory() as s:
        s.add(
            WorkRole(
                id=work_role_id,
                workspace_id=workspace_id,
                key=key,
                name=key.title(),
                created_at=_PINNED,
            )
        )
        s.flush()
        s.add(
            UserWorkRole(
                id=new_ulid(),
                user_id=user_id,
                workspace_id=workspace_id,
                work_role_id=work_role_id,
                started_on=_PINNED.date(),
                created_at=_PINNED,
            )
        )
        s.commit()
    return work_role_id


def _seed_booking(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
) -> str:
    """Insert one live booking (+ its work engagement) for ``user_id``."""
    booking_id = new_ulid()
    engagement_id = new_ulid()
    with session_factory() as s:
        s.add(
            WorkEngagement(
                id=engagement_id,
                user_id=user_id,
                workspace_id=workspace_id,
                engagement_kind="payroll",
                started_on=_PINNED.date(),
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        s.flush()
        s.add(
            Booking(
                id=booking_id,
                workspace_id=workspace_id,
                work_engagement_id=engagement_id,
                user_id=user_id,
                status="scheduled",
                scheduled_start=_PINNED,
                scheduled_end=_PINNED + timedelta(hours=2),
                actual_minutes_paid=0,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        s.commit()
    return booking_id


def _seed_payslip(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    period_index: int,
) -> str:
    """Insert one pay period + one payslip for ``user_id``; returns payslip id.

    ``period_index`` offsets the period window so distinct callers do not
    collide on the ``(workspace_id, starts_at, ends_at)`` UNIQUE.
    """
    payslip_id = new_ulid()
    window_start = _PINNED + timedelta(days=30 * period_index)
    with session_factory() as s:
        period = PayPeriod(
            id=new_ulid(),
            workspace_id=workspace_id,
            starts_at=window_start,
            ends_at=window_start + timedelta(days=14),
            created_at=_PINNED,
        )
        s.add(period)
        s.flush()
        s.add(
            Payslip(
                id=payslip_id,
                workspace_id=workspace_id,
                pay_period_id=period.id,
                user_id=user_id,
                shift_hours_decimal=Decimal("8"),
                gross_cents=10_000,
                net_cents=10_000,
                created_at=_PINNED,
            )
        )
        s.commit()
    return payslip_id


def _mint_pat(
    session_factory: sessionmaker[Session],
    *,
    subject_user_id: str,
    label: str,
    scopes: dict[str, bool],
) -> str:
    """Mint a personal access token for ``subject_user_id`` with ``scopes``."""
    with session_factory() as s:
        minted = mint_token(
            s,
            None,
            user_id=subject_user_id,
            label=label,
            scopes=scopes,
            expires_at=None,
            kind="personal",
            subject_user_id=subject_user_id,
            now=_PINNED,
        )
        s.commit()
        return minted.token


def _sweep(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_ids: tuple[str, ...],
) -> None:
    """Delete every row this test landed."""
    from sqlalchemy import select

    from app.adapters.db.audit.models import AuditLog
    from app.adapters.db.authz.models import (
        PermissionGroup,
        PermissionGroupMember,
        RoleGrant,
    )
    from app.adapters.db.identity.models import User
    from app.adapters.db.workspace.models import UserWorkspace, Workspace
    from app.tenancy import tenant_agnostic

    with session_factory() as s, tenant_agnostic():
        delete_api_tokens_for_scope(s, workspace_ids=(workspace_id,), user_ids=user_ids)
        for scoped_model in (
            Occurrence,
            Booking,
            Payslip,
            PayPeriod,
            WorkEngagement,
            UserWorkRole,
            WorkRole,
        ):
            for row in s.scalars(
                select(scoped_model).where(scoped_model.workspace_id == workspace_id)
            ).all():
                s.delete(row)
        for model in (RoleGrant, PermissionGroupMember, PermissionGroup, UserWorkspace):
            for row in s.scalars(
                select(model).where(model.workspace_id == workspace_id)
            ).all():
                s.delete(row)
        for audit in s.scalars(
            select(AuditLog).where(AuditLog.workspace_id == workspace_id)
        ).all():
            s.delete(audit)
        ws = s.get(Workspace, workspace_id)
        if ws is not None:
            s.delete(ws)
        for user_id in user_ids:
            user_row = s.get(User, user_id)
            if user_row is not None:
                s.delete(user_row)
        s.commit()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestMeScopedRoutes:
    """A PAT exercises exactly the ``me.*`` scopes it was minted with."""

    def test_me_data_routes(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, owner_id, alice_id, bob_id = _seed_two_members(session_factory)
        alice_task = _seed_task(
            session_factory,
            workspace_id=ws_id,
            assignee_user_id=alice_id,
            title="Alice task",
        )
        bob_task = _seed_task(
            session_factory,
            workspace_id=ws_id,
            assignee_user_id=bob_id,
            title="Bob task",
        )
        # Alice holds the "cook" work role; an unassigned cook task is in her
        # eligible pool, an unassigned task for a role she lacks is not.
        alice_role_id = _seed_work_role_for_user(
            session_factory,
            workspace_id=ws_id,
            user_id=alice_id,
            key="me-scoped-cook",
        )
        open_cook_task = _seed_task(
            session_factory,
            workspace_id=ws_id,
            assignee_user_id=None,
            title="Unassigned cook task",
            expected_role_id=alice_role_id,
        )
        open_other_role_task = _seed_task(
            session_factory,
            workspace_id=ws_id,
            assignee_user_id=None,
            title="Unassigned driver task",
            expected_role_id=new_ulid(),
        )
        alice_booking = _seed_booking(
            session_factory, workspace_id=ws_id, user_id=alice_id
        )
        bob_booking = _seed_booking(session_factory, workspace_id=ws_id, user_id=bob_id)
        alice_payslip = _seed_payslip(
            session_factory, workspace_id=ws_id, user_id=alice_id, period_index=0
        )
        bob_payslip = _seed_payslip(
            session_factory, workspace_id=ws_id, user_id=bob_id, period_index=1
        )
        try:
            tasks_pat = _mint_pat(
                session_factory,
                subject_user_id=alice_id,
                label="alice-tasks",
                scopes={"me.tasks:read": True},
            )
            bookings_pat = _mint_pat(
                session_factory,
                subject_user_id=alice_id,
                label="alice-bookings",
                scopes={"me.bookings:read": True},
            )
            expenses_pat = _mint_pat(
                session_factory,
                subject_user_id=alice_id,
                label="alice-expenses",
                scopes={"me.expenses:read": True},
            )
            profile_pat = _mint_pat(
                session_factory,
                subject_user_id=alice_id,
                label="alice-profile",
                scopes={"me.profile:read": True},
            )

            app = _build_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                self._assert_reads_own_and_role_tasks(
                    client,
                    tasks_pat,
                    own=alice_task,
                    other=bob_task,
                    open_matching=open_cook_task,
                    open_other_role=open_other_role_task,
                )
                self._assert_bookings_scope_reads_own(
                    client,
                    bookings_pat,
                    own_booking=alice_booking,
                    other_booking=bob_booking,
                    own_payslip=alice_payslip,
                    other_payslip=bob_payslip,
                )
                self._assert_bookings_and_tasks_scopes_are_isolated(
                    client, tasks_pat=tasks_pat, bookings_pat=bookings_pat
                )
                self._assert_scope_exceeded_on_expenses(client, tasks_pat)
                self._assert_expenses_scope_admits_list(client, expenses_pat)
                self._assert_profile_scope_reads_own(
                    client, profile_pat, subject_id=alice_id
                )
                self._assert_profile_scope_cannot_read_tasks(client, profile_pat)
        finally:
            _sweep(
                session_factory,
                workspace_id=ws_id,
                user_ids=(owner_id, alice_id, bob_id),
            )

    def _assert_reads_own_and_role_tasks(
        self,
        client: TestClient,
        pat: str,
        *,
        own: str,
        other: str,
        open_matching: str,
        open_other_role: str,
    ) -> None:
        r = client.get(f"/w/{_SLUG}/api/v1/me/tasks", headers=_bearer(pat))
        assert r.status_code == 200, r.text
        ids = {row["id"] for row in r.json()["data"]}
        # Own assigned task + the unassigned task whose expected role Alice
        # holds are both in the §03 me.tasks:read union.
        assert own in ids
        assert open_matching in ids
        # The structural self-key: another subject's assigned task never
        # surfaces, and an unassigned task for a role Alice lacks does not
        # leak in via the unassigned arm.
        assert other not in ids
        assert open_other_role not in ids

    def _assert_bookings_scope_reads_own(
        self,
        client: TestClient,
        pat: str,
        *,
        own_booking: str,
        other_booking: str,
        own_payslip: str,
        other_payslip: str,
    ) -> None:
        r = client.get(f"/w/{_SLUG}/api/v1/me/bookings", headers=_bearer(pat))
        assert r.status_code == 200, r.text
        body = r.json()
        booking_ids = {row["id"] for row in body["bookings"]}
        payslip_ids = {row["id"] for row in body["payslips"]}
        # Self-keyed on ctx.actor_id: only Alice's own bookings + payslips.
        assert own_booking in booking_ids
        assert other_booking not in booking_ids
        assert own_payslip in payslip_ids
        assert other_payslip not in payslip_ids

    def _assert_bookings_and_tasks_scopes_are_isolated(
        self, client: TestClient, *, tasks_pat: str, bookings_pat: str
    ) -> None:
        # A me.bookings:read PAT cannot reach /me/tasks, and a me.tasks:read
        # PAT cannot reach /me/bookings — each me.* verb admits only its route.
        for pat, path, want_scope in (
            (bookings_pat, "me/tasks", "me.tasks:read"),
            (tasks_pat, "me/bookings", "me.bookings:read"),
        ):
            r = client.get(f"/w/{_SLUG}/api/v1/{path}", headers=_bearer(pat))
            assert r.status_code == 403, (path, r.text)
            challenge = r.headers["WWW-Authenticate"]
            assert 'error="insufficient_scope"' in challenge
            assert f'scope="{want_scope}"' in challenge
            body = r.json()
            assert body["error"] == "insufficient_scope"
            assert body["scope"] == want_scope

    def _assert_scope_exceeded_on_expenses(self, client: TestClient, pat: str) -> None:
        # A me.tasks:read PAT cannot reach the expenses surface at all. The
        # GET gate names me.expenses:read; the POST gate names the :write it
        # demands — proving read cannot masquerade as write.
        for method, want_scope in (
            ("get", "me.expenses:read"),
            ("post", "me.expenses:write"),
        ):
            call = getattr(client, method)
            kwargs: dict[str, Any] = {"headers": _bearer(pat)}
            if method == "post":
                kwargs["json"] = {}
            r = call(f"/w/{_SLUG}/api/v1/me/expenses", **kwargs)
            assert r.status_code == 403, (method, r.text)
            assert r.headers["content-type"].startswith("application/problem+json")
            challenge = r.headers["WWW-Authenticate"]
            # RFC 6750 challenge names the scope the PAT lacks (§03 "Usage").
            assert 'error="insufficient_scope"' in challenge
            assert f'scope="{want_scope}"' in challenge
            body = r.json()
            # Route-level gate maps through HTTPException, so ``type`` is the
            # status-derived ``forbidden`` while ``error`` carries the precise
            # discriminator agents switch on (matches the scoped-token gate).
            assert body["error"] == "insufficient_scope"
            assert body["scope"] == want_scope

    def _assert_expenses_scope_admits_list(self, client: TestClient, pat: str) -> None:
        # The matching scope admits the route — no claims seeded, so empty.
        r = client.get(f"/w/{_SLUG}/api/v1/me/expenses", headers=_bearer(pat))
        assert r.status_code == 200, r.text
        assert r.json()["data"] == []

    def _assert_profile_scope_reads_own(
        self, client: TestClient, pat: str, *, subject_id: str
    ) -> None:
        r = client.get(f"/w/{_SLUG}/api/v1/me/profile", headers=_bearer(pat))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == subject_id
        assert body["display_name"] == "Alice"

    def _assert_profile_scope_cannot_read_tasks(
        self, client: TestClient, pat: str
    ) -> None:
        # me.profile:read is not me.tasks:read — the gate refuses.
        r = client.get(f"/w/{_SLUG}/api/v1/me/tasks", headers=_bearer(pat))
        assert r.status_code == 403, r.text
        assert 'error="insufficient_scope"' in r.headers["WWW-Authenticate"]
