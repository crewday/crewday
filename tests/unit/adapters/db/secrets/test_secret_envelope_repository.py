"""Unit tests for :class:`SqlAlchemySecretEnvelopeRepository` (cd-znv4).

Exercises the SA-backed concretion of the
:class:`~app.domain.secrets.ports.SecretEnvelopeRepository` Protocol
over an in-memory SQLite session:

* ``insert`` lands a fresh row and returns the projection;
  the row is observable via the underlying session immediately
  (the helper flushes).
* ``get_by_id`` round-trips a seeded row.
* ``get_by_id`` returns ``None`` when the row is absent (the
  cipher's decrypt branch maps that to ``EnvelopeDecryptError``).
* The ``LargeBinary`` / ``DateTime(timezone=True)`` columns
  round-trip correctly: ``ciphertext`` / ``nonce`` / ``key_fp``
  come back as :class:`bytes`; ``created_at`` re-attaches UTC on
  the SQLite path.

Mirrors the cd-24im email-change adapter test shape. Schema-shape
coverage of the underlying table + index lives in the cd-znv4
migration test (``tests/integration/secrets``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.secrets.repositories import (
    SqlAlchemySecretEnvelopeRepository,
)

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_ENVELOPE_ID = "01HWA00000000000000000ENV1"


# ---------------------------------------------------------------------------
# Engine fixture — mirrors the sibling email-change adapter test
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Walk the adapter packages so cross-package FKs resolve.

    Without this step a test run order that imports a later context
    first could leave ``Base.metadata`` with dangling FKs. The
    secret_envelope table itself has no FKs but the test suite may
    pre-create sibling tables; loading every adapter keeps the
    create_all step's posture identical to integration runs.
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
def repo(session: Session) -> SqlAlchemySecretEnvelopeRepository:
    return SqlAlchemySecretEnvelopeRepository(session)


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


class TestInsert:
    def test_lands_row_and_returns_projection(
        self,
        session: Session,
        repo: SqlAlchemySecretEnvelopeRepository,
    ) -> None:
        ciphertext = b"\xde\xad\xbe\xef" * 8
        nonce = b"\x01" * 12
        key_fp = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11"

        row = repo.insert(
            envelope_id=_ENVELOPE_ID,
            owner_entity_kind="ical_feed",
            owner_entity_id="feed-001",
            purpose="ical-feed-url",
            ciphertext=ciphertext,
            nonce=nonce,
            key_fp=key_fp,
            created_at=_PINNED,
        )
        assert row.id == _ENVELOPE_ID
        assert row.owner_entity_kind == "ical_feed"
        assert row.owner_entity_id == "feed-001"
        assert row.purpose == "ical-feed-url"
        assert row.ciphertext == ciphertext
        assert row.nonce == nonce
        assert row.key_fp == key_fp
        assert row.created_at == _PINNED
        assert row.rotated_at is None

        # Row observable via the underlying session (the repo
        # flushes); a peer SELECT in the same UoW sees the new row.
        persisted = session.get(SecretEnvelope, _ENVELOPE_ID)
        assert persisted is not None
        assert bytes(persisted.ciphertext) == ciphertext
        assert bytes(persisted.nonce) == nonce
        assert bytes(persisted.key_fp) == key_fp


# ---------------------------------------------------------------------------
# get_by_id
# ---------------------------------------------------------------------------


class TestGetById:
    def test_returns_projection_for_seeded_row(
        self,
        repo: SqlAlchemySecretEnvelopeRepository,
    ) -> None:
        ciphertext = b"ciphertext-bytes"
        nonce = b"\x02" * 12
        key_fp = b"\x00" * 8
        repo.insert(
            envelope_id=_ENVELOPE_ID,
            owner_entity_kind="ical_feed",
            owner_entity_id="feed-x",
            purpose="ical-feed-url",
            ciphertext=ciphertext,
            nonce=nonce,
            key_fp=key_fp,
            created_at=_PINNED,
        )
        row = repo.get_by_id(envelope_id=_ENVELOPE_ID)
        assert row is not None
        assert row.id == _ENVELOPE_ID
        assert row.ciphertext == ciphertext
        assert row.nonce == nonce
        assert row.key_fp == key_fp
        # SQLite strips tzinfo on the way back out — the projection
        # re-attaches UTC so callers compare against an aware
        # datetime directly.
        assert row.created_at.tzinfo is not None
        assert row.created_at == _PINNED

    def test_returns_none_for_missing_row(
        self,
        repo: SqlAlchemySecretEnvelopeRepository,
    ) -> None:
        # Decrypt branch maps this to EnvelopeDecryptError; the repo
        # itself returns ``None`` so tests of the cipher can pick the
        # error message themselves.
        assert repo.get_by_id(envelope_id="missing") is None


# ---------------------------------------------------------------------------
# Round-trip + binary fidelity
# ---------------------------------------------------------------------------


class TestRoundTripFidelity:
    """The cipher feeds opaque bytes — every column must survive verbatim."""

    def test_arbitrary_byte_values_round_trip(
        self,
        repo: SqlAlchemySecretEnvelopeRepository,
    ) -> None:
        # Cover every byte value 0..255 in the ciphertext to catch
        # any latin-1 / utf-8 lossy round-trip a careless adapter
        # would introduce.
        ciphertext = bytes(range(256))
        nonce = bytes(range(12))
        key_fp = bytes(range(8))
        repo.insert(
            envelope_id=_ENVELOPE_ID,
            owner_entity_kind="prop",
            owner_entity_id="p1",
            purpose="wifi-password",
            ciphertext=ciphertext,
            nonce=nonce,
            key_fp=key_fp,
            created_at=_PINNED,
        )
        row = repo.get_by_id(envelope_id=_ENVELOPE_ID)
        assert row is not None
        assert row.ciphertext == ciphertext
        assert row.nonce == nonce
        assert row.key_fp == key_fp
