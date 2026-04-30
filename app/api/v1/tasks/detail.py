"""Task-detail projection helpers."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.instructions.models import Instruction, InstructionVersion
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import ChecklistItem
from app.domain.tasks.oneoff import TaskView
from app.tenancy import WorkspaceContext

from .derived import _aware_utc
from .payloads import (
    ResolvedInventoryEffectPayload,
    TaskChecklistItemPayload,
    TaskDetailInstructionPayload,
    TaskDetailPayload,
    TaskDetailPropertyPayload,
    TaskPayload,
)


def _property_timezone(session: Session, property_id: str | None) -> str | None:
    """Return the IANA timezone for ``property_id`` or ``None``.

    ``None`` on unknown id keeps the caller from rendering a stale
    window; a junk zone string is surfaced up so the caller can
    decide (we let :func:`_compute_time_window_local` swallow it,
    so the window collapses to ``None``). One query per task list
    would be noisy; callers fetching a page pre-resolve zones via
    :func:`_resolve_zones_for_views` below.
    """
    if property_id is None:
        return None
    zone = session.scalar(select(Property.timezone).where(Property.id == property_id))
    return zone


def _resolve_zones_for_views(session: Session, views: list[TaskView]) -> dict[str, str]:
    """Fetch the ``property.timezone`` for every property in ``views``.

    One SELECT per page, keyed by ``property_id``, so the
    ``TaskPayload.from_view`` factory can pick the zone out of a dict
    instead of firing a query per task. Rows without a property (personal
    tasks) are filtered at the call site.
    """
    ids = {v.property_id for v in views if v.property_id is not None}
    if not ids:
        return {}
    rows = session.execute(
        select(Property.id, Property.timezone).where(Property.id.in_(list(ids)))
    ).all()
    return {row[0]: row[1] for row in rows}


def _detail_property_payload(
    session: Session, property_id: str | None
) -> TaskDetailPropertyPayload | None:
    """Return the property summary for a visible task, if any."""
    if property_id is None:
        return None
    row = session.get(Property, property_id)
    if row is None or row.deleted_at is not None:
        return None
    address_json = row.address_json or {}
    city_raw = address_json.get("city")
    city = city_raw if isinstance(city_raw, str) else ""
    name = row.name if row.name is not None else row.address
    return TaskDetailPropertyPayload(
        id=row.id,
        name=name,
        city=city,
        timezone=row.timezone,
        color=_detail_property_color(row.id),
        kind=_detail_property_kind(row.kind),
        areas=[],
        evidence_policy="inherit",
        country=row.country or "XX",
        locale=row.locale if row.locale is not None else "en-US",
        settings_override={},
        client_org_id=None,
        owner_user_id=None,
    )


def _detail_property_color(property_id: str) -> Literal["moss", "sky", "rust"]:
    """Stable property-chip color for the worker detail surface."""
    colors: tuple[Literal["moss", "sky", "rust"], ...] = ("moss", "sky", "rust")
    return colors[sum(property_id.encode("utf-8")) % len(colors)]


def _detail_property_kind(
    value: str,
) -> Literal["str", "vacation", "residence", "mixed"]:
    """Narrow the DB property kind enum for the task-detail payload."""
    if value == "str":
        return "str"
    if value == "vacation":
        return "vacation"
    if value == "residence":
        return "residence"
    if value == "mixed":
        return "mixed"
    raise ValueError(f"unknown property.kind {value!r} on loaded row")


def _task_checklist_payload(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
) -> list[TaskChecklistItemPayload]:
    """Return runtime checklist rows for ``task_id`` in display order."""
    rows = session.scalars(
        select(ChecklistItem)
        .where(
            ChecklistItem.workspace_id == ctx.workspace_id,
            ChecklistItem.occurrence_id == task_id,
        )
        .order_by(ChecklistItem.position.asc(), ChecklistItem.id.asc())
    ).all()
    return [TaskChecklistItemPayload.from_row(row) for row in rows]


def _instruction_payloads(
    session: Session,
    ctx: WorkspaceContext,
    *,
    instruction_ids: list[str],
) -> list[TaskDetailInstructionPayload]:
    """Resolve linked instructions to their current-version payloads."""
    if not instruction_ids:
        return []
    rows = session.execute(
        select(Instruction, InstructionVersion)
        .join(
            InstructionVersion,
            InstructionVersion.id == Instruction.current_version_id,
        )
        .where(
            Instruction.workspace_id == ctx.workspace_id,
            Instruction.id.in_(instruction_ids),
        )
    ).all()
    by_id = {instruction.id: (instruction, version) for instruction, version in rows}
    payloads: list[TaskDetailInstructionPayload] = []
    for instruction_id in instruction_ids:
        pair = by_id.get(instruction_id)
        if pair is None:
            continue
        instruction, version = pair
        payloads.append(
            TaskDetailInstructionPayload(
                id=instruction.id,
                title=instruction.title,
                scope=_instruction_scope(instruction.scope_kind),
                property_id=(
                    instruction.scope_id
                    if instruction.scope_kind == "property"
                    else None
                ),
                area=instruction.scope_id if instruction.scope_kind == "area" else None,
                tags=[],
                body_md=version.body_md,
                version=version.version_num,
                updated_at=_aware_utc(version.created_at) or version.created_at,
            )
        )
    return payloads


def _instruction_scope(value: str) -> Literal["global", "property", "area"]:
    """Map persisted instruction scope names to the SPA contract."""
    if value == "workspace":
        return "global"
    if value == "property":
        return "property"
    if value == "area":
        return "area"
    return "global"


def _inventory_effects_for_task(view: TaskView) -> list[ResolvedInventoryEffectPayload]:
    """Project v1 consume-only inventory hints into the detail envelope."""
    return [
        ResolvedInventoryEffectPayload(
            item_ref=item_ref,
            kind="consume",
            qty=qty,
            item_id=None,
            item_name=item_ref,
            unit="each",
            on_hand=None,
        )
        for item_ref, qty in view.inventory_consumption_json.items()
    ]


def _task_detail_payload(
    session: Session,
    ctx: WorkspaceContext,
    view: TaskView,
) -> TaskDetailPayload:
    """Build the worker task-detail envelope from a visible task view."""
    zone = _property_timezone(session, view.property_id)
    return TaskDetailPayload(
        task=TaskPayload.from_view(view, property_timezone=zone),
        property=_detail_property_payload(session, view.property_id),
        instructions=_instruction_payloads(
            session,
            ctx,
            instruction_ids=list(view.linked_instruction_ids),
        ),
        checklist=_task_checklist_payload(session, ctx, task_id=view.id),
        inventory_effects=_inventory_effects_for_task(view),
    )
