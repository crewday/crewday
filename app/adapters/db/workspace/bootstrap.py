"""Workspace catalogue bootstrap helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import WorkRole
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid

__all__ = [
    "STARTER_WORK_ROLE_KEYS",
    "seed_starter_work_roles",
]


@dataclass(frozen=True, slots=True)
class StarterWorkRole:
    key: str
    name: str
    description_md: str
    default_settings_json: dict[str, object]
    icon_name: str


STARTER_WORK_ROLES: tuple[StarterWorkRole, ...] = (
    StarterWorkRole(
        # code-health: ignore[duplicate] Seed catalog rows stay explicit data.
        key="maid",  # code-health: ignore[duplicate] Seed catalog rows stay explicit data.  # noqa: E501
        name="Maid",  # code-health: ignore[duplicate] Seed catalog rows stay explicit data.  # noqa: E501
        description_md="Cleaning, housekeeping, and turnover preparation.",
        default_settings_json={},
        icon_name="BrushCleaning",
    ),
    StarterWorkRole(
        key="cook",
        name="Cook",
        description_md="Meal preparation and kitchen support.",
        default_settings_json={},
        icon_name="ChefHat",
    ),
    StarterWorkRole(
        key="driver",
        name="Driver",
        description_md="Guest, household, and errand transportation.",
        default_settings_json={},
        icon_name="Car",
    ),
    StarterWorkRole(
        key="gardener",
        name="Gardener",
        description_md="Garden, grounds, and planting care.",
        default_settings_json={},
        icon_name="Sprout",
    ),
    StarterWorkRole(
        key="handyman",
        name="Handyman",
        description_md="Repairs, maintenance, and general fixes.",
        default_settings_json={},
        icon_name="Wrench",
    ),
    StarterWorkRole(
        key="nanny",
        name="Nanny",
        description_md="Childcare and family support.",
        default_settings_json={},
        icon_name="Baby",
    ),
    StarterWorkRole(
        key="pool_tech",
        name="Pool technician",
        description_md="Pool cleaning, testing, and equipment care.",
        default_settings_json={},
        icon_name="WavesLadder",
    ),
    StarterWorkRole(
        key="concierge",
        name="Concierge",
        description_md="Guest services, arrivals, and local coordination.",
        default_settings_json={},
        icon_name="Bell",
    ),
    StarterWorkRole(
        key="personal_assistant",
        name="Personal assistant",
        description_md="Errands, scheduling, and household administration.",
        default_settings_json={},
        icon_name="BriefcaseBusiness",
    ),
    StarterWorkRole(
        key="property_manager",
        name="Property manager",
        description_md="Property operations and staff coordination.",
        default_settings_json={},
        icon_name="Building2",
    ),
)

STARTER_WORK_ROLE_KEYS: tuple[str, ...] = tuple(role.key for role in STARTER_WORK_ROLES)


def seed_starter_work_roles(
    session: Session, *, workspace_id: str, now: datetime
) -> list[WorkRole]:
    """Insert missing starter ``work_role`` rows for ``workspace_id``.

    Existing rows are left untouched, including tombstoned rows. That keeps
    the starter catalog idempotent without undoing an operator's later edits
    or deletions.
    """
    with tenant_agnostic():
        existing_keys = set(
            session.scalars(
                select(WorkRole.key).where(
                    WorkRole.workspace_id == workspace_id,
                    WorkRole.key.in_(STARTER_WORK_ROLE_KEYS),
                )
            ).all()
        )
        rows: list[WorkRole] = []
        for starter in STARTER_WORK_ROLES:
            if starter.key in existing_keys:
                continue
            row = WorkRole(
                id=new_ulid(),
                workspace_id=workspace_id,
                key=starter.key,
                name=starter.name,
                description_md=starter.description_md,
                default_settings_json=dict(starter.default_settings_json),
                icon_name=starter.icon_name,
                created_at=now,
            )
            session.add(row)
            rows.append(row)
        session.flush()
    return rows
