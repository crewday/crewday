"""Demo scenario fixture loader.

The demo fixture is bootstrap data, not user-authored production data.
Several target tables already have domain services, but not every seeded
shape has a production create surface yet (notably reservations, task
occurrences, and historical expenses). Keeping the direct ORM inserts in
this module makes the exception explicit and local to demo mode.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import yaml
from pydantic import SecretStr
from sqlalchemy import select

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.demo.models import DemoWorkspace
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Area, Property, PropertyWorkspace, Unit
from app.adapters.db.ports import DbSession
from app.adapters.db.stays.models import Reservation, StayBundle
from app.adapters.db.tasks.models import (
    ChecklistItem,
    ChecklistTemplateItem,
    Occurrence,
    Schedule,
    TaskTemplate,
)
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
    WorkRole,
    Workspace,
)
from app.demo.cookies import (
    DemoCookieBinding,
    binding_digest,
    demo_cookie_name,
    load_demo_cookie,
)
from app.util.ulid import new_ulid

DEFAULT_SCENARIO_KEY: Final[str] = "rental-manager"
SCENARIO_KEYS: Final[tuple[str, ...]] = (
    "villa-owner",
    "rental-manager",
    "housekeeper",
)

_FIXTURE_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "fixtures" / "demo"
_OFFSET_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<sign>[+-])(?P<n>\d+)(?P<u>[dhm])$"
)
_START_RE: Final[re.Pattern[str]] = re.compile(r"^/[-A-Za-z0-9_./]*$")
_WINDOW: Final[timedelta] = timedelta(days=30)
_DEMO_WORKSPACE_TTL: Final[timedelta] = timedelta(days=30)
_ACTIVITY_TOUCH_INTERVAL: Final[timedelta] = timedelta(seconds=5)


@dataclass(frozen=True, slots=True)
class SeededDemoWorkspace:
    """Result returned after a scenario has been seeded."""

    scenario_key: str
    workspace_id: str
    workspace_slug: str
    persona_key: str
    persona_user_id: str
    user_ids: Mapping[str, str]
    property_ids: Mapping[str, str]
    counts: Mapping[str, int]
    default_start: str


def resolve_relative_timestamp(
    value: str,
    now: datetime,
    anchors: Mapping[str, datetime] | None = None,
) -> datetime:
    """Resolve ``T-2d``, ``T+3h``, ISO datetimes, and ``anchor:+7d``."""
    base_now = _to_utc(now)
    text = value.strip()
    if text == "T":
        return base_now
    if text.startswith("T") and len(text) > 1:
        return base_now + _parse_offset(text[1:])
    if ":" in text and not text.startswith("http"):
        anchor_key, offset = text.split(":", 1)
        anchor_map = anchors or {}
        anchor = anchor_map.get(anchor_key)
        if anchor is not None:
            return _to_utc(anchor) + _parse_offset(offset)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"unsupported relative timestamp {value!r}") from exc
    return _to_utc(parsed)


def seed_workspace(
    session: DbSession,
    scenario_key: str,
    *,
    persona_key: str | None = None,
    now: datetime | None = None,
) -> SeededDemoWorkspace:
    """Seed one fresh demo workspace from ``scenario_key``."""
    resolved_now = _to_utc(now or datetime.now(UTC))
    scenario = scenario_key if scenario_key in SCENARIO_KEYS else DEFAULT_SCENARIO_KEY
    fixture = _load_fixture(scenario)
    personas = _mapping(fixture.get("personas"), "personas")
    if persona_key is not None and persona_key in personas:
        selected_persona = persona_key
    else:
        selected_persona = _str(fixture, "default_persona")
    if selected_persona not in personas:
        raise ValueError(f"default persona {selected_persona!r} is not declared")

    workspace_data = _mapping(fixture.get("workspace"), "workspace")
    workspace_id = new_ulid()
    slug = f"demo-{scenario}-{new_ulid().lower()[:10]}"
    workspace = Workspace(
        id=workspace_id,
        slug=slug,
        name=_str(workspace_data, "name"),
        plan="free",
        quota_json={},
        settings_json={},
        default_timezone=_str(workspace_data, "timezone", default="UTC"),
        default_locale=_str(workspace_data, "locale", default="en"),
        default_currency=_str(workspace_data, "currency", default="USD"),
        created_at=resolved_now,
        updated_at=resolved_now,
        owner_onboarded_at=resolved_now,
    )
    session.add(workspace)
    session.flush()

    user_ids = _seed_users(
        session,
        fixture=fixture,
        personas=personas,
        workspace_id=workspace_id,
        workspace_slug=slug,
        now=resolved_now,
    )
    session.flush()
    selected_persona_data = _mapping(
        personas[selected_persona], f"personas.{selected_persona}"
    )
    persona_user_id = user_ids[_str(selected_persona_data, "user")]
    _seed_demo_workspace(
        session,
        workspace_id=workspace_id,
        scenario_key=scenario,
        seed_digest=_seed_digest(fixture),
        persona_user_id=persona_user_id,
        now=resolved_now,
    )

    role_ids = _seed_work_roles(session, fixture, workspace_id, resolved_now)
    session.flush()
    engagement_ids = _seed_worker_links(
        session,
        fixture=fixture,
        workspace_id=workspace_id,
        user_ids=user_ids,
        role_ids=role_ids,
        now=resolved_now,
    )
    session.flush()
    property_ids = _seed_properties(session, fixture, workspace_id, resolved_now)
    session.flush()
    template_ids = _seed_templates(
        session,
        fixture=fixture,
        workspace_id=workspace_id,
        property_ids=property_ids,
        role_ids=role_ids,
        now=resolved_now,
    )
    session.flush()
    schedule_ids = _seed_schedules(
        session,
        fixture=fixture,
        workspace_id=workspace_id,
        property_ids=property_ids,
        template_ids=template_ids,
        user_ids=user_ids,
        now=resolved_now,
    )
    session.flush()
    anchors = _seed_stays(
        session,
        fixture=fixture,
        workspace_id=workspace_id,
        property_ids=property_ids,
        template_ids=template_ids,
        now=resolved_now,
    )
    session.flush()
    _seed_tasks(
        session,
        fixture=fixture,
        workspace_id=workspace_id,
        property_ids=property_ids,
        template_ids=template_ids,
        schedule_ids=schedule_ids,
        user_ids=user_ids,
        anchors=anchors,
        now=resolved_now,
    )
    session.flush()
    _seed_expenses(
        session,
        fixture=fixture,
        workspace_id=workspace_id,
        property_ids=property_ids,
        user_ids=user_ids,
        engagement_ids=engagement_ids,
        now=resolved_now,
    )
    session.flush()
    return SeededDemoWorkspace(
        scenario_key=scenario,
        workspace_id=workspace_id,
        workspace_slug=slug,
        persona_key=selected_persona,
        persona_user_id=persona_user_id,
        user_ids=user_ids,
        property_ids=property_ids,
        counts=_counts(fixture),
        default_start=_str(fixture, "default_start", default="/"),
    )


def normalise_start_path(fixture: Mapping[str, object], candidate: str | None) -> str:
    """Validate a workspace-relative start path from the demo URL."""
    default = _str(fixture, "default_start", default="/")
    if candidate is None or len(candidate) > 256 or candidate.startswith("/w/"):
        return default
    if not _START_RE.match(candidate):
        return default
    allowlist = _string_list(fixture.get("start_paths"), "start_paths")
    if candidate not in allowlist:
        return default
    return candidate


def load_scenario_fixture(scenario_key: str) -> Mapping[str, object]:
    """Load a scenario fixture after applying unknown-key fallback."""
    scenario = scenario_key if scenario_key in SCENARIO_KEYS else DEFAULT_SCENARIO_KEY
    return _load_fixture(scenario)


def load_bound_demo_workspace(
    session: DbSession,
    secret: SecretStr,
    *,
    scenario_key: str,
    value: str | None,
    now: datetime | None = None,
) -> DemoCookieBinding | None:
    """Validate a signed cookie against its live demo workspace row."""
    binding = load_demo_cookie(secret, scenario_key=scenario_key, value=value)
    if binding is None:
        return None

    row = session.get(DemoWorkspace, binding.workspace_id)
    resolved_now = _to_utc(now or datetime.now(UTC))
    if row is None or row.scenario_key != scenario_key:
        return None
    if _to_utc(row.expires_at) < resolved_now:
        return None
    expected_digest = binding_digest(
        scenario_key=binding.scenario_key,
        workspace_id=binding.workspace_id,
        persona_user_id=binding.persona_user_id,
    )
    if row.cookie_binding_digest != expected_digest:
        return None

    _touch_demo_workspace(row, now=resolved_now)
    return binding


def load_bound_demo_workspace_for_slug(
    session: DbSession,
    secret: SecretStr,
    *,
    workspace_slug: str,
    cookies: Mapping[str, str],
    now: datetime | None = None,
) -> DemoCookieBinding | None:
    """Validate the scenario cookie that belongs to ``workspace_slug``."""
    scenario_key = session.scalar(
        select(DemoWorkspace.scenario_key)
        .join(Workspace, Workspace.id == DemoWorkspace.id)
        .where(Workspace.slug == workspace_slug)
    )
    if not isinstance(scenario_key, str):
        return None
    return load_bound_demo_workspace(
        session,
        secret,
        scenario_key=scenario_key,
        value=cookies.get(demo_cookie_name(scenario_key)),
        now=now,
    )


def _seed_users(
    session: DbSession,
    *,
    fixture: Mapping[str, object],
    personas: Mapping[str, object],
    workspace_id: str,
    workspace_slug: str,
    now: datetime,
) -> dict[str, str]:
    owner_group_id = new_ulid()
    session.add(
        PermissionGroup(
            id=owner_group_id,
            workspace_id=workspace_id,
            slug="owners",
            name="Owners",
            system=True,
            capabilities_json={},
            created_at=now,
        )
    )
    session.flush()
    users = _mapping(fixture.get("users"), "users")
    user_ids: dict[str, str] = {}
    for user_key, raw in users.items():
        data = _mapping(raw, f"users.{user_key}")
        user_id = new_ulid()
        user_ids[user_key] = user_id
        email = _demo_email(_str(data, "email"), workspace_slug)
        session.add(
            User(
                id=user_id,
                email=email,
                email_lower=email.lower(),
                display_name=_str(data, "name"),
                locale=_str(data, "locale", default="en"),
                timezone=_str(data, "timezone", default="UTC"),
                avatar_blob_hash=None,
                agent_approval_mode=_str(data, "agent_approval_mode", default="strict"),
                created_at=now,
            )
        )
        session.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=now,
            )
        )
        session.flush()
        grants = _string_list(data.get("grants"), f"users.{user_key}.grants")
        for grant in grants:
            if grant == "owner":
                session.add(
                    PermissionGroupMember(
                        group_id=owner_group_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        added_at=now,
                        added_by_user_id=None,
                    )
                )
                continue
            session.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=user_id,
                    grant_role=grant,
                    scope_kind="workspace",
                    scope_property_id=None,
                    created_at=now,
                    created_by_user_id=None,
                )
            )
    for persona_key, raw in personas.items():
        data = _mapping(raw, f"personas.{persona_key}")
        user_key = _str(data, "user")
        if user_key not in user_ids:
            raise ValueError(f"persona {persona_key!r} references unknown user")
    return user_ids


def _seed_demo_workspace(
    session: DbSession,
    *,
    workspace_id: str,
    scenario_key: str,
    seed_digest: str,
    persona_user_id: str,
    now: datetime,
) -> None:
    session.add(
        DemoWorkspace(
            id=workspace_id,
            scenario_key=scenario_key,
            seed_digest=seed_digest,
            created_at=now,
            last_activity_at=now,
            expires_at=now + _DEMO_WORKSPACE_TTL,
            cookie_binding_digest=binding_digest(
                scenario_key=scenario_key,
                workspace_id=workspace_id,
                persona_user_id=persona_user_id,
            ),
        )
    )


def _seed_work_roles(
    session: DbSession,
    fixture: Mapping[str, object],
    workspace_id: str,
    now: datetime,
) -> dict[str, str]:
    role_ids: dict[str, str] = {}
    for raw in _mapping_list(fixture.get("work_roles"), "work_roles"):
        key = _str(raw, "key")
        role_id = new_ulid()
        role_ids[key] = role_id
        session.add(
            WorkRole(
                id=role_id,
                workspace_id=workspace_id,
                key=key,
                name=_str(raw, "name"),
                description_md=_str(raw, "description", default=""),
                default_settings_json={},
                icon_name=_str(raw, "icon", default=""),
                created_at=now,
            )
        )
    return role_ids


def _seed_worker_links(
    session: DbSession,
    *,
    fixture: Mapping[str, object],
    workspace_id: str,
    user_ids: Mapping[str, str],
    role_ids: Mapping[str, str],
    now: datetime,
) -> dict[str, str]:
    engagement_ids: dict[str, str] = {}
    users = _mapping(fixture.get("users"), "users")
    for user_key, raw in users.items():
        data = _mapping(raw, f"users.{user_key}")
        roles = _string_list(data.get("work_roles"), f"users.{user_key}.work_roles")
        if not roles:
            continue
        user_id = user_ids[user_key]
        engagement_id = new_ulid()
        engagement_ids[user_key] = engagement_id
        session.add(
            WorkEngagement(
                id=engagement_id,
                user_id=user_id,
                workspace_id=workspace_id,
                engagement_kind=_str(data, "engagement_kind", default="payroll"),
                supplier_org_id=None,
                pay_destination_id=None,
                reimbursement_destination_id=None,
                started_on=(now - timedelta(days=120)).date(),
                archived_on=None,
                notes_md="",
                created_at=now,
                updated_at=now,
            )
        )
        for role_key in roles:
            role_id = role_ids[role_key]
            session.add(
                UserWorkRole(
                    id=new_ulid(),
                    user_id=user_id,
                    workspace_id=workspace_id,
                    work_role_id=role_id,
                    started_on=(now - timedelta(days=120)).date(),
                    pay_rule_id=None,
                    created_at=now,
                )
            )
    return engagement_ids


def _seed_properties(
    session: DbSession,
    fixture: Mapping[str, object],
    workspace_id: str,
    now: datetime,
) -> dict[str, str]:
    property_ids: dict[str, str] = {}
    for raw in _mapping_list(fixture.get("properties"), "properties"):
        key = _str(raw, "key")
        property_id = new_ulid()
        property_ids[key] = property_id
        name = _str(raw, "name")
        city = _str(raw, "city")
        country = _str(raw, "country", default="FR")
        session.add(
            Property(
                id=property_id,
                name=name,
                kind=_str(raw, "kind", default="str"),
                address=f"{name}, {city}",
                address_json={"city": city, "country": country},
                country=country,
                locale=_str(raw, "locale", default="fr-FR"),
                default_currency=_str(raw, "currency", default="EUR"),
                timezone=_str(raw, "timezone", default="Europe/Paris"),
                tags_json=[],
                welcome_defaults_json={},
                property_notes_md=_str(raw, "notes", default=""),
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=workspace_id,
                label=name,
                membership_role="owner_workspace",
                share_guest_identity=True,
                status="active",
                created_at=now,
            )
        )
        for ordinal, unit in enumerate(_mapping_list(raw.get("units"), "units")):
            session.add(
                Unit(
                    id=new_ulid(),
                    property_id=property_id,
                    name=_str(unit, "name"),
                    ordinal=ordinal,
                    max_guests=_int(unit, "max_guests", default=2),
                    welcome_overrides_json={},
                    settings_override_json={},
                    notes_md="",
                    label=_str(unit, "name"),
                    type=_str(unit, "type", default="villa"),
                    capacity=_int(unit, "max_guests", default=2),
                    created_at=now,
                    updated_at=now,
                )
            )
        for ordering, label in enumerate(_string_list(raw.get("areas"), "areas")):
            session.add(
                Area(
                    id=new_ulid(),
                    property_id=property_id,
                    label=label,
                    icon=None,
                    ordering=ordering,
                    created_at=now,
                )
            )
    return property_ids


def _seed_templates(
    session: DbSession,
    *,
    fixture: Mapping[str, object],
    workspace_id: str,
    property_ids: Mapping[str, str],
    role_ids: Mapping[str, str],
    now: datetime,
) -> dict[str, str]:
    template_ids: dict[str, str] = {}
    for raw in _mapping_list(fixture.get("task_templates"), "task_templates"):
        key = _str(raw, "key")
        template_id = new_ulid()
        template_ids[key] = template_id
        role_key = _str(raw, "role", default="")
        checklist = _checklist_payload(raw.get("checklist"))
        property_refs = _string_list(
            raw.get("property_refs"), f"task_templates.{key}.property_refs"
        )
        listed_property_ids = [property_ids[item] for item in property_refs]
        property_scope = "listed" if listed_property_ids else "any"
        title = _str(raw, "name")
        session.add(
            TaskTemplate(
                id=template_id,
                workspace_id=workspace_id,
                title=title,
                name=title,
                role_id=role_ids.get(role_key),
                description_md=_str(raw, "description", default=""),
                default_duration_min=_int(raw, "duration_minutes", default=30),
                duration_minutes=_int(raw, "duration_minutes", default=30),
                required_evidence=_required_evidence(raw),
                photo_required=_str(raw, "photo_evidence", default="disabled")
                == "required",
                default_assignee_role="worker",
                property_scope=property_scope,
                listed_property_ids=listed_property_ids,
                area_scope="any",
                listed_area_ids=[],
                checklist_template_json=checklist,
                photo_evidence=_str(raw, "photo_evidence", default="disabled"),
                linked_instruction_ids=[],
                priority=_str(raw, "priority", default="normal"),
                inventory_effects_json=[],
                created_at=now,
            )
        )
        session.flush()
        for position, item in enumerate(checklist):
            label = _str(item, "label")
            session.add(
                ChecklistTemplateItem(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    template_id=template_id,
                    label=label,
                    position=position,
                    requires_photo=False,
                    created_at=now,
                )
            )
    return template_ids


def _seed_schedules(
    session: DbSession,
    *,
    fixture: Mapping[str, object],
    workspace_id: str,
    property_ids: Mapping[str, str],
    template_ids: Mapping[str, str],
    user_ids: Mapping[str, str],
    now: datetime,
) -> dict[str, str]:
    schedule_ids: dict[str, str] = {}
    for raw in _mapping_list(fixture.get("schedules"), "schedules"):
        key = _str(raw, "key")
        schedule_id = new_ulid()
        schedule_ids[key] = schedule_id
        dtstart = resolve_relative_timestamp(_str(raw, "dtstart"), now)
        session.add(
            Schedule(
                id=schedule_id,
                workspace_id=workspace_id,
                template_id=template_ids[_str(raw, "template")],
                property_id=property_ids.get(_str(raw, "property", default="")),
                name=_str(raw, "name"),
                area_id=None,
                rrule_text=_str(raw, "rrule"),
                dtstart=dtstart,
                dtstart_local=dtstart.replace(tzinfo=None).isoformat(
                    timespec="minutes"
                ),
                duration_minutes=_int(raw, "duration_minutes", default=30),
                rdate_local="",
                exdate_local="",
                active_from=(now - timedelta(days=14)).date().isoformat(),
                active_until=None,
                assignee_user_id=user_ids.get(_str(raw, "assignee", default="")),
                backup_assignee_user_ids=[],
                assignee_role="worker",
                enabled=True,
                next_generation_at=now,
                created_at=now,
            )
        )
    return schedule_ids


def _seed_stays(
    session: DbSession,
    *,
    fixture: Mapping[str, object],
    workspace_id: str,
    property_ids: Mapping[str, str],
    template_ids: Mapping[str, str],
    now: datetime,
) -> dict[str, datetime]:
    anchors: dict[str, datetime] = {"T": now}
    for index, raw in enumerate(_mapping_list(fixture.get("stays"), "stays")):
        key = _str(raw, "key")
        check_in = resolve_relative_timestamp(_str(raw, "check_in"), now, anchors)
        check_out = resolve_relative_timestamp(_str(raw, "check_out"), now, anchors)
        anchors[key] = check_in
        if index == 0:
            anchors["stay"] = check_in
        reservation_id = new_ulid()
        session.add(
            Reservation(
                id=reservation_id,
                workspace_id=workspace_id,
                property_id=property_ids[_str(raw, "property")],
                ical_feed_id=None,
                external_uid=f"demo-{workspace_id}-{key}",
                check_in=check_in,
                check_out=check_out,
                guest_name=_str(raw, "guest_name"),
                guest_count=_int(raw, "guest_count", default=2),
                status=_reservation_status(_str(raw, "status", default="scheduled")),
                source="manual",
                raw_summary=_str(raw, "channel", default="manual"),
                raw_description=None,
                created_at=now,
            )
        )
        session.flush()
        bundle_templates = _string_list(raw.get("bundle_templates"), "bundle_templates")
        if bundle_templates:
            session.add(
                StayBundle(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    reservation_id=reservation_id,
                    kind="turnover",
                    tasks_json=[
                        {"template_id": template_ids[item]} for item in bundle_templates
                    ],
                    created_at=now,
                )
            )
    return anchors


def _seed_tasks(
    session: DbSession,
    *,
    fixture: Mapping[str, object],
    workspace_id: str,
    property_ids: Mapping[str, str],
    template_ids: Mapping[str, str],
    schedule_ids: Mapping[str, str],
    user_ids: Mapping[str, str],
    anchors: Mapping[str, datetime],
    now: datetime,
) -> None:
    for raw in _mapping_list(fixture.get("tasks"), "tasks"):
        starts_at = resolve_relative_timestamp(_str(raw, "starts_at"), now, anchors)
        duration = _int(raw, "duration_minutes", default=30)
        state = _task_state(_str(raw, "state", default="pending"))
        occurrence_id = new_ulid()
        assignee = _str(raw, "assignee", default="")
        template_key = _str(raw, "template", default="")
        property_key = _str(raw, "property", default="")
        completed_at = now if state in {"done", "approved"} else None
        session.add(
            Occurrence(
                id=occurrence_id,
                workspace_id=workspace_id,
                schedule_id=schedule_ids.get(_str(raw, "schedule", default="")),
                template_id=template_ids.get(template_key),
                property_id=property_ids.get(property_key),
                assignee_user_id=user_ids.get(assignee),
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=duration),
                scheduled_for_local=starts_at.replace(tzinfo=None).isoformat(
                    timespec="minutes"
                ),
                originally_scheduled_for=starts_at.replace(tzinfo=None).isoformat(
                    timespec="minutes"
                ),
                state=state,
                completed_at=completed_at,
                completed_by_user_id=user_ids.get(assignee) if completed_at else None,
                title=_str(raw, "title"),
                description_md=_str(raw, "description", default=""),
                priority=_str(raw, "priority", default="normal"),
                photo_evidence=_str(raw, "photo_evidence", default="disabled"),
                duration_minutes=duration,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=user_ids.get(_str(raw, "created_by", default="")),
                created_at=_created_at(raw, now),
            )
        )
        session.flush()
        for position, item in enumerate(_checklist_payload(raw.get("checklist"))):
            checked = bool(item.get("done", False))
            session.add(
                ChecklistItem(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    occurrence_id=occurrence_id,
                    label=_str(item, "label"),
                    position=position,
                    requires_photo=False,
                    checked=checked,
                    checked_at=now if checked else None,
                )
            )


def _seed_expenses(
    session: DbSession,
    *,
    fixture: Mapping[str, object],
    workspace_id: str,
    property_ids: Mapping[str, str],
    user_ids: Mapping[str, str],
    engagement_ids: Mapping[str, str],
    now: datetime,
) -> None:
    for raw in _mapping_list(fixture.get("expenses"), "expenses"):
        worker = _str(raw, "worker")
        engagement_id = engagement_ids.get(worker)
        if engagement_id is None:
            continue
        state = _str(raw, "state", default="submitted")
        purchased_at = resolve_relative_timestamp(_str(raw, "purchased_at"), now)
        submitted_at = now if state != "draft" else None
        decided_at = now if state in {"approved", "reimbursed"} else None
        session.add(
            ExpenseClaim(
                id=new_ulid(),
                workspace_id=workspace_id,
                work_engagement_id=engagement_id,
                submitted_at=submitted_at,
                vendor=_str(raw, "vendor"),
                purchased_at=purchased_at,
                currency=_str(raw, "currency", default="EUR"),
                total_amount_cents=_int(raw, "amount_cents"),
                exchange_rate_to_default=Decimal("1.0")
                if state in {"approved", "reimbursed"}
                else None,
                owed_currency=_str(raw, "currency", default="EUR")
                if state in {"approved", "reimbursed"}
                else None,
                owed_amount_cents=_int(raw, "amount_cents")
                if state in {"approved", "reimbursed"}
                else None,
                owed_exchange_rate=Decimal("1.0")
                if state in {"approved", "reimbursed"}
                else None,
                owed_rate_source="manual"
                if state in {"approved", "reimbursed"}
                else None,
                category=_str(raw, "category", default="other"),
                property_id=property_ids.get(_str(raw, "property", default="")),
                note_md=_str(raw, "note", default=""),
                llm_autofill_json=None,
                autofill_confidence_overall=None,
                state=state,
                decided_by=user_ids.get(_str(raw, "decided_by", default="")),
                decided_at=decided_at,
                decision_note_md=None,
                reimbursed_at=now if state == "reimbursed" else None,
                reimbursed_via="bank" if state == "reimbursed" else None,
                reimbursed_by=user_ids.get(_str(raw, "decided_by", default="")),
                created_at=_created_at(raw, now),
            )
        )


def _load_fixture(scenario_key: str) -> Mapping[str, object]:
    path = _FIXTURE_DIR / f"{scenario_key}.yml"
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _mapping(loaded, str(path))


def _touch_demo_workspace(
    row: DemoWorkspace,
    *,
    now: datetime,
) -> None:
    if now - _to_utc(row.last_activity_at) < _ACTIVITY_TOUCH_INTERVAL:
        return
    row.last_activity_at = now
    row.expires_at = now + _DEMO_WORKSPACE_TTL


def _seed_digest(fixture: Mapping[str, object]) -> str:
    body = json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _counts(fixture: Mapping[str, object]) -> dict[str, int]:
    return {
        "users": len(_mapping(fixture.get("users"), "users")),
        "properties": len(_mapping_list(fixture.get("properties"), "properties")),
        "task_templates": len(
            _mapping_list(fixture.get("task_templates"), "task_templates")
        ),
        "schedules": len(_mapping_list(fixture.get("schedules"), "schedules")),
        "stays": len(_mapping_list(fixture.get("stays"), "stays")),
        "tasks": len(_mapping_list(fixture.get("tasks"), "tasks")),
        "expenses": len(_mapping_list(fixture.get("expenses"), "expenses")),
    }


def _created_at(data: Mapping[str, object], now: datetime) -> datetime:
    raw = data.get("created_at")
    resolved = now if raw is None else resolve_relative_timestamp(_expect_str(raw), now)
    if not now - _WINDOW <= resolved <= now + _WINDOW:
        raise ValueError("demo fixture created_at is outside the allowed window")
    return resolved


def _parse_offset(value: str) -> timedelta:
    match = _OFFSET_RE.match(value)
    if match is None:
        raise ValueError(f"unsupported relative offset {value!r}")
    amount = int(match.group("n"))
    if match.group("sign") == "-":
        amount = -amount
    unit = match.group("u")
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _demo_email(email: str, workspace_slug: str) -> str:
    local, marker, domain = email.partition("@")
    if not marker:
        raise ValueError(f"invalid demo email {email!r}")
    return f"{local}+{workspace_slug}@{domain}"


def _reservation_status(value: str) -> str:
    return {
        "scheduled": "scheduled",
        "tentative": "scheduled",
        "in_house": "checked_in",
        "checked_in": "checked_in",
        "checked_out": "completed",
        "completed": "completed",
        "cancelled": "cancelled",
    }.get(value, "scheduled")


def _task_state(value: str) -> str:
    return {"completed": "done"}.get(value, value)


def _required_evidence(data: Mapping[str, object]) -> str:
    return (
        "photo"
        if _str(data, "photo_evidence", default="disabled") == "required"
        else "none"
    )


def _checklist_payload(value: object) -> list[Mapping[str, object]]:
    if value is None:
        return []
    return _mapping_list(value, "checklist")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
        result[key] = item
    return result


def _mapping_list(value: object, label: str) -> list[Mapping[str, object]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError(f"{label} must be a list")
    return [_mapping(item, label) for item in value]


def _string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError(f"{label} must be a list")
    return [_expect_str(item) for item in value]


def _str(
    data: Mapping[str, object],
    key: str,
    *,
    default: str | None = None,
) -> str:
    value = data.get(key)
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"missing required field {key!r}")
    return _expect_str(value)


def _expect_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected string, got {type(value).__name__}")
    return value


def _int(
    data: Mapping[str, object],
    key: str,
    *,
    default: int | None = None,
) -> int:
    value = data.get(key)
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"missing required field {key!r}")
    if not isinstance(value, int):
        raise ValueError(f"{key!r} must be an integer")
    return value


def workspace_exists(session: DbSession, workspace_id: str) -> bool:
    """Return whether a live demo workspace binding still exists."""
    row = session.execute(
        select(DemoWorkspace.id).where(DemoWorkspace.id == workspace_id)
    ).first()
    return row is not None


def demo_workspace_slug(session: DbSession, workspace_id: str) -> str | None:
    """Return the slug for a live demo workspace binding."""
    row = session.execute(
        select(Workspace.slug)
        .join(DemoWorkspace, DemoWorkspace.id == Workspace.id)
        .where(Workspace.id == workspace_id)
    ).first()
    if row is None:
        return None
    slug = row[0]
    return slug if isinstance(slug, str) else None
