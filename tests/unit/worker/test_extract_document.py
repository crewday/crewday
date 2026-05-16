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
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import FileExtraction
from app.adapters.db.base import Base
from app.adapters.db.llm.models import (
    BudgetLedger,
    LlmAssignment,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
    LlmUsage,
)
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.llm.ports import (
    ChatMessage,
    LlmProviderError,
    LlmRateLimited,
    LLMResponse,
    LlmThinkingLevel,
    LlmThinkingStrategy,
    LlmTransportError,
    LLMUsage,
    Tool,
)
from app.adapters.storage.ports import MimeSniffer
from app.domain.assets.assets import create_asset
from app.domain.assets.documents import attach_document
from app.domain.llm.budget import PriceComponents
from app.domain.llm.client import LLMClient as RoutedLLMClient
from app.domain.llm.router import invalidate_cache
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.redact import ConsentSet
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


@pytest.fixture(autouse=True)
def reset_llm_router_cache() -> Iterator[None]:
    invalidate_cache()
    try:
        yield
    finally:
        invalidate_cache()


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


class _RecordingVisionAdapter:
    def __init__(
        self,
        *,
        text: str = "Front panel\nReset button",
        finish_reason: str = "stop",
        error: BaseException | None = None,
    ) -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.error = error
        self.chat_messages: list[Sequence[ChatMessage]] = []
        self.chat_tools: list[Sequence[Tool] | None] = []

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_level: LlmThinkingLevel = "disabled",
        thinking_strategy: LlmThinkingStrategy = "none",
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_level: LlmThinkingLevel = "disabled",
        thinking_strategy: LlmThinkingStrategy = "none",
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        del max_tokens, temperature, thinking_level, thinking_strategy, consents
        self.chat_messages.append(messages)
        self.chat_tools.append(tools)
        if self.error is not None:
            raise self.error
        return LLMResponse(
            text=self.text,
            usage=LLMUsage(prompt_tokens=17, completion_tokens=3, total_tokens=20),
            model_id=model_id,
            finish_reason=self.finish_reason,
        )

    def ocr(
        self,
        *,
        model_id: str,
        image_bytes: bytes,
        consents: ConsentSet | None = None,
    ) -> str:
        raise NotImplementedError

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_level: LlmThinkingLevel = "disabled",
        thinking_strategy: LlmThinkingStrategy = "none",
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
    ) -> Iterator[str]:
        raise NotImplementedError


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


def _seed_documents_ocr_assignment(
    session: Session,
    ctx: WorkspaceContext,
    *,
    cap_cents: int = 10_000,
) -> None:
    period_start = _PINNED - timedelta(days=1)
    period_end = _PINNED + timedelta(days=29)
    session.add_all(
        [
            LlmProvider(
                id="01HWA0000000000000OCRPRV",
                name=f"ocr-provider-{ctx.workspace_slug}",
                provider_type="fake",
                timeout_s=60,
                requests_per_minute=60,
                is_enabled=True,
                created_at=_PINNED,
                updated_at=_PINNED,
            ),
            LlmModel(
                id="01HWA0000000000000OCRMOD",
                canonical_name=f"vision/{ctx.workspace_slug}",
                display_name="Vision",
                capabilities=["vision"],
                is_active=True,
                price_source="",
                created_at=_PINNED,
                updated_at=_PINNED,
            ),
        ]
    )
    session.flush()
    session.add(
        LlmProviderModel(
            id="01HWA00000000000000OCRPM",
            provider_id="01HWA0000000000000OCRPRV",
            model_id="01HWA0000000000000OCRMOD",
            api_model_id="vision-test-model",
            supports_system_prompt=True,
            supports_temperature=True,
            is_enabled=True,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    session.flush()
    session.add_all(
        [
            LlmAssignment(
                id="01HWA000000000000OCRASG",
                workspace_id=None,
                capability="documents.ocr",
                model_id="01HWA00000000000000OCRPM",
                provider="fake",
                priority=0,
                enabled=True,
                required_capabilities=["vision"],
                created_at=_PINNED,
            ),
            BudgetLedger(
                id="01HWA000000000000OCRBDG",
                workspace_id=ctx.workspace_id,
                period_start=period_start,
                period_end=period_end,
                spent_cents=0,
                cap_cents=cap_cents,
                updated_at=_PINNED,
            ),
        ]
    )
    session.flush()


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


def test_extract_pending_documents_uses_documents_ocr_for_image(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
) -> None:
    with factory() as session:
        ctx = _seed_workspace(session, slug="ocrimage")
        _seed_documents_ocr_assignment(session, ctx)
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"\x89PNG\r\n\x1a\nimagebytes",
            filename="panel.png",
            content_type="image/png",
            blob_hash="f" * 64,
            asset_token="EXT100000015",
            clock=clock,
        )
        session.commit()

    adapter = _RecordingVisionAdapter(text="Front panel\nReset button")
    llm_client = RoutedLLMClient(adapter)
    sniffer: MimeSniffer = _FixedSniffer("image/png")

    report = extract_pending_documents(
        clock=clock,
        storage=storage,
        mime_sniffer=sniffer,
        llm_client=llm_client,
    )

    assert report.succeeded == 1
    assert report.processed_ids == (document_id,)
    assert len(adapter.chat_messages) == 1
    assert adapter.chat_tools == [None]

    message = adapter.chat_messages[0][0]
    assert message["role"] == "user"
    content = message["content"]
    assert isinstance(content, list)
    prompt_blocks = [block["text"] for block in content if block["type"] == "text"]
    assert len(prompt_blocks) == 1
    prompt = prompt_blocks[0]
    assert "verbatim" in prompt.lower()
    assert "receipt" not in prompt.lower()
    assert "vendor" not in prompt.lower()
    assert "amount_cents" not in prompt
    assert "is_receipt" not in prompt
    image_blocks = [
        block["image_url"]["url"] for block in content if block["type"] == "image_url"
    ]
    assert image_blocks == ["data:image/png;base64,iVBORw0KGgppbWFnZWJ5dGVz"]

    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        assert row.extraction_status == "succeeded"
        assert row.extractor == "ocr"
        assert row.body_text == "Front panel\nReset button"
        assert row.pages_json == [{"page": 1, "char_start": 0, "char_end": 24}]
        assert row.token_count == 4
        usage = session.query(LlmUsage).one()
        assert usage.capability == "documents.ocr"
        assert usage.agent_label == "asset-document-extraction"


def test_extract_pending_documents_empty_ocr_output_is_empty(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
) -> None:
    with factory() as session:
        ctx = _seed_workspace(session, slug="ocrempty")
        _seed_documents_ocr_assignment(session, ctx)
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"\xff\xd8\xffimagebytes",
            filename="blank.jpg",
            content_type="image/jpeg",
            blob_hash="0" * 64,
            asset_token="EXT100000016",
            clock=clock,
        )
        session.commit()

    report = extract_pending_documents(
        clock=clock,
        storage=storage,
        mime_sniffer=_FixedSniffer("image/jpeg"),
        llm_client=RoutedLLMClient(_RecordingVisionAdapter(text=" \n\t ")),
    )

    assert report.empty == 1
    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        assert row.extraction_status == "empty"
        assert row.extractor == "ocr"
        assert row.body_text == ""


