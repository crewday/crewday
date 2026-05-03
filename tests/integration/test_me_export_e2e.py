"""End-to-end integration test for ``/api/v1/me/export`` (cd-p8qc).

Drives the bare-host privacy-export router through a live FastAPI
:class:`TestClient` against the migrated DB (SQLite by default;
Postgres when ``CREWDAY_TEST_DB=postgres``). The test proves the §15
"Privacy and data rights" contract end-to-end:

* The 202 response carries a poll URL + signed download URL the
  bundle is fetchable from.
* The ZIP contains the requester's own rows but **no PII from other
  users** — emails of sibling members must not appear anywhere in the
  serialised JSON, even when they share the workspace.
* The ``email_delivery`` ledger gets one queued → sent row with
  ``template_key='privacy_export_ready'``.
* The :class:`AuditLog` carries one ``audit.privacy.export.issued``
  row pinned to the requester's workspace.

See ``docs/specs/15-security-privacy.md`` §"Privacy and data rights"
and ``docs/specs/10-messaging-notifications.md`` §10.1.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.messaging.models import EmailDelivery
from app.adapters.db.privacy.models import PrivacyExport
from app.api.deps import db_session as db_session_dep
from app.api.deps import get_storage
from app.api.v1.auth import me_export as me_export_module
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import registry, tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_TEST_UA: str = "pytest-me-export-e2e"
_TEST_ACCEPT_LANGUAGE: str = "en"


# Messaging tables are workspace-scoped; sibling integration tests'
# autouse fixtures can wipe the process-wide registry, so re-register
# defensively. Mirrors the pattern in
# :mod:`tests.integration.messaging.test_notification_fanout`.
_MESSAGING_TABLES: tuple[str, ...] = (
    "notification",
    "push_token",
    "digest_record",
    "chat_channel",
    "chat_message",
    "email_opt_out",
    "email_delivery",
)


@pytest.fixture(autouse=True)
def _ensure_messaging_registered() -> None:
    for table in _MESSAGING_TABLES:
        registry.register(table)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-me-export-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def seed_users(
    session_factory: sessionmaker[Session],
) -> tuple[str, str, str]:
    """Seed two users sharing one workspace.

    Returns ``(workspace_id, requester_id, sibling_email)``. The
    sibling email is the load-bearing string the bundle MUST NOT
    contain — proving cross-user PII isolation under §15.
    """
    clock = SystemClock()
    requester_email = f"requester+{new_ulid()}@example.com"
    sibling_email = f"sibling+{new_ulid()}@example.com"
    with session_factory() as s, tenant_agnostic():
        owner = bootstrap_user(
            s,
            email=requester_email,
            display_name="Requester",
            clock=clock,
        )
        # The sibling user is seeded into the same DB so their email
        # column carries ``sibling_email`` — proving the bundle for
        # ``owner`` does not leak that string even though the row is
        # discoverable via SELECT * FROM "user". The local variable
        # reference below silences the linter and documents the role.
        sibling = bootstrap_user(
            s,
            email=sibling_email,
            display_name="Sibling",
            clock=clock,
        )
        assert sibling.email == sibling_email
        workspace = bootstrap_workspace(
            s,
            slug=f"export-e2e-{new_ulid()[:10].lower()}",
            name="Export E2E",
            owner_user_id=owner.id,
            clock=clock,
        )
        s.commit()
        # Hold ids before close to avoid detached-instance reads.
        return workspace.id, owner.id, sibling_email


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    storage: InMemoryStorage,
    mailer: InMemoryMailer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Mount the export router on a minimal FastAPI for the e2e flow."""
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(
        me_export_module.build_me_export_router(mailer=mailer),
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


class TestPrivacyExportEndToEnd:
    def test_bundle_excludes_other_users_pii(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_users: tuple[str, str, str],
        storage: InMemoryStorage,
        mailer: InMemoryMailer,
    ) -> None:
        """End-to-end: the requester's bundle never contains the sibling
        user's email, an ``email_delivery`` row is persisted, and the
        audit ledger lands one ``audit.privacy.export.issued`` row.
        """
        workspace_id, requester_id, sibling_email = seed_users
        cookie_value = _issue_cookie(
            session_factory, user_id=requester_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post("/api/v1/me/export")
        assert r.status_code == 202, r.text
        body = r.json()
        export_id = body["id"]
        download_url = body["download_url"]
        assert download_url is not None

        # Pull the ZIP out of in-memory storage and read its manifest.
        with session_factory() as s, tenant_agnostic():
            job = s.get(PrivacyExport, export_id)
            assert job is not None
            assert job.user_id == requester_id
            assert job.blob_hash is not None
            blob_hash = job.blob_hash

        with zipfile.ZipFile(storage.get(blob_hash)) as archive:
            payload = json.loads(archive.read("export.json"))

        # The user table contains the requester only — never the sibling.
        users = payload["tables"]["user"]
        assert [row["id"] for row in users] == [requester_id]

        # The sibling's email must not appear anywhere in the bundle.
        # The redactor's sensitive-key pass strips ``email`` keys
        # outright; the regex pass is the safety net for free-text
        # leaves. Either way the byte string must not survive.
        serialised = json.dumps(payload)
        assert sibling_email not in serialised

        # email_delivery row: queued → sent, template = privacy_export_ready
        with session_factory() as s, tenant_agnostic():
            deliveries = (
                s.execute(
                    select(EmailDelivery).where(
                        EmailDelivery.to_person_id == requester_id,
                        EmailDelivery.workspace_id == workspace_id,
                    )
                )
                .scalars()
                .all()
            )
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery.template_key == "privacy_export_ready"
        assert delivery.delivery_state == "sent"
        assert delivery.sent_at is not None

        # Audit row: one ``audit.privacy.export.issued`` per workspace
        # the requester belongs to (here exactly one).
        with session_factory() as s, tenant_agnostic():
            audit_rows = (
                s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "audit.privacy.export.issued",
                        AuditLog.entity_id == export_id,
                    )
                )
                .scalars()
                .all()
            )
        assert len(audit_rows) == 1
        assert audit_rows[0].workspace_id == workspace_id
        assert audit_rows[0].actor_id == requester_id

        # Mailer sent exactly one message — the privacy export email.
        assert len(mailer.sent) == 1
        sent = mailer.sent[0]
        assert "data export" in sent.subject.lower()
        assert download_url in sent.body_text
