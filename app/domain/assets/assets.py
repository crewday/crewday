"""Tracked asset service: CRUD, placement, QR tokens, and audit."""

from __future__ import annotations

import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import Asset, AssetType
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import AssetChanged
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.tokens import short_token
from app.util.ulid import new_ulid

__all__ = [
    "ASSET_CONDITIONS",
    "ASSET_STATUSES",
    "AssetCreate",
    "AssetNotFound",
    "AssetPlacementInvalid",
    "AssetQrTokenExhausted",
    "AssetScanArchived",
    "AssetTypeUnavailable",
    "AssetUpdate",
    "AssetValidationError",
    "AssetView",
    "archive_asset",
    "create_asset",
    "get_asset",
    "get_asset_by_qr_token",
    "list_assets",
    "move_asset",
    "regenerate_qr",
    "restore_asset",
    "update_asset",
]


ASSET_CONDITIONS: tuple[str, ...] = (
    "new",
    "good",
    "fair",
    "poor",
    "needs_replacement",
)
ASSET_STATUSES: tuple[str, ...] = (
    "active",
    "in_repair",
    "decommissioned",
    "disposed",
)
_MAX_NAME_LEN = 200
_MAX_TEXT_LEN = 20_000
_MAX_TOKEN_RETRIES = 20


class AssetNotFound(LookupError):
    """No asset row matched the caller's workspace and filters."""


class AssetScanArchived(LookupError):
    """The QR token belongs to a soft-deleted asset."""


class AssetTypeUnavailable(ValueError):
    """The asset type is not visible to the caller's workspace."""


class AssetPlacementInvalid(ValueError):
    """The property/area placement failed workspace or ownership checks."""


class AssetQrTokenExhausted(RuntimeError):
    """A unique QR token could not be generated after bounded retries."""


class AssetValidationError(ValueError):
    """Submitted asset data failed service-level validation."""

    __slots__ = ("error", "field")

    def __init__(self, field: str, error: str) -> None:
        super().__init__(f"{field}: {error}")
        self.field = field
        self.error = error


class AssetCreate(BaseModel):
    """Input for creating a tracked asset."""

    model_config = ConfigDict(extra="forbid")

    asset_type_id: str | None = None
    property_id: str
    area_id: str | None = None
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    make: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=160)
    serial_number: str | None = Field(default=None, max_length=160)
    condition: Literal["new", "good", "fair", "poor", "needs_replacement"] = "good"
    status: Literal["active", "in_repair", "decommissioned", "disposed"] = "active"
    installed_on: date | None = None
    purchased_on: date | None = None
    purchase_price_cents: int | None = Field(default=None, ge=0)
    purchase_currency: str | None = Field(default=None, min_length=3, max_length=3)
    purchase_vendor: str | None = Field(default=None, max_length=160)
    warranty_expires_on: date | None = None
    expected_lifespan_years: int | None = Field(default=None, ge=1)
    estimated_replacement_on: date | None = None
    cover_photo_file_id: str | None = None
    guest_visible: bool = False
    guest_instructions_md: str | None = Field(default=None, max_length=_MAX_TEXT_LEN)
    notes_md: str | None = Field(default=None, max_length=_MAX_TEXT_LEN)
    settings_override_json: dict[str, object] | None = None

    @model_validator(mode="after")
    def _normalise(self) -> AssetCreate:
        if not self.name.strip():
            raise ValueError("name must be a non-blank string")
        return self


class AssetUpdate(BaseModel):
    """Sparse update input for a tracked asset."""

    model_config = ConfigDict(extra="forbid")

    asset_type_id: str | None = None
    area_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LEN)
    make: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=160)
    serial_number: str | None = Field(default=None, max_length=160)
    condition: Literal["new", "good", "fair", "poor", "needs_replacement"] | None = None
    status: Literal["active", "in_repair", "decommissioned", "disposed"] | None = None
    installed_on: date | None = None
    purchased_on: date | None = None
    purchase_price_cents: int | None = Field(default=None, ge=0)
    purchase_currency: str | None = Field(default=None, min_length=3, max_length=3)
    purchase_vendor: str | None = Field(default=None, max_length=160)
    warranty_expires_on: date | None = None
    expected_lifespan_years: int | None = Field(default=None, ge=1)
    estimated_replacement_on: date | None = None
    cover_photo_file_id: str | None = None
    guest_visible: bool | None = None
    guest_instructions_md: str | None = Field(default=None, max_length=_MAX_TEXT_LEN)
    notes_md: str | None = Field(default=None, max_length=_MAX_TEXT_LEN)
    settings_override_json: dict[str, object] | None = None

    @model_validator(mode="after")
    def _validate_sparse(self) -> AssetUpdate:
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        for required in ("name", "condition", "status"):
            if required in self.model_fields_set and getattr(self, required) is None:
                raise ValueError(f"{required} cannot be cleared")
        if self.name is not None and not self.name.strip():
            raise ValueError("name must be a non-blank string")
        return self


