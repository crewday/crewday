"""Clock-in geofence evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.time.models import GeofenceSetting
from app.tenancy import WorkspaceContext
from app.util.geo import haversine_distance_m

__all__ = [
    "GeofenceMode",
    "GeofenceRejected",
    "GeofenceStatus",
    "GeofenceVerdict",
    "check_geofence",
]

GeofenceMode = Literal["enforce", "warn", "off"]
GeofenceStatus = Literal["ok", "outside", "disabled", "no_fix"]


@dataclass(frozen=True, slots=True)
class GeofenceVerdict:
    """Result of evaluating a clock-in against a property's geofence."""

    status: GeofenceStatus
    mode: GeofenceMode
    property_id: str | None
    distance_m: float | None
    radius_m: int | None
    gps_accuracy_m: float | None

    def to_audit_diff(self) -> dict[str, object]:
        """Return a JSON-safe audit payload."""
        return {
            "status": self.status,
            "mode": self.mode,
            "property_id": self.property_id,
            "distance_m": self.distance_m,
            "radius_m": self.radius_m,
            "gps_accuracy_m": self.gps_accuracy_m,
        }

    def to_http_detail(self) -> dict[str, object]:
        """Return the public 422 error shape."""
        error = (
            "geofence_fix_required" if self.status == "no_fix" else "geofence_outside"
        )
        detail: dict[str, object] = {
            "error": error,
            "property_id": self.property_id,
        }
        if self.distance_m is not None:
            detail["distance_m"] = self.distance_m
        if self.radius_m is not None:
            detail["radius_m"] = self.radius_m
        if self.gps_accuracy_m is not None:
            detail["gps_accuracy_m"] = self.gps_accuracy_m
        return detail


class GeofenceRejected(ValueError):
    """Clock-in was blocked by an enforcing geofence setting."""

    def __init__(self, verdict: GeofenceVerdict) -> None:
        self.verdict = verdict
        super().__init__(verdict.to_http_detail()["error"])


def _narrow_mode(value: str) -> GeofenceMode:
    if value == "enforce":
        return "enforce"
    if value == "warn":
        return "warn"
    if value == "off":
        return "off"
    raise ValueError(f"unknown geofence_setting.mode {value!r} on loaded row")


def _validate_fix(
    *,
    client_lat: float | None,
    client_lon: float | None,
    gps_accuracy_m: float | None,
) -> None:
    if client_lat is not None and not -90 <= client_lat <= 90:
        raise ValueError("client_lat must be between -90 and 90")
    if client_lon is not None and not -180 <= client_lon <= 180:
        raise ValueError("client_lon must be between -180 and 180")
    if gps_accuracy_m is not None and gps_accuracy_m < 0:
        raise ValueError("gps_accuracy_m must be non-negative")


def check_geofence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None,
    client_lat: float | None,
    client_lon: float | None,
    gps_accuracy_m: float | None,
) -> GeofenceVerdict:
    """Evaluate the configured property geofence for a clock-in attempt."""
    _validate_fix(
        client_lat=client_lat,
        client_lon=client_lon,
        gps_accuracy_m=gps_accuracy_m,
    )
    if property_id is None:
        return GeofenceVerdict(
            status="disabled",
            mode="off",
            property_id=None,
            distance_m=None,
            radius_m=None,
            gps_accuracy_m=gps_accuracy_m,
        )

    stmt = select(GeofenceSetting).where(
        GeofenceSetting.workspace_id == ctx.workspace_id,
        GeofenceSetting.property_id == property_id,
    )
    setting = session.scalars(stmt).one_or_none()
    if setting is None:
        return GeofenceVerdict(
            status="disabled",
            mode="off",
            property_id=property_id,
            distance_m=None,
            radius_m=None,
            gps_accuracy_m=gps_accuracy_m,
        )

    mode = _narrow_mode(setting.mode)
    if not setting.enabled or mode == "off":
        return GeofenceVerdict(
            status="disabled",
            mode="off",
            property_id=property_id,
            distance_m=None,
            radius_m=setting.radius_m,
            gps_accuracy_m=gps_accuracy_m,
        )

    if client_lat is None or client_lon is None:
        return GeofenceVerdict(
            status="no_fix",
            mode=mode,
            property_id=property_id,
            distance_m=None,
            radius_m=setting.radius_m,
            gps_accuracy_m=gps_accuracy_m,
        )

    accuracy_m = gps_accuracy_m if gps_accuracy_m is not None else 0.0
    distance_m = haversine_distance_m(setting.lat, setting.lon, client_lat, client_lon)
    status: GeofenceStatus = (
        "ok" if distance_m <= setting.radius_m + accuracy_m else "outside"
    )
    return GeofenceVerdict(
        status=status,
        mode=mode,
        property_id=property_id,
        distance_m=distance_m,
        radius_m=setting.radius_m,
        gps_accuracy_m=accuracy_m,
    )
