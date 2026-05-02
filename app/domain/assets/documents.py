"""Asset document attachment service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import AssetDocument
from app.adapters.storage.ports import Storage
from app.audit import write_audit
from app.domain.assets.assets import (
    _as_utc,
    _load_asset,
    _pending_event,
    _queue_asset_changed,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ASSET_DOCUMENT_CATEGORIES",
    "AssetDocumentNotFound",
    "AssetDocumentValidationError",
    "AssetDocumentView",
    "attach_document",
    "delete_document",
    "list_documents",
    "list_workspace_documents",
]


ASSET_DOCUMENT_CATEGORIES: tuple[str, ...] = (
    "manual",
    "warranty",
    "invoice",
    "receipt",
    "photo",
    "certificate",
    "contract",
    "permit",
    "insurance",
    "other",
)
AssetDocumentCategory = Literal[
    "manual",
    "warranty",
    "invoice",
    "receipt",
    "photo",
    "certificate",
    "contract",
    "permit",
    "insurance",
    "other",
]


class AssetDocumentNotFound(LookupError):
    """No document matched the caller's workspace."""


class AssetDocumentValidationError(ValueError):
    """Submitted asset document data failed validation."""

    __slots__ = ("error", "field")

    def __init__(self, field: str, error: str) -> None:
        super().__init__(f"{field}: {error}")
        self.field = field
        self.error = error


@dataclass(frozen=True, slots=True)
class AssetDocumentView:
    id: str
    workspace_id: str
    file_id: str | None
    blob_hash: str | None
    filename: str | None
    asset_id: str | None
    property_id: str | None
    kind: str
    category: str
    title: str
    notes_md: str | None
    expires_on: date | None
    amount_cents: int | None
    amount_currency: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


def attach_document(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
    *,
    blob_hash: str,
    filename: str,
    category: str,
    title: str | None = None,
    notes_md: str | None = None,
    storage: Storage | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssetDocumentView:
    """Attach an already-stored blob to an asset."""
    asset = _load_asset(session, ctx, asset_id, include_archived=False)
    if storage is not None and not storage.exists(blob_hash):
        raise AssetDocumentValidationError("blob_hash", "not_found")
    validated_category = _validate_category(category)
    cleaned_filename = filename.strip()
    if not cleaned_filename:
        raise AssetDocumentValidationError("filename", "required")
    cleaned_title = title.strip() if title is not None else cleaned_filename
    if not cleaned_title:
        raise AssetDocumentValidationError("title", "required")

    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    row = AssetDocument(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        file_id=None,
        blob_hash=blob_hash,
        filename=cleaned_filename,
        asset_id=asset.id,
        property_id=None,
        kind=validated_category,
        title=cleaned_title,
        notes_md=_clean_text(notes_md),
        expires_on=None,
        amount_cents=None,
        amount_currency=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset_document",
        entity_id=row.id,
        action="asset_document.create",
        diff={"after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx,
            asset,
            resolved_bus,
            action="document_create",
            changed_fields=("asset_documents",),
        ),
    )
    # Mint the paired ``file_extraction`` row in ``pending`` so the
    # cd-mo9e worker tick picks it up. Local import — the extraction
    # service imports from here for ``AssetDocumentNotFound``, so
    # eager-importing it at module top would create a cycle.
    from app.domain.assets.extraction import enqueue_extraction

    enqueue_extraction(session, ctx, row.id, clock=resolved_clock)
    return _row_to_view(row)


def list_documents(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
    *,
    category: str | None = None,
) -> list[AssetDocumentView]:
    """List active documents attached to an asset."""
    _load_asset(session, ctx, asset_id, include_archived=False)
    stmt = select(AssetDocument).where(
        AssetDocument.workspace_id == ctx.workspace_id,
        AssetDocument.asset_id == asset_id,
        AssetDocument.deleted_at.is_(None),
    )
    if category is not None:
        stmt = stmt.where(AssetDocument.kind == _validate_category(category))
    stmt = stmt.order_by(AssetDocument.created_at.desc(), AssetDocument.id.desc())
    with tenant_agnostic():
        rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


def list_workspace_documents(
    session: Session,
    ctx: WorkspaceContext,
    *,
    asset_id: str | None = None,
    property_id: str | None = None,
    category: str | None = None,
    expires_before: date | None = None,
) -> list[AssetDocumentView]:
    """List active documents attached anywhere in the workspace."""
    stmt = select(AssetDocument).where(
        AssetDocument.workspace_id == ctx.workspace_id,
        AssetDocument.deleted_at.is_(None),
    )
    if asset_id is not None:
        stmt = stmt.where(AssetDocument.asset_id == asset_id)
    if property_id is not None:
        stmt = stmt.where(AssetDocument.property_id == property_id)
    if category is not None:
        stmt = stmt.where(AssetDocument.kind == _validate_category(category))
    if expires_before is not None:
        stmt = stmt.where(
            AssetDocument.expires_on.is_not(None),
            AssetDocument.expires_on <= expires_before,
        )
    stmt = stmt.order_by(AssetDocument.created_at.desc(), AssetDocument.id.desc())
    with tenant_agnostic():
        rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


def delete_document(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssetDocumentView:
    """Soft-delete an asset document."""
    row = _load_document(session, ctx, document_id)
    assert row.asset_id is not None
    asset = _load_asset(session, ctx, row.asset_id, include_archived=False)
    resolved_clock = clock if clock is not None else SystemClock()
    before = _audit_dict(row)
    row.deleted_at = resolved_clock.now()
    row.updated_at = row.deleted_at
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset_document",
        entity_id=row.id,
        action="asset_document.delete",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx,
            asset,
            event_bus if event_bus is not None else default_event_bus,
            action="document_delete",
            changed_fields=("asset_documents",),
        ),
    )
    return _row_to_view(row)


def _load_document(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> AssetDocument:
    with tenant_agnostic():
        row = session.scalars(
            select(AssetDocument).where(
                AssetDocument.workspace_id == ctx.workspace_id,
                AssetDocument.id == document_id,
                AssetDocument.asset_id.is_not(None),
                AssetDocument.deleted_at.is_(None),
            )
        ).one_or_none()
    if row is None:
        raise AssetDocumentNotFound(document_id)
    return row


def _validate_category(category: str) -> AssetDocumentCategory:
    if category not in ASSET_DOCUMENT_CATEGORIES:
        raise AssetDocumentValidationError("category", "invalid")
    return cast(AssetDocumentCategory, category)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _audit_dict(row: AssetDocument) -> dict[str, object | None]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "asset_id": row.asset_id,
        "property_id": row.property_id,
        "blob_hash": row.blob_hash,
        "filename": row.filename,
        "kind": row.kind,
        "title": row.title,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }


def _row_to_view(row: AssetDocument) -> AssetDocumentView:
    return AssetDocumentView(
        id=row.id,
        workspace_id=row.workspace_id,
        file_id=row.file_id,
        blob_hash=row.blob_hash,
        filename=row.filename,
        asset_id=row.asset_id,
        property_id=row.property_id,
        kind=row.kind,
        category=row.kind,
        title=row.title,
        notes_md=row.notes_md,
        expires_on=row.expires_on,
        amount_cents=row.amount_cents,
        amount_currency=row.amount_currency,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        deleted_at=_as_utc(row.deleted_at) if row.deleted_at is not None else None,
    )
