"""Unit tests for :mod:`app.domain.identity.push_tokens` (cd-nq9s).

Exercises the service surface against an in-memory SQLite engine
built via ``Base.metadata.create_all()`` — no alembic, no tenant
filter (this surface is identity-scoped and runs every read / write
under ``tenant_agnostic``), just the ORM round-trip + the audit
side-effects.

Covers:

* :func:`register` happy path — new row persists, one audit row
  with ``user_push_token.registered`` fires, raw ``token`` does NOT
  appear in the audit ``diff``.
* :func:`register` idempotent — same ``(user_id, platform, token)``
  triple bumps ``last_seen_at`` and writes no second row, no second
  audit entry.
* :func:`register` cross-user collision — the second user's
  registration of the same ``(platform, token)`` pair raises
  :class:`TokenClaimed`.
* :func:`register` invalid platform — a string outside the v1
  whitelist raises :class:`InvalidPlatform`.
* :func:`list_for_user` — returns every row owned by the user
  (active + disabled-equivalent), sorted by ``created_at`` then ``id``.
* :func:`refresh` happy path — bumps ``last_seen_at``, no audit row.
* :func:`refresh` with rotation — swaps the row's ``token`` and
  bumps ``last_seen_at``; no audit row (routine OS event).
* :func:`refresh` cross-user collapse — the second user targeting
  another user's row raises :class:`PushTokenNotFound`.
* :func:`unregister` happy path — row removed, one audit row with
  ``user_push_token.deleted`` fires, audit ``diff`` redacts
  raw ``token``.
* :func:`unregister` no-op — missing id is a silent no-op, no audit
  row.
* :func:`unregister` cross-user collapse — second user targeting
  another user's row is a silent no-op (no audit row, no error).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, UserPushToken, canonicalise_email
from app.adapters.db.identity.repositories import (
    SqlAlchemyUserPushTokenRepository,
)
from app.adapters.db.session import make_engine
from app.domain.identity.push_tokens import (
    InvalidPlatform,
    PushTokenNotFound,
    TokenClaimed,
    list_for_user,
    refresh,
    register,
    unregister,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
_AGNOSTIC_WORKSPACE_ID = "00000000000000000000000000"


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
    """In-memory SQLite engine, schema built from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _bootstrap_user(s: Session, *, email: str, display_name: str) -> str:
    """Insert a :class:`User` row and return its id."""
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


def _repo(session: Session) -> SqlAlchemyUserPushTokenRepository:
    """Wrap ``session`` in the production SA-backed repository."""
    return SqlAlchemyUserPushTokenRepository(session)


def _audit_rows(s: Session, *, action: str) -> list[AuditLog]:
    """Return every audit row in the agnostic-workspace bucket for ``action``."""
    with tenant_agnostic():
        return list(
            s.scalars(
                select(AuditLog).where(
                    AuditLog.workspace_id == _AGNOSTIC_WORKSPACE_ID,
                    AuditLog.action == action,
                )
            ).all()
        )


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