@dataclass(frozen=True, slots=True)
class AssetView:
    id: str
    workspace_id: str
    property_id: str
    area_id: str | None
    asset_type_id: str | None
    name: str
    make: str | None
    model: str | None
    serial_number: str | None
    condition: str
    status: str
    installed_on: date | None
    purchased_on: date | None
    purchase_price_cents: int | None
    purchase_currency: str | None
    purchase_vendor: str | None
    warranty_expires_on: date | None
    expected_lifespan_years: int | None
    estimated_replacement_on: date | None
    cover_photo_file_id: str | None
    qr_token: str
    guest_visible: bool
    guest_instructions_md: str | None
    notes_md: str | None
    settings_override_json: dict[str, object] | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class _PendingAssetEvent:
    bus: EventBus
    event: AssetChanged


_PENDING_EVENTS: weakref.WeakKeyDictionary[Session, list[_PendingAssetEvent]] = (
    weakref.WeakKeyDictionary()
)
_HOOKED_SESSIONS: weakref.WeakSet[Session] = weakref.WeakSet()


def list_assets(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None = None,
    area_id: str | None = None,
    status: str | None = None,
    condition: str | None = None,
    asset_type_id: str | None = None,
    q: str | None = None,
    include_archived: bool = False,
    limit: int = 101,
    after_id: str | None = None,
) -> Sequence[AssetView]:
    """List assets in the caller's workspace."""
    stmt = select(Asset).where(Asset.workspace_id == ctx.workspace_id)
    if property_id is not None:
        stmt = stmt.where(Asset.property_id == property_id)
    if area_id is not None:
        stmt = stmt.where(Asset.area_id == area_id)
    if status is not None:
        stmt = stmt.where(Asset.status == status)
    if condition is not None:
        stmt = stmt.where(Asset.condition == condition)
    if asset_type_id is not None:
        stmt = stmt.where(Asset.asset_type_id == asset_type_id)
    if q is not None and q.strip():
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(Asset.name).like(needle))
    if not include_archived:
        stmt = stmt.where(Asset.deleted_at.is_(None))
    if after_id is not None:
        stmt = stmt.where(Asset.id > after_id)
    stmt = stmt.order_by(Asset.id.asc()).limit(limit)
    with tenant_agnostic():
        rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


def get_asset(
    session: Session,
    ctx: WorkspaceContext,
    *,
    asset_id: str,
    include_archived: bool = False,
) -> AssetView:
    """Return one asset in the caller's workspace."""
    return _row_to_view(_load_asset(session, ctx, asset_id, include_archived))


def get_asset_by_qr_token(
    session: Session,
    ctx: WorkspaceContext,
    *,
    qr_token: str,
) -> AssetView:
    """Return the active asset addressed by ``qr_token``."""
    with tenant_agnostic():
        row = session.scalars(
            select(Asset).where(
                Asset.workspace_id == ctx.workspace_id,
                Asset.qr_token == qr_token,
            )
        ).one_or_none()
    if row is None:
        raise AssetNotFound(qr_token)
    if row.deleted_at is not None:
        raise AssetScanArchived(qr_token)
    return _row_to_view(row)


