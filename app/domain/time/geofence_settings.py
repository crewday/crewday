"""Per-property geofence setting service.

The clock-in path reads :class:`~app.adapters.db.time.models.GeofenceSetting`
rows, but managers also need a narrow API for configuring those rows.
This module owns that write surface so HTTP handlers stay thin and the
same capability / audit behaviour is available to future CLI callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import PropertyWorkspace
from app.adapters.db.time.models import GeofenceSetting
from app.audit import write_audit
from app.authz import InvalidScope, PermissionDenied, UnknownActionKey, require
from app.tenancy import WorkspaceContext
from app.util.clock import Clock
from app.util.ulid import new_ulid

__all__ = [
    "GeofenceMode",
    "GeofenceSettingNotFound",
    "GeofenceSettingPermissionDenied",
    "GeofenceSettingUpsert",
    "GeofenceSettingView",
    "delete_geofence_setting",
    "get_geofence_setting",
    "upsert_geofence_setting",
]


GeofenceMode = Literal["enforce", "warn", "off"]


class GeofenceSettingNotFound(LookupError):
    """The requested property has no geofence setting in this workspace."""


class GeofenceSettingPermissionDenied(PermissionError):
    """The caller lacks manager-level authority over time settings."""


class GeofenceSettingUpsert(BaseModel):
    """Request DTO for creating or replacing one property geofence."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: int = Field(gt=0)
    enabled: bool = True
    mode: GeofenceMode = "enforce"


@dataclass(frozen=True, slots=True)
class GeofenceSettingView:
    """Read projection of a per-property geofence setting."""

    id: str
    workspace_id: str
    property_id: str
    lat: float
    lon: float
    radius_m: int
    enabled: bool
    mode: GeofenceMode


def _narrow_mode(value: str) -> GeofenceMode:
    if value == "enforce":
        return "enforce"
    if value == "warn":
        return "warn"
    if value == "off":
        return "off"
    raise ValueError(f"unknown geofence_setting.mode {value!r} on loaded row")


def _row_to_view(row: GeofenceSetting) -> GeofenceSettingView:
    return GeofenceSettingView(
        id=row.id,
        workspace_id=row.workspace_id,
        property_id=row.property_id,
        lat=row.lat,
        lon=row.lon,
        radius_m=row.radius_m,
        enabled=row.enabled,
        mode=_narrow_mode(row.mode),
    )


def _view_to_diff(view: GeofenceSettingView) -> dict[str, str | int | float | bool]:
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "property_id": view.property_id,
        "lat": view.lat,
        "lon": view.lon,
        "radius_m": view.radius_m,
        "enabled": view.enabled,
        "mode": view.mode,
    }


def _require_manage(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> None:
    try:
        require(
            session,
            ctx,
            action_key="time.edit_others",
            scope_kind="property",
            scope_id=property_id,
        )
    except PermissionDenied as exc:
        raise GeofenceSettingPermissionDenied(str(exc)) from exc
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError("authz catalog misconfigured for time.edit_others") from exc


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> GeofenceSetting:
    stmt = select(GeofenceSetting).where(
        GeofenceSetting.workspace_id == ctx.workspace_id,
        GeofenceSetting.property_id == property_id,
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise GeofenceSettingNotFound(property_id)
    return row


def _require_property_link(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> None:
    linked = session.scalars(
        select(PropertyWorkspace.property_id).where(
            PropertyWorkspace.workspace_id == ctx.workspace_id,
            PropertyWorkspace.property_id == property_id,
            PropertyWorkspace.status == "active",
        )
    ).one_or_none()
    if linked is None:
        raise GeofenceSettingNotFound(property_id)


def get_geofence_setting(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> GeofenceSettingView:
    """Return one property's geofence setting or raise."""

    _require_manage(session, ctx, property_id=property_id)
    _require_property_link(session, ctx, property_id=property_id)
    return _row_to_view(_load_row(session, ctx, property_id=property_id))


def upsert_geofence_setting(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    body: GeofenceSettingUpsert,
    clock: Clock | None = None,
) -> GeofenceSettingView:
    """Create or replace the geofence setting for ``property_id``."""

    _require_manage(session, ctx, property_id=property_id)
    _require_property_link(session, ctx, property_id=property_id)
    try:
        row = _load_row(session, ctx, property_id=property_id)
        before = _row_to_view(row)
        action = "geofence_setting.updated"
    except GeofenceSettingNotFound:
        row = GeofenceSetting(
            id=new_ulid(),
            workspace_id=ctx.workspace_id,
            property_id=property_id,
            lat=body.lat,
            lon=body.lon,
            radius_m=body.radius_m,
            enabled=body.enabled,
            mode=body.mode,
        )
        session.add(row)
        before = None
        action = "geofence_setting.created"

    row.lat = body.lat
    row.lon = body.lon
    row.radius_m = body.radius_m
    row.enabled = body.enabled
    row.mode = body.mode
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="geofence_setting",
        entity_id=property_id,
        action=action,
        diff={
            "before": _view_to_diff(before) if before is not None else None,
            "after": _view_to_diff(after),
        },
        clock=clock,
    )
    return after


def delete_geofence_setting(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    clock: Clock | None = None,
) -> GeofenceSettingView:
    """Delete one property's geofence setting and return the removed row."""

    _require_manage(session, ctx, property_id=property_id)
    _require_property_link(session, ctx, property_id=property_id)
    row = _load_row(session, ctx, property_id=property_id)
    before = _row_to_view(row)
    session.delete(row)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="geofence_setting",
        entity_id=property_id,
        action="geofence_setting.deleted",
        diff={"before": _view_to_diff(before), "after": None},
        clock=clock,
    )
    return before
