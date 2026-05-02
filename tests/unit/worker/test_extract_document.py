"""Unit tests for the document text-extraction worker tick (cd-mo9e).

Mirrors the in-memory engine + ``make_uow`` patch used by
:mod:`tests.unit.chat_gateway.test_sweep`. Drives the public
:func:`extract_pending_documents` entry point against a real
:class:`InMemoryStorage`, covering the v1 rung dispatch:

* text/plain payload -> ``succeeded`` (with one ``pages_json`` entry).
* text payload that scrubs to empty -> ``empty`` (terminal).
* binary payload -> ``unsupported`` (terminal).
* Missing blob -> ``failed`` (re-arms to ``pending`` until the cap).

The extraction state machine is covered separately in
:mod:`tests.unit.test_assets_extraction`; this module pins the worker's
glue: storage read, MIME sniff, and per-row UoW commit.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import FileExtraction
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.storage.ports import MimeSniffer
from app.domain.assets.assets import create_asset
from app.domain.assets.documents import attach_document
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.worker.tasks.extract_document import extract_pending_documents
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_PINNED = datetime(2026, 5, 2, 9, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every adapter model so ``Base.metadata`` is complete."""
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
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def patched_uow(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]
) -> Iterator[None]:
    """Redirect ``make_uow`` (in the worker module) to the test factory."""

    @contextlib.contextmanager
    def _make_uow() -> Iterator[Session]:
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(
        "app.worker.tasks.extract_document.make_uow",
        _make_uow,
    )
    yield


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


class _FixedSniffer:
    """A :class:`MimeSniffer` that returns whatever it was constructed with.

    Lets each test pick the MIME the rung dispatcher sees without
    relying on the real magic-byte sniffer's heuristics. Mirrors the
    minimal Protocol shape on
    :class:`app.adapters.storage.ports.MimeSniffer`.
    """

    def __init__(self, mime: str | None) -> None:
        self._mime = mime

    def sniff(self, payload: bytes, hint: str | None = None) -> str | None:
        return self._mime


def _seed_workspace(session: Session, *, slug: str = "extract") -> WorkspaceContext:
    owner = bootstrap_user(
        session,
        email=f"{slug}-owner@example.com",
        display_name="Owner",
    )
    workspace = bootstrap_workspace(
        session,
        slug=slug,
        name=slug.title(),
        owner_user_id=owner.id,
    )
    property_id = f"prop_{slug}"
    session.add(
        Property(
            id=property_id,
            name=f"{slug.title()} House",
            kind="residence",
            address="3 Test Way",
            address_json={"line1": "3 Test Way", "country": "US"},
            country="US",
            timezone="UTC",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
    )
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace.id,
            label=f"{slug.title()} House",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_PINNED,
        )
    )
    session.flush()
    session.commit()
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_extract_doc",
    )


def _attach(
    session: Session,
    ctx: WorkspaceContext,
    storage: InMemoryStorage,
    *,
    body: bytes,
    filename: str,
    content_type: str,
    blob_hash: str,
    asset_token: str,
    clock: FrozenClock,
) -> str:
    asset = create_asset(
        session,
        ctx,
        property_id=f"prop_{ctx.workspace_slug}",
        label=f"Asset {asset_token[-3:]}",
        token_factory=lambda: asset_token,
        clock=clock,
    )
    storage.put(blob_hash, io.BytesIO(body), content_type=content_type)
    doc = attach_document(
        session,
        ctx,
        asset.id,
        blob_hash=blob_hash,
        filename=filename,
        category="manual",
        title="Manual",
        storage=storage,
        clock=clock,
    )
    session.commit()
    return doc.id


def test_extract_pending_documents_happy_path_text_plain(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
) -> None:
    with factory() as session:
        ctx = _seed_workspace(session)
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"hello extraction",
            filename="notes.txt",
            content_type="text/plain",
            blob_hash="a" * 64,
            asset_token="EXT100000010",
            clock=clock,
        )

    sniffer: MimeSniffer = _FixedSniffer("text/plain")
    report = extract_pending_documents(
        clock=clock, storage=storage, mime_sniffer=sniffer
    )

    assert report.processed_count == 1
    assert report.succeeded == 1
    assert report.processed_ids == (document_id,)

    with factory() as session:
        # Re-read the row's status under the test session.
        row = session.get(FileExtraction, document_id)
        assert row is not None
        assert row.extraction_status == "succeeded"
        assert row.body_text == "hello extraction"
        assert row.token_count == 2
        assert row.has_secret_marker is False