def create_asset(
    session: Session,
    ctx: WorkspaceContext,
    *,
    asset_type_id: str | None = None,
    property_id: str | None = None,
    area_id: str | None = None,
    label: str | None = None,
    name: str | None = None,
    purchased_at: date | None = None,
    purchased_on: date | None = None,
    warranty_ends_at: date | None = None,
    warranty_expires_on: date | None = None,
    metadata: Mapping[str, object] | None = None,
    settings_override_json: Mapping[str, object] | None = None,
    body: AssetCreate | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    token_factory: Callable[[], str] | None = None,
) -> AssetView:
    """Create a tracked asset and assign a workspace-unique QR token."""
    if body is None:
        resolved_name = name if name is not None else label
        if resolved_name is None:
            raise AssetValidationError("name", "required")
        if property_id is None:
            raise AssetValidationError("property_id", "required")
        body = AssetCreate(
            asset_type_id=asset_type_id,
            property_id=property_id,
            area_id=area_id,
            name=resolved_name,
            purchased_on=purchased_on if purchased_on is not None else purchased_at,
            warranty_expires_on=(
                warranty_expires_on
                if warranty_expires_on is not None
                else warranty_ends_at
            ),
            settings_override_json=dict(
                settings_override_json
                if settings_override_json is not None
                else metadata or {}
            )
            or None,
        )

    _validate_placement(
        session, ctx, property_id=body.property_id, area_id=body.area_id
    )
    if body.asset_type_id is not None:
        _validate_asset_type(session, ctx, body.asset_type_id)

    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    asset_id = new_ulid(clock=clock)
    row = Asset(
        id=asset_id,
        workspace_id=ctx.workspace_id,
        property_id=body.property_id,
        area_id=body.area_id,
        asset_type_id=body.asset_type_id,
        name=body.name.strip(),
        make=_clean_optional_str(body.make),
        model=_clean_optional_str(body.model),
        serial_number=_clean_optional_str(body.serial_number),
        condition=body.condition,
        status=body.status,
        installed_on=body.installed_on,
        purchased_on=body.purchased_on,
        purchase_price_cents=body.purchase_price_cents,
        purchase_currency=(
            body.purchase_currency.upper()
            if body.purchase_currency is not None
            else None
        ),
        purchase_vendor=_clean_optional_str(body.purchase_vendor),
        warranty_expires_on=body.warranty_expires_on,
        expected_lifespan_years=body.expected_lifespan_years,
        estimated_replacement_on=body.estimated_replacement_on,
        cover_photo_file_id=body.cover_photo_file_id,
        qr_token=_unique_qr_token(
            session,
            ctx,
            asset_id=asset_id,
            token_factory=token_factory,
        ),
        guest_visible=body.guest_visible,
        guest_instructions_md=body.guest_instructions_md,
        notes_md=body.notes_md,
        settings_override_json=body.settings_override_json,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="asset",
        entity_id=row.id,
        action="asset.create",
        diff={"after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(ctx, row, resolved_bus, action="create", changed_fields=("id",)),
    )
    return _row_to_view(row)


def update_asset(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
    *,
    body: AssetUpdate | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    **fields: object,
) -> AssetView:
    """Patch mutable asset fields and audit material changes."""
    if body is None:
        body = AssetUpdate.model_validate(fields)
    row = _load_asset(session, ctx, asset_id, include_archived=False)
    if "asset_type_id" in body.model_fields_set and body.asset_type_id is not None:
        _validate_asset_type(session, ctx, body.asset_type_id)
    if "area_id" in body.model_fields_set:
        _validate_placement(
            session,
            ctx,
            property_id=row.property_id,
            area_id=body.area_id,
        )

    before = _audit_dict(row)
    changed: list[str] = []
    for field_name in (
        "asset_type_id",
        "area_id",
        "name",
        "make",
        "model",
        "serial_number",
        "condition",
        "status",
        "installed_on",
        "purchased_on",
        "purchase_price_cents",
        "purchase_currency",
        "purchase_vendor",
        "warranty_expires_on",
        "expected_lifespan_years",
        "estimated_replacement_on",
        "cover_photo_file_id",
        "guest_visible",
        "guest_instructions_md",
        "notes_md",
        "settings_override_json",
    ):
        if field_name not in body.model_fields_set:
            continue
        value = getattr(body, field_name)
        if field_name in {"name", "make", "model", "serial_number", "purchase_vendor"}:
            value = _clean_optional_str(value) if value is not None else value
        if field_name == "purchase_currency" and value is not None:
            value = str(value).upper()
        if getattr(row, field_name) != value:
            setattr(row, field_name, value)
            changed.append(field_name)

    if not changed:
        return _row_to_view(row)

    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    row.updated_at = resolved_clock.now()
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset",
        entity_id=row.id,
        action="asset.update",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx, row, resolved_bus, action="update", changed_fields=tuple(changed)
        ),
    )
    return _row_to_view(row)


