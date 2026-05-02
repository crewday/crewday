"""``extract_document`` — document text-extraction worker tick (cd-mo9e).

Walks every ``file_extraction`` row in ``status='pending'``, opens a
fresh UoW per row, and runs the v1 extraction pipeline:

1. Resolve the matching :class:`AssetDocument` (filename + blob hash)
   and the deployment-wide :class:`Storage` adapter.
2. Bracket the row's mutations in a system-actor
   :class:`~app.tenancy.WorkspaceContext` so audit rows + SSE events
   carry the row's own ``workspace_id``.
3. Flip ``pending`` -> ``extracting``, attempt the rung, then settle
   on a terminal:

   - ``text/*`` (sniffed) -> ``passthrough`` rung. Empty body -> ``empty``.
     Non-empty body -> ``succeeded`` (with one ``pages_json`` entry
     covering the whole text).
   - Anything else (PDF, DOCX, images) -> ``unsupported`` for v1.
     The §21 rung table has slots for ``pdf`` / ``docx`` / ``ocr``
     extractors; cd-mo9e only ships the passthrough rung.
   - Storage / IO errors -> ``record_extraction_failure``; the row
     re-arms back to ``pending`` until ``MAX_EXTRACTION_ATTEMPTS``.

Cross-tenant by design — like the sibling webhook + invite-TTL
sweeps, the tick is deployment-scope. Each ``file_extraction`` row
carries its own ``workspace_id``; the per-row UoW + system-actor
context keep tenant isolation honest while letting one global tick
clear the queue without a per-workspace fan-out.

See ``docs/specs/02-domain-model.md`` §"file_extraction",
``docs/specs/21-assets.md`` §"Document text extraction", and
``docs/specs/16-deployment-operations.md`` §"Worker process".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import AssetDocument
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import Workspace
from app.adapters.storage.mime import FiletypeMimeSniffer
from app.adapters.storage.ports import BlobNotFound, MimeSniffer, Storage
from app.api.factory import _build_storage
from app.config import get_settings
from app.domain.assets.extraction import (
    list_pending_extractions,
    record_extraction_empty,
    record_extraction_failure,
    record_extraction_success,
    record_extraction_unsupported,
    start_extraction,
)
from app.tenancy import reset_current, set_current, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.redact import scrub_string

__all__ = [
    "ExtractDocumentReport",
    "extract_pending_documents",
]

_log = logging.getLogger(__name__)

_LOG_EVENT: Final[str] = "extract_document.tick"

# Max documents to drain per tick. Mirrors the 50-row cap on
# :func:`list_pending_extractions` — keeps a backlog clearing across
# multiple ticks instead of pinning one tick on a thousand-row scan.
_MAX_PER_TICK: Final[int] = 50

# Max bytes the passthrough rung will read off storage in v1. Aligned
# with :data:`app.api.assets._shared.MAX_ASSET_DOCUMENT_BYTES` (25 MB);
# the §21 spec pins 50 MB at the deployment-setting level, but the
# upload route already clamps writes at 25 MB so anything larger
# cannot be on disk for the worker to pick up. A future cd-mo9e-pdf
# rung that handles bigger files lifts this cap.
_MAX_EXTRACT_BYTES: Final[int] = 25 * 1024 * 1024

# Pinned system-actor identifier. Mirrors the constant in
# :mod:`app.worker.jobs.common` — kept module-private here to avoid
# importing the leading-underscore name across worker subpackages.
_SYSTEM_ACTOR_ZERO_ULID: Final[str] = "00000000000000000000000000"

# Truncation cap for ``last_error`` strings. Long stack traces would
# bloat the row and the SSE payload; 240 chars matches the §02 audit
# diff-string cap so a failure surface fits in a chip + tooltip.
_LAST_ERROR_MAX_CHARS: Final[int] = 240


@dataclass(frozen=True, slots=True)
class ExtractDocumentReport:
    """Summary of one :func:`extract_pending_documents` call.

    ``processed_count`` is the number of rows touched. The four
    outcome counters (``succeeded`` / ``failed`` / ``unsupported`` /
    ``empty``) split the per-row terminals so an operator dashboard
    can graph the rung-distribution. ``processed_ids`` is the full
    set of document ids the tick handled — useful for tests pinning
    determinism on a fixture set.
    """

    processed_count: int
    succeeded: int
    failed: int
    unsupported: int
    empty: int
    processed_ids: tuple[str, ...]


def extract_pending_documents(
    *,
    clock: Clock | None = None,
    storage: Storage | None = None,
    mime_sniffer: MimeSniffer | None = None,
) -> ExtractDocumentReport:
    """Run one extraction sweep across the deployment.

    Reads up to :data:`_MAX_PER_TICK` ``pending`` rows, opens a fresh
    UoW per row, and walks each through the v1 pipeline. Each row's
    UoW commits independently so a failure on one document does not
    roll back the sweep.

    The clock + storage + sniffer are injectable for tests; production
    passes ``None`` and the helpers fall back to :class:`SystemClock`,
    :func:`_build_storage` (LocalFs by default), and
    :class:`FiletypeMimeSniffer`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_storage = storage if storage is not None else _resolve_storage()
    if resolved_storage is None:
        # No storage backend is wired (root key unset, S3 not yet
        # implemented). Bail noisily so /readyz catches the misconfig
        # via the heartbeat — the next tick retries once the deploy
        # is fixed.
        _log.warning(
            "extract_document tick: storage backend unavailable; skipping",
            extra={"event": "extract_document.skipped_no_storage"},
        )
        return ExtractDocumentReport(
            processed_count=0,
            succeeded=0,
            failed=0,
            unsupported=0,
            empty=0,
            processed_ids=(),
        )
    resolved_sniffer = (
        mime_sniffer if mime_sniffer is not None else FiletypeMimeSniffer()
    )

    # First read — gather pending ids in one short UoW. We process
    # each row in its own UoW below so a long sweep never pins one
    # transaction across every extraction.
    with make_uow() as session:
        assert isinstance(session, Session)
        pending_rows = list_pending_extractions(session, limit=_MAX_PER_TICK)
        # Materialise the (id, workspace_id) tuples; the row objects
        # are bound to a session that closes when this block exits.
        pending: list[tuple[str, str]] = [
            (row.id, row.workspace_id) for row in pending_rows
        ]

    succeeded = 0
    failed = 0
    unsupported = 0
    empty = 0
    processed: list[str] = []

    for document_id, workspace_id in pending:
        try:
            outcome = _extract_one(
                document_id=document_id,
                workspace_id=workspace_id,
                clock=resolved_clock,
                storage=resolved_storage,
                sniffer=resolved_sniffer,
            )
        except Exception:
            # Per-row failure: log and continue. The tick must not
            # die on one bad row; the row's own status flip (or the
            # next tick's retry) closes the loop.
            _log.exception(
                "extract_document row failed unexpectedly",
                extra={
                    "event": "extract_document.row_error",
                    "document_id": document_id,
                    "workspace_id": workspace_id,
                },
            )
            failed += 1
            processed.append(document_id)
            continue

        processed.append(document_id)
        if outcome == "succeeded":
            succeeded += 1
        elif outcome == "failed":
            failed += 1
        elif outcome == "unsupported":
            unsupported += 1
        elif outcome == "empty":
            empty += 1

    _log.info(
        "extract_document tick completed",
        extra={
            "event": _LOG_EVENT,
            "processed_count": len(processed),
            "succeeded": succeeded,
            "failed": failed,
            "unsupported": unsupported,
            "empty": empty,
        },
    )
    return ExtractDocumentReport(
        processed_count=len(processed),
        succeeded=succeeded,
        failed=failed,
        unsupported=unsupported,
        empty=empty,
        processed_ids=tuple(processed),
    )


