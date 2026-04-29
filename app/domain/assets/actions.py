"""Asset action log service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import Asset, AssetAction, AssetType
from app.adapters.db.authz.models import RoleGrant
from app.adapters.storage.ports import Storage
from app.audit import write_audit
from app.domain.assets.assets import (
    _as_utc,
    _load_asset,
    _pending_event,
    _PendingAssetEvent,
    _queue_asset_changed,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import AssetActionPerformed
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ASSET_ACTION_KINDS",
    "AssetActionAccessDenied",
    "AssetActionNotFound",
    "AssetActionValidationError",
    "AssetActionView",
    "AssetNextDueView",
    "delete_action",
    "list_actions",
    "next_due",
    "record_action",
    "update_action",
]


ASSET_ACTION_KINDS: tuple[str, ...] = (
    "service",
    "repair",
    "replace",
    "inspect",
    "read",
)
AssetActionKind = Literal["service", "repair", "replace", "inspect", "read"]


class AssetActionNotFound(LookupError):
    """No asset action matched the caller's workspace."""


class AssetActionValidationError(ValueError):
    """Submitted asset action data failed validation."""

    __slots__ = ("error", "field")

    def __init__(self, field: str, error: str) -> None:
        super().__init__(f"{field}: {error}")
        self.field = field
        self.error = error


class AssetActionAccessDenied(PermissionError):
    """A worker tried to record an action outside their property grant."""


@dataclass(frozen=True, slots=True)
class AssetActionView:
    id: str
    workspace_id: str
    asset_id: str
    key: str | None
    kind: str
    label: str
    description_md: str | None
    interval_days: int | None
    last_performed_at: datetime | None
    performed_at: datetime | None
    performed_by: str | None
    notes_md: str | None
    meter_reading: Decimal | None
    evidence_blob_hash: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class AssetNextDueView:
    key: str | None
    kind: str
    label: str
    due_at: datetime
    interval_days: int
    last_performed_at: datetime | None
    action_id: str | None


