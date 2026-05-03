"""End-to-end ``X-Correlation-Id`` round-trip across the audit + LLM seams.

cd-iws5 acceptance criterion: one agent turn writes audit + llm_usage
rows sharing the same id as the response's ``X-Correlation-Id-Echo``
header. This test composes the two middlewares (correlation id +
workspace context) in their production order and asserts the contract
across all three surfaces in one request.

The probe handler stands in for "an agent turn":

* It reads the active :class:`~app.tenancy.WorkspaceContext`.
* It calls :func:`app.audit.write_audit` (the canonical seam every
  domain action uses to persist an ``audit_log`` row).
* It inserts an :class:`~app.adapters.db.llm.models.LlmUsage` row with
  ``correlation_id=ctx.audit_correlation_id`` (matching the §11
  ``llm_usage.correlation_id`` contract — the recorder builds the
  same row in production).
* The handler commits explicitly so we can re-open the session and
  inspect the written rows.

Two cases are exercised:

* **Inbound header preserved** — the caller stamps
  ``X-Correlation-Id: <known>``; the response ``X-Correlation-Id-Echo``,
  the audit row, and the llm_usage row all carry ``<known>``.
* **Server-minted fallback** — the caller omits the header; the
  middleware mints a ULID, echoes it, and the same value lands on
  both rows.

See ``docs/specs/11-llm-and-agents.md`` §"Client abstraction" and
``docs/specs/02-domain-model.md`` §"Correlation scope".
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.workspace.models import Workspace
from app.api.transport.correlation_id import (
    CORRELATION_ID_ECHO_HEADER,
    CORRELATION_ID_HEADER,
    CorrelationIdMiddleware,
)
from app.audit import write_audit
from app.config import Settings
from app.tenancy.current import get_current
from app.tenancy.middleware import (
    TEST_ACTOR_ID_HEADER,
    TEST_WORKSPACE_ID_HEADER,
    WorkspaceContextMiddleware,
)
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import SystemClock
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration

# ULID is 26 chars of Crockford base32 (0-9 + A-Z minus I, L, O, U).
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

_WORKSPACE_ID = "01WS00000000000000CORRID00"
_ACTOR_ID = "01US00000000000000CORRID00"
_PROVIDER_MODEL_ID = "01PM00000000000000CORRID00"


@pytest.fixture
def stub_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Enable the Phase-0 tenancy stub.

    The stub gives us a synthesised :class:`WorkspaceContext` from the
    ``X-Test-Workspace-Id`` / ``X-Test-Actor-Id`` headers without
    needing to seed a workspace + user_workspace + role_grant set —
    the test focuses on the correlation-id contract, not the resolver.
    """
    settings = Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-test-correlation-id-root"),
        phase0_stub_enabled=True,
    )
    monkeypatch.setattr("app.tenancy.middleware.get_settings", lambda: settings)
    yield settings