def _extract_one(
    *,
    document_id: str,
    workspace_id: str,
    clock: Clock,
    storage: Storage,
    sniffer: MimeSniffer,
) -> str:
    """Process one pending row inside its own UoW. Returns the terminal name."""
    with make_uow() as session:
        assert isinstance(session, Session)
        ctx = _build_context(session, workspace_id=workspace_id)
        if ctx is None:
            # Workspace was deleted between the queue read and this
            # row's pop. The orphaned ``file_extraction`` row will be
            # CASCADE-removed when the underlying ``asset_document`` /
            # ``workspace`` cleanup runs; for now we leave it in
            # ``pending`` and the next tick re-checks.
            _log.warning(
                "extract_document: workspace missing, skipping row",
                extra={
                    "event": "extract_document.workspace_missing",
                    "document_id": document_id,
                    "workspace_id": workspace_id,
                },
            )
            return "skipped"

        token = set_current(ctx)
        try:
            document = _load_document(session, ctx, document_id)
            if document is None:
                # Document row was deleted; same as workspace_missing.
                # The CASCADE on ``asset_document`` removes the row.
                return "skipped"
            asset_id = document.asset_id

            # Flip pending -> extracting (also bumps attempts).
            start_extraction(session, ctx, document_id, clock=clock)
            payload = _read_blob(storage, document.blob_hash)
            mime = _sniff_mime(sniffer, payload, filename=document.filename)
            return _run_pipeline(
                session,
                ctx,
                document_id=document_id,
                asset_id=asset_id,
                payload=payload,
                mime=mime,
                clock=clock,
            )
        except _ExtractionError as exc:
            record_extraction_failure(
                session,
                ctx,
                document_id,
                error=_truncate_error(exc.message),
                asset_id=_safe_asset_id(session, ctx, document_id),
                clock=clock,
            )
            return "failed"
        except Exception as exc:
            # Defensive backstop. We persist a short summary on the row
            # and let the UoW *commit* the failure write — re-raising
            # here would roll back the failure record (and the
            # ``attempts`` increment from ``start_extraction``), which
            # would leave the row stuck in ``pending`` with
            # ``attempts=0`` and trip an infinite-retry loop on poisoned
            # rows. Log the trace inline so operators can still see
            # what went wrong; the caller's defensive ``except`` block
            # is now unreachable for this path but stays as a guard
            # against a future contributor accidentally re-raising.
            _log.exception(
                "extract_document row failed unexpectedly",
                extra={
                    "event": "extract_document.row_unexpected_error",
                    "document_id": document_id,
                    "workspace_id": ctx.workspace_id,
                    "exception_type": type(exc).__name__,
                },
            )
            record_extraction_failure(
                session,
                ctx,
                document_id,
                error=_truncate_error(f"unexpected: {type(exc).__name__}: {exc}"),
                asset_id=_safe_asset_id(session, ctx, document_id),
                clock=clock,
            )
            return "failed"
        finally:
            reset_current(token)


