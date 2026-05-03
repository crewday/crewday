"""Domain tests for the privacy export builder (cd-p8qc).

Exercises :func:`app.domain.privacy.request_user_export` against an
in-memory SQLite engine + the in-memory :class:`Storage` fake. The
focus here is the **bundle contract**, not the route layer:

* Free-text leaves go through :func:`app.util.redact.redact` under
  ``scope="export"`` — emails of *other* users that ended up in
  free-text columns owned by the requester land redacted in the ZIP.
* The ``ExportReadyNotifier`` callable is invoked exactly once on
  completion with the right shape (user_id, export_id, download_url,
  expires_at). When ``notifier`` is ``None`` no callback fires and
  the function still returns a complete :class:`ExportResult`.
* Cross-user isolation: the bundle for ``u1`` never contains rows
  belonging to ``u2`` even when both users live in the same workspace.

See ``docs/specs/15-security-privacy.md`` §"Privacy and data rights"
and the project memory entry "Per-context Protocol seams".
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.audit.models import AuditLog  # noqa: F401  (metadata wiring)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.privacy.models import PrivacyExport
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.privacy import (
    EXPORT_TTL,
    ExportResult,
    request_user_export,
)
from app.tenancy import tenant_agnostic
from tests._fakes.storage import InMemoryStorage

_PINNED = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every model's table created."""
    eng = create_engine(
        "sqlite://",
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
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _seed_workspace(session: Session, *, workspace_id: str = "w1") -> None:
    session.add(
        Workspace(
            id=workspace_id,
            slug=workspace_id,
            name=workspace_id,
            plan="free",
            quota_json={},
            settings_json={},
            default_timezone="UTC",
            default_locale="en",
            default_currency="USD",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )


def _seed_user(session: Session, *, user_id: str, email: str) -> None:
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=email.lower(),
            display_name=user_id,
            locale="en",
            timezone="UTC",
            avatar_blob_hash=None,
            created_at=_PINNED,
        )
    )


def _seed_membership(
    session: Session, *, user_id: str, workspace_id: str = "w1"
) -> None:
    session.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )


# ---------------------------------------------------------------------------
# Notifier seam
# ---------------------------------------------------------------------------


class TestExportReadyNotifierSeam:
    """The route layer wires a closure; the domain just calls it."""

    def test_notifier_invoked_once_with_complete_payload(
        self, session: Session
    ) -> None:
        """Happy path: ``request_user_export`` calls ``notifier`` exactly
        once after the bundle is written to storage.
        """
        with tenant_agnostic():
            _seed_workspace(session)
            _seed_user(session, user_id="u1", email="u1@example.test")
            _seed_membership(session, user_id="u1")
            session.commit()

            calls: list[dict[str, object]] = []

            def _notify(
                *,
                user_id: str,
                export_id: str,
                download_url: str | None,
                expires_at: datetime | None,
            ) -> None:
                calls.append(
                    {
                        "user_id": user_id,
                        "export_id": export_id,
                        "download_url": download_url,
                        "expires_at": expires_at,
                    }
                )

            result = request_user_export(
                session,
                InMemoryStorage(),
                user_id="u1",
                notifier=_notify,
            )

        assert isinstance(result, ExportResult)
        assert result.status == "completed"
        assert result.download_url is not None
        assert result.download_url.startswith("memory://")
        assert result.expires_at is not None

        # Notifier called exactly once with the same fields the route
        # layer surfaces to the client.
        assert len(calls) == 1
        call = calls[0]
        assert call["user_id"] == "u1"
        assert call["export_id"] == result.id
        assert call["download_url"] == result.download_url
        assert call["expires_at"] == result.expires_at

    def test_no_notifier_still_completes_export(self, session: Session) -> None:
        """Tests that exercise only the bundle contract pass ``None``;
        the function must still queue + complete + sign the URL.
        """
        with tenant_agnostic():
            _seed_workspace(session)
            _seed_user(session, user_id="u1", email="u1@example.test")
            _seed_membership(session, user_id="u1")
            session.commit()

            result = request_user_export(
                session,
                InMemoryStorage(),
                user_id="u1",
                notifier=None,
            )

        assert result.status == "completed"
        assert result.download_url is not None
        # The PrivacyExport row landed and its TTL matches the constant.
        with tenant_agnostic():
            job = session.get(PrivacyExport, result.id)
            assert job is not None
            assert job.expires_at is not None
            assert job.completed_at is not None
            assert job.expires_at - job.completed_at == EXPORT_TTL


# ---------------------------------------------------------------------------
# Redaction safety net
# ---------------------------------------------------------------------------


class TestRedactionAppliedToBundle:
    """Free-text leaves owned by the requester are redacted before the ZIP.

    The route filters rows to ``user_id`` already (``_subject_filters``)
    — these tests prove the **regex safety net** still scrubs PII that
    leaked into a free-text column belonging to the requester (e.g.
    they pasted another user's email into a comment).
    """

    def test_foreign_email_in_own_display_name_is_redacted(
        self, session: Session
    ) -> None:
        """``u1`` set their ``display_name`` to a string containing
        ``u2@example.test``. The bundle filters by ``user.id == u1`` so
        the row is owned by the requester, but the regex pass MUST
        still scrub the sibling email so a privacy export never leaks
        foreign PII through a free-text leaf.
        """
        with tenant_agnostic():
            _seed_workspace(session)
            session.add(
                User(
                    id="u1",
                    email="u1@example.test",
                    email_lower="u1@example.test",
                    # u1 pasted u2's email into their own display name.
                    display_name="alias for u2@example.test",
                    locale="en",
                    timezone="UTC",
                    avatar_blob_hash=None,
                    created_at=_PINNED,
                )
            )
            _seed_user(session, user_id="u2", email="u2@example.test")
            _seed_membership(session, user_id="u1")
            _seed_membership(session, user_id="u2")
            session.commit()

            storage = InMemoryStorage()
            result = request_user_export(session, storage, user_id="u1")
            job = session.get(PrivacyExport, result.id)
            assert job is not None
            assert job.blob_hash is not None
            with zipfile.ZipFile(storage.get(job.blob_hash)) as archive:
                payload = json.loads(archive.read("export.json"))

        # The user row is in the bundle (it's u1's own row).
        users = payload["tables"]["user"]
        assert [row["id"] for row in users] == ["u1"]

        # The sibling user's email must not appear anywhere in the
        # serialised JSON — the redactor's regex pass scrubs it. u1's
        # own email lives under the ``email`` key, which the redactor's
        # sensitive-key pass scrubs to ``[REDACTED]`` regardless of the
        # row's ownership; that protects the bundle from emails of any
        # other user that happened to land in a free-text leaf.
        serialised = json.dumps(payload)
        assert "u2@example.test" not in serialised


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


class TestCrossUserIsolation:
    """A bundle for u1 must never contain u2's rows."""

    def test_only_requester_rows_in_user_table(self, session: Session) -> None:
        with tenant_agnostic():
            _seed_workspace(session)
            _seed_user(session, user_id="u1", email="u1@example.test")
            _seed_user(session, user_id="u2", email="u2@example.test")
            _seed_membership(session, user_id="u1")
            _seed_membership(session, user_id="u2")
            session.commit()

            storage = InMemoryStorage()
            result = request_user_export(session, storage, user_id="u1")
            job = session.get(PrivacyExport, result.id)
            assert job is not None
            assert job.blob_hash is not None
            with zipfile.ZipFile(storage.get(job.blob_hash)) as archive:
                payload = json.loads(archive.read("export.json"))

        users = payload["tables"]["user"]
        assert [row["id"] for row in users] == ["u1"]