def move_asset(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
    *,
    property_id: str,
    area_id: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssetView:
    """Move an asset to a property/area and audit before/after placement."""
    _validate_placement(session, ctx, property_id=property_id, area_id=area_id)
    row = _load_asset(session, ctx, asset_id, include_archived=False)
    before = {"property_id": row.property_id, "area_id": row.area_id}
    after = {"property_id": property_id, "area_id": area_id}
    if before == after:
        return _row_to_view(row)

    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    row.property_id = property_id
    row.area_id = area_id
    row.updated_at = resolved_clock.now()
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset",
        entity_id=row.id,
        action="asset.move",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx,
            row,
            resolved_bus,
            action="move",
            changed_fields=("property_id", "area_id"),
        ),
    )
    return _row_to_view(row)


def regenerate_qr(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    token_factory: Callable[[], str] | None = None,
) -> AssetView:
    """Replace an asset's QR token, invalidating the old scan token."""
    row = _load_asset(session, ctx, asset_id, include_archived=False)
    old_token = row.qr_token
    new_token = _unique_qr_token(
        session,
        ctx,
        asset_id=row.id,
        token_factory=token_factory,
        exclude_asset_id=row.id,
    )
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    row.qr_token = new_token
    row.updated_at = resolved_clock.now()
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset",
        entity_id=row.id,
        action="asset.qr_regenerate",
        diff={"before": {"qr_token": old_token}, "after": {"qr_token": new_token}},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx, row, resolved_bus, action="qr_regenerate", changed_fields=("qr_token",)
        ),
    )
    return _row_to_view(row)


def archive_asset(
    session: Session,
    ctx: WorkspaceContext,
    *,
    asset_id: str,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssetView:
    """Soft-delete an asset."""
    row = _load_asset(session, ctx, asset_id, include_archived=True)
    if row.deleted_at is not None:
        return _row_to_view(row)
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    before = _audit_dict(row)
    now = resolved_clock.now()
    row.deleted_at = now
    row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset",
        entity_id=row.id,
        action="asset.delete",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx, row, resolved_bus, action="delete", changed_fields=("deleted_at",)
        ),
    )
    return _row_to_view(row)


def restore_asset(
    session: Session,
    ctx: WorkspaceContext,
    *,
    asset_id: str,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssetView:
    """Restore a soft-deleted asset."""
    row = _load_asset(session, ctx, asset_id, include_archived=True)
    if row.deleted_at is None:
        return _row_to_view(row)
    _validate_placement(session, ctx, property_id=row.property_id, area_id=row.area_id)
    if row.asset_type_id is not None:
        _validate_asset_type(session, ctx, row.asset_type_id)
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    before = _audit_dict(row)
    row.deleted_at = None
    row.updated_at = resolved_clock.now()
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset",
        entity_id=row.id,
        action="asset.restore",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx, row, resolved_bus, action="restore", changed_fields=("deleted_at",)
        ),
    )
    return _row_to_view(row)


def _load_asset(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
    include_archived: bool,
) -> Asset:
    stmt = select(Asset).where(
        Asset.workspace_id == ctx.workspace_id, Asset.id == asset_id
    )
    if not include_archived:
        stmt = stmt.where(Asset.deleted_at.is_(None))
    with tenant_agnostic():
        row = session.scalars(stmt).one_or_none()
    if row is None:
        raise AssetNotFound(asset_id)
    return row


def _validate_placement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    area_id: str | None,
) -> None:
    with tenant_agnostic():
        property_exists = session.scalar(
            select(Property.id)
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
            .where(
                Property.id == property_id,
                Property.deleted_at.is_(None),
                PropertyWorkspace.workspace_id == ctx.workspace_id,
                PropertyWorkspace.status == "active",
            )
            .limit(1)
        )
    if property_exists is None:
        raise AssetPlacementInvalid("property is not active in this workspace")
    if area_id is None:
        return
    with tenant_agnostic():
        area_exists = session.scalar(
            select(Area.id)
            .where(
                Area.id == area_id,
                Area.property_id == property_id,
                Area.deleted_at.is_(None),
            )
            .limit(1)
        )
    if area_exists is None:
        raise AssetPlacementInvalid("area does not belong to property")