def _run_pipeline(
    session: Session,
    ctx: WorkspaceContext,
    *,
    document_id: str,
    asset_id: str | None,
    payload: bytes,
    mime: str | None,
    clock: Clock,
) -> str:
    """Pick a rung and persist the terminal. Returns the terminal name."""
    if mime is None or not _is_text_mime(mime):
        # v1 only ships the passthrough rung. PDF / DOCX / OCR rungs
        # are §21 follow-ups; until they land, anything not text/* is
        # ``unsupported`` (terminal) so the manager can decide whether
        # to re-upload as text or wait for the rung.
        record_extraction_unsupported(
            session,
            ctx,
            document_id,
            asset_id=asset_id,
            clock=clock,
        )
        return "unsupported"

    body = _decode_text(payload)
    scrubbed = scrub_string(body)
    has_secret = scrubbed != body
    if not scrubbed.strip():
        record_extraction_empty(
            session,
            ctx,
            document_id,
            extractor="passthrough",
            asset_id=asset_id,
            clock=clock,
        )
        return "empty"

    pages_json: list[dict[str, int]] = [
        {"page": 1, "char_start": 0, "char_end": len(scrubbed)},
    ]
    record_extraction_success(
        session,
        ctx,
        document_id,
        extractor="passthrough",
        body_text=scrubbed,
        pages_json=pages_json,
        # Cheap whitespace-split token estimate. The §11 vector-DB
        # ingest does its own tokenisation; this number is for the UI
        # chip + budget hints only.
        token_count=len(scrubbed.split()),
        has_secret_marker=has_secret,
        asset_id=asset_id,
        clock=clock,
    )
    return "succeeded"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ExtractionError(Exception):
    """Raised inside ``_extract_one`` when the rung cannot run.

    Carries a short user-facing ``message`` (e.g. ``"blob missing"``,
    ``"file_too_large"``) suitable for ``last_error`` after truncation.
    """

    __slots__ = ("message",)

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _resolve_storage() -> Storage | None:
    return _build_storage(get_settings())