@pytest.fixture
def wire_default_uow(engine: Engine) -> Iterator[None]:
    """Redirect :func:`app.adapters.db.session.make_uow` to the test engine.

    The probe handler opens a UoW to commit the audit + llm_usage
    rows. Without this fixture the UoW would build a fresh engine
    from :envvar:`CREWDAY_DATABASE_URL` (unset outside the
    alembic-upgrade window), which would rebuild against a different
    DB than the one the test inspects after.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def seeded_workspace(engine: Engine) -> Iterator[None]:
    """Seed the well-known stub workspace id and clean it up after.

    ``llm_usage.workspace_id`` carries a FK to ``workspace.id``, so a
    direct INSERT (the probe handler's path) needs the parent row to
    exist. The audit + usage rows are purged in the same teardown
    pass so a previous run does not pollute the assertions.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.query(AuditLog).filter(AuditLog.workspace_id == _WORKSPACE_ID).delete()
        session.query(LlmUsage).filter(LlmUsage.workspace_id == _WORKSPACE_ID).delete()
        # Insert / refresh the parent ``workspace`` row.
        existing = session.get(Workspace, _WORKSPACE_ID)
        if existing is None:
            now = SystemClock().now()
            session.add(
                Workspace(
                    id=_WORKSPACE_ID,
                    slug="villa-sud",
                    name="Villa Sud",
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    yield
    with factory() as session:
        session.query(AuditLog).filter(AuditLog.workspace_id == _WORKSPACE_ID).delete()
        session.query(LlmUsage).filter(LlmUsage.workspace_id == _WORKSPACE_ID).delete()
        session.query(Workspace).filter(Workspace.id == _WORKSPACE_ID).delete()
        session.commit()


def _build_app() -> FastAPI:
    """Compose the two middlewares in their production order.

    Registration is INNER → OUTER (FastAPI prepends), so the call
    order below puts ``CorrelationIdMiddleware`` outer of
    ``WorkspaceContextMiddleware`` — matching :func:`app.api.factory.
    create_app`'s real ordering. The probe handler runs inside both,
    sees the resolved ctx, and writes both rows in a single UoW.
    """
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.post("/w/{slug}/api/v1/agent_turn")
    async def agent_turn(slug: str, request: Request) -> JSONResponse:
        ctx = get_current()
        assert ctx is not None, "tenancy ctx must be bound for the probe"
        # Open a fresh UoW for the probe write so the assertions can
        # re-open the engine and read the committed rows back.
        from app.adapters.db.session import make_uow
        from app.tenancy.current import set_current

        # ``make_uow`` opens a new session bound to the test engine
        # via ``wire_default_uow``. The tenancy ContextVar is already
        # set by the middleware; the inner session inherits it for
        # filter purposes.
        token = set_current(ctx)
        try:
            with make_uow() as session:
                assert isinstance(session, Session)
                # 1) audit_log row — drives ``audit_log.correlation_id``.
                write_audit(
                    session,
                    ctx,
                    entity_kind="probe",
                    entity_id="01EN00000000000000CORRID00",
                    action="probe.executed",
                    via="api",
                )
                # 2) llm_usage row — drives ``llm_usage.correlation_id``.
                #    Built directly here (rather than via the recorder)
                #    so the test stays focused on the header / row id
                #    plumbing rather than the budget bump path.
                session.add(
                    LlmUsage(
                        id=new_ulid(),
                        workspace_id=ctx.workspace_id,
                        capability="probe.capability",
                        provider_model_id=_PROVIDER_MODEL_ID,
                        tokens_in=1,
                        tokens_out=1,
                        cost_cents=0,
                        latency_ms=0,
                        status="ok",
                        correlation_id=ctx.audit_correlation_id,
                        attempt=0,
                        finish_reason="stop",
                        actor_user_id=ctx.actor_id,
                        created_at=SystemClock().now(),
                    )
                )
        finally:
            from app.tenancy.current import reset_current

            reset_current(token)

        return JSONResponse(
            {
                "audit_correlation_id": ctx.audit_correlation_id,
                "workspace_id": ctx.workspace_id,
            }
        )

    return app


@pytest.fixture
def composed_client(
    stub_settings: Settings,
    wire_default_uow: None,
    seeded_workspace: None,
) -> Iterator[TestClient]:
    """TestClient over the composed app with the stub tenancy enabled."""
    app = _build_app()
    with TestClient(app) as client:
        yield client


def _read_audit(engine: Engine, *, workspace_id: str) -> list[AuditLog]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        return list(
            session.scalars(
                select(AuditLog).where(AuditLog.workspace_id == workspace_id)
            )
        )


def _read_llm_usage(engine: Engine, *, workspace_id: str) -> list[LlmUsage]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        return list(
            session.scalars(
                select(LlmUsage).where(LlmUsage.workspace_id == workspace_id)
            )
        )


class TestRoundTrip:
    """One agent turn → one shared correlation id across header + 2 rows."""

    def test_inbound_correlation_id_round_trips_to_audit_and_llm_usage(
        self, composed_client: TestClient, engine: Engine
    ) -> None:
        incoming = "01HXC0RR3LATI0NIDV4LU3X4ZW"
        resp = composed_client.post(
            "/w/villa-sud/api/v1/agent_turn",
            headers={
                CORRELATION_ID_HEADER: incoming,
                TEST_WORKSPACE_ID_HEADER: _WORKSPACE_ID,
                TEST_ACTOR_ID_HEADER: _ACTOR_ID,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers[CORRELATION_ID_ECHO_HEADER] == incoming
        assert resp.json()["audit_correlation_id"] == incoming

        audit_rows = _read_audit(engine, workspace_id=_WORKSPACE_ID)
        assert len(audit_rows) == 1
        assert audit_rows[0].correlation_id == incoming

        usage_rows = _read_llm_usage(engine, workspace_id=_WORKSPACE_ID)
        assert len(usage_rows) == 1
        assert usage_rows[0].correlation_id == incoming

    def test_absent_inbound_header_mints_ulid_shared_across_rows(
        self, composed_client: TestClient, engine: Engine
    ) -> None:
        resp = composed_client.post(
            "/w/villa-sud/api/v1/agent_turn",
            headers={
                TEST_WORKSPACE_ID_HEADER: _WORKSPACE_ID,
                TEST_ACTOR_ID_HEADER: _ACTOR_ID,
            },
        )
        assert resp.status_code == 200, resp.text

        echoed = resp.headers[CORRELATION_ID_ECHO_HEADER]
        assert _ULID_RE.match(echoed), f"not a ULID: {echoed!r}"
        # The handler observed the same id via the workspace context.
        assert resp.json()["audit_correlation_id"] == echoed

        audit_rows = _read_audit(engine, workspace_id=_WORKSPACE_ID)
        usage_rows = _read_llm_usage(engine, workspace_id=_WORKSPACE_ID)
        assert len(audit_rows) == 1
        assert len(usage_rows) == 1
        assert audit_rows[0].correlation_id == echoed
        assert usage_rows[0].correlation_id == echoed
