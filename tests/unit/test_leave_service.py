"""Unit tests for :mod:`app.services.leave.service`.

Exercises the service surface against an in-memory SQLite engine
built via ``Base.metadata.create_all()`` — no alembic, no tenant
filter, just the ORM round-trip + the pure-Python DTO validators and
authz seam.

Covers:

* Error-class hierarchy: 404 -> :class:`LookupError`,
  422 -> :class:`LeaveBoundaryInvalid` (a :class:`ValueError`),
  409 -> :class:`LeaveTransitionForbidden` (a :class:`ValueError`),
  403 -> :class:`LeavePermissionDenied` (a :class:`PermissionError`).
* DTO shape: ``extra="forbid"`` rejects unknown fields; the
  ``starts_at < ends_at`` invariant fires on the model boundary.
* :func:`create_leave`: self happy path, manager on-behalf-of,
  worker cross-user rejected, bad window rejected.
* :func:`cancel_own`: pending cancel, approved-future cancel,
  approved-past reject, already-cancelled reject, manager cancel
  on behalf of worker, peer worker cross-user rejected.
* :func:`update_dates`: pending happy path, non-pending rejected,
  bad window rejected, manager edit of worker's pending leave.
* :func:`list_for_user`: self scope, manager scope, worker
  cross-user 403, status filter.
* :func:`list_for_workspace`: manager happy path, worker 403.
* Tenant isolation: a leave in workspace A is invisible to ctx B.
* Audit: one row per mutation with the expected action + diff shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.time.models import Leave
from app.adapters.db.workspace.models import Workspace
from app.services.leave import (
    LeaveBoundaryInvalid,
    LeaveCreate,
    LeaveKindInvalid,
    LeaveNotFound,
    LeavePermissionDenied,
    LeaveTransitionForbidden,
    LeaveUpdateDates,
    LeaveView,
    cancel_own,
    create_leave,
    get_leave,
    list_for_user,
    list_for_workspace,
    update_dates,
)
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_FUTURE = _PINNED + timedelta(days=7)
_FUTURE_END = _FUTURE + timedelta(days=2)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
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
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh session per test; no tenant filter installed."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _bootstrap_workspace(s: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, email: str, display_name: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=display_name,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _grant(s: Session, *, workspace_id: str, user_id: str, grant_role: str) -> None:
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    slug: str = "ws",
    grant_role: ActorGrantRole = "worker",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


@pytest.fixture
def worker_env(
    session: Session,
) -> tuple[WorkspaceContext, str, FrozenClock]:
    """Worker with ``all_workers`` membership via a ``worker`` grant."""
    ws_id = _bootstrap_workspace(session, slug="worker-env")
    user_id = _bootstrap_user(session, email="w@example.com", display_name="W")
    _grant(session, workspace_id=ws_id, user_id=user_id, grant_role="worker")
    session.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="worker")
    return ctx, user_id, FrozenClock(_PINNED)


@pytest.fixture
def manager_env(
    session: Session,
) -> tuple[WorkspaceContext, str, FrozenClock]:
    """Manager with ``managers`` membership via a ``manager`` grant."""
    ws_id = _bootstrap_workspace(session, slug="manager-env")
    user_id = _bootstrap_user(session, email="m@example.com", display_name="M")
    _grant(session, workspace_id=ws_id, user_id=user_id, grant_role="manager")
    session.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="manager")
    return ctx, user_id, FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorTypes:
    def test_not_found_is_lookup_error(self) -> None:
        assert issubclass(LeaveNotFound, LookupError)

    def test_boundary_invalid_is_value_error(self) -> None:
        assert issubclass(LeaveBoundaryInvalid, ValueError)

    def test_kind_invalid_is_value_error(self) -> None:
        assert issubclass(LeaveKindInvalid, ValueError)

    def test_transition_forbidden_is_value_error(self) -> None:
        assert issubclass(LeaveTransitionForbidden, ValueError)

    def test_forbidden_is_permission_error(self) -> None:
        assert issubclass(LeavePermissionDenied, PermissionError)

    def test_errors_are_distinct(self) -> None:
        classes = {
            LeaveNotFound,
            LeaveBoundaryInvalid,
            LeaveKindInvalid,
            LeaveTransitionForbidden,
            LeavePermissionDenied,
        }
        assert len(classes) == 5


# ---------------------------------------------------------------------------
# LeaveView invariants
# ---------------------------------------------------------------------------


class TestLeaveView:
    def _view(self) -> LeaveView:
        return LeaveView(
            id="l",
            workspace_id="w",
            user_id="u",
            kind="vacation",
            starts_at=_FUTURE,
            ends_at=_FUTURE_END,
            status="pending",
            reason_md=None,
            decided_by=None,
            decided_at=None,
            created_at=_PINNED,
        )

    def test_view_is_slotted(self) -> None:
        view = self._view()
        with pytest.raises((AttributeError, TypeError)):
            view.extra = "nope"  # type: ignore[attr-defined]

    def test_view_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        view = self._view()
        with pytest.raises(FrozenInstanceError):
            view.starts_at = _PINNED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DTO validation
# ---------------------------------------------------------------------------


class TestLeaveCreateDto:
    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            LeaveCreate(
                kind="vacation",
                starts_at=_FUTURE,
                ends_at=_FUTURE_END,
                bogus="yes",  # type: ignore[call-arg]
            )

    def test_rejects_bad_kind_literal(self) -> None:
        with pytest.raises(ValidationError):
            LeaveCreate(
                kind="nope",
                starts_at=_FUTURE,
                ends_at=_FUTURE_END,
            )

    def test_rejects_zero_length_window(self) -> None:
        with pytest.raises(ValidationError):
            LeaveCreate(
                kind="vacation",
                starts_at=_FUTURE,
                ends_at=_FUTURE,
            )

    def test_rejects_negative_window(self) -> None:
        with pytest.raises(ValidationError):
            LeaveCreate(
                kind="vacation",
                starts_at=_FUTURE_END,
                ends_at=_FUTURE,
            )

    def test_happy_defaults(self) -> None:
        dto = LeaveCreate(
            kind="vacation",
            starts_at=_FUTURE,
            ends_at=_FUTURE_END,
        )
        assert dto.user_id is None
        assert dto.reason_md is None


class TestLeaveUpdateDatesDto:
    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            LeaveUpdateDates(
                starts_at=_FUTURE,
                ends_at=_FUTURE_END,
                bogus="y",  # type: ignore[call-arg]
            )

    def test_rejects_zero_length_window(self) -> None:
        with pytest.raises(ValidationError):
            LeaveUpdateDates(starts_at=_FUTURE, ends_at=_FUTURE)


# ---------------------------------------------------------------------------
# create_leave
# ---------------------------------------------------------------------------


def _create_body(
    *,
    user_id: str | None = None,
    kind: str = "vacation",
    starts_at: datetime = _FUTURE,
    ends_at: datetime = _FUTURE_END,
    reason_md: str | None = None,
) -> LeaveCreate:
    return LeaveCreate(
        user_id=user_id,
        kind=kind,
        starts_at=starts_at,
        ends_at=ends_at,
        reason_md=reason_md,
    )


class TestCreateLeave:
    def test_worker_creates_own_leave(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, user_id, clock = worker_env
        view = create_leave(session, ctx, body=_create_body(), clock=clock)
        assert view.user_id == user_id
        assert view.status == "pending"
        assert view.kind == "vacation"
        assert view.starts_at == _FUTURE
        assert view.ends_at == _FUTURE_END
        assert view.decided_by is None

    def test_manager_creates_leave_for_worker(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _mid, clock = manager_env
        worker_id = _bootstrap_user(session, email="w2@ex.com", display_name="W")
        _grant(
            session,
            workspace_id=ctx.workspace_id,
            user_id=worker_id,
            grant_role="worker",
        )
        session.commit()
        view = create_leave(
            session, ctx, body=_create_body(user_id=worker_id), clock=clock
        )
        assert view.user_id == worker_id

    def test_worker_cannot_create_leave_for_another_user(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        other_id = _bootstrap_user(session, email="o@ex.com", display_name="O")
        _grant(
            session,
            workspace_id=ctx.workspace_id,
            user_id=other_id,
            grant_role="worker",
        )
        session.commit()
        with pytest.raises(LeavePermissionDenied):
            create_leave(session, ctx, body=_create_body(user_id=other_id), clock=clock)

    def test_stranger_cannot_create_leave(
        self,
        session: Session,
    ) -> None:
        """A user with no workspace grant hits 403."""
        ws_id = _bootstrap_workspace(session, slug="stranger")
        uid = _bootstrap_user(session, email="s@ex.com", display_name="S")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=uid, grant_role="guest")
        with pytest.raises(LeavePermissionDenied):
            create_leave(session, ctx, body=_create_body(), clock=FrozenClock(_PINNED))

    def test_create_rejects_bad_window_via_service(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Bypassing the DTO with ``model_construct`` still traps a bad window."""
        ctx, _uid, clock = worker_env
        bad_body = LeaveCreate.model_construct(
            user_id=None,
            kind="vacation",
            starts_at=_FUTURE_END,
            ends_at=_FUTURE,  # inverted on purpose
            reason_md=None,
        )
        with pytest.raises(LeaveBoundaryInvalid):
            create_leave(session, ctx, body=bad_body, clock=clock)

    def test_create_rejects_bad_kind_via_service(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """``model_construct`` bypasses the Literal; the service defence
        catches the out-of-set ``kind`` with :class:`LeaveKindInvalid`
        rather than a bare ``ValueError`` (which would surface as 500).
        """
        ctx, _uid, clock = worker_env
        bad_body = LeaveCreate.model_construct(
            user_id=None,
            kind="festival",  # type: ignore[arg-type]  # bypass the Literal
            starts_at=_FUTURE,
            ends_at=_FUTURE_END,
            reason_md=None,
        )
        with pytest.raises(LeaveKindInvalid):
            create_leave(session, ctx, body=bad_body, clock=clock)

    def test_create_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        view = create_leave(session, ctx, body=_create_body(), clock=clock)
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        leave_rows = [r for r in rows if r.entity_kind == "leave"]
        assert [r.action for r in leave_rows] == ["leave.created"]
        assert leave_rows[0].entity_id == view.id
        assert "after" in leave_rows[0].diff


# ---------------------------------------------------------------------------
# cancel_own
# ---------------------------------------------------------------------------


class TestCancelOwn:
    def test_cancel_pending_own(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        cancelled = cancel_own(session, ctx, leave_id=created.id, clock=clock)
        assert cancelled.status == "cancelled"
        assert cancelled.id == created.id

    def test_cancel_approved_future(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Pre-approval, a worker may cancel an upcoming approved leave."""
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        # Simulate an approval by flipping the row state directly —
        # the approval service (cd-8pi) isn't shipped yet.
        row = session.get(Leave, created.id)
        assert row is not None
        row.status = "approved"
        row.decided_by = ctx.actor_id
        row.decided_at = clock.now()
        session.flush()

        # Clock is still _PINNED, leave starts at _FUTURE — cancellable.
        cancelled = cancel_own(session, ctx, leave_id=created.id, clock=clock)
        assert cancelled.status == "cancelled"

    def test_cancel_approved_past_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        row = session.get(Leave, created.id)
        assert row is not None
        row.status = "approved"
        session.flush()

        # Fast-forward past the leave start — not cancellable anymore.
        clock.set(_FUTURE + timedelta(hours=1))
        with pytest.raises(LeaveTransitionForbidden):
            cancel_own(session, ctx, leave_id=created.id, clock=clock)

    def test_cancel_already_cancelled_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        cancel_own(session, ctx, leave_id=created.id, clock=clock)
        with pytest.raises(LeaveTransitionForbidden):
            cancel_own(session, ctx, leave_id=created.id, clock=clock)

    def test_cancel_rejected_state_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        row = session.get(Leave, created.id)
        assert row is not None
        row.status = "rejected"
        session.flush()
        with pytest.raises(LeaveTransitionForbidden):
            cancel_own(session, ctx, leave_id=created.id, clock=clock)

    def test_manager_cancels_workers_leave(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="mgr-cancel")
        worker_id = _bootstrap_user(session, email="w@c.com", display_name="W")
        mgr_id = _bootstrap_user(session, email="m@c.com", display_name="M")
        _grant(session, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
        session.commit()

        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        clock = FrozenClock(_PINNED)

        created = create_leave(session, ctx_worker, body=_create_body(), clock=clock)
        cancelled = cancel_own(session, ctx_mgr, leave_id=created.id, clock=clock)
        assert cancelled.status == "cancelled"

    def test_peer_worker_cannot_cancel(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="peer-cancel")
        a_id = _bootstrap_user(session, email="a@p.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@p.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        session.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        clock = FrozenClock(_PINNED)

        created = create_leave(session, ctx_a, body=_create_body(), clock=clock)
        with pytest.raises(LeavePermissionDenied):
            cancel_own(session, ctx_b, leave_id=created.id, clock=clock)

    def test_cancel_missing_404(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        with pytest.raises(LeaveNotFound):
            cancel_own(session, ctx, leave_id="nope", clock=clock)

    def test_cancel_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        cancel_own(session, ctx, leave_id=created.id, clock=clock)
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "leave"]
        assert actions == ["leave.created", "leave.cancelled"]
        cancel_row = next(r for r in rows if r.action == "leave.cancelled")
        assert "before" in cancel_row.diff and "after" in cancel_row.diff
        assert cancel_row.diff["before"]["status"] == "pending"
        assert cancel_row.diff["after"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# update_dates
# ---------------------------------------------------------------------------


class TestUpdateDates:
    def test_update_pending_dates_happy(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        new_start = _FUTURE + timedelta(days=1)
        new_end = _FUTURE_END + timedelta(days=1)
        edited = update_dates(
            session,
            ctx,
            leave_id=created.id,
            body=LeaveUpdateDates(starts_at=new_start, ends_at=new_end),
            clock=clock,
        )
        assert edited.starts_at == new_start
        assert edited.ends_at == new_end
        assert edited.status == "pending"

    def test_update_approved_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        row = session.get(Leave, created.id)
        assert row is not None
        row.status = "approved"
        session.flush()
        with pytest.raises(LeaveTransitionForbidden):
            update_dates(
                session,
                ctx,
                leave_id=created.id,
                body=LeaveUpdateDates(
                    starts_at=_FUTURE + timedelta(days=2),
                    ends_at=_FUTURE_END + timedelta(days=2),
                ),
                clock=clock,
            )

    def test_update_cancelled_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        cancel_own(session, ctx, leave_id=created.id, clock=clock)
        with pytest.raises(LeaveTransitionForbidden):
            update_dates(
                session,
                ctx,
                leave_id=created.id,
                body=LeaveUpdateDates(
                    starts_at=_FUTURE + timedelta(days=3),
                    ends_at=_FUTURE_END + timedelta(days=3),
                ),
                clock=clock,
            )

    def test_update_missing_404(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        with pytest.raises(LeaveNotFound):
            update_dates(
                session,
                ctx,
                leave_id="nope",
                body=LeaveUpdateDates(starts_at=_FUTURE, ends_at=_FUTURE_END),
                clock=clock,
            )

    def test_manager_updates_workers_pending_leave(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="mgr-upd")
        worker_id = _bootstrap_user(session, email="w@u.com", display_name="W")
        mgr_id = _bootstrap_user(session, email="m@u.com", display_name="M")
        _grant(session, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
        session.commit()

        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        clock = FrozenClock(_PINNED)

        created = create_leave(session, ctx_worker, body=_create_body(), clock=clock)
        edited = update_dates(
            session,
            ctx_mgr,
            leave_id=created.id,
            body=LeaveUpdateDates(
                starts_at=_FUTURE + timedelta(days=5),
                ends_at=_FUTURE_END + timedelta(days=5),
            ),
            clock=clock,
        )
        assert edited.starts_at == _FUTURE + timedelta(days=5)

    def test_peer_worker_cannot_update(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="peer-upd")
        a_id = _bootstrap_user(session, email="a@u.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@u.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        session.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        clock = FrozenClock(_PINNED)

        created = create_leave(session, ctx_a, body=_create_body(), clock=clock)
        with pytest.raises(LeavePermissionDenied):
            update_dates(
                session,
                ctx_b,
                leave_id=created.id,
                body=LeaveUpdateDates(
                    starts_at=_FUTURE + timedelta(days=2),
                    ends_at=_FUTURE_END + timedelta(days=2),
                ),
                clock=clock,
            )

    def test_update_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        update_dates(
            session,
            ctx,
            leave_id=created.id,
            body=LeaveUpdateDates(
                starts_at=_FUTURE + timedelta(days=3),
                ends_at=_FUTURE_END + timedelta(days=3),
            ),
            clock=clock,
        )
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "leave"]
        assert actions == ["leave.created", "leave.updated"]


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListForUser:
    def test_list_self_default(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, user_id, clock = worker_env
        create_leave(session, ctx, body=_create_body(), clock=clock)
        views = list_for_user(session, ctx)
        assert len(views) == 1
        assert views[0].user_id == user_id

    def test_list_self_filter_by_status(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        a = create_leave(session, ctx, body=_create_body(), clock=clock)
        b = create_leave(
            session,
            ctx,
            body=_create_body(
                starts_at=_FUTURE + timedelta(days=10),
                ends_at=_FUTURE_END + timedelta(days=10),
            ),
            clock=clock,
        )
        cancel_own(session, ctx, leave_id=a.id, clock=clock)

        pending = list_for_user(session, ctx, status="pending")
        cancelled = list_for_user(session, ctx, status="cancelled")
        assert [v.id for v in pending] == [b.id]
        assert [v.id for v in cancelled] == [a.id]

    def test_worker_cannot_list_other_user(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="list-x")
        a_id = _bootstrap_user(session, email="a@l.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@l.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        session.commit()
        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        with pytest.raises(LeavePermissionDenied):
            list_for_user(session, ctx_a, user_id=b_id)

    def test_manager_can_list_other_user(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="list-m")
        worker_id = _bootstrap_user(session, email="w@l.com", display_name="W")
        mgr_id = _bootstrap_user(session, email="m@l.com", display_name="M")
        _grant(session, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
        session.commit()

        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        clock = FrozenClock(_PINNED)

        create_leave(session, ctx_worker, body=_create_body(), clock=clock)
        manager_view = list_for_user(session, ctx_mgr, user_id=worker_id)
        assert len(manager_view) == 1
        assert manager_view[0].user_id == worker_id


class TestListForWorkspace:
    def test_manager_sees_workspace_queue(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="queue-m")
        w1 = _bootstrap_user(session, email="w1@q.com", display_name="W1")
        w2 = _bootstrap_user(session, email="w2@q.com", display_name="W2")
        mgr_id = _bootstrap_user(session, email="m@q.com", display_name="M")
        _grant(session, workspace_id=ws_id, user_id=w1, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=w2, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
        session.commit()

        clock = FrozenClock(_PINNED)
        ctx_w1 = _ctx(workspace_id=ws_id, actor_id=w1, grant_role="worker")
        ctx_w2 = _ctx(workspace_id=ws_id, actor_id=w2, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")

        create_leave(session, ctx_w1, body=_create_body(), clock=clock)
        create_leave(
            session,
            ctx_w2,
            body=_create_body(
                starts_at=_FUTURE + timedelta(days=10),
                ends_at=_FUTURE_END + timedelta(days=10),
            ),
            clock=clock,
        )
        pending = list_for_workspace(session, ctx_mgr, status="pending")
        assert {v.user_id for v in pending} == {w1, w2}

    def test_worker_cannot_list_workspace(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, _clock = worker_env
        with pytest.raises(LeavePermissionDenied):
            list_for_workspace(session, ctx)


# ---------------------------------------------------------------------------
# Pagination — limit + after_id (§12 cursor-isolation contract)
# ---------------------------------------------------------------------------


class TestLeaveListPagination:
    """Domain-layer coverage for the ``limit`` / ``after_id`` arguments
    on :func:`list_for_user` and :func:`list_for_workspace`.

    Mirrors the shifts pagination shard: overshoot sizing, deterministic
    keyset walk, and the §12 cursor-isolation guarantee that an
    out-of-scope cursor returns the empty list rather than walking
    onto another user's or another workspace's rows.
    """

    def _three_leaves(
        self,
        session: Session,
        ctx: WorkspaceContext,
        clock: FrozenClock,
    ) -> list[str]:
        """Three pending leaves with strictly distinct ``starts_at``."""
        ids: list[str] = []
        for offset_days in (0, 14, 28):
            view = create_leave(
                session,
                ctx,
                body=_create_body(
                    starts_at=_FUTURE + timedelta(days=offset_days),
                    ends_at=_FUTURE_END + timedelta(days=offset_days),
                ),
                clock=clock,
            )
            ids.append(view.id)
        return ids

    def test_list_for_user_limit_returns_overshoot(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        self._three_leaves(session, ctx, clock)
        views = list_for_user(session, ctx, limit=2)
        assert len(views) == 3  # limit + 1

    def test_list_for_user_after_id_walks_forward(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        ids = self._three_leaves(session, ctx, clock)
        page = list_for_user(session, ctx, after_id=ids[0], limit=10)
        assert [v.id for v in page] == [ids[1], ids[2]]

    def test_list_for_user_cursor_unknown_id_returns_empty(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, _clock = worker_env
        assert list_for_user(session, ctx, after_id="01ZZZZNOLEAVE") == []

    def test_list_for_user_cursor_for_other_user_returns_empty(
        self,
        session: Session,
    ) -> None:
        """A worker paging their own leaves cannot use a manager's
        cursor row from a different user as the seek key — the §12
        cursor-isolation rule kicks in via the ``user_id`` mismatch."""
        ws_id = _bootstrap_workspace(session, slug="leave-page-x")
        a_id = _bootstrap_user(session, email="lpa@x.com", display_name="A")
        b_id = _bootstrap_user(session, email="lpb@x.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        session.commit()
        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        clock = FrozenClock(_PINNED)
        b_view = create_leave(session, ctx_b, body=_create_body(), clock=clock)
        # A pages own leaves but supplies B's leave id as the cursor.
        assert list_for_user(session, ctx_a, after_id=b_view.id) == []

    def test_list_for_workspace_limit_returns_overshoot(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="ws-page")
        worker_id = _bootstrap_user(session, email="wp@x.com", display_name="W")
        mgr_id = _bootstrap_user(session, email="mp@x.com", display_name="M")
        _grant(session, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
        session.commit()
        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        clock = FrozenClock(_PINNED)
        for offset_days in (0, 14, 28):
            create_leave(
                session,
                ctx_worker,
                body=_create_body(
                    starts_at=_FUTURE + timedelta(days=offset_days),
                    ends_at=_FUTURE_END + timedelta(days=offset_days),
                ),
                clock=clock,
            )
        views = list_for_workspace(session, ctx_mgr, limit=2)
        assert len(views) == 3  # limit + 1

    def test_list_for_workspace_cursor_from_other_workspace_returns_empty(
        self,
        session: Session,
    ) -> None:
        """A cursor row belonging to another workspace yields the empty
        list rather than leaking the receiving workspace's rows."""
        # Workspace A — manager + worker with one leave.
        ws_a = _bootstrap_workspace(session, slug="leak-a")
        wa_id = _bootstrap_user(session, email="la@x.com", display_name="A")
        ma_id = _bootstrap_user(session, email="lma@x.com", display_name="MA")
        _grant(session, workspace_id=ws_a, user_id=wa_id, grant_role="worker")
        _grant(session, workspace_id=ws_a, user_id=ma_id, grant_role="manager")
        # Workspace B — manager + worker with one leave we'll use as the
        # foreign cursor.
        ws_b = _bootstrap_workspace(session, slug="leak-b")
        wb_id = _bootstrap_user(session, email="lb@x.com", display_name="B")
        mb_id = _bootstrap_user(session, email="lmb@x.com", display_name="MB")
        _grant(session, workspace_id=ws_b, user_id=wb_id, grant_role="worker")
        _grant(session, workspace_id=ws_b, user_id=mb_id, grant_role="manager")
        session.commit()

        clock = FrozenClock(_PINNED)
        ctx_wa = _ctx(workspace_id=ws_a, actor_id=wa_id, grant_role="worker")
        ctx_ma = _ctx(workspace_id=ws_a, actor_id=ma_id, grant_role="manager")
        ctx_wb = _ctx(workspace_id=ws_b, actor_id=wb_id, grant_role="worker")
        create_leave(session, ctx_wa, body=_create_body(), clock=clock)
        b_view = create_leave(session, ctx_wb, body=_create_body(), clock=clock)

        # Manager of A scans the workspace queue with B's cursor →
        # empty list; never leaks A's leave.
        assert list_for_workspace(session, ctx_ma, after_id=b_view.id) == []


# ---------------------------------------------------------------------------
# get_leave
# ---------------------------------------------------------------------------


class TestGetLeave:
    def test_owner_can_read(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        view = get_leave(session, ctx, leave_id=created.id)
        assert view.id == created.id

    def test_missing_404(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, *_ = worker_env
        with pytest.raises(LeaveNotFound):
            get_leave(session, ctx, leave_id="no-such")

    def test_peer_worker_cannot_read(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="peer-read")
        a_id = _bootstrap_user(session, email="a@r.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@r.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        session.commit()
        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        clock = FrozenClock(_PINNED)
        created = create_leave(session, ctx_a, body=_create_body(), clock=clock)
        with pytest.raises(LeavePermissionDenied):
            get_leave(session, ctx_b, leave_id=created.id)


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_cross_workspace_get_is_404(
        self,
        session: Session,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="iso-a")
        ws_b = _bootstrap_workspace(session, slug="iso-b")
        user_a = _bootstrap_user(session, email="a@i.com", display_name="A")
        user_b = _bootstrap_user(session, email="b@i.com", display_name="B")
        _grant(session, workspace_id=ws_a, user_id=user_a, grant_role="worker")
        _grant(session, workspace_id=ws_b, user_id=user_b, grant_role="worker")
        session.commit()

        ctx_a = _ctx(workspace_id=ws_a, actor_id=user_a, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_b, actor_id=user_b, grant_role="worker")
        clock = FrozenClock(_PINNED)
        created = create_leave(session, ctx_a, body=_create_body(), clock=clock)

        # Workspace B cannot see workspace A's leave.
        with pytest.raises(LeaveNotFound):
            get_leave(session, ctx_b, leave_id=created.id)

    def test_list_is_workspace_scoped(
        self,
        session: Session,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="list-a")
        ws_b = _bootstrap_workspace(session, slug="list-b")
        user_a = _bootstrap_user(session, email="a@l2.com", display_name="A")
        user_b = _bootstrap_user(session, email="b@l2.com", display_name="B")
        _grant(session, workspace_id=ws_a, user_id=user_a, grant_role="worker")
        _grant(session, workspace_id=ws_b, user_id=user_b, grant_role="worker")
        session.commit()

        ctx_a = _ctx(workspace_id=ws_a, actor_id=user_a, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_b, actor_id=user_b, grant_role="worker")
        clock = FrozenClock(_PINNED)
        create_leave(session, ctx_a, body=_create_body(), clock=clock)

        assert list_for_user(session, ctx_a) != []
        assert list_for_user(session, ctx_b) == []


# ---------------------------------------------------------------------------
# DB row shape sanity
# ---------------------------------------------------------------------------


class TestDbRowShape:
    def test_created_row_has_expected_columns(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        created = create_leave(session, ctx, body=_create_body(), clock=clock)
        row = session.get(Leave, created.id)
        assert row is not None
        assert row.status == "pending"
        assert row.kind == "vacation"
        assert row.workspace_id == ctx.workspace_id
        assert row.decided_by is None


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _audit_rows(session: Session, *, workspace_id: str) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    return list(session.scalars(stmt).all())