def _build_context(
    session: Session,
    *,
    workspace_id: str,
) -> WorkspaceContext | None:
    with tenant_agnostic():
        slug = session.scalar(
            select(Workspace.slug).where(Workspace.id == workspace_id)
        )
    if slug is None:
        return None
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=_SYSTEM_ACTOR_ZERO_ULID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=_SYSTEM_ACTOR_ZERO_ULID,
        principal_kind="system",
    )


def _load_document(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> AssetDocument | None:
    with tenant_agnostic():
        return session.scalar(
            select(AssetDocument).where(
                AssetDocument.workspace_id == ctx.workspace_id,
                AssetDocument.id == document_id,
            )
        )


def _safe_asset_id(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> str | None:
    """Return the document's ``asset_id`` for SSE payloads, ignoring lookup errors."""
    try:
        document = _load_document(session, ctx, document_id)
    except Exception:
        return None
    return document.asset_id if document is not None else None


def _read_blob(storage: Storage, blob_hash: str | None) -> bytes:
    """Read the document's bytes off the content-addressed store.

    Caps reads at :data:`_MAX_EXTRACT_BYTES`; anything larger raises
    :class:`_ExtractionError("file_too_large")` so the row terminates
    on ``failed`` (not ``unsupported``) while we wait for the bigger-
    file rungs to land.
    """
    if blob_hash is None:
        raise _ExtractionError("blob_hash_missing")
    try:
        with storage.get(blob_hash) as fh:
            data = fh.read(_MAX_EXTRACT_BYTES + 1)
    except BlobNotFound as exc:
        raise _ExtractionError("blob_missing") from exc
    if len(data) > _MAX_EXTRACT_BYTES:
        raise _ExtractionError("file_too_large")
    return data


def _sniff_mime(
    sniffer: MimeSniffer, payload: bytes, *, filename: str | None
) -> str | None:
    """Return the sniffed MIME, falling back to a filename-extension hint.

    The default :class:`FiletypeMimeSniffer` does not detect plain
    text via magic bytes (text has no signature); for ``.txt`` /
    ``.md`` / ``.csv`` uploads we fall back to the filename suffix
    so the passthrough rung can still claim them. A future MIME
    sniffer that runs a heuristic on text-shaped payloads can drop
    this branch — until then the suffix is the cheapest check.
    """
    sniffed = sniffer.sniff(payload)
    if sniffed is not None:
        return sniffed
    if filename is None:
        return None
    lower = filename.lower()
    if lower.endswith((".txt", ".text")):
        return "text/plain"
    if lower.endswith((".md", ".markdown")):
        return "text/markdown"
    if lower.endswith(".csv"):
        return "text/csv"
    if lower.endswith((".html", ".htm")):
        return "text/html"
    return None


def _is_text_mime(mime: str) -> bool:
    """Return whether ``mime`` is in the v1 passthrough rung's vocabulary."""
    if mime.startswith("text/"):
        return True
    # Permit ``application/json`` even though §21's rung table does
    # not list it explicitly: the passthrough rung handles any
    # UTF-8-decodable text payload, and JSON documents (asset config
    # exports, manifest dumps) are a legitimate manager attachment.
    return mime == "application/json"


def _decode_text(payload: bytes) -> str:
    """Decode ``payload`` as UTF-8 with ``replace`` for malformed bytes.

    The passthrough rung is tolerant — a single bad byte should not
    fail the whole extraction; the §11 secret scrub runs on whatever
    text we recover. Pages with too many replacement characters are
    a follow-up signal (cd-mo9e-quality) but are not a v1 terminal.
    """
    return payload.decode("utf-8", errors="replace")


def _truncate_error(message: str) -> str:
    """Bound ``last_error`` length so the row stays small."""
    if len(message) <= _LAST_ERROR_MAX_CHARS:
        return message
    return message[: _LAST_ERROR_MAX_CHARS - 3] + "..."