def _validate_asset_type(
    session: Session,
    ctx: WorkspaceContext,
    asset_type_id: str,
) -> None:
    with tenant_agnostic():
        type_exists = session.scalar(
            select(AssetType.id)
            .where(
                AssetType.id == asset_type_id,
                AssetType.deleted_at.is_(None),
                or_(
                    AssetType.workspace_id == ctx.workspace_id,
                    AssetType.workspace_id.is_(None),
                ),
            )
            .limit(1)
        )
    if type_exists is None:
        raise AssetTypeUnavailable(asset_type_id)


def _unique_qr_token(
    session: Session,
    ctx: WorkspaceContext,
    *,
    asset_id: str,
    token_factory: Callable[[], str] | None,
    exclude_asset_id: str | None = None,
) -> str:
    for _ in range(_MAX_TOKEN_RETRIES):
        token = (
            token_factory()
            if token_factory is not None
            else short_token(workspace_id=ctx.workspace_id, asset_id=asset_id)
        )
        _validate_qr_token(token)
        stmt = select(Asset.id).where(
            Asset.workspace_id == ctx.workspace_id,
            Asset.qr_token == token,
        )
        if exclude_asset_id is not None:
            stmt = stmt.where(Asset.id != exclude_asset_id)
        with tenant_agnostic():
            existing = session.scalar(stmt.limit(1))
        if existing is None:
            return token
    raise AssetQrTokenExhausted("could not generate a unique QR token")


def _validate_qr_token(token: str) -> None:
    if len(token) != 12:
        raise AssetValidationError("qr_token", "invalid_length")
    allowed = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    if any(char not in allowed for char in token):
        raise AssetValidationError("qr_token", "invalid_alphabet")


def _queue_asset_changed(session: Session, pending: _PendingAssetEvent) -> None:
    if session not in _HOOKED_SESSIONS:
        from sqlalchemy import event

        event.listen(session, "after_commit", _publish_pending_events)
        event.listen(session, "after_rollback", _clear_pending_events)
        _HOOKED_SESSIONS.add(session)
    _PENDING_EVENTS.setdefault(session, []).append(pending)


def _pending_event(
    ctx: WorkspaceContext,
    row: Asset,
    bus: EventBus,
    *,
    action: str,
    changed_fields: tuple[str, ...],
) -> _PendingAssetEvent:
    return _PendingAssetEvent(
        bus=bus,
        event=AssetChanged(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=row.updated_at,
            asset_id=row.id,
            action=action,
            changed_fields=changed_fields,
        ),
    )


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


def _row_to_view(row: Asset) -> AssetView:
    return AssetView(
        id=row.id,
        workspace_id=row.workspace_id,
        property_id=row.property_id,
        area_id=row.area_id,
        asset_type_id=row.asset_type_id,
        name=row.name,
        make=row.make,
        model=row.model,
        serial_number=row.serial_number,
        condition=row.condition,
        status=row.status,
        installed_on=row.installed_on,
        purchased_on=row.purchased_on,
        purchase_price_cents=row.purchase_price_cents,
        purchase_currency=row.purchase_currency,
        purchase_vendor=row.purchase_vendor,
        warranty_expires_on=row.warranty_expires_on,
        expected_lifespan_years=row.expected_lifespan_years,
        estimated_replacement_on=row.estimated_replacement_on,
        cover_photo_file_id=row.cover_photo_file_id,
        qr_token=row.qr_token,
        guest_visible=row.guest_visible,
        guest_instructions_md=row.guest_instructions_md,
        notes_md=row.notes_md,
        settings_override_json=row.settings_override_json,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _audit_dict(row: Asset) -> dict[str, object | None]:
    return {
        "id": row.id,
        "property_id": row.property_id,
        "area_id": row.area_id,
        "asset_type_id": row.asset_type_id,
        "name": row.name,
        "condition": row.condition,
        "status": row.status,
        "installed_on": row.installed_on.isoformat() if row.installed_on else None,
        "purchased_on": row.purchased_on.isoformat() if row.purchased_on else None,
        "warranty_expires_on": (
            row.warranty_expires_on.isoformat() if row.warranty_expires_on else None
        ),
        "qr_token": row.qr_token,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }


def _clean_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
