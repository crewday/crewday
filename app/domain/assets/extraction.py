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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    select,
    update,
)
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

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
    "FileExtractionRecord",
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


@dataclass(frozen=True, slots=True)
class FileExtractionRecord:
    """Domain-owned projection of a ``file_extraction`` row."""

    id: str
    workspace_id: str
    extraction_status: str
    extractor: str | None
    body_text: str | None
    pages_json: list[dict[str, int]] | None
    token_count: int | None
    has_secret_marker: bool
    attempts: int
    last_error: str | None
    extracted_at: datetime | None
    created_at: datetime
    updated_at: datetime


# Body-preview cap shared with §21 "Public surface": ``GET /extraction``
# returns at most this many leading characters. The full ``body_text``
# stays on the row for ``GET /extraction/pages/{n}`` to slice.
_BODY_PREVIEW_CHARS: Final[int] = 4000

_METADATA = MetaData()
_FILE_EXTRACTION = Table(
    "file_extraction",
    _METADATA,
    Column("id", String),
    Column("workspace_id", String),
    Column("extraction_status", String),
    Column("extractor", String),
    Column("body_text", String),
    Column("pages_json", JSON),
    Column("token_count", Integer),
    Column("has_secret_marker", Boolean),
    Column("attempts", Integer),
    Column("last_error", String),
    Column("extracted_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)
_ASSET_DOCUMENT = Table(
    "asset_document",
    _METADATA,
    Column("id", String),
    Column("workspace_id", String),
    Column("asset_id", String),
)


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
) -> FileExtractionRecord:
    """Mint a ``pending`` ``file_extraction`` row for a fresh document.

    Called inline from :func:`app.domain.assets.documents.attach_document`
    so the row exists in the same UoW as the document insert. No SSE
    event fires here — the upload's REST response is the user-visible
    "we got it" signal; subsequent state transitions emit their own
    events as the worker walks the row.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    values = {
        "id": document_id,
        "workspace_id": ctx.workspace_id,
        "extraction_status": "pending",
        "extractor": None,
        "body_text": None,
        "pages_json": None,
        "token_count": None,
        "has_secret_marker": False,
        "attempts": 0,
        "last_error": None,
        "extracted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    session.execute(insert(_FILE_EXTRACTION).values(values))
    session.flush()
    row = _record_from_mapping(values)
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
) -> FileExtractionRecord:
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
    values = {
        "extraction_status": "extracting",
        "attempts": row.attempts + 1,
        "updated_at": resolved_clock.now(),
    }
    _update_extraction_row(session, ctx, document_id, values)
    updated = _load_extraction_row(session, ctx, document_id)
    write_audit(
        session,
        ctx,
        entity_kind="file_extraction",
        entity_id=document_id,
        action="file_extraction.extracting",
        diff={"after": {"status": "extracting", "attempts": updated.attempts}},
        clock=resolved_clock,
    )
    return updated


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
) -> FileExtractionRecord:
    """Persist a successful extraction and fire ``asset_document.extracted``."""
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    _update_extraction_row(
        session,
        ctx,
        document_id,
        {
            "extraction_status": "succeeded",
            "extractor": extractor,
            "body_text": body_text,
            "pages_json": list(pages_json),
            "token_count": token_count,
            "has_secret_marker": has_secret_marker,
            "last_error": None,
            "extracted_at": now,
            "updated_at": now,
        },
    )
    row = _load_extraction_row(session, ctx, document_id)
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
) -> FileExtractionRecord:
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
    # Reset to ``pending`` while under the cap so the next worker tick
    # picks it up; the ``attempts`` counter already moved on this run.
    next_status = "failed" if row.attempts >= MAX_EXTRACTION_ATTEMPTS else "pending"
    _update_extraction_row(
        session,
        ctx,
        document_id,
        {
            "extraction_status": next_status,
            "last_error": error,
            "updated_at": now,
        },
    )
    row = _load_extraction_row(session, ctx, document_id)
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
) -> FileExtractionRecord:
    """Terminal-flip ``unsupported`` (MIME outside the rung table).

    No ``last_error`` text and ``attempts`` is not incremented further
    on entry — this is a content-shape signal, not a runtime failure.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    _update_extraction_row(
        session,
        ctx,
        document_id,
        {
            "extraction_status": "unsupported",
            "last_error": None,
            "updated_at": now,
        },
    )
    row = _load_extraction_row(session, ctx, document_id)
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
) -> FileExtractionRecord:
    """Terminal-flip ``empty`` (rung returned no readable text)."""
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    _update_extraction_row(
        session,
        ctx,
        document_id,
        {
            "extraction_status": "empty",
            "extractor": extractor,
            "body_text": "",
            "pages_json": [],
            "token_count": 0,
            "has_secret_marker": False,
            "last_error": None,
            "extracted_at": now,
            "updated_at": now,
        },
    )
    row = _load_extraction_row(session, ctx, document_id)
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
) -> FileExtractionRecord:
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
    _update_extraction_row(
        session,
        ctx,
        document_id,
        {
            "extraction_status": "pending",
            "attempts": 0,
            "last_error": None,
            "updated_at": now,
        },
    )
    row = _load_extraction_row(session, ctx, document_id)
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
) -> list[FileExtractionRecord]:
    """Return up to ``limit`` rows in ``status='pending'`` across the deployment.

    Cross-tenant by design — the worker tick is deployment-scope (see
    :mod:`app.worker.tasks.extract_document`). Each row carries its
    own ``workspace_id``; the worker rebuilds a per-row system-actor
    :class:`WorkspaceContext` before mutating.
    """
    stmt = (
        select(_FILE_EXTRACTION)
        .where(_FILE_EXTRACTION.c.extraction_status == "pending")
        .order_by(_FILE_EXTRACTION.c.created_at.asc(), _FILE_EXTRACTION.c.id.asc())
        .limit(limit)
    )
    with tenant_agnostic():
        rows = session.execute(stmt).mappings().all()
    return [_record_from_mapping(row) for row in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_extraction_row(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> FileExtractionRecord:
    with tenant_agnostic():
        row = (
            session.execute(
                select(_FILE_EXTRACTION).where(
                    _FILE_EXTRACTION.c.workspace_id == ctx.workspace_id,
                    _FILE_EXTRACTION.c.id == document_id,
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise AssetDocumentNotFound(document_id)
    return _record_from_mapping(row)


def _update_extraction_row(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    values: Mapping[str, object],
) -> None:
    with tenant_agnostic():
        session.execute(
            update(_FILE_EXTRACTION)
            .where(
                _FILE_EXTRACTION.c.workspace_id == ctx.workspace_id,
                _FILE_EXTRACTION.c.id == document_id,
            )
            .values(dict(values))
        )
    session.flush()


def _document_asset_id(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> str | None:
    with tenant_agnostic():
        row = (
            session.execute(
                select(_ASSET_DOCUMENT.c.asset_id).where(
                    _ASSET_DOCUMENT.c.workspace_id == ctx.workspace_id,
                    _ASSET_DOCUMENT.c.id == document_id,
                )
            )
            .mappings()
            .one_or_none()
        )
    return str(row["asset_id"]) if row is not None and row["asset_id"] else None


def _row_to_view(row: FileExtractionRecord) -> DocumentExtractionView:
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


def _record_from_mapping(
    row: Mapping[str, object] | RowMapping,
) -> FileExtractionRecord:
    pages = row["pages_json"]
    if pages is None:
        pages_json = None
    elif isinstance(pages, list):
        pages_json = [_page_from_mapping(page) for page in pages]
    else:
        raise RuntimeError(f"unexpected pages_json type: {type(pages).__name__}")
    return FileExtractionRecord(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        extraction_status=str(row["extraction_status"]),
        extractor=str(row["extractor"]) if row["extractor"] is not None else None,
        body_text=str(row["body_text"]) if row["body_text"] is not None else None,
        pages_json=pages_json,
        token_count=_expect_optional_int(row["token_count"], "token_count"),
        has_secret_marker=bool(row["has_secret_marker"]),
        attempts=_expect_int(row["attempts"], "attempts"),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        extracted_at=_expect_optional_datetime(row["extracted_at"], "extracted_at"),
        created_at=_expect_datetime(row["created_at"], "created_at"),
        updated_at=_expect_datetime(row["updated_at"], "updated_at"),
    )


def _expect_datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    raise RuntimeError(f"file_extraction.{field} is not a datetime")


def _expect_optional_datetime(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _expect_datetime(value, field)


def _page_from_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise RuntimeError("file_extraction.pages_json entry is not an object")
    return {
        str(key): _expect_int(item, f"pages_json.{key}") for key, item in value.items()
    }


def _expect_int(value: object, field: str) -> int:
    if isinstance(value, int):
        return value
    raise RuntimeError(f"file_extraction.{field} is not an int")


def _expect_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _expect_int(value, field)
