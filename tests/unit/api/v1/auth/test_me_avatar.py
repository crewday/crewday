"""Unit tests for the bare-host ``/api/v1/me/avatar`` router.

Exercises :mod:`app.api.v1.auth.me_avatar` against a minimal FastAPI
instance — no factory, no tenancy middleware, no CSRF middleware. The
integration suite (``tests/integration/auth/...``) carries the full
stack; the shape here matches :mod:`tests.integration.auth.test_me_tokens_pg`
but runs against an in-memory SQLite engine + an in-memory
:class:`Storage` fake so every case finishes in milliseconds.

Coverage:

* ``POST`` happy path with PNG / JPEG / WebP → 200, avatar URL points
  at the stored blob, ``users.avatar_blob_hash`` is updated, and an
  ``identity.avatar.updated`` audit row lands with the acting user's
  id in the diff.
* ``POST`` rejected content types (``image/gif``) → 415.
* ``POST`` oversized body — ``Content-Length: 3_000_000`` → 413 before
  the body is read.
* ``POST`` streaming overflow (no ``Content-Length``, body > 2 MB)
  → 413 from the accumulator guard.
* ``POST`` without a session cookie → 401.
* ``POST`` replace flow — second upload swaps the hash and leaves the
  previous blob on disk (orphan GC contract).
* ``DELETE`` clears ``users.avatar_blob_hash``, preserves the
  previous blob on disk, and writes an ``identity.avatar.cleared``
  audit row (only when an avatar was actually set).
* ``DELETE`` on an already-null avatar → 200 but NO audit row (the
  log would just accumulate no-op rows on buggy-client retries).
* ``DELETE`` without a session cookie → 401.

See ``docs/specs/05-employees-and-roles.md`` §"Worker surface" and
``docs/specs/12-rest-api.md`` §"Avatar upload".
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

# Import the workspace + authz model packages so :data:`Base.metadata`
# resolves every FK the identity tables reference — ``session.workspace_id``
# points at ``workspace.id``, and the authz tables join against both.
# The F401 noqas flag "imported but unused in Python code" — correct,
# and the whole point: we're pulling the models into the metadata graph.
from app.adapters.db import audit, authz, workspace  # noqa: F401
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.session import make_engine
from app.api.deps import db_session as db_session_dep
from app.api.deps import get_storage
from app.api.v1.auth import me_avatar as me_avatar_module
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage

# Pinned UA / Accept-Language so the :func:`validate` fingerprint gate
# agrees with the seed :func:`issue` call — matches the integration
# test's convention.
_TEST_UA: str = "pytest-me-avatar"
_TEST_ACCEPT_LANGUAGE: str = "en"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Minimal :class:`Settings` pinned to SQLite + a fixed root key.

    The root key is load-bearing — :func:`auth_session.issue` / ``validate``
    derive the fingerprint pepper from it, and both sides must agree
    on the value.
    """
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-me-avatar-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every model's table created.

    A shared-cache URL keeps the same DB visible across connections
    inside the test — :class:`TestClient` opens its own requests and
    the ``issue`` seed call runs out-of-band, so a vanilla ``:memory:``
    URL would hand each call a different empty DB.
    """
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=Session,
    )


@pytest.fixture
def seed_user(session_factory: sessionmaker[Session]) -> str:
    """Seed a :class:`User` row and return its id."""
    from app.util.clock import SystemClock

    user_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            User(
                id=user_id,
                email=f"avatar-{user_id[-6:].lower()}@example.com",
                email_lower=f"avatar-{user_id[-6:].lower()}@example.com",
                display_name="Avatar User",
                created_at=SystemClock().now(),
            )
        )
        s.commit()
    return user_id


@pytest.fixture
def storage() -> InMemoryStorage:
    """Shared in-memory storage across every request in a test."""
    return InMemoryStorage()


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    storage: InMemoryStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Mount the avatar router on a minimal FastAPI.

    Pinned ``User-Agent`` + ``Accept-Language`` headers so every
    request through the client carries the fingerprint the seeded
    session was minted under.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(
        me_avatar_module.build_me_avatar_router(),
        prefix="/api/v1",
    )

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[db_session_dep] = _session
    app.dependency_overrides[get_storage] = lambda: storage

    with TestClient(
        app,
        base_url="https://testserver",
        headers={
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
    ) as c:
        yield c


def _issue_cookie(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a live session for ``user_id`` and return the raw cookie value."""
    with session_factory() as s:
        result = issue(
            s,
            user_id=user_id,
            has_owner_grant=False,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


# Tiny but real image payloads. Kept inline rather than in
# ``tests/_fixtures/`` because the router only cares about the bytes
# + the multipart ``content_type`` string — it never decodes the
# payload. Using the minimal PNG signature keeps every byte accounted
# for in assertions.
_MIN_PNG: bytes = (
    b"\x89PNG\r\n\x1a\n"  # PNG signature
    b"\x00\x00\x00\rIHDR"  # IHDR length + type
    b"\x00\x00\x00\x01\x00\x00\x00\x01"  # 1x1
    b"\x08\x02\x00\x00\x00\x90wS\xde"  # bit depth + color type + CRC
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01"
    b"|\xdd8\x8c\x00\x00\x00\x00IEND\xaeB`\x82"
)

_MIN_JPEG: bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00payload\xff\xd9"
_MIN_WEBP: bytes = b"RIFF\x1c\x00\x00\x00WEBPVP8 pixeldata"


# ---------------------------------------------------------------------------
# Happy path + format allowlist
# ---------------------------------------------------------------------------


class TestPostAvatarHappyPath:
    """``POST /me/avatar`` accepts the three allowlisted content types."""

    @pytest.mark.parametrize(
        ("payload", "content_type"),
        [
            (_MIN_PNG, "image/png"),
            (_MIN_JPEG, "image/jpeg"),
            (_MIN_WEBP, "image/webp"),
        ],
    )
    def test_post_avatar_stores_blob_and_updates_user(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        storage: InMemoryStorage,
        payload: bytes,
        content_type: str,
    ) -> None:
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post(
            "/api/v1/me/avatar",
            files={"image": ("avatar.bin", payload, content_type)},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        digest = hashlib.sha256(payload).hexdigest()
        assert body["avatar_url"] == f"memory://{digest}?ttl=3600"

        # Storage keeps the exact bytes under the SHA-256 digest.
        assert storage.exists(digest)
        assert storage.get(digest).read() == payload

        # User row is pointed at the new blob.
        with session_factory() as s, tenant_agnostic():
            user = s.get(User, seed_user)
            assert user is not None
            assert user.avatar_blob_hash == digest

            # Audit row lands in the same UoW as the user update —
            # matches the spec's ``identity.avatar.updated`` contract.
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == seed_user)
                .filter(AuditLog.action == "identity.avatar.updated")
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.entity_kind == "user"
            assert row.actor_kind == "system"
            diff = row.diff
            assert isinstance(diff, dict)
            assert diff["user_id"] == seed_user
            assert diff["before_hash"] is None
            assert diff["after_hash"] == digest
            assert diff["content_type"] == content_type
            assert diff["size_bytes"] == len(payload)


# ---------------------------------------------------------------------------
# Rejections — content type, size, auth
# ---------------------------------------------------------------------------


class TestPostAvatarRejections:
    """Validation and auth rejection paths."""

    def test_post_avatar_gif_is_415(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """``image/gif`` is not in the allowlist — 415 before any DB write."""
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post(
            "/api/v1/me/avatar",
            files={"image": ("avatar.gif", b"GIF89apixels", "image/gif")},
        )
        assert r.status_code == 415, r.text
        assert r.json()["detail"]["error"] == "avatar_content_type_rejected"

    def test_post_avatar_content_length_too_large_is_413(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """Content-Length above 2 MiB trips the pre-read guard (413)."""
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        # Pass a tiny body but forge Content-Length to exceed the cap.
        # ``httpx`` lets us override the header verbatim; the router's
        # pre-read check rejects without looking at the body.
        r = client.post(
            "/api/v1/me/avatar",
            content=b"tiny",
            headers={
                "Content-Length": "3000000",
                "Content-Type": "image/png",
            },
        )
        assert r.status_code == 413, r.text
        assert r.json()["detail"]["error"] == "avatar_too_large"

    def test_post_avatar_streamed_body_too_large_is_413(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """A body > 2 MiB trips the streaming accumulator even without a
        pre-declared size — the SPA's ``fetch(..., body: FormData)`` does
        carry Content-Length, but a hostile client can omit it.

        We still forge a tiny Content-Length so the fast-path does not
        fire; the streaming guard is what trips here, which is the
        defensive leg of the 413 contract.
        """
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        # Craft a multipart body where the image part holds 2 MiB + 1
        # byte. Using ``files=`` here would let httpx set an honest
        # Content-Length; instead, we build the multipart envelope by
        # hand and override Content-Length to something tiny so the
        # pre-read check lets the body through to the streaming guard.
        boundary = "streamtest"
        big = b"A" * (2 * 1024 * 1024 + 1)
        body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="image"; filename="big.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode()
            + big
            + f"\r\n--{boundary}--\r\n".encode()
        )
        r = client.post(
            "/api/v1/me/avatar",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": "100",
            },
        )
        assert r.status_code == 413, r.text
        assert r.json()["detail"]["error"] == "avatar_too_large"

    def test_post_avatar_without_session_is_401(
        self,
        client: TestClient,
    ) -> None:
        """Anonymous POST is rejected before any storage / DB work."""
        client.cookies.clear()
        r = client.post(
            "/api/v1/me/avatar",
            files={"image": ("avatar.png", _MIN_PNG, "image/png")},
        )
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_required"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


class TestDeleteAvatar:
    """``DELETE /me/avatar`` clears the pointer but preserves the blob."""

    def test_delete_avatar_clears_hash_and_preserves_blob(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        storage: InMemoryStorage,
    ) -> None:
        """DELETE returns 200 + null URL; the underlying blob is preserved."""
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        # Seed a prior avatar.
        r_post = client.post(
            "/api/v1/me/avatar",
            files={"image": ("a.png", _MIN_PNG, "image/png")},
        )
        assert r_post.status_code == 200, r_post.text
        first_digest = hashlib.sha256(_MIN_PNG).hexdigest()

        r = client.delete("/api/v1/me/avatar")
        assert r.status_code == 200, r.text
        assert r.json() == {"avatar_url": None}

        # User row pointer is null.
        with session_factory() as s, tenant_agnostic():
            user = s.get(User, seed_user)
            assert user is not None
            assert user.avatar_blob_hash is None

            # Exactly one ``identity.avatar.cleared`` audit row lands —
            # emitted on the real state transition, not on idempotent
            # retries.
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == seed_user)
                .filter(AuditLog.action == "identity.avatar.cleared")
                .all()
            )
            assert len(rows) == 1
            diff = rows[0].diff
            assert isinstance(diff, dict)
            assert diff["user_id"] == seed_user
            assert diff["before_hash"] == first_digest

        # The previous blob survives — GC is a sweep concern, not this
        # router's responsibility.
        assert storage.exists(first_digest)

    def test_delete_avatar_is_idempotent(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """A second DELETE on an already-cleared avatar still returns 200.

        Only the first DELETE (the real state transition) emits an
        audit row — the second is a no-op that must not pad the log
        with a synthetic ``cleared`` event that didn't correspond to
        an actual change of state.
        """
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r1 = client.delete("/api/v1/me/avatar")
        r2 = client.delete("/api/v1/me/avatar")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json() == {"avatar_url": None}

        # Neither DELETE produced an audit row: the avatar was already
        # null on entry, so no state transition occurred. A future
        # change that decides to log "attempted clear on already-null"
        # would flip this assertion; today the contract is state-gated.
        with session_factory() as s, tenant_agnostic():
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == seed_user)
                .filter(AuditLog.action == "identity.avatar.cleared")
                .all()
            )
            assert rows == []

    def test_delete_avatar_without_session_is_401(
        self,
        client: TestClient,
    ) -> None:
        client.cookies.clear()
        r = client.delete("/api/v1/me/avatar")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_required"