def test_extract_pending_documents_unsupported_for_binary(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
) -> None:
    with factory() as session:
        ctx = _seed_workspace(session)
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"\x00\x01\x02\x03binarybytes",
            filename="manual.bin",
            content_type="application/octet-stream",
            blob_hash="b" * 64,
            asset_token="EXT100000011",
            clock=clock,
        )

    sniffer: MimeSniffer = _FixedSniffer("application/pdf")
    report = extract_pending_documents(
        clock=clock, storage=storage, mime_sniffer=sniffer
    )

    assert report.unsupported == 1
    assert report.processed_ids == (document_id,)

    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        assert row.extraction_status == "unsupported"
        # Terminal: no last_error, body never persisted.
        assert row.last_error is None
        assert row.body_text is None


def test_extract_pending_documents_empty_for_whitespace_body(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
) -> None:
    with factory() as session:
        ctx = _seed_workspace(session)
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"   \n\t  \n",
            filename="empty.txt",
            content_type="text/plain",
            blob_hash="c" * 64,
            asset_token="EXT100000012",
            clock=clock,
        )

    sniffer: MimeSniffer = _FixedSniffer("text/plain")
    report = extract_pending_documents(
        clock=clock, storage=storage, mime_sniffer=sniffer
    )

    assert report.empty == 1

    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        assert row.extraction_status == "empty"
        assert row.body_text == ""
        assert row.token_count == 0


def test_extract_pending_documents_failure_rearms_for_missing_blob(
    factory: sessionmaker[Session],
    patched_uow: None,
    clock: FrozenClock,
) -> None:
    """Storage with no matching blob -> ``record_extraction_failure``.

    First tick sees ``attempts < MAX``, so the row re-arms back to
    ``pending``; the row's ``last_error`` carries the truncated reason.
    """
    storage = InMemoryStorage()  # deliberately empty
    with factory() as session:
        ctx = _seed_workspace(session, slug="missingblob")
        # Attach against a blob hash that the storage never received.
        # ``attach_document`` enforces ``storage.exists(blob_hash)``;
        # work around it here by attaching against a freshly-put blob
        # that we then drop from storage to simulate a deleted file
        # under the row's nose.
        storage.put("d" * 64, io.BytesIO(b"placeholder"), content_type="text/plain")
        ctx_with_doc = ctx
        document_id = _attach(
            session,
            ctx_with_doc,
            storage,
            body=b"placeholder",  # already in storage
            filename="lost.txt",
            content_type="text/plain",
            blob_hash="d" * 64,
            asset_token="EXT100000013",
            clock=clock,
        )

    # Drop the blob: the worker tick will fail to read it.
    storage.delete("d" * 64)

    sniffer: MimeSniffer = _FixedSniffer("text/plain")
    report = extract_pending_documents(
        clock=clock, storage=storage, mime_sniffer=sniffer
    )

    assert report.failed == 1

    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        # First failure: ``attempts < MAX_EXTRACTION_ATTEMPTS`` so the
        # row re-arms back to ``pending`` for the next tick.
        assert row.extraction_status == "pending"
        assert row.attempts == 1
        assert row.last_error == "blob_missing"


def test_extract_pending_documents_unexpected_exception_persists_failure(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``Exception`` from the rung must not roll back the row.

    Regression for the cd-mo9e self-review bug: re-raising after
    ``record_extraction_failure`` would roll back the failure write
    (and the ``attempts`` increment from ``start_extraction``),
    leaving the row stuck in ``pending`` with ``attempts=0`` and
    triggering an infinite retry loop on poisoned rows. The worker
    must instead let the UoW commit the failure write and return.
    """
    with factory() as session:
        ctx = _seed_workspace(session, slug="poison")
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"hello",
            filename="poison.txt",
            content_type="text/plain",
            blob_hash="e" * 64,
            asset_token="EXT100000014",
            clock=clock,
        )

    # Force an unexpected error inside the rung (post-``start_extraction``)
    # by monkeypatching ``_run_pipeline`` to raise a non-``_ExtractionError``.
    def _explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("rung exploded")

    monkeypatch.setattr(
        "app.worker.tasks.extract_document._run_pipeline",
        _explode,
    )

    sniffer: MimeSniffer = _FixedSniffer("text/plain")
    report = extract_pending_documents(
        clock=clock, storage=storage, mime_sniffer=sniffer
    )

    # Per-row unexpected exception is now caught and committed inside
    # ``_extract_one`` so the outer ``except`` is unreachable; the
    # tick reports the row as ``failed`` either way.
    assert report.failed == 1
    assert report.processed_ids == (document_id,)

    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        # Critical: ``attempts`` is *committed* (no rollback) so a
        # poisoned row hits the cap after MAX_EXTRACTION_ATTEMPTS
        # ticks instead of looping forever.
        assert row.attempts == 1
        # First failure re-arms to ``pending`` (cap not yet hit).
        assert row.extraction_status == "pending"
        assert row.last_error is not None
        assert row.last_error.startswith("unexpected: RuntimeError")
