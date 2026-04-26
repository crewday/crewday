"""Unit tests for :class:`SqlAlchemyEmailChangeRepository` (cd-24im).

Exercises the SA-backed concretion of the cd-24im
:class:`~app.domain.identity.email_change_ports.EmailChangeRepository`
Protocol over an in-memory SQLite session:

* ``canonicalise_email`` mirrors the adapter helper.
* ``get_user`` round-trips a seeded :class:`User`.
* ``update_user_email`` swaps ``email`` and the
  ``before_update`` listener keeps ``email_lower`` in sync.
* ``email_taken_by_other`` returns ``False`` for the caller's own
  row, ``True`` for a sibling holder, ``False`` when the column is
  absent.
* ``latest_passkey_created_at`` returns the most-recent timestamp
  with tzinfo normalised to UTC for the SQLite roundtrip; returns
  ``None`` for a user with zero passkeys.
* ``insert_pending`` lands a fresh
  :class:`~app.adapters.db.identity.models.EmailChangePending` row;
  ``find_pending_by_request_jti`` / ``find_pending_by_revert_jti``
  surface it; ``mark_verified`` / ``mark_reverted`` flip the
  lifecycle stamps.

Mirrors the cd-2upg ``test_user_leave.py`` adapter-shape pattern.
Schema-shape coverage of the underlying table lives in the model
tests.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.adapters.db.identity.models import (
    EmailChangePending,
    PasskeyCredential,
    User,
)
from app.adapters.db.identity.repositories import SqlAlchemyEmailChangeRepository

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_USER_ID = "01HWA00000000000000000USR1"
_OTHER_ID = "01HWA00000000000000000USR2"


# ---------------------------------------------------------------------------
# Engine fixture — mirrors the sibling availability adapter tests
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Walk the adapter packages so cross-package FKs resolve.

    Without this step a test run order that imports a later context
    first could leave ``Base.metadata`` with dangling FKs.
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
    """In-memory SQLite engine with FK enforcement on.

    The ``email_change_pending`` row carries a ``user_id`` FK with
    ``ON DELETE CASCADE`` — enabling the pragma keeps the cascade
    semantics observable inside the test.
    """
    _load_all_models()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def repo(session: Session) -> SqlAlchemyEmailChangeRepository:
    return SqlAlchemyEmailChangeRepository(session)


def _seed_user(
    session: Session,
    *,
    user_id: str = _USER_ID,
    email: str = "alice@example.com",
    display_name: str = "Alice",
) -> User:
    user = User(
        id=user_id,
        email=email,
        email_lower=email.lower(),
        display_name=display_name,
        locale=None,
        timezone=None,
        created_at=_PINNED,
    )
    session.add(user)
    session.flush()
    return user


def _seed_passkey(
    session: Session,
    *,
    cred_id: bytes,
    user_id: str,
    created_at: datetime,
) -> PasskeyCredential:
    row = PasskeyCredential(
        id=cred_id,
        user_id=user_id,
        public_key=b"\x00" * 32,
        sign_count=0,
        transports=None,
        backup_eligible=False,
        label=None,
        created_at=created_at,
        last_used_at=None,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


class TestCanonicaliseEmail:
    """Mirrors the adapter helper without delegating to it."""

    def test_lowers_and_strips(self, repo: SqlAlchemyEmailChangeRepository) -> None:
        assert repo.canonicalise_email("  Alice@Example.COM  ") == "alice@example.com"

    def test_empty_string(self, repo: SqlAlchemyEmailChangeRepository) -> None:
        assert repo.canonicalise_email("") == ""


# ---------------------------------------------------------------------------
# User reads + writes
# ---------------------------------------------------------------------------


class TestGetUser:
    def test_returns_projection_for_seeded_user(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        row = repo.get_user(user_id=_USER_ID)
        assert row is not None
        assert row.id == _USER_ID
        assert row.email == "alice@example.com"
        assert row.email_lower == "alice@example.com"
        assert row.display_name == "Alice"

    def test_returns_none_for_missing_user(
        self, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        assert repo.get_user(user_id="missing") is None


class TestUpdateUserEmail:
    def test_swaps_email_and_listener_syncs_lower(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        updated = repo.update_user_email(
            user_id=_USER_ID, new_email="Alice.New@Example.com"
        )
        assert updated.email == "Alice.New@Example.com"
        assert updated.email_lower == "alice.new@example.com"
        # The DB row reflects the swap too.
        row = session.get(User, _USER_ID)
        assert row is not None
        assert row.email == "Alice.New@Example.com"
        assert row.email_lower == "alice.new@example.com"

    def test_raises_runtime_error_for_missing_user(
        self, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        with pytest.raises(RuntimeError, match="not found"):
            repo.update_user_email(user_id="missing", new_email="x@y.z")


# ---------------------------------------------------------------------------
# Uniqueness + cool-off probes
# ---------------------------------------------------------------------------


class TestEmailTakenByOther:
    def test_returns_false_when_only_self_holds(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        # Self-address should not register as a clash.
        assert (
            repo.email_taken_by_other(
                new_email_lower="alice@example.com",
                current_user_id=_USER_ID,
            )
            is False
        )

    def test_returns_true_when_sibling_holds(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        _seed_user(
            session,
            user_id=_OTHER_ID,
            email="bob@example.com",
            display_name="Bob",
        )
        assert (
            repo.email_taken_by_other(
                new_email_lower="bob@example.com",
                current_user_id=_USER_ID,
            )
            is True
        )

    def test_returns_false_when_unknown(
        self, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        # No matching row at all.
        assert (
            repo.email_taken_by_other(
                new_email_lower="missing@example.com",
                current_user_id=_USER_ID,
            )
            is False
        )


class TestLatestPasskeyCreatedAt:
    def test_returns_none_for_user_with_no_passkeys(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        assert repo.latest_passkey_created_at(user_id=_USER_ID) is None

    def test_returns_most_recent_with_utc_tzinfo(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        old = _PINNED - timedelta(hours=1)
        new = _PINNED - timedelta(minutes=5)
        _seed_passkey(session, cred_id=b"\x01" * 8, user_id=_USER_ID, created_at=old)
        _seed_passkey(session, cred_id=b"\x02" * 8, user_id=_USER_ID, created_at=new)
        latest = repo.latest_passkey_created_at(user_id=_USER_ID)
        assert latest is not None
        assert latest == new
        # SQLite strips tzinfo on the way back out — the repo
        # re-attaches UTC so the domain's aware comparison works
        # straight off the projection.
        assert latest.tzinfo is UTC


# ---------------------------------------------------------------------------
# EmailChangePending CRUD
# ---------------------------------------------------------------------------


class TestEmailChangePendingCRUD:
    def test_insert_pending_round_trip(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        row = repo.insert_pending(
            pending_id="01HWA00000000000000000PND1",
            user_id=_USER_ID,
            request_jti="01HWA00000000000000000JTI1",
            previous_email="alice@example.com",
            previous_email_lower="alice@example.com",
            new_email="alice.new@example.com",
            new_email_lower="alice.new@example.com",
            created_at=_PINNED,
        )
        assert row.revert_jti is None
        assert row.verified_at is None
        assert row.reverted_at is None
        # The DB row landed.
        db_row = session.get(EmailChangePending, "01HWA00000000000000000PND1")
        assert db_row is not None
        assert db_row.request_jti == "01HWA00000000000000000JTI1"
        assert db_row.previous_email == "alice@example.com"
        assert db_row.new_email == "alice.new@example.com"

    def test_find_by_request_jti_matches(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        repo.insert_pending(
            pending_id="01HWA00000000000000000PND1",
            user_id=_USER_ID,
            request_jti="01HWA00000000000000000JTI1",
            previous_email="alice@example.com",
            previous_email_lower="alice@example.com",
            new_email="alice.new@example.com",
            new_email_lower="alice.new@example.com",
            created_at=_PINNED,
        )
        found = repo.find_pending_by_request_jti(
            request_jti="01HWA00000000000000000JTI1"
        )
        assert found is not None
        assert found.id == "01HWA00000000000000000PND1"

    def test_find_by_request_jti_misses(
        self, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        assert repo.find_pending_by_request_jti(request_jti="missing") is None

    def test_find_by_revert_jti_matches_after_mark_verified(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        repo.insert_pending(
            pending_id="01HWA00000000000000000PND1",
            user_id=_USER_ID,
            request_jti="01HWA00000000000000000JTI1",
            previous_email="alice@example.com",
            previous_email_lower="alice@example.com",
            new_email="alice.new@example.com",
            new_email_lower="alice.new@example.com",
            created_at=_PINNED,
        )
        revert_at = _PINNED + timedelta(hours=72)
        verified = repo.mark_verified(
            pending_id="01HWA00000000000000000PND1",
            revert_jti="01HWA00000000000000000RJT1",
            revert_expires_at=revert_at,
            verified_at=_PINNED + timedelta(seconds=30),
        )
        assert verified.revert_jti == "01HWA00000000000000000RJT1"
        assert verified.verified_at == _PINNED + timedelta(seconds=30)
        assert verified.revert_expires_at == revert_at
        # The lookup-by-revert-jti now resolves.
        found = repo.find_pending_by_revert_jti(revert_jti="01HWA00000000000000000RJT1")
        assert found is not None
        assert found.id == "01HWA00000000000000000PND1"

    def test_find_by_revert_jti_misses(
        self, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        assert repo.find_pending_by_revert_jti(revert_jti="missing") is None

    def test_mark_verified_raises_for_missing(
        self, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        with pytest.raises(RuntimeError, match="not found"):
            repo.mark_verified(
                pending_id="missing",
                revert_jti="x",
                revert_expires_at=_PINNED,
                verified_at=_PINNED,
            )

    def test_mark_reverted_stamps_reverted_at(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        _seed_user(session)
        repo.insert_pending(
            pending_id="01HWA00000000000000000PND1",
            user_id=_USER_ID,
            request_jti="01HWA00000000000000000JTI1",
            previous_email="alice@example.com",
            previous_email_lower="alice@example.com",
            new_email="alice.new@example.com",
            new_email_lower="alice.new@example.com",
            created_at=_PINNED,
        )
        reverted = repo.mark_reverted(
            pending_id="01HWA00000000000000000PND1",
            reverted_at=_PINNED + timedelta(hours=12),
        )
        assert reverted.reverted_at == _PINNED + timedelta(hours=12)

    def test_mark_reverted_raises_for_missing(
        self, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        with pytest.raises(RuntimeError, match="not found"):
            repo.mark_reverted(pending_id="missing", reverted_at=_PINNED)


# ---------------------------------------------------------------------------
# Cross-cutting — workspace-agnostic reads
# ---------------------------------------------------------------------------


class TestSessionAccessor:
    """The ``session`` accessor surfaces the SA session for audit threading."""

    def test_session_returns_underlying_session(
        self, session: Session, repo: SqlAlchemyEmailChangeRepository
    ) -> None:
        # Identity check — the accessor must surface the same instance
        # the caller passed in, so :func:`app.audit.write_audit` sees
        # the caller's UoW (not a fresh one).
        assert repo.session is session
