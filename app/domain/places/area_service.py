"""``area`` CRUD service scoped through the parent property.

Areas are property subdivisions (kitchen, pool, bedroom, trash
station) used by task and instruction targeting. The table itself is
not workspace-scoped; every read and write reaches the tenancy
boundary by joining ``area`` -> ``property`` -> ``property_workspace``.

The service exposes §04 names (``name`` and ``order_hint``) while
keeping the legacy ``label`` / ``ordering`` columns in sync for
existing adapters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Area, Property, PropertyWorkspace, Unit
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AreaCreate",
    "AreaKind",
    "AreaNestingTooDeep",
    "AreaNotFound",
    "AreaReorderItem",
    "AreaUpdate",
    "AreaView",
    "create_area",
    "delete_area",
    "get_area",
    "list_areas",
    "move_area",
    "reorder_areas",
    "seed_default_areas_for_unit",
    "update_area",
]


AreaKind = Literal["indoor_room", "outdoor", "service"]

_MAX_NAME_LEN = 200
_MAX_NOTES_LEN = 20_000
_MAX_ID_LEN = 64

_STR_DEFAULT_AREAS: tuple[tuple[str, AreaKind], ...] = (
    ("Entry", "indoor_room"),
    ("Kitchen", "indoor_room"),
    ("Living", "indoor_room"),
    ("Bedroom 1", "indoor_room"),
    ("Bathroom 1", "indoor_room"),
    ("Outdoor", "outdoor"),
    ("Trash & Laundry", "service"),
)


class AreaNotFound(LookupError):
    """The requested area or parent property is not visible to the caller."""


class AreaNestingTooDeep(ValueError):
    """A create, update, or move would make an area tree deeper than one level."""


class _AreaBody(BaseModel):
    """Shared mutable body for create and full-update DTOs."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    kind: AreaKind = "indoor_room"
    unit_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    order_hint: int = Field(default=0, ge=0)
    parent_area_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    notes_md: str = Field(default="", max_length=_MAX_NOTES_LEN)

    @model_validator(mode="after")
    def _normalise(self) -> _AreaBody:
        if not self.name.strip():
            raise ValueError("name must be a non-blank string")
        return self


class AreaCreate(_AreaBody):
    """Request body for :func:`create_area`."""


class AreaUpdate(_AreaBody):
    """Request body for :func:`update_area`-style full replacement."""


class AreaReorderItem(BaseModel):
    """One area/order pair accepted by :func:`reorder_areas`."""

    model_config = ConfigDict(extra="forbid")

    area_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN)
    order_hint: int = Field(..., ge=0)


@dataclass(frozen=True, slots=True)
class AreaView:
    """Immutable read projection of an ``area`` row."""

    id: str
    property_id: str
    unit_id: str | None
    name: str
    kind: AreaKind
    order_hint: int
    parent_area_id: str | None
    notes_md: str
    created_at: datetime
    updated_at: datetime | None
    deleted_at: datetime | None


