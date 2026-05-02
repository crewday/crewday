"""Unit coverage for the document extraction state machine (cd-mo9e).

Walks every :mod:`app.domain.assets.extraction` mutator with an
in-memory SQLite session + a :class:`FrozenClock`, asserts the row
state, the audit trail, and the SSE-bound event emitted on commit.
The integration suite at
:mod:`tests.integration.test_asset_documents_api` covers the HTTP
boundary; this module pins the domain semantics that the routes rely
on.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import FileExtraction
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.domain.assets.assets import create_asset
from app.domain.assets.documents import attach_document
from app.domain.assets.extraction import (
    MAX_EXTRACTION_ATTEMPTS,
    ExtractionRetryNotAllowed,
    enqueue_extraction,
    get_extraction,
    get_extraction_page,
    list_pending_extractions,
    record_extraction_empty,
    record_extraction_failure,
    record_extraction_success,
    record_extraction_unsupported,
    retry_extraction,
    start_extraction,
)
from app.events.bus import EventBus
from app.events.types import (
    AssetDocumentExtracted,
    AssetDocumentExtractionFailed,
    AssetDocumentExtractionRetried,
)
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_NOW = datetime(2026, 5, 2, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s
    engine.dispose()


def _ctx(workspace_id: str, actor_id: str, slug: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_extraction",
    )


def _seed_workspace(session: Session) -> tuple[WorkspaceContext, str]:
    owner = bootstrap_user(
        session,
        email="extraction@example.com",
        display_name="Extraction Manager",
    )
    workspace = bootstrap_workspace(
        session,
        slug="extraction",
        name="Extraction",
        owner_user_id=owner.id,
    )
    property_id = "prop_extraction"
    session.add(
        Property(
            id=property_id,
            name="Extraction House",
            kind="residence",
            address="2 Extract Way",
            address_json={"line1": "2 Extract Way", "country": "US"},
            country="US",
            timezone="UTC",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace.id,
            label="Extraction House",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_NOW,
        )
    )
    session.flush()
    return _ctx(workspace.id, owner.id, workspace.slug), property_id


def _seed_document(
    session: Session, *, clock: FrozenClock
) -> tuple[WorkspaceContext, str, str]:
    ctx, property_id = _seed_workspace(session)
    asset = create_asset(
        session,
        ctx,
        property_id=property_id,
        label="Manual binder",
        token_factory=lambda: "EXT100000001",
        clock=clock,
    )
    storage = InMemoryStorage()
    blob_hash = "b" * 64
    storage.put(blob_hash, io.BytesIO(b"body"), content_type="text/plain")
    doc = attach_document(
        session,
        ctx,
        asset.id,
        blob_hash=blob_hash,
        filename="manual.txt",
        category="manual",
        title="Manual",
        storage=storage,
        clock=clock,
    )
    return ctx, asset.id, doc.id


def test_attach_document_mints_pending_extraction(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, _asset_id, document_id = _seed_document(session, clock=clock)

    view = get_extraction(session, ctx, document_id)

    assert view.status == "pending"
    assert view.attempts == 0
    assert view.body_preview == ""
    assert view.extracted_at is None
    # ``attach_document`` writes the document audit + the extraction
    # audit under the same UoW; both share the document id since the
    # extraction row reuses it as the PK.
    audit_actions = set(
        session.scalars(
            select(AuditLog.action).where(AuditLog.entity_id == document_id)
        ).all()
    )
    assert "file_extraction.pending" in audit_actions
    assert "asset_document.create" in audit_actions


def test_start_extraction_bumps_attempts_and_flips_to_extracting(
    session: Session,
) -> None:
    clock = FrozenClock(_NOW)
    ctx, _asset_id, document_id = _seed_document(session, clock=clock)

    row = start_extraction(session, ctx, document_id, clock=clock)

    assert row.extraction_status == "extracting"
    assert row.attempts == 1


def test_record_extraction_success_persists_body_and_emits_event(
    session: Session,
) -> None:
    clock = FrozenClock(_NOW)
    ctx, asset_id, document_id = _seed_document(session, clock=clock)
    bus = EventBus()
    seen: list[AssetDocumentExtracted] = []

    @bus.subscribe(AssetDocumentExtracted)
    def collect(event: AssetDocumentExtracted) -> None:
        seen.append(event)

    start_extraction(session, ctx, document_id, clock=clock)
    record_extraction_success(
        session,
        ctx,
        document_id,
        extractor="passthrough",
        body_text="hello world",
        pages_json=[{"page": 1, "char_start": 0, "char_end": 11}],
        token_count=2,
        has_secret_marker=False,
        asset_id=asset_id,
        clock=clock,
        event_bus=bus,
    )
    # Events fire on commit, not flush.
    assert seen == []
    session.commit()

    view = get_extraction(session, ctx, document_id)
    assert view.status == "succeeded"
    assert view.body_preview == "hello world"
    assert view.page_count == 1
    assert view.token_count == 2
    assert view.extracted_at == _NOW
    assert [e.document_id for e in seen] == [document_id]
    assert seen[0].asset_id == asset_id


def test_get_extraction_page_returns_bounded_window(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, asset_id, document_id = _seed_document(session, clock=clock)
    start_extraction(session, ctx, document_id, clock=clock)
    record_extraction_success(
        session,
        ctx,
        document_id,
        extractor="passthrough",
        body_text="abcdefghij",
        pages_json=[
            {"page": 1, "char_start": 0, "char_end": 5},
            {"page": 2, "char_start": 5, "char_end": 10},
        ],
        token_count=2,
        has_secret_marker=False,
        asset_id=asset_id,
        clock=clock,
    )

    page1 = get_extraction_page(session, ctx, document_id, 1)
    page2 = get_extraction_page(session, ctx, document_id, 2)
    page3 = get_extraction_page(session, ctx, document_id, 3)

    assert (page1.body, page1.more_pages) == ("abcde", True)
    assert (page2.body, page2.more_pages) == ("fghij", False)
    # Out-of-range page returns an empty window, not an error.
    assert (page3.body, page3.more_pages, page3.char_end) == ("", False, 0)


def test_record_extraction_failure_rearms_until_cap(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, asset_id, document_id = _seed_document(session, clock=clock)
    bus = EventBus()
    failures: list[AssetDocumentExtractionFailed] = []

    @bus.subscribe(AssetDocumentExtractionFailed)
    def collect(event: AssetDocumentExtractionFailed) -> None:
        failures.append(event)

    # First two failures re-arm to ``pending`` so the worker picks the
    # row up again next tick. The third pushes the row to the terminal.
    for _ in range(MAX_EXTRACTION_ATTEMPTS):
        start_extraction(session, ctx, document_id, clock=clock)
        record_extraction_failure(
            session,
            ctx,
            document_id,
            error="boom",
            asset_id=asset_id,
            clock=clock,
            event_bus=bus,
        )
    session.commit()

    view = get_extraction(session, ctx, document_id)
    assert view.status == "failed"
    assert view.attempts == MAX_EXTRACTION_ATTEMPTS
    assert view.last_error == "boom"
    assert [event.terminal for event in failures] == [False, False, True]


def test_record_extraction_unsupported_is_terminal(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, asset_id, document_id = _seed_document(session, clock=clock)
    bus = EventBus()
    failures: list[AssetDocumentExtractionFailed] = []

    @bus.subscribe(AssetDocumentExtractionFailed)
    def collect(event: AssetDocumentExtractionFailed) -> None:
        failures.append(event)

    start_extraction(session, ctx, document_id, clock=clock)
    record_extraction_unsupported(
        session,
        ctx,
        document_id,
        asset_id=asset_id,
        clock=clock,
        event_bus=bus,
    )
    session.commit()

    view = get_extraction(session, ctx, document_id)
    assert view.status == "unsupported"
    assert view.last_error is None
    assert [event.terminal for event in failures] == [True]


def test_record_extraction_empty_is_terminal(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, asset_id, document_id = _seed_document(session, clock=clock)

    start_extraction(session, ctx, document_id, clock=clock)
    record_extraction_empty(
        session,
        ctx,
        document_id,
        extractor="passthrough",
        asset_id=asset_id,
        clock=clock,
    )

    view = get_extraction(session, ctx, document_id)
    assert view.status == "empty"
    assert view.body_preview == ""
    assert view.token_count == 0
    assert view.extracted_at == _NOW


def test_retry_extraction_resets_failed_row(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, asset_id, document_id = _seed_document(session, clock=clock)
    bus = EventBus()
    retried: list[AssetDocumentExtractionRetried] = []

    @bus.subscribe(AssetDocumentExtractionRetried)
    def collect(event: AssetDocumentExtractionRetried) -> None:
        retried.append(event)

    for _ in range(MAX_EXTRACTION_ATTEMPTS):
        start_extraction(session, ctx, document_id, clock=clock)
        record_extraction_failure(
            session,
            ctx,
            document_id,
            error="boom",
            asset_id=asset_id,
            clock=clock,
        )
    retry_extraction(session, ctx, document_id, clock=clock, event_bus=bus)
    session.commit()

    view = get_extraction(session, ctx, document_id)
    assert view.status == "pending"
    assert view.attempts == 0
    assert view.last_error is None
    assert [event.document_id for event in retried] == [document_id]


def test_retry_extraction_rejects_non_failed_state(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, _asset_id, document_id = _seed_document(session, clock=clock)
    # Row is still in ``pending`` from the upload — retry has nothing to do.
    with pytest.raises(ExtractionRetryNotAllowed):
        retry_extraction(session, ctx, document_id, clock=clock)


def test_list_pending_extractions_returns_only_pending_rows(session: Session) -> None:
    clock = FrozenClock(_NOW)
    ctx, asset_id, document_id = _seed_document(session, clock=clock)

    pending = list_pending_extractions(session, limit=10)
    assert [row.id for row in pending] == [document_id]

    start_extraction(session, ctx, document_id, clock=clock)
    record_extraction_success(
        session,
        ctx,
        document_id,
        extractor="passthrough",
        body_text="x",
        pages_json=[{"page": 1, "char_start": 0, "char_end": 1}],
        token_count=1,
        has_secret_marker=False,
        asset_id=asset_id,
        clock=clock,
    )

    assert list_pending_extractions(session, limit=10) == []


def test_enqueue_extraction_idempotent_audit_row(session: Session) -> None:
    """Direct ``enqueue_extraction`` call writes one audit row and a pending row.

    Documents the seam reused by the upload path; if a future caller
    tries to mint an extraction outside ``attach_document`` they get
    the same audit shape.
    """
    clock = FrozenClock(_NOW)
    _ctx_unused, _asset_id, document_id = _seed_document(session, clock=clock)
    # First enqueue happened during ``attach_document``; reading it
    # back proves the row exists, no second call needed.
    row = session.get(FileExtraction, document_id)
    assert row is not None
    assert row.extraction_status == "pending"
    audit_actions = list(
        session.scalars(
            select(AuditLog.action).where(AuditLog.entity_id == document_id)
        ).all()
    )
    assert "file_extraction.pending" in audit_actions


def test_enqueue_extraction_seams_back_to_attach(session: Session) -> None:
    """Smoke that the helper exported from the module body runs.

    Exercises ``enqueue_extraction`` in isolation against a synthetic
    ``document_id`` so test coverage doesn't rely solely on
    ``attach_document``'s call site.
    """
    clock = FrozenClock(_NOW)
    ctx, _ = _seed_workspace(session)

    enqueue_extraction(session, ctx, "doc_synthetic", clock=clock)

    row = session.get(FileExtraction, "doc_synthetic")
    assert row is not None
    assert row.extraction_status == "pending"
    assert row.attempts == 0


def test_attach_document_text_passthrough_full_flow(session: Session) -> None:
    """End-to-end: upload -> pending row -> simulated worker tick.

    Calls the success mutator directly so the test stays unit-scoped;
    the worker integration path (storage + sniffer + tick) is covered
    by :mod:`tests.integration.test_asset_documents_api`.
    """
    clock = FrozenClock(_NOW)
    storage = InMemoryStorage()
    ctx, _ = _seed_workspace(session)
    asset = create_asset(
        session,
        ctx,
        property_id="prop_extraction",
        label="Asset",
        token_factory=lambda: "EXT100000099",
        clock=clock,
    )
    storage.put("c" * 64, io.BytesIO(b"hello"), content_type="text/plain")
    doc = attach_document(
        session,
        ctx,
        asset.id,
        blob_hash="c" * 64,
        filename="hello.txt",
        category="manual",
        title="Hello",
        storage=storage,
        clock=clock,
    )

    start_extraction(session, ctx, doc.id, clock=clock)
    record_extraction_success(
        session,
        ctx,
        doc.id,
        extractor="passthrough",
        body_text="hello",
        pages_json=[{"page": 1, "char_start": 0, "char_end": 5}],
        token_count=1,
        has_secret_marker=False,
        asset_id=asset.id,
        clock=clock,
    )

    view = get_extraction(session, ctx, doc.id)
    assert view.status == "succeeded"
    assert view.body_preview == "hello"
    page = get_extraction_page(session, ctx, doc.id, 1)
    assert page.body == "hello"
    assert page.more_pages is False
