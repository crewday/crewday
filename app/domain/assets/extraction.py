"""Document text-extraction state machine (cd-mo9e).

Owns the ``file_extraction`` row paired 1:1 with each
:class:`~app.adapters.db.assets.models.AssetDocument`. The state
machine and field shapes are pinned by
``docs/specs/02-domain-model.md`` §"file_extraction" and
``docs/specs/21-assets.md`` §"Document text extraction":

``pending`` (minted on upload)
-> ``extracting`` (worker claims the row; ``attempts`` += 1)
-> ``succeeded`` | ``failed`` | ``unsupported`` | ``empty``

A ``failed`` row whose ``attempts < MAX_EXTRACTION_ATTEMPTS`` may be
re-armed back to ``pending`` by a manager via ``POST
/documents/{id}/extraction/retry`` (§21 "Failure modes"). Retry resets
``attempts`` to ``0`` per the spec — the manager is asserting we
should try again from scratch — and writes a fresh audit row.

Audit + SSE posture mirrors :mod:`app.domain.assets.documents`:
domain mutators write a workspace-scoped audit row inline and queue
an :class:`~app.events.types.Event` on the session that fires once
the UoW commits (the ``after_commit`` hook here is shared with the
asset-event hook so a single transaction emits one ordered batch).
"""

from __future__ import annotations

