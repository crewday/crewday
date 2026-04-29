"""Workspace bootstrap for the asset-type base catalog."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import AssetType
from app.domain.assets.types import validate_default_actions
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = ["BASE_ASSET_TYPE_CATALOG", "seed_asset_type_catalog"]


@dataclass(frozen=True, slots=True)
class BaseAssetType:
    key: str
    name: str
    category: str
    default_lifespan_years: int | None
    default_actions: tuple[dict[str, object], ...]


def _action(
    label: str,
    interval_days: int,
    *,
    kind: str = "service",
    warn_before_days: int = 14,
) -> dict[str, object]:
    return {
        "kind": kind,
        "label": label,
        "interval_days": interval_days,
        "warn_before_days": min(warn_before_days, interval_days),
    }


BASE_ASSET_TYPE_CATALOG: tuple[BaseAssetType, ...] = (
    BaseAssetType(
        "air_conditioner",
        "Air conditioner",
        "climate",
        12,
        (
            _action("Clean / replace filter", 30, warn_before_days=7),
            _action("Wash coils", 180, warn_before_days=30),
            _action("Check refrigerant", 365, kind="inspect", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "oven_range",
        "Oven / range",
        "appliance",
        15,
        (
            _action("Deep clean", 90, warn_before_days=14),
            _action("Check calibration", 365, kind="inspect", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "refrigerator",
        "Refrigerator",
        "appliance",
        14,
        (
            _action("Clean coils", 180, warn_before_days=30),
            _action("Inspect door seals", 180, kind="inspect", warn_before_days=30),
            _action("Defrost", 90, warn_before_days=14),
        ),
    ),
    BaseAssetType(
        "dishwasher",
        "Dishwasher",
        "appliance",
        10,
        (
            _action("Clean filter", 30, warn_before_days=7),
            _action("Check spray arms", 90, kind="inspect", warn_before_days=14),
        ),
    ),
    BaseAssetType(
        "washing_machine",
        "Washing machine",
        "appliance",
        10,
        (
            _action("Clean drum", 30, warn_before_days=7),
            _action("Clean filter", 60, warn_before_days=14),
            _action("Inspect hoses", 180, kind="inspect", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "dryer",
        "Dryer",
        "appliance",
        12,
        (
            _action("Clean lint trap", 7, warn_before_days=1),
            _action("Clean vent", 90, warn_before_days=14),
        ),
    ),
    BaseAssetType(
        "water_heater",
        "Water heater",
        "plumbing",
        12,
        (
            _action("Inspect anode", 365, kind="inspect", warn_before_days=30),
            _action("Flush tank", 180, warn_before_days=30),
            _action("Test pressure relief", 365, kind="inspect", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "boiler",
        "Boiler",
        "heating",
        15,
        (
            _action("Annual service", 365, warn_before_days=30),
            _action("Check pressure", 90, kind="inspect", warn_before_days=14),
        ),
    ),
    BaseAssetType(
        "pool_pump",
        "Pool pump",
        "pool",
        8,
        (
            _action("Clean basket", 7, warn_before_days=1),
            _action("Inspect seal", 180, kind="inspect", warn_before_days=30),
            _action("Service motor", 365, warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "pool_heater",
        "Pool heater",
        "pool",
        10,
        (
            _action("Descale", 180, warn_before_days=30),
            _action("Annual service", 365, warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "smoke_detector",
        "Smoke detector",
        "safety",
        10,
        (
            _action("Test alarm", 30, kind="inspect", warn_before_days=7),
            _action("Replace battery", 365, kind="replace", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "fire_extinguisher",
        "Fire extinguisher",
        "safety",
        12,
        (
            _action("Visual inspection", 30, kind="inspect", warn_before_days=7),
            _action("Professional service", 365, warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "generator",
        "Generator",
        "outdoor",
        20,
        (
            _action("Change oil", 180, warn_before_days=30),
            _action("Load test", 90, kind="inspect", warn_before_days=14),
            _action("Check fuel system", 365, kind="inspect", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "solar_panel",
        "Solar panel",
        "outdoor",
        25,
        (
            _action("Clean panels", 90, warn_before_days=14),
            _action("Check inverter", 365, kind="inspect", warn_before_days=30),
            _action("Inspect wiring", 365, kind="inspect", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "septic_tank",
        "Septic tank",
        "plumbing",
        30,
        (
            _action("Pump out", 1095, warn_before_days=60),
            _action("Inspection", 365, kind="inspect", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "irrigation",
        "Irrigation system",
        "outdoor",
        15,
        (
            _action("Inspect heads", 90, kind="inspect", warn_before_days=14),
            _action("Winterize", 365, warn_before_days=30),
            _action("Spring startup", 365, warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "alarm_system",
        "Alarm system",
        "security",
        10,
        (
            _action("Test sensors", 90, kind="inspect", warn_before_days=14),
            _action("Replace battery", 365, kind="replace", warn_before_days=30),
            _action("Review codes", 180, kind="inspect", warn_before_days=30),
        ),
    ),
    BaseAssetType(
        "vehicle",
        "Vehicle",
        "vehicle",
        None,
        (
            _action("Change oil", 180, warn_before_days=30),
            _action("Inspect tires", 90, kind="inspect", warn_before_days=14),
            _action("Renew registration", 365, warn_before_days=30),
        ),
    ),
)


def seed_asset_type_catalog(
    session: Session,
    ctx: WorkspaceContext,
    *,
    clock: Clock | None = None,
) -> list[AssetType]:
    """Seed the §21 base asset-type catalog once for ``ctx.workspace_id``.

    Re-running is idempotent: existing keys are left untouched so manager
    customisations are never overwritten.
    """
    now = (clock if clock is not None else SystemClock()).now()
    # justification: workspace creation runs before an ambient context is
    # installed; this query explicitly pins the workspace partition.
    with tenant_agnostic():
        existing = set(
            session.scalars(
                select(AssetType.key).where(AssetType.workspace_id == ctx.workspace_id)
            ).all()
        )
        rows: list[AssetType] = []
        for item in BASE_ASSET_TYPE_CATALOG:
            if item.key in existing:
                continue
            row = AssetType(
                id=new_ulid(clock=clock),
                workspace_id=ctx.workspace_id,
                key=item.key,
                name=item.name,
                category=item.category,
                icon_name=None,
                description_md=None,
                default_lifespan_years=item.default_lifespan_years,
                default_actions_json=validate_default_actions(item.default_actions),
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
            session.add(row)
            rows.append(row)
        if rows:
            session.flush()
    return rows