def _row_to_view(row: Area) -> AreaView:
    return AreaView(
        id=row.id,
        property_id=row.property_id,
        unit_id=row.unit_id,
        name=row.name if row.name is not None else row.label,
        kind=_narrow_kind(row.kind),
        order_hint=row.ordering,
        parent_area_id=row.parent_area_id,
        notes_md=row.notes_md or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _narrow_kind(value: str) -> AreaKind:
    if value == "indoor_room":
        return "indoor_room"
    if value == "outdoor":
        return "outdoor"
    if value == "service":
        return "service"
    raise ValueError(f"unknown area.kind {value!r} on loaded row")


def _view_to_diff_dict(view: AreaView) -> dict[str, Any]:
    return {
        "id": view.id,
        "property_id": view.property_id,
        "unit_id": view.unit_id,
        "name": view.name,
        "kind": view.kind,
        "order_hint": view.order_hint,
        "parent_area_id": view.parent_area_id,
        "notes_md": view.notes_md,
        "created_at": view.created_at.isoformat(),
        "updated_at": (
            view.updated_at.isoformat() if view.updated_at is not None else None
        ),
        "deleted_at": (
            view.deleted_at.isoformat() if view.deleted_at is not None else None
        ),
    }


def _assert_property_in_workspace(
    session: Session, ctx: WorkspaceContext, *, property_id: str
) -> None:
    stmt = (
        select(Property.id)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            Property.id == property_id,
            Property.deleted_at.is_(None),
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    if session.scalars(stmt).one_or_none() is None:
        raise AreaNotFound(property_id)


def _load_area_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    area_id: str,
    include_deleted: bool = False,
) -> Area:
    stmt = (
        select(Area)
        .join(Property, Property.id == Area.property_id)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            Area.id == area_id,
            Property.deleted_at.is_(None),
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    if not include_deleted:
        stmt = stmt.where(Area.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise AreaNotFound(area_id)
    return row


def _assert_unit_in_property(
    session: Session, *, property_id: str, unit_id: str | None
) -> None:
    if unit_id is None:
        return
    stmt = select(Unit.id).where(
        Unit.id == unit_id,
        Unit.property_id == property_id,
        Unit.deleted_at.is_(None),
    )
    if session.scalars(stmt).one_or_none() is None:
        raise AreaNotFound(unit_id)


def _load_parent_area(
    session: Session, *, property_id: str, parent_area_id: str | None
) -> Area | None:
    if parent_area_id is None:
        return None
    stmt = select(Area).where(
        Area.id == parent_area_id,
        Area.property_id == property_id,
        Area.deleted_at.is_(None),
    )
    parent = session.scalars(stmt).one_or_none()
    if parent is None:
        raise AreaNotFound(parent_area_id)
    return parent


def _has_live_children(session: Session, *, area_id: str) -> bool:
    stmt = select(func.count(Area.id)).where(
        Area.parent_area_id == area_id,
        Area.deleted_at.is_(None),
    )
    return int(session.scalars(stmt).one()) > 0


def _validate_parent(
    session: Session,
    *,
    property_id: str,
    area_id: str | None,
    parent_area_id: str | None,
) -> None:
    if area_id is not None and parent_area_id == area_id:
        raise AreaNestingTooDeep("an area cannot be its own parent")
    parent = _load_parent_area(
        session, property_id=property_id, parent_area_id=parent_area_id
    )
    if parent is None:
        return
    if parent.parent_area_id is not None:
        raise AreaNestingTooDeep("areas cannot be nested more than one level deep")
    if area_id is not None and _has_live_children(session, area_id=area_id):
        raise AreaNestingTooDeep("moving an area with children would exceed depth 1")


def get_area(
    session: Session,
    ctx: WorkspaceContext,
    *,
    area_id: str,
    include_deleted: bool = False,
) -> AreaView:
    row = _load_area_row(session, ctx, area_id=area_id, include_deleted=include_deleted)
    return _row_to_view(row)


def list_areas(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    deleted: bool = False,
) -> Sequence[AreaView]:
    _assert_property_in_workspace(session, ctx, property_id=property_id)
    stmt = select(Area).where(Area.property_id == property_id)
    if deleted:
        stmt = stmt.where(Area.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Area.deleted_at.is_(None))
    stmt = stmt.order_by(Area.ordering.asc(), Area.id.asc())
    return [_row_to_view(row) for row in session.scalars(stmt).all()]


def create_area(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    body: AreaCreate,
    clock: Clock | None = None,
) -> AreaView:
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    _assert_property_in_workspace(session, ctx, property_id=property_id)
    _assert_unit_in_property(session, property_id=property_id, unit_id=body.unit_id)
    _validate_parent(
        session,
        property_id=property_id,
        area_id=None,
        parent_area_id=body.parent_area_id,
    )

    view = _insert_area_row(
        session,
        property_id=property_id,
        unit_id=body.unit_id,
        name=body.name,
        kind=body.kind,
        order_hint=body.order_hint,
        parent_area_id=body.parent_area_id,
        notes_md=body.notes_md,
        now=now,
    )
    write_audit(
        session,
        ctx,
        entity_kind="area",
        entity_id=view.id,
        action="create",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def update_area(
    session: Session,
    ctx: WorkspaceContext,
    *,
    area_id: str,
    body: AreaUpdate,
    clock: Clock | None = None,
) -> AreaView:
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_area_row(session, ctx, area_id=area_id)
    before = _row_to_view(row)

    _assert_unit_in_property(session, property_id=row.property_id, unit_id=body.unit_id)
    _validate_parent(
        session,
        property_id=row.property_id,
        area_id=row.id,
        parent_area_id=body.parent_area_id,
    )

    row.unit_id = body.unit_id
    row.name = body.name
    row.label = body.name
    row.kind = body.kind
    row.ordering = body.order_hint
    row.parent_area_id = body.parent_area_id
    row.notes_md = body.notes_md
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="area",
        entity_id=row.id,
        action="update",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def delete_area(
    session: Session,
    ctx: WorkspaceContext,
    *,
    area_id: str,
    clock: Clock | None = None,
) -> AreaView:
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_area_row(session, ctx, area_id=area_id)
    before = _row_to_view(row)

    children = session.scalars(
        select(Area).where(
            Area.parent_area_id == row.id,
            Area.deleted_at.is_(None),
        )
    ).all()
    for child in children:
        child.deleted_at = now
        child.updated_at = now
    row.deleted_at = now
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="area",
        entity_id=row.id,
        action="delete",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
            "deleted_child_ids": [child.id for child in children],
        },
        clock=resolved_clock,
    )
    return after


def reorder_areas(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    orderings: Sequence[AreaReorderItem],
    clock: Clock | None = None,
) -> Sequence[AreaView]:
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    _assert_property_in_workspace(session, ctx, property_id=property_id)

    ids = [item.area_id for item in orderings]
    if len(set(ids)) != len(ids):
        raise ValueError("reorder_areas received duplicate area ids")

    rows = session.scalars(
        select(Area).where(
            Area.id.in_(ids),
            Area.property_id == property_id,
            Area.deleted_at.is_(None),
        )
    ).all()
    if len(rows) != len(ids):
        missing = sorted(set(ids) - {row.id for row in rows})
        raise AreaNotFound(missing[0] if missing else property_id)

    before = [_view_to_diff_dict(_row_to_view(row)) for row in rows]
    by_id = {row.id: row for row in rows}
    for item in orderings:
        row = by_id[item.area_id]
        row.ordering = item.order_hint
        row.updated_at = now
    session.flush()

    after_rows = session.scalars(
        select(Area)
        .where(
            Area.id.in_(ids),
            Area.property_id == property_id,
            Area.deleted_at.is_(None),
        )
        .order_by(Area.ordering.asc(), Area.id.asc())
    ).all()
    after = [_view_to_diff_dict(_row_to_view(row)) for row in after_rows]
    write_audit(
        session,
        ctx,
        entity_kind="area",
        entity_id=property_id,
        action="reorder",
        diff={
            "summary": f"reordered {len(orderings)} areas",
            "before": before,
            "after": after,
        },
        clock=resolved_clock,
    )
    return [_row_to_view(row) for row in after_rows]


def move_area(
    session: Session,
    ctx: WorkspaceContext,
    *,
    area_id: str,
    parent_area_id: str | None,
    order_hint: int | None = None,
    clock: Clock | None = None,
) -> AreaView:
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_area_row(session, ctx, area_id=area_id)
    before = _row_to_view(row)
    _validate_parent(
        session,
        property_id=row.property_id,
        area_id=row.id,
        parent_area_id=parent_area_id,
    )
    row.parent_area_id = parent_area_id
    if order_hint is not None:
        if order_hint < 0:
            raise ValueError("order_hint must be non-negative")
        row.ordering = order_hint
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="area",
        entity_id=row.id,
        action="move",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def seed_default_areas_for_unit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    unit_id: str,
    now: datetime,
    clock: Clock,
) -> Sequence[AreaView]:
    """Insert the §04 default area set for a newly-created seeded unit."""
    _assert_property_in_workspace(session, ctx, property_id=property_id)
    _assert_unit_in_property(session, property_id=property_id, unit_id=unit_id)
    existing_count = session.scalars(
        select(func.count(Area.id)).where(
            Area.property_id == property_id,
            Area.unit_id == unit_id,
            Area.deleted_at.is_(None),
        )
    ).one()
    if int(existing_count) > 0:
        return []

    views: list[AreaView] = []
    for index, (name, kind) in enumerate(_STR_DEFAULT_AREAS):
        view = _insert_area_row(
            session,
            property_id=property_id,
            unit_id=unit_id,
            name=name,
            kind=kind,
            order_hint=index,
            parent_area_id=None,
            notes_md="",
            now=now,
        )
        views.append(view)
        write_audit(
            session,
            ctx,
            entity_kind="area",
            entity_id=view.id,
            action="create",
            diff={"after": _view_to_diff_dict(view), "seeded": True},
            clock=clock,
        )
    return views


def _insert_area_row(
    session: Session,
    *,
    property_id: str,
    unit_id: str | None,
    name: str,
    kind: AreaKind,
    order_hint: int,
    parent_area_id: str | None,
    notes_md: str,
    now: datetime,
) -> AreaView:
    row = Area(
        id=new_ulid(),
        property_id=property_id,
        unit_id=unit_id,
        name=name,
        label=name,
        kind=kind,
        icon=None,
        ordering=order_hint,
        parent_area_id=parent_area_id,
        notes_md=notes_md,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return _row_to_view(row)