# ---------------------------------------------------------------------------
# Replace semantics — orphan GC contract
# ---------------------------------------------------------------------------


class TestReplaceAvatar:
    """A second POST swaps the pointer and leaves the old blob on disk."""

    def test_post_avatar_twice_orphans_the_old_blob(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        storage: InMemoryStorage,
    ) -> None:
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        first_body = _MIN_PNG
        second_body = _MIN_JPEG
        first_digest = hashlib.sha256(first_body).hexdigest()
        second_digest = hashlib.sha256(second_body).hexdigest()

        r1 = client.post(
            "/api/v1/me/avatar",
            files={"image": ("a.png", first_body, "image/png")},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/api/v1/me/avatar",
            files={"image": ("b.jpg", second_body, "image/jpeg")},
        )
        assert r2.status_code == 200

        with session_factory() as s, tenant_agnostic():
            user = s.get(User, seed_user)
            assert user is not None
            assert user.avatar_blob_hash == second_digest

            # Two audit rows — one per POST. The second row's
            # ``before_hash`` pins the transition from the first
            # digest to the second, so an investigator can replay the
            # orphan chain without cross-referencing another table.
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == seed_user)
                .filter(AuditLog.action == "identity.avatar.updated")
                .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
                .all()
            )
            assert len(rows) == 2
            first_diff = rows[0].diff
            second_diff = rows[1].diff
            assert isinstance(first_diff, dict)
            assert isinstance(second_diff, dict)
            assert first_diff["before_hash"] is None
            assert first_diff["after_hash"] == first_digest
            assert second_diff["before_hash"] == first_digest
            assert second_diff["after_hash"] == second_digest

        # Both blobs are on disk; the first is an orphan the GC sweep
        # will reap.
        assert storage.exists(first_digest)
        assert storage.exists(second_digest)


# ---------------------------------------------------------------------------
# Sanity — empty multipart part is rejected
# ---------------------------------------------------------------------------


class TestEmptyPayload:
    """An empty ``image`` part is a 400, not a 200 with an empty blob."""

    def test_post_avatar_empty_image_is_400(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_cookie(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post(
            "/api/v1/me/avatar",
            files={"image": ("empty.png", io.BytesIO(b""), "image/png")},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "avatar_empty"