class TestRegister:
    def test_happy_path_persists_and_audits(self, session: Session) -> None:
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        clock = FrozenClock(_PINNED)

        view = register(
            _repo(session),
            user_id=user_id,
            platform="android",
            token="fcm-handle-alpha",
            device_label="Pixel 9",
            app_version="1.0.0",
            clock=clock,
        )
        session.flush()

        assert view.user_id == user_id
        assert view.platform == "android"
        assert view.device_label == "Pixel 9"
        assert view.app_version == "1.0.0"
        # ``view`` is the freshly-inserted row — tzinfo is intact on
        # the Python object before any second-read drops it.
        assert view.created_at == _PINNED
        assert view.last_seen_at == _PINNED
        assert view.disabled_at is None

        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(UserPushToken).where(UserPushToken.user_id == user_id)
                ).all()
            )
        assert len(rows) == 1
        assert rows[0].token == "fcm-handle-alpha"

        audit = _audit_rows(session, action="user_push_token.registered")
        assert len(audit) == 1
        # §02 "no token payload" — diff carries metadata, never raw token.
        diff = audit[0].diff
        assert isinstance(diff, dict)
        assert diff["user_id"] == user_id
        assert diff["platform"] == "android"
        assert diff["device_label"] == "Pixel 9"
        assert diff["app_version"] == "1.0.0"
        assert "token" not in diff

    def test_idempotent_re_registration_no_second_row_or_audit(
        self,
        session: Session,
    ) -> None:
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        clock_first = FrozenClock(_PINNED)
        clock_second = FrozenClock(_PINNED + timedelta(hours=1))

        first = register(
            _repo(session),
            user_id=user_id,
            platform="ios",
            token="apns-handle-alpha",
            device_label="iPhone 15",
            clock=clock_first,
        )
        second = register(
            _repo(session),
            user_id=user_id,
            platform="ios",
            token="apns-handle-alpha",
            # Caller rotated label / version on re-registration; the
            # idempotent path returns the existing row unchanged
            # except for ``last_seen_at`` — we deliberately do NOT
            # apply caller-supplied label / version on re-register.
            device_label="iPhone 15 Pro",
            clock=clock_second,
        )

        assert first.id == second.id
        # ``last_seen_at`` advanced; ``created_at`` did not. SQLite
        # drops ``tzinfo`` on second-read so we compare the naive
        # representation; the column is ``DateTime(timezone=True)`` on
        # Postgres in production.
        assert second.created_at.replace(tzinfo=None) == _PINNED.replace(tzinfo=None)
        assert second.last_seen_at.replace(tzinfo=None) == (
            _PINNED + timedelta(hours=1)
        ).replace(tzinfo=None)

        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(UserPushToken).where(UserPushToken.user_id == user_id)
                ).all()
            )
        assert len(rows) == 1

        audit = _audit_rows(session, action="user_push_token.registered")
        assert len(audit) == 1  # only the initial register

    def test_cross_user_collision_raises_token_claimed(
        self,
        session: Session,
    ) -> None:
        owner_id = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        intruder_id = _bootstrap_user(
            session, email="intruder@example.com", display_name="Intruder"
        )
        session.commit()
        clock = FrozenClock(_PINNED)

        register(
            _repo(session),
            user_id=owner_id,
            platform="android",
            token="shared-handle",
            clock=clock,
        )
        with pytest.raises(TokenClaimed):
            register(
                _repo(session),
                user_id=intruder_id,
                platform="android",
                token="shared-handle",
                clock=clock,
            )

        # No second row was inserted for the intruder.
        with tenant_agnostic():
            intruder_rows = list(
                session.scalars(
                    select(UserPushToken).where(UserPushToken.user_id == intruder_id)
                ).all()
            )
        assert intruder_rows == []
        # No audit row leaked for the failed attempt — only the
        # owner's original register.
        audit = _audit_rows(session, action="user_push_token.registered")
        assert len(audit) == 1

    def test_invalid_platform_raises(self, session: Session) -> None:
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        clock = FrozenClock(_PINNED)

        with pytest.raises(InvalidPlatform):
            register(
                _repo(session),
                user_id=user_id,
                platform="windows",
                token="handle",
                clock=clock,
            )


# ---------------------------------------------------------------------------
# list_for_user()
# ---------------------------------------------------------------------------


class TestListForUser:
    def test_returns_only_the_caller_rows_sorted(self, session: Session) -> None:
        owner_id = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        other_id = _bootstrap_user(
            session, email="other@example.com", display_name="Other"
        )
        session.commit()

        clock_a = FrozenClock(_PINNED)
        clock_b = FrozenClock(_PINNED + timedelta(minutes=1))
        register(
            _repo(session),
            user_id=owner_id,
            platform="android",
            token="t-a",
            clock=clock_a,
        )
        register(
            _repo(session),
            user_id=owner_id,
            platform="ios",
            token="t-b",
            clock=clock_b,
        )
        register(
            _repo(session),
            user_id=other_id,
            platform="android",
            token="t-other",
            clock=clock_a,
        )

        views = list_for_user(_repo(session), user_id=owner_id)
        assert len(views) == 2
        # Sorted by created_at ascending — the android row precedes
        # the ios row by 1 minute.
        assert views[0].platform == "android"
        assert views[1].platform == "ios"
        # No row leaked from the other user.
        assert all(v.user_id == owner_id for v in views)
        # Raw token never surfaces on the view.
        assert not hasattr(views[0], "token")


# ---------------------------------------------------------------------------
# refresh()
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_bumps_last_seen_no_audit(self, session: Session) -> None:
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()

        first = register(
            _repo(session),
            user_id=user_id,
            platform="android",
            token="t-a",
            clock=FrozenClock(_PINNED),
        )
        later = _PINNED + timedelta(hours=2)
        view = refresh(
            _repo(session),
            user_id=user_id,
            token_id=first.id,
            clock=FrozenClock(later),
        )
        # SQLite drops tzinfo on second-read; compare naive.
        assert view.last_seen_at.replace(tzinfo=None) == later.replace(tzinfo=None)
        assert view.created_at.replace(tzinfo=None) == _PINNED.replace(
            tzinfo=None
        )  # unchanged

        # No audit row for a routine refresh.
        assert _audit_rows(session, action="user_push_token.refreshed") == []

    def test_token_rotation_swaps_token(self, session: Session) -> None:
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()

        first = register(
            _repo(session),
            user_id=user_id,
            platform="ios",
            token="t-old",
            clock=FrozenClock(_PINNED),
        )
        later = _PINNED + timedelta(days=1)
        refresh(
            _repo(session),
            user_id=user_id,
            token_id=first.id,
            token="t-new",
            clock=FrozenClock(later),
        )

        with tenant_agnostic():
            row = session.get(UserPushToken, first.id)
        assert row is not None
        assert row.token == "t-new"
        assert row.last_seen_at.replace(tzinfo=None) == later.replace(tzinfo=None)

    def test_cross_user_target_raises_not_found(self, session: Session) -> None:
        owner_id = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        intruder_id = _bootstrap_user(
            session, email="intruder@example.com", display_name="Intruder"
        )
        session.commit()

        first = register(
            _repo(session),
            user_id=owner_id,
            platform="android",
            token="t-a",
            clock=FrozenClock(_PINNED),
        )
        with pytest.raises(PushTokenNotFound):
            refresh(
                _repo(session),
                user_id=intruder_id,
                token_id=first.id,
                clock=FrozenClock(_PINNED),
            )