import weakref
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import AssetDocument, FileExtraction
from app.audit import write_audit
from app.domain.assets.documents import AssetDocumentNotFound
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.registry import Event
from app.events.types import (
    AssetDocumentExtracted,
    AssetDocumentExtractionFailed,
    AssetDocumentExtractionRetried,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.clock import aware_utc as _as_utc

__all__ = [
    "MAX_EXTRACTION_ATTEMPTS",
    "DocumentExtractionPageView",
    "DocumentExtractionView",
    "ExtractionRetryNotAllowed",
    "enqueue_extraction",
    "get_extraction",
    "get_extraction_page",
    "list_pending_extractions",
    "record_extraction_empty",
    "record_extraction_failure",
    "record_extraction_success",
    "record_extraction_unsupported",
    "retry_extraction",
    "start_extraction",
]


# §21 "Failure modes" pins three attempts before we stop retrying. The
# fourth ``attempts`` increment lands on the ``failed`` terminal — the
# row sticks until a manager retries.
MAX_EXTRACTION_ATTEMPTS: Final[int] = 3


class ExtractionRetryNotAllowed(ValueError):
    """The current row is not in a retryable state."""


@dataclass(frozen=True, slots=True)
class DocumentExtractionPageView:
    """One bounded page window from the persisted ``pages_json``."""

    page: int
    char_start: int
    char_end: int
    body: str
    more_pages: bool


@dataclass(frozen=True, slots=True)
class DocumentExtractionView:
    """Read-shape of a ``file_extraction`` row."""

    document_id: str
    workspace_id: str
    status: str
    extractor: str | None
    body_preview: str
    page_count: int
    token_count: int
    has_secret_marker: bool
    last_error: str | None
    extracted_at: datetime | None
    attempts: int


# Body-preview cap shared with §21 "Public surface": ``GET /extraction``
# returns at most this many leading characters. The full ``body_text``
# stays on the row for ``GET /extraction/pages/{n}`` to slice.
_BODY_PREVIEW_CHARS: Final[int] = 4000


# ---------------------------------------------------------------------------
# Pending-event hook (mirrors app.domain.assets.assets._queue_asset_changed).
# Each mutator queues an :class:`Event` on the session; the
# ``after_commit`` listener publishes the batch once the UoW commits.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PendingExtractionEvent:
    bus: EventBus
    event: Event


_PENDING_EVENTS: weakref.WeakKeyDictionary[Session, list[_PendingExtractionEvent]] = (
    weakref.WeakKeyDictionary()
)
_HOOKED_SESSIONS: weakref.WeakSet[Session] = weakref.WeakSet()


def _queue_event(session: Session, pending: _PendingExtractionEvent) -> None:
    if session not in _HOOKED_SESSIONS:
        from sqlalchemy import event

        event.listen(session, "after_commit", _publish_pending_events)
        event.listen(session, "after_rollback", _clear_pending_events)
        _HOOKED_SESSIONS.add(session)
    _PENDING_EVENTS.setdefault(session, []).append(pending)


def _publish_pending_events(session: Session) -> None:
    if session.in_nested_transaction():
        return
    pending = _PENDING_EVENTS.pop(session, [])
    for item in pending:
        item.bus.publish(item.event)


def _clear_pending_events(session: Session) -> None:
    if session.in_nested_transaction():
        return
    _PENDING_EVENTS.pop(session, None)


# ---------------------------------------------------------------------------
# Domain service entry points
# ---------------------------------------------------------------------------


def enqueue_extraction(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    *,
    clock: Clock | None = None,
) -> FileExtraction:
    """Mint a ``pending`` ``file_extraction`` row for a fresh document.

    Called inline from :func:`app.domain.assets.documents.attach_document`
    so the row exists in the same UoW as the document insert. No SSE
    event fires here — the upload's REST response is the user-visible
    "we got it" signal; subsequent state transitions emit their own
    events as the worker walks the row.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = FileExtraction(
        id=document_id,
        workspace_id=ctx.workspace_id,
        extraction_status="pending",
        extractor=None,
        body_text=None,
        pages_json=None,
        token_count=None,
        has_secret_marker=False,
        attempts=0,
        last_error=None,
        extracted_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="file_extraction",
        entity_id=document_id,
        action="file_extraction.pending",
        diff={"after": {"status": "pending", "attempts": 0}},
        clock=resolved_clock,
    )
    return row


def start_extraction(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    *,
    clock: Clock | None = None,
) -> FileExtraction:
    """Flip a ``pending`` row to ``extracting`` and bump ``attempts``.

    Called from the worker tick once it has decided to process the row.
    Idempotent against a row already in ``extracting`` (the worker is
    deployment-scope; if a previous tick crashed mid-flight the row
    can stay in this state — the recovery path runs ``record_extraction_failure``
    or ``record_extraction_success`` to advance regardless).
    """
    row = _load_extraction_row(session, ctx, document_id)
    if row.extraction_status not in ("pending", "extracting"):
        # ``succeeded`` / ``failed`` / ``unsupported`` / ``empty`` are
        # terminal; the worker should not be picking them up. Refuse
        # loudly so an out-of-order tick surfaces a real bug instead
        # of silently double-counting attempts.
        raise ExtractionRetryNotAllowed(
            f"cannot start extraction from status={row.extraction_status!r}"
        )
    resolved_clock = clock if clock is not None else SystemClock()
    row.extraction_status = "extracting"
    row.attempts = row.attempts + 1
    row.updated_at = resolved_clock.now()
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="file_extraction",
        entity_id=document_id,
        action="file_extraction.extracting",
        diff={"after": {"status": "extracting", "attempts": row.attempts}},
        clock=resolved_clock,
    )
    return row


def record_extraction_success(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    *,
    extractor: str,
    body_text: str,
    pages_json: Sequence[dict[str, int]],
    token_count: int,
    has_secret_marker: bool,
    asset_id: str | None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> FileExtraction:
    """Persist a successful extraction and fire ``asset_document.extracted``."""
    row = _load_extraction_row(session, ctx, document_id)
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    row.extraction_status = "succeeded"
    row.extractor = extractor
    row.body_text = body_text
    row.pages_json = list(pages_json)
    row.token_count = token_count
    row.has_secret_marker = has_secret_marker
    row.last_error = None
    row.extracted_at = now
    row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="file_extraction",
        entity_id=document_id,
        action="file_extraction.succeeded",
        diff={
            "after": {
                "status": "succeeded",
                "extractor": extractor,
                "page_count": len(pages_json),
                "token_count": token_count,
                "has_secret_marker": has_secret_marker,
            }
        },
        clock=resolved_clock,
    )
    _queue_event(
        session,
        _PendingExtractionEvent(
            bus=resolved_bus,
            event=AssetDocumentExtracted(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=_as_utc(now),
                document_id=document_id,
                asset_id=asset_id,
            ),
        ),
    )
    return row


def record_extraction_failure(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    *,
    error: str,
    asset_id: str | None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> FileExtraction:
    """Mark a tick as failed.

    Re-arms the row to ``pending`` (the worker picks it up on the
    next tick) when ``attempts < MAX_EXTRACTION_ATTEMPTS``; advances
    to the ``failed`` terminal once the cap is hit. Either path emits
    ``asset_document.extraction_failed`` so the SPA can surface the
    error chip without polling — ``terminal=False`` says another
    tick is on the way; ``terminal=True`` says the manager must
    explicitly retry.
    """
    row = _load_extraction_row(session, ctx, document_id)
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    row.last_error = error
    if row.attempts >= MAX_EXTRACTION_ATTEMPTS:
        row.extraction_status = "failed"
    else:
        # Reset to ``pending`` so the next worker tick picks it up; the
        # ``attempts`` counter already moved on this run. Leaving the
        # row in ``extracting`` would require the worker to know about
        # in-flight reuse, which it explicitly does not.
        row.extraction_status = "pending"
    row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="file_extraction",
        entity_id=document_id,
        action="file_extraction.failed",
        diff={
            "after": {
                "status": row.extraction_status,
                "attempts": row.attempts,
                "last_error": error,
            }
        },
        clock=resolved_clock,
    )
    _queue_event(
        session,
        _PendingExtractionEvent(
            bus=resolved_bus,
            event=AssetDocumentExtractionFailed(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=_as_utc(now),
                document_id=document_id,
                asset_id=asset_id,
                attempts=row.attempts,
                terminal=row.extraction_status == "failed",
            ),
        ),
    )
    return row


def record_extraction_unsupported(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    *,
    asset_id: str | None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> FileExtraction:
    """Terminal-flip ``unsupported`` (MIME outside the rung table).

    No ``last_error`` text and ``attempts`` is not incremented further
    on entry — this is a content-shape signal, not a runtime failure.
    """
    row = _load_extraction_row(session, ctx, document_id)
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    row.extraction_status = "unsupported"
    row.last_error = None
    row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="file_extraction",
        entity_id=document_id,
        action="file_extraction.unsupported",
        diff={"after": {"status": "unsupported"}},
        clock=resolved_clock,
    )
    _queue_event(
        session,
        _PendingExtractionEvent(
            bus=resolved_bus,
            event=AssetDocumentExtractionFailed(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=_as_utc(now),
                document_id=document_id,
                asset_id=asset_id,
                attempts=row.attempts,
                terminal=True,
            ),
        ),
    )
    return row


def record_extraction_empty(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    *,
    extractor: str,
    asset_id: str | None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> FileExtraction:
    """Terminal-flip ``empty`` (rung returned no readable text)."""
    row = _load_extraction_row(session, ctx, document_id)
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    row.extraction_status = "empty"
    row.extractor = extractor
    row.body_text = ""
    row.pages_json = []
    row.token_count = 0
    row.has_secret_marker = False
    row.last_error = None
    row.extracted_at = now
    row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="file_extraction",
        entity_id=document_id,
        action="file_extraction.empty",
        diff={"after": {"status": "empty", "extractor": extractor}},
        clock=resolved_clock,
    )
    _queue_event(
        session,
        _PendingExtractionEvent(
            bus=resolved_bus,
            event=AssetDocumentExtracted(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=_as_utc(now),
                document_id=document_id,
                asset_id=asset_id,
            ),
        ),
    )
    return row


def retry_extraction(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> FileExtraction:
    """Re-arm a ``failed`` row back to ``pending``.

    Spec §21: "the manager asserts we should try again from scratch",
    so ``attempts`` resets to zero. ``unsupported`` and ``empty`` are
    not retryable — those terminals describe the document's content,
    not a transient failure. ``pending`` and ``extracting`` are also
    not retryable; the worker will pick them up on its own.
    """
    row = _load_extraction_row(session, ctx, document_id)
    if row.extraction_status != "failed":
        raise ExtractionRetryNotAllowed(
            f"only failed rows can be retried; status={row.extraction_status!r}"
        )
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    row.extraction_status = "pending"
    row.attempts = 0
    row.last_error = None
    row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="file_extraction",
        entity_id=document_id,
        action="file_extraction.retried",
        diff={"after": {"status": "pending", "attempts": 0}},
        clock=resolved_clock,
    )
    asset_id = _document_asset_id(session, ctx, document_id)
    _queue_event(
        session,
        _PendingExtractionEvent(
            bus=resolved_bus,
            event=AssetDocumentExtractionRetried(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=_as_utc(now),
                document_id=document_id,
                asset_id=asset_id,
            ),
        ),
    )
    return row


def get_extraction(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> DocumentExtractionView:
    """Read the persisted ``file_extraction`` row by document id."""
    row = _load_extraction_row(session, ctx, document_id)
    return _row_to_view(row)


def get_extraction_page(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    page: int,
) -> DocumentExtractionPageView:
    """Return one bounded page window from the persisted ``body_text``.

    Pages are 1-indexed (matches the API surface); a page outside the
    range returns a zero-length window with ``more_pages=False``. The
    domain layer does not raise — the route layer cannot enforce a
    bound without first reading the row, and a 404 is reserved for
    missing documents.
    """
    if page < 1:
        raise ValueError("page must be >= 1")
    row = _load_extraction_row(session, ctx, document_id)
    pages = row.pages_json or []
    if page > len(pages):
        return DocumentExtractionPageView(
            page=page,
            char_start=0,
            char_end=0,
            body="",
            more_pages=False,
        )
    entry = pages[page - 1]
    char_start = int(entry.get("char_start", 0))
    char_end = int(entry.get("char_end", 0))
    body = (row.body_text or "")[char_start:char_end]
    return DocumentExtractionPageView(
        page=page,
        char_start=char_start,
        char_end=char_end,
        body=body,
        more_pages=page < len(pages),
    )


def list_pending_extractions(
    session: Session,
    *,
    limit: int = 50,
) -> list[FileExtraction]:
    """Return up to ``limit`` rows in ``status='pending'`` across the deployment.

    Cross-tenant by design — the worker tick is deployment-scope (see
    :mod:`app.worker.tasks.extract_document`). Each row carries its
    own ``workspace_id``; the worker rebuilds a per-row system-actor
    :class:`WorkspaceContext` before mutating.
    """
    stmt = (
        select(FileExtraction)
        .where(FileExtraction.extraction_status == "pending")
        .order_by(FileExtraction.created_at.asc(), FileExtraction.id.asc())
        .limit(limit)
    )
    with tenant_agnostic():
        rows: Iterable[FileExtraction] = session.scalars(stmt).all()
    return list(rows)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_extraction_row(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> FileExtraction:
    with tenant_agnostic():
        row = session.scalars(
            select(FileExtraction).where(
                FileExtraction.workspace_id == ctx.workspace_id,
                FileExtraction.id == document_id,
            )
        ).one_or_none()
    if row is None:
        raise AssetDocumentNotFound(document_id)
    return row


def _document_asset_id(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> str | None:
    with tenant_agnostic():
        row = session.scalars(
            select(AssetDocument).where(
                AssetDocument.workspace_id == ctx.workspace_id,
                AssetDocument.id == document_id,
            )
        ).one_or_none()
    return row.asset_id if row is not None else None


def _row_to_view(row: FileExtraction) -> DocumentExtractionView:
    body_text = row.body_text or ""
    return DocumentExtractionView(
        document_id=row.id,
        workspace_id=row.workspace_id,
        status=row.extraction_status,
        extractor=row.extractor,
        body_preview=body_text[:_BODY_PREVIEW_CHARS],
        page_count=len(row.pages_json or []),
        token_count=row.token_count or 0,
        has_secret_marker=row.has_secret_marker,
        last_error=row.last_error,
        extracted_at=(
            _as_utc(row.extracted_at) if row.extracted_at is not None else None
        ),
        attempts=row.attempts,
    )