def test_extract_pending_documents_unassigned_documents_ocr_is_unsupported(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
) -> None:
    with factory() as session:
        ctx = _seed_workspace(session, slug="ocrunassigned")
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"RIFF----WEBPimagebytes",
            filename="manual.webp",
            content_type="image/webp",
            blob_hash="1" * 64,
            asset_token="EXT100000017",
            clock=clock,
        )
        session.commit()

    adapter = _RecordingVisionAdapter()
    report = extract_pending_documents(
        clock=clock,
        storage=storage,
        mime_sniffer=_FixedSniffer("image/webp"),
        llm_client=RoutedLLMClient(adapter),
    )

    assert report.unsupported == 1
    assert adapter.chat_messages == []
    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        assert row.extraction_status == "unsupported"
        assert row.last_error is None


@pytest.mark.parametrize(
    ("adapter", "last_error"),
    [
        (
            _RecordingVisionAdapter(
                finish_reason="safety",
                text="I cannot process this image.",
            ),
            "provider_refused",
        ),
        (_RecordingVisionAdapter(error=LlmRateLimited("slow down")), "rate_limited"),
        (
            _RecordingVisionAdapter(error=LlmTransportError("network down")),
            "transport_error",
        ),
        (
            _RecordingVisionAdapter(error=LlmProviderError("bad request")),
            "provider_error",
        ),
    ],
)
def test_extract_pending_documents_ocr_failures_rearm(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
    adapter: _RecordingVisionAdapter,
    last_error: str,
) -> None:
    with factory() as session:
        ctx = _seed_workspace(session, slug=f"ocrfail{last_error[:4]}")
        _seed_documents_ocr_assignment(session, ctx)
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"\x89PNG\r\n\x1a\nbadimage",
            filename="bad.png",
            content_type="image/png",
            blob_hash=last_error.encode("utf-8").hex().ljust(64, "0")[:64],
            asset_token="EXT100000018",
            clock=clock,
        )
        session.commit()

    report = extract_pending_documents(
        clock=clock,
        storage=storage,
        mime_sniffer=_FixedSniffer("image/png"),
        llm_client=RoutedLLMClient(adapter),
    )

    assert report.failed == 1
    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        assert row.extraction_status == "pending"
        assert row.attempts == 1
        assert row.last_error == last_error


def test_extract_pending_documents_ocr_budget_exceeded_rearms(
    factory: sessionmaker[Session],
    patched_uow: None,
    storage: InMemoryStorage,
    clock: FrozenClock,
) -> None:
    with factory() as session:
        ctx = _seed_workspace(session, slug="ocrbudget")
        _seed_documents_ocr_assignment(session, ctx, cap_cents=0)
        document_id = _attach(
            session,
            ctx,
            storage,
            body=b"\x89PNG\r\n\x1a\nbudget",
            filename="budget.png",
            content_type="image/png",
            blob_hash="2" * 64,
            asset_token="EXT100000019",
            clock=clock,
        )
        session.commit()

    adapter = _RecordingVisionAdapter()
    report = extract_pending_documents(
        clock=clock,
        storage=storage,
        mime_sniffer=_FixedSniffer("image/png"),
        llm_client=RoutedLLMClient(
            adapter,
            pricing={
                "vision-test-model": PriceComponents(fixed_cost_per_call_usd=1),
            },
        ),
    )

    assert report.failed == 1
    assert adapter.chat_messages == []
    with factory() as session:
        row = session.get(FileExtraction, document_id)
        assert row is not None
        assert row.extraction_status == "pending"
        assert row.last_error == "budget_exceeded"


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
