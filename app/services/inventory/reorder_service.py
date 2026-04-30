"""Inventory reorder-point service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.inventory.models import Item
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
from app.adapters.db.workspace.models import WorkRole, Workspace
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import InventoryLowStock
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "OPEN_TASK_STATES",
    "InventoryReorderReport",
    "check_reorder_points",
]


OPEN_TASK_STATES: frozenset[str] = frozenset(
    {"scheduled", "pending", "in_progress", "overdue"}
)
_RESTOCK_ROLE_SETTING_KEY = "inventory.restock_assignee_role"
_DEFAULT_RESTOCK_ROLE_KEY = "property_manager"


@dataclass(frozen=True, slots=True)
class InventoryReorderReport:
    checked_items: int = 0
    tasks_created: int = 0
    events_emitted: int = 0
    skipped_above_threshold: int = 0
    skipped_existing_open_task: int = 0
    skipped_missing_property: int = 0


def check_reorder_points(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_ids: tuple[str, ...] | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> InventoryReorderReport:
    """Ensure one open restock task exists for each low-stock item."""
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    stmt = (
        select(Item)
        .where(
            Item.workspace_id == ctx.workspace_id,
            Item.deleted_at.is_(None),
            Item.reorder_point.is_not(None),
        )
        .order_by(Item.id)
        .with_for_update()
    )
    if item_ids is not None:
        if not item_ids:
            return InventoryReorderReport()
        stmt = stmt.where(Item.id.in_(item_ids))

    checked = 0
    created = 0
    emitted = 0
    skipped_above = 0
    skipped_existing = 0
    skipped_missing_property = 0

    for item in session.scalars(stmt).all():
        checked += 1
        if item.property_id is None:
            skipped_missing_property += 1
            continue
        reorder_point = _clean_decimal(item.reorder_point)
        on_hand = _clean_decimal(item.on_hand)
        if on_hand > reorder_point:
            skipped_above += 1
            continue
        existing_task_id = _open_restock_task_id(session, ctx, item_id=item.id)
        if existing_task_id is not None:
            skipped_existing += 1
            continue

        task = _create_restock_task(
            session,
            ctx,
            item=item,
            now=now,
            clock=resolved_clock,
        )
        write_audit(
            session,
            ctx,
            entity_kind="inventory_item",
            entity_id=item.id,
            action="inventory.auto_restock_created",
            diff={
                "item_id": item.id,
                "property_id": item.property_id,
                "restock_task_id": task.id,
                "on_hand": str(on_hand),
                "reorder_point": str(reorder_point),
            },
            clock=resolved_clock,
        )
        resolved_bus.publish(
            InventoryLowStock(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=resolved_clock.now(),
                property_id=item.property_id,
                item_id=item.id,
                on_hand=on_hand,
                reorder_point=reorder_point,
                restock_task_id=task.id,
            )
        )
        created += 1
        emitted += 1

    return InventoryReorderReport(
        checked_items=checked,
        tasks_created=created,
        events_emitted=emitted,
        skipped_above_threshold=skipped_above,
        skipped_existing_open_task=skipped_existing,
        skipped_missing_property=skipped_missing_property,
    )


def _create_restock_task(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item: Item,
    now: datetime,
    clock: Clock,
) -> Occurrence:
    template = _find_restock_template(session, ctx, property_id=item.property_id)
    role_id = _resolve_restock_role_id(session, ctx, property_id=item.property_id)
    title = _restock_title(item, template)
    description = (
        template.description_md
        if template is not None and template.description_md
        else _default_description(item)
    )
    duration = _duration_minutes(template)
    starts_at = now
    ends_at = starts_at + timedelta(minutes=duration)
    scheduled_for_local = _local_iso(
        session,
        property_id=item.property_id,
        at=starts_at,
    )
    task = Occurrence(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        schedule_id=None,
        template_id=template.id if template is not None else None,
        property_id=item.property_id,
        assignee_user_id=None,
        starts_at=starts_at,
        ends_at=ends_at,
        scheduled_for_local=scheduled_for_local,
        originally_scheduled_for=scheduled_for_local,
        state="pending",
        overdue_since=None,
        completed_at=None,
        completed_by_user_id=None,
        reviewer_user_id=None,
        reviewed_at=None,
        cancellation_reason=None,
        title=title,
        description_md=description,
        priority=_template_priority(template),
        photo_evidence=_template_photo_evidence(template),
        duration_minutes=duration,
        area_id=None,
        unit_id=None,
        expected_role_id=role_id,
        linked_instruction_ids=list(template.linked_instruction_ids)
        if template is not None
        else [],
        inventory_consumption_json={},
        is_personal=False,
        created_by_user_id=None,
        created_at=now,
    )
    session.add(task)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="task",
        entity_id=task.id,
        action="task.create_oneoff",
        diff={
            "after": {
                "id": task.id,
                "title": task.title,
                "property_id": task.property_id,
                "expected_role_id": task.expected_role_id,
                "inventory_restock_item_id": item.id,
            }
        },
        clock=clock,
    )
    return task


def _open_restock_task_id(
    session: Session, ctx: WorkspaceContext, *, item_id: str
) -> str | None:
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.workspace_id == ctx.workspace_id,
            AuditLog.action == "inventory.auto_restock_created",
            AuditLog.entity_kind == "inventory_item",
            AuditLog.entity_id == item_id,
        )
        .order_by(AuditLog.created_at.desc())
    ).all()
    for row in rows:
        diff = row.diff if isinstance(row.diff, dict) else {}
        task_id = diff.get("restock_task_id")
        if not isinstance(task_id, str):
            continue
        state = session.scalar(
            select(Occurrence.state).where(
                Occurrence.workspace_id == ctx.workspace_id,
                Occurrence.id == task_id,
            )
        )
        if state in OPEN_TASK_STATES:
            return task_id
    return None


def _find_restock_template(
    session: Session, ctx: WorkspaceContext, *, property_id: str | None
) -> TaskTemplate | None:
    candidates = session.scalars(
        select(TaskTemplate)
        .where(
            TaskTemplate.workspace_id == ctx.workspace_id,
            TaskTemplate.deleted_at.is_(None),
        )
        .order_by(TaskTemplate.created_at, TaskTemplate.id)
    ).all()
    for template in candidates:
        if not _is_restock_template(template):
            continue
        if _template_applies_to_property(template, property_id):
            return template
    return None


def _is_restock_template(template: TaskTemplate) -> bool:
    name = (template.name or template.title or "").strip().lower()
    return name in {"restock", "restock {item}"}


def _template_applies_to_property(
    template: TaskTemplate, property_id: str | None
) -> bool:
    if template.property_scope == "any":
        return True
    ids = template.listed_property_ids or []
    if property_id is None:
        return False
    if template.property_scope == "one":
        return ids == [property_id]
    if template.property_scope == "listed":
        return property_id in ids
    return False


def _resolve_restock_role_id(
    session: Session, ctx: WorkspaceContext, *, property_id: str | None
) -> str | None:
    key = _DEFAULT_RESTOCK_ROLE_KEY
    with tenant_agnostic():
        settings = session.scalar(
            select(Workspace.settings_json).where(Workspace.id == ctx.workspace_id)
        )
    if isinstance(settings, dict):
        candidate = settings.get(_RESTOCK_ROLE_SETTING_KEY)
        if isinstance(candidate, str) and candidate.strip():
            key = candidate.strip()

    role = session.scalar(
        select(WorkRole)
        .where(
            WorkRole.workspace_id == ctx.workspace_id,
            WorkRole.key == key,
            WorkRole.deleted_at.is_(None),
        )
        .limit(1)
    )
    if role is not None:
        return role.id

    fallback = session.scalar(
        select(WorkRole.id)
        .where(
            WorkRole.workspace_id == ctx.workspace_id,
            WorkRole.key == _DEFAULT_RESTOCK_ROLE_KEY,
            WorkRole.deleted_at.is_(None),
        )
        .limit(1)
    )
    if fallback is not None:
        return fallback
    _ = property_id
    return None


def _local_iso(session: Session, *, property_id: str | None, at: datetime) -> str:
    zone = ZoneInfo("UTC")
    if property_id is not None:
        with tenant_agnostic():
            timezone = session.scalar(
                select(Property.timezone).where(Property.id == property_id)
            )
        if isinstance(timezone, str):
            try:
                zone = ZoneInfo(timezone)
            except ZoneInfoNotFoundError:
                zone = ZoneInfo("UTC")
    return at.astimezone(zone).replace(tzinfo=None, microsecond=0).isoformat()


def _restock_title(item: Item, template: TaskTemplate | None) -> str:
    if template is not None:
        raw = (template.name or template.title or "").strip()
        if raw and raw.lower() != "restock":
            return raw.replace("{item}", item.name)
    return f"Restock {item.name}"


def _default_description(item: Item) -> str:
    target = (
        f" Reorder target: {item.reorder_target} {item.unit}."
        if item.reorder_target is not None
        else ""
    )
    vendor = f" Vendor: {item.vendor}." if item.vendor else ""
    return (
        "Automatically created because stock is at or below the reorder point."
        f"{target}{vendor}"
    )


def _duration_minutes(template: TaskTemplate | None) -> int:
    if template is None:
        return 30
    if template.duration_minutes is not None and template.duration_minutes > 0:
        return template.duration_minutes
    if template.default_duration_min > 0:
        return template.default_duration_min
    return 30


def _template_priority(template: TaskTemplate | None) -> str:
    if template is None:
        return "normal"
    return str(template.priority or "normal")


def _template_photo_evidence(template: TaskTemplate | None) -> str:
    if template is None:
        return "disabled"
    return str(template.photo_evidence or "disabled")


def _clean_decimal(value: Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return value