# ---------------------------------------------------------------------------
# unregister()
# ---------------------------------------------------------------------------


class TestUnregister:
    def test_happy_path_removes_and_audits(self, session: Session) -> None:
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()

        first = register(
            _repo(session),
            user_id=user_id,
            platform="android",
            token="t-a",
            device_label="Pixel",
            clock=FrozenClock(_PINNED),
        )
        unregister(
            _repo(session),
            user_id=user_id,
            token_id=first.id,
            clock=FrozenClock(_PINNED),
        )

        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(UserPushToken).where(UserPushToken.user_id == user_id)
                ).all()
            )
        assert rows == []

        audit = _audit_rows(session, action="user_push_token.deleted")
        assert len(audit) == 1
        diff = audit[0].diff
        assert isinstance(diff, dict)
        assert diff["user_id"] == user_id
        assert diff["platform"] == "android"
        assert diff["device_label"] == "Pixel"
        assert "token" not in diff

    def test_missing_id_is_silent_noop(self, session: Session) -> None:
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()

        # No row exists; the call is a no-op.
        unregister(
            _repo(session),
            user_id=user_id,
            token_id=new_ulid(),
            clock=FrozenClock(_PINNED),
        )

        # No audit row — no-op deletes are not audit-worthy.
        assert _audit_rows(session, action="user_push_token.deleted") == []

    def test_cross_user_target_is_silent_noop(self, session: Session) -> None:
        owner_id = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        intruder_id = _bootstrap_user(
            session, email="intruder@example.com", display_name="Intruder"
        )
        session.commit()

        first = register(
            _repo(session),
            user_id=owner_id,
            platform="android",
            token="t-a",
            clock=FrozenClock(_PINNED),
        )
        # Intruder targets the owner's row id — collapses to a no-op.
        unregister(
            _repo(session),
            user_id=intruder_id,
            token_id=first.id,
            clock=FrozenClock(_PINNED),
        )

        # Owner's row is intact.
        with tenant_agnostic():
            row = session.get(UserPushToken, first.id)
        assert row is not None

        # No audit row leaked for the cross-user attempt — only the
        # initial register.
        assert _audit_rows(session, action="user_push_token.deleted") == []


# ---------------------------------------------------------------------------
# Identity-scope regression — operations succeed under an active
# WorkspaceContext without the tenant filter injecting a workspace_id
# predicate (the table has no such column). cd-k32k6 selfreview.
# ---------------------------------------------------------------------------


class TestIdentityScope:
    """``user_push_token`` is NOT in :mod:`app.tenancy.registry`.

    Even when a ``WorkspaceContext`` is active (e.g. a request that
    landed on ``/w/<slug>/...`` somehow reaches the identity-scoped
    push-token surface), the SA repo wraps every read / write in
    :func:`tenant_agnostic` so no ``workspace_id`` predicate is
    injected. This test pins the invariant.
    """

    def test_register_then_list_under_active_workspace_context(
        self,
        session: Session,
    ) -> None:
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()

        # Synthesise an active WorkspaceContext that has nothing to do
        # with the push-token rows we're about to insert. The table
        # carries no ``workspace_id`` column, so a misfired tenant
        # filter would either crash the SELECT or return zero rows.
        ctx = WorkspaceContext(
            workspace_id="01HWAACTIVEWORKSPACEID0000",
            workspace_slug="active-ws",
            actor_id=user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id=new_ulid(),
            principal_kind="session",
        )
        token = set_current(ctx)
        try:
            view = register(
                _repo(session),
                user_id=user_id,
                platform="android",
                token="ws-ctx-handle",
                clock=FrozenClock(_PINNED),
            )
            views = list_for_user(_repo(session), user_id=user_id)
        finally:
            reset_current(token)

        assert len(views) == 1
        assert views[0].id == view.id
        # Audit row landed in the agnostic-workspace bucket regardless
        # of the active ctx — the domain service synthesises its own
        # bare-host ctx for emission. The active ctx must not bleed
        # into the audit row's ``workspace_id``.
        audit = _audit_rows(session, action="user_push_token.registered")
        assert len(audit) == 1
        assert audit[0].workspace_id == _AGNOSTIC_WORKSPACE_ID