def record_action(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
    *,
    kind: str,
    performed_at: datetime | None = None,
    notes_md: str | None = None,
    meter_reading: Decimal | int | str | None = None,
    evidence_blob_hash: str | None = None,
    storage: Storage | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssetActionView:
    """Record one service/repair/replacement/inspection/meter-read event."""
    asset = _load_asset(session, ctx, asset_id, include_archived=False)
    _assert_record_access(session, ctx, property_id=asset.property_id)
    validated_kind = _validate_kind(kind)
    if evidence_blob_hash is not None and storage is not None:
        _assert_blob_exists(storage, evidence_blob_hash)

    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    action_time = performed_at if performed_at is not None else now
    row = AssetAction(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        asset_id=asset.id,
        key=None,
        kind=validated_kind,
        label=_default_label(validated_kind),
        description_md=None,
        task_template_id=None,
        schedule_id=None,
        interval_days=None,
        estimated_duration_minutes=None,
        inventory_effects_json=None,
        last_performed_at=action_time,
        last_performed_task_id=None,
        performed_by=ctx.actor_id,
        notes_md=_clean_text(notes_md),
        meter_reading=_coerce_meter_reading(meter_reading),
        evidence_blob_hash=evidence_blob_hash,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset_action",
        entity_id=row.id,
        action="asset_action.performed",
        diff={"after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_action_events(session, ctx, asset, row, resolved_bus)
    return _row_to_view(row)


def list_actions(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
    *,
    kind: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[AssetActionView]:
    """List active actions for one asset, newest first."""
    _load_asset(session, ctx, asset_id, include_archived=False)
    stmt = select(AssetAction).where(
        AssetAction.workspace_id == ctx.workspace_id,
        AssetAction.asset_id == asset_id,
        AssetAction.deleted_at.is_(None),
    )
    if kind is not None:
        stmt = stmt.where(AssetAction.kind == _validate_kind(kind))
    if since is not None:
        stmt = stmt.where(AssetAction.last_performed_at >= since)
    if until is not None:
        stmt = stmt.where(AssetAction.last_performed_at <= until)
    stmt = stmt.order_by(AssetAction.last_performed_at.desc(), AssetAction.id.desc())
    with tenant_agnostic():
        rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


def update_action(
    session: Session,
    ctx: WorkspaceContext,
    action_id: str,
    *,
    label: str | None = None,
    notes_md: str | None = None,
    meter_reading: Decimal | int | str | None = None,
    performed_at: datetime | None = None,
    evidence_blob_hash: str | None = None,
    storage: Storage | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssetActionView:
    """Patch mutable fields on a recorded asset action."""
    row = _load_action(session, ctx, action_id)
    asset = _load_asset(session, ctx, row.asset_id, include_archived=False)
    if evidence_blob_hash is not None and storage is not None:
        _assert_blob_exists(storage, evidence_blob_hash)

    before = _audit_dict(row)
    changed: list[str] = []
    if label is not None and label.strip() != row.label:
        row.label = label.strip()
        changed.append("label")
    if notes_md is not None and _clean_text(notes_md) != row.notes_md:
        row.notes_md = _clean_text(notes_md)
        changed.append("notes_md")
    if meter_reading is not None:
        parsed = _coerce_meter_reading(meter_reading)
        if parsed != row.meter_reading:
            row.meter_reading = parsed
            changed.append("meter_reading")
    if performed_at is not None and performed_at != row.last_performed_at:
        row.last_performed_at = performed_at
        changed.append("last_performed_at")
    if evidence_blob_hash is not None and evidence_blob_hash != row.evidence_blob_hash:
        row.evidence_blob_hash = evidence_blob_hash
        changed.append("evidence_blob_hash")
    if not changed:
        return _row_to_view(row)

    resolved_clock = clock if clock is not None else SystemClock()
    row.updated_at = resolved_clock.now()
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset_action",
        entity_id=row.id,
        action="asset_action.update",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx,
            asset,
            event_bus if event_bus is not None else default_event_bus,
            action="action_update",
            changed_fields=("asset_actions",),
        ),
    )
    return _row_to_view(row)


def delete_action(
    session: Session,
    ctx: WorkspaceContext,
    action_id: str,
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssetActionView:
    """Soft-delete a recorded asset action."""
    row = _load_action(session, ctx, action_id)
    asset = _load_asset(session, ctx, row.asset_id, include_archived=False)
    resolved_clock = clock if clock is not None else SystemClock()
    before = _audit_dict(row)
    row.deleted_at = resolved_clock.now()
    row.updated_at = row.deleted_at
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset_action",
        entity_id=row.id,
        action="asset_action.delete",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    _queue_asset_changed(
        session,
        _pending_event(
            ctx,
            asset,
            event_bus if event_bus is not None else default_event_bus,
            action="action_delete",
            changed_fields=("asset_actions",),
        ),
    )
    return _row_to_view(row)


def next_due(
    session: Session,
    ctx: WorkspaceContext,
    asset_id: str,
) -> AssetNextDueView | None:
    """Return the soonest due scheduled action for an asset."""
    asset = _load_asset(session, ctx, asset_id, include_archived=False)
    with tenant_agnostic():
        actions = list(
            session.scalars(
                select(AssetAction).where(
                    AssetAction.workspace_id == ctx.workspace_id,
                    AssetAction.asset_id == asset.id,
                    AssetAction.deleted_at.is_(None),
                )
            ).all()
        )
        asset_type = (
            session.get(AssetType, asset.asset_type_id)
            if asset.asset_type_id is not None
            else None
        )

    candidates: list[AssetNextDueView] = []
    for action in actions:
        if action.interval_days is None:
            continue
        performed_at = (
            _as_utc(action.last_performed_at)
            if action.last_performed_at is not None
            else None
        )
        base = performed_at or _asset_start(asset)
        candidates.append(
            AssetNextDueView(
                key=action.key,
                kind=action.kind,
                label=action.label,
                due_at=base + timedelta(days=action.interval_days),
                interval_days=action.interval_days,
                last_performed_at=performed_at,
                action_id=action.id,
            )
        )

    if asset_type is not None and asset_type.default_actions_json:
        candidates.extend(_default_action_candidates(asset, actions, asset_type))

    if not candidates:
        return None
    return min(candidates, key=lambda candidate: candidate.due_at)


def _default_action_candidates(
    asset: Asset,
    actions: list[AssetAction],
    asset_type: AssetType,
) -> list[AssetNextDueView]:
    candidates: list[AssetNextDueView] = []
    for item in asset_type.default_actions_json:
        interval_days = _positive_int(item.get("interval_days"))
        kind = item.get("kind")
        if interval_days is None or not isinstance(kind, str):
            continue
        try:
            validated_kind = _validate_kind(kind)
        except AssetActionValidationError:
            continue
        key = item.get("key")
        key_str = key if isinstance(key, str) and key else None
        label = item.get("label")
        label_str = (
            label.strip()
            if isinstance(label, str) and label.strip()
            else _default_label(validated_kind)
        )
        last = _latest_matching_action(actions, key=key_str, kind=validated_kind)
        base = (
            _as_utc(last.last_performed_at)
            if last is not None and last.last_performed_at is not None
            else _asset_start(asset)
        )
        last_performed_at = (
            _as_utc(last.last_performed_at)
            if last is not None and last.last_performed_at is not None
            else None
        )
        candidates.append(
            AssetNextDueView(
                key=key_str,
                kind=validated_kind,
                label=label_str,
                due_at=base + timedelta(days=interval_days),
                interval_days=interval_days,
                last_performed_at=last_performed_at,
                action_id=last.id if last is not None else None,
            )
        )
    return candidates


def _latest_matching_action(
    actions: list[AssetAction],
    *,
    key: str | None,
    kind: str,
) -> AssetAction | None:
    matches = [
        action
        for action in actions
        if action.last_performed_at is not None
        and ((key is not None and action.key == key) or action.kind == kind)
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda action: (
            _as_utc(action.last_performed_at)
            if action.last_performed_at is not None
            else datetime.min.replace(tzinfo=UTC)
        ),
    )


def _load_action(
    session: Session, ctx: WorkspaceContext, action_id: str
) -> AssetAction:
    with tenant_agnostic():
        row = session.scalars(
            select(AssetAction).where(
                AssetAction.workspace_id == ctx.workspace_id,
                AssetAction.id == action_id,
                AssetAction.deleted_at.is_(None),
            )
        ).one_or_none()
    if row is None:
        raise AssetActionNotFound(action_id)
    return row


def _assert_record_access(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> None:
    if ctx.actor_grant_role == "manager":
        return
    if ctx.actor_grant_role != "worker":
        raise AssetActionAccessDenied(property_id)
    with tenant_agnostic():
        grant = session.scalar(
            select(RoleGrant.id)
            .where(
                RoleGrant.workspace_id == ctx.workspace_id,
                RoleGrant.user_id == ctx.actor_id,
                RoleGrant.grant_role == "worker",
                RoleGrant.scope_kind == "workspace",
                or_(
                    RoleGrant.scope_property_id == property_id,
                    RoleGrant.scope_property_id.is_(None),
                ),
            )
            .limit(1)
        )
    if grant is None:
        raise AssetActionAccessDenied(property_id)


def _assert_blob_exists(storage: Storage, blob_hash: str) -> None:
    if not storage.exists(blob_hash):
        raise AssetActionValidationError("evidence_blob_hash", "not_found")


def _validate_kind(kind: str) -> AssetActionKind:
    if kind not in ASSET_ACTION_KINDS:
        raise AssetActionValidationError("kind", "invalid")
    return cast(AssetActionKind, kind)


def _coerce_meter_reading(value: Decimal | int | str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AssetActionValidationError("meter_reading", "invalid") from exc
    if parsed < 0:
        raise AssetActionValidationError("meter_reading", "negative")
    return parsed


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _default_label(kind: str) -> str:
    return {
        "service": "Service",
        "repair": "Repair",
        "replace": "Replace",
        "inspect": "Inspection",
        "read": "Meter read",
    }[kind]


def _asset_start(asset: Asset) -> datetime:
    if asset.installed_on is not None:
        return datetime.combine(asset.installed_on, time.min, tzinfo=UTC)
    return _as_utc(asset.created_at)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _queue_action_events(
    session: Session,
    ctx: WorkspaceContext,
    asset: Asset,
    row: AssetAction,
    bus: EventBus,
) -> None:
    _queue_asset_changed(
        session,
        _pending_event(
            ctx,
            asset,
            bus,
            action="action_recorded",
            changed_fields=("asset_actions",),
        ),
    )
    _queue_asset_changed(
        session,
        _PendingAssetEvent(
            bus=bus,
            event=AssetActionPerformed(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=_as_utc(row.updated_at),
                asset_id=asset.id,
                action_id=row.id,
                kind=row.kind,
            ),
        ),
    )


def _audit_dict(row: AssetAction) -> dict[str, object | None]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "asset_id": row.asset_id,
        "key": row.key,
        "kind": row.kind,
        "label": row.label,
        "last_performed_at": row.last_performed_at.isoformat()
        if row.last_performed_at is not None
        else None,
        "performed_by": row.performed_by,
        "notes_md": row.notes_md,
        "meter_reading": str(row.meter_reading)
        if row.meter_reading is not None
        else None,
        "evidence_blob_hash": row.evidence_blob_hash,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }


def _row_to_view(row: AssetAction) -> AssetActionView:
    return AssetActionView(
        id=row.id,
        workspace_id=row.workspace_id,
        asset_id=row.asset_id,
        key=row.key,
        kind=row.kind,
        label=row.label,
        description_md=row.description_md,
        interval_days=row.interval_days,
        last_performed_at=(
            _as_utc(row.last_performed_at)
            if row.last_performed_at is not None
            else None
        ),
        performed_at=(
            _as_utc(row.last_performed_at)
            if row.last_performed_at is not None
            else None
        ),
        performed_by=row.performed_by,
        notes_md=row.notes_md,
        meter_reading=row.meter_reading,
        evidence_blob_hash=row.evidence_blob_hash,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        deleted_at=_as_utc(row.deleted_at) if row.deleted_at is not None else None,
    )
