"""Generic §02 settings cascade resolver."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property, Unit
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
from app.adapters.db.workspace.models import WorkEngagement, Workspace
from app.tenancy import tenant_agnostic

SettingLayer = Literal["workspace", "property", "unit", "work_engagement", "task"]


@dataclass(frozen=True, slots=True)
class SettingScopeValue:
    """One concrete value found at a cascade layer."""

    layer: SettingLayer
    entity_id: str
    value: object


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """Effective setting value plus §02 provenance."""

    value: object
    source_layer: SettingLayer
    source_entity_id: str


@dataclass(frozen=True, slots=True)
class SettingScopeChain:
    """Entity ids needed to resolve a setting through the §02 cascade."""

    workspace_id: str
    property_id: str | None = None
    unit_id: str | None = None
    actor_user_id: str | None = None
    task_id: str | None = None
    template_id: str | None = None


@dataclass(frozen=True, slots=True)
class _LayerSettings:
    entity_id: str
    settings: Mapping[str, object]


CATALOG_DEFAULTS: Mapping[str, object] = {
    "evidence.policy": "optional",
    "tasks.checklist_required": False,
    "tasks.allow_skip_with_reason": True,
    "inventory.apply_on_task": True,
}


def task_scope_chain(
    task: Occurrence, *, workspace_id: str, actor_user_id: str | None
) -> SettingScopeChain:
    """Build the normal task completion cascade scope for an occurrence."""

    return SettingScopeChain(
        workspace_id=workspace_id,
        property_id=task.property_id,
        unit_id=task.unit_id,
        actor_user_id=actor_user_id,
        task_id=task.id,
        template_id=task.template_id,
    )


def concrete_values(
    session: Session,
    key: str,
    chain: SettingScopeChain,
    *,
    default: object | None = None,
) -> tuple[SettingScopeValue, ...]:
    """Return concrete values broadest → most-specific for ``key``.

    Absent keys, explicit ``None``, and the string ``"inherit"`` are
    inheritance markers. Workspace is always concrete: when the row has
    no value, the catalog/default argument supplies the root value.
    """

    fallback = CATALOG_DEFAULTS.get(key) if default is None else default
    values: list[SettingScopeValue] = []

    workspace_settings = _workspace_settings(session, chain.workspace_id)
    workspace_value = _value_from_map(workspace_settings, key)
    values.append(
        SettingScopeValue(
            "workspace",
            chain.workspace_id,
            fallback if workspace_value is _INHERIT else workspace_value,
        )
    )

    property_settings = _property_settings(session, chain.property_id)
    if property_settings is not None:
        _append_if_concrete(values, "property", property_settings, key)

    unit_settings = _unit_settings(session, chain)
    if unit_settings is not None:
        _append_if_concrete(values, "unit", unit_settings, key)

    engagement_settings = _work_engagement_settings(
        session,
        workspace_id=chain.workspace_id,
        actor_user_id=chain.actor_user_id,
    )
    if engagement_settings is not None:
        _append_if_concrete(values, "work_engagement", engagement_settings, key)

    task_settings = _task_settings(session, chain)
    if task_settings is not None:
        _append_if_concrete(values, "task", task_settings, key)

    return tuple(values)


def resolve_setting(
    session: Session,
    key: str,
    chain: SettingScopeChain,
    *,
    default: object | None = None,
) -> ResolvedSetting:
    """Resolve ``key`` with normal most-specific-wins semantics and provenance."""

    value = concrete_values(session, key, chain, default=default)[-1]
    return ResolvedSetting(
        value=value.value,
        source_layer=value.layer,
        source_entity_id=value.entity_id,
    )


def resolve_most_specific(
    session: Session,
    key: str,
    chain: SettingScopeChain,
    *,
    default: object | None = None,
) -> object:
    """Resolve ``key`` with normal most-specific-wins semantics."""

    return resolve_setting(session, key, chain, default=default).value


def resolve_evidence_policy(
    session: Session, chain: SettingScopeChain
) -> Literal["forbid", "require", "optional"]:
    """Resolve ``evidence.policy`` with the domain-specific forbid rule."""

    values = concrete_values(session, "evidence.policy", chain, default="optional")
    if any(value.value == "forbid" for value in values):
        return "forbid"
    raw = values[-1].value
    if raw == "require":
        return "require"
    return "optional"


_INHERIT = object()


def _append_if_concrete(
    values: list[SettingScopeValue],
    layer: SettingLayer,
    layer_settings: _LayerSettings,
    key: str,
) -> None:
    value = _value_from_map(layer_settings.settings, key)
    if value is not _INHERIT:
        values.append(SettingScopeValue(layer, layer_settings.entity_id, value))


def _value_from_map(settings: Mapping[str, object], key: str) -> object:
    if key not in settings:
        return _INHERIT
    value = settings[key]
    if value is None or value == "inherit":
        return _INHERIT
    return value


def _workspace_settings(session: Session, workspace_id: str) -> Mapping[str, object]:
    with tenant_agnostic():
        row = session.scalar(
            select(Workspace.settings_json).where(Workspace.id == workspace_id)
        )
    return row if isinstance(row, dict) else {}


def _property_settings(
    session: Session, property_id: str | None
) -> _LayerSettings | None:
    if property_id is None:
        return None
    with tenant_agnostic():
        row = session.scalar(
            select(Property.settings_override_json).where(Property.id == property_id)
        )
    settings = row if isinstance(row, dict) else {}
    return _LayerSettings(property_id, settings)


def _unit_settings(session: Session, chain: SettingScopeChain) -> _LayerSettings | None:
    if chain.unit_id is None:
        return None
    query = select(Unit.settings_override_json).where(Unit.id == chain.unit_id)
    if chain.property_id is not None:
        query = query.where(Unit.property_id == chain.property_id)
    with tenant_agnostic():
        row = session.scalar(query)
    if row is None:
        return None
    settings = row if isinstance(row, dict) else {}
    return _LayerSettings(chain.unit_id, settings)


def _work_engagement_settings(
    session: Session, *, workspace_id: str, actor_user_id: str | None
) -> _LayerSettings | None:
    if actor_user_id is None:
        return None
    with tenant_agnostic():
        row = session.execute(
            select(WorkEngagement.id, WorkEngagement.settings_override_json)
            .where(
                WorkEngagement.workspace_id == workspace_id,
                WorkEngagement.user_id == actor_user_id,
                WorkEngagement.archived_on.is_(None),
            )
            .limit(1)
        ).one_or_none()
    if row is None:
        return None
    settings = row[1] if isinstance(row[1], dict) else {}
    return _LayerSettings(row[0], settings)


def _task_settings(session: Session, chain: SettingScopeChain) -> _LayerSettings | None:
    settings: dict[str, object] = {}
    entity_id = chain.task_id if chain.task_id is not None else chain.template_id
    if chain.template_id is not None:
        with tenant_agnostic():
            template_settings = session.scalar(
                select(TaskTemplate.settings_override_json).where(
                    TaskTemplate.workspace_id == chain.workspace_id,
                    TaskTemplate.id == chain.template_id,
                )
            )
        if isinstance(template_settings, dict):
            settings.update(template_settings)
        with tenant_agnostic():
            template_photo = session.scalar(
                select(TaskTemplate.photo_evidence).where(
                    TaskTemplate.workspace_id == chain.workspace_id,
                    TaskTemplate.id == chain.template_id,
                )
            )
        legacy_policy = _legacy_photo_policy(template_photo)
        if legacy_policy is not None and "evidence.policy" not in settings:
            settings["evidence.policy"] = legacy_policy

    if chain.task_id is not None:
        with tenant_agnostic():
            task_settings = session.scalar(
                select(Occurrence.settings_override_json).where(
                    Occurrence.workspace_id == chain.workspace_id,
                    Occurrence.id == chain.task_id,
                )
            )
        if isinstance(task_settings, dict):
            settings.update(task_settings)
        with tenant_agnostic():
            task_photo = session.scalar(
                select(Occurrence.photo_evidence).where(
                    Occurrence.workspace_id == chain.workspace_id,
                    Occurrence.id == chain.task_id,
                )
            )
        legacy_policy = _legacy_photo_policy(task_photo)
        if legacy_policy is not None and "evidence.policy" not in settings:
            settings["evidence.policy"] = legacy_policy
    if entity_id is None:
        return None
    return _LayerSettings(entity_id, settings)


def _legacy_photo_policy(raw: object) -> str | None:
    if raw == "disabled":
        return "forbid"
    if raw == "required":
        return "require"
    if raw == "optional":
        return "optional"
    return None
