"""Asset-type catalog service.

Asset types are workspace-scoped templates for physical assets. The
workspace bootstrap seeds the base catalog into each workspace; managers
can then create, edit, or archive their own rows without touching other
workspaces.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import Asset, AssetType
from app.audit import write_audit
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ASSET_TYPE_CATEGORIES",
    "AssetTypeCreate",
    "AssetTypeInUse",
    "AssetTypeKeyConflict",
    "AssetTypeNotFound",
    "AssetTypeReadOnly",
    "AssetTypeUpdate",
    "AssetTypeView",
    "DefaultAssetAction",
    "create_type",
    "delete_type",
    "get_type",
    "list_types",
    "update_type",
    "validate_default_actions",
]


ASSET_TYPE_CATEGORIES: tuple[str, ...] = (
    "climate",
    "appliance",
    "plumbing",
    "pool",
    "heating",
    "outdoor",
    "safety",
    "security",
    "vehicle",
    "other",
)
ASSET_ACTION_KINDS: tuple[str, ...] = (
    "service",
    "repair",
    "replace",
    "inspect",
    "read",
)

_MAX_KEY_LEN = 80
_MAX_NAME_LEN = 160
_MAX_ICON_LEN = 64
_MAX_DESCRIPTION_LEN = 20_000


class AssetTypeNotFound(LookupError):
    """No visible asset-type row exists for this workspace."""


class AssetTypeReadOnly(PermissionError):
    """The row is system-owned and cannot be mutated by a workspace."""


class AssetTypeKeyConflict(ValueError):
    """The workspace already has an asset type with this key."""


class AssetTypeInUse(ValueError):
    """The row is referenced by at least one asset."""


class DefaultAssetAction(BaseModel):
    """Default maintenance action seeded onto assets of this type."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["service", "repair", "replace", "inspect", "read"]
    label: str = Field(..., min_length=1, max_length=160)
    interval_days: int = Field(..., ge=1)
    warn_before_days: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _warn_inside_interval(self) -> DefaultAssetAction:
        if self.warn_before_days > self.interval_days:
            raise ValueError("warn_before_days must be <= interval_days")
        if not self.label.strip():
            raise ValueError("label must be a non-blank string")
        return self


_DEFAULT_ACTIONS_ADAPTER: TypeAdapter[list[DefaultAssetAction]] = TypeAdapter(
    list[DefaultAssetAction]
)


class AssetTypeCreate(BaseModel):
    """Input for creating a workspace-custom asset type."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=_MAX_KEY_LEN)
    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    category: Literal[
        "climate",
        "appliance",
        "plumbing",
        "pool",
        "heating",
        "outdoor",
        "safety",
        "security",
        "vehicle",
        "other",
    ] = "other"
    icon_name: str | None = Field(default=None, max_length=_MAX_ICON_LEN)
    description_md: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LEN)
    default_lifespan_years: int | None = Field(default=None, ge=1)
    default_actions: list[DefaultAssetAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalise(self) -> AssetTypeCreate:
        if not self.key.strip():
            raise ValueError("key must be a non-blank string")
        if not self.name.strip():
            raise ValueError("name must be a non-blank string")
        if self.icon_name is not None and not self.icon_name.strip():
            raise ValueError("icon_name must be non-blank when provided")
        return self


class AssetTypeUpdate(BaseModel):
    """Sparse update input for a workspace-custom asset type."""

    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, min_length=1, max_length=_MAX_KEY_LEN)
    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LEN)
    category: (
        Literal[
            "climate",
            "appliance",
            "plumbing",
            "pool",
            "heating",
            "outdoor",
            "safety",
            "security",
            "vehicle",
            "other",
        ]
        | None
    ) = None
    icon_name: str | None = Field(default=None, max_length=_MAX_ICON_LEN)
    description_md: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LEN)
    default_lifespan_years: int | None = Field(default=None, ge=1)
    default_actions: list[DefaultAssetAction] | None = None

    @model_validator(mode="after")
    def _validate_sparse(self) -> AssetTypeUpdate:
        for required in ("key", "name", "category"):
            if required in self.model_fields_set and getattr(self, required) is None:
                raise ValueError(f"{required} cannot be cleared")
        if "default_actions" in self.model_fields_set and self.default_actions is None:
            raise ValueError("default_actions cannot be cleared; send [] to reset")
        if self.key is not None and not self.key.strip():
            raise ValueError("key must be a non-blank string")
        if self.name is not None and not self.name.strip():
            raise ValueError("name must be a non-blank string")
        if self.icon_name is not None and not self.icon_name.strip():
            raise ValueError("icon_name must be non-blank when provided")
        return self


@dataclass(frozen=True, slots=True)
class AssetTypeView:
    id: str
    workspace_id: str | None
    key: str
    name: str
    category: str
    icon_name: str | None
    description_md: str | None
    default_lifespan_years: int | None
    default_actions: list[dict[str, object]]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    is_system: bool


DefaultActionInput = DefaultAssetAction | Mapping[str, object]


def validate_default_actions(
    default_actions: Sequence[DefaultActionInput] | None,
) -> list[dict[str, object]]:
    """Return canonical JSON for a default-action catalog."""
    if default_actions is None:
        return []
    validated = _DEFAULT_ACTIONS_ADAPTER.validate_python(default_actions)
    return [
        {
            "kind": item.kind,
            "label": item.label.strip(),
            "interval_days": item.interval_days,
            "warn_before_days": item.warn_before_days,
        }
        for item in validated
    ]


def list_types(
    session: Session,
    ctx: WorkspaceContext,
    *,
    category: str | None = None,
    workspace_only: bool = False,
    include_archived: bool = False,
    limit: int = 101,
    after_id: str | None = None,
) -> Sequence[AssetTypeView]:
    """List asset types visible to the workspace."""
    stmt = select(AssetType)
    if workspace_only:
        stmt = stmt.where(AssetType.workspace_id == ctx.workspace_id)
    else:
        stmt = stmt.where(
            or_(
                AssetType.workspace_id == ctx.workspace_id,
                AssetType.workspace_id.is_(None),
            )
        )
    if category is not None:
        stmt = stmt.where(AssetType.category == category)
    if not include_archived:
        stmt = stmt.where(AssetType.deleted_at.is_(None))
    if after_id is not None:
        stmt = stmt.where(AssetType.id > after_id)
    stmt = stmt.order_by(AssetType.id.asc())
    stmt = stmt.limit(limit)
    # justification: asset_type intentionally mixes workspace rows with
    # system rows (`workspace_id IS NULL`); every query reasserts scope.
    with tenant_agnostic():
        rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


def get_type(
    session: Session,
    ctx: WorkspaceContext,
    *,
    type_id: str,
    include_archived: bool = False,
) -> AssetTypeView:
    """Return one asset type visible to the workspace."""
    return _row_to_view(
        _load_row(session, ctx, type_id=type_id, include_archived=include_archived)
    )


def create_type(
    session: Session,
    ctx: WorkspaceContext,
    *,
    key: str | None = None,
    slug: str | None = None,
    name: str | None = None,
    category: str = "other",
    icon: str | None = None,
    icon_name: str | None = None,
    description_md: str | None = None,
    default_lifespan_years: int | None = None,
    default_actions: Sequence[DefaultActionInput] | None = None,
    body: AssetTypeCreate | None = None,
    clock: Clock | None = None,
) -> AssetTypeView:
    """Create a workspace-custom asset type and audit it."""
    if body is None:
        resolved_key = key if key is not None else slug
        if resolved_key is None:
            raise ValueError("key or slug is required")
        if name is None:
            raise ValueError("name is required")
        body = AssetTypeCreate(
            key=resolved_key,
            name=name,
            category=category,
            icon_name=icon_name if icon_name is not None else icon,
            description_md=description_md,
            default_lifespan_years=default_lifespan_years,
            default_actions=_DEFAULT_ACTIONS_ADAPTER.validate_python(
                default_actions or []
            ),
        )
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _raise_on_key_conflict(session, ctx, key=body.key)
    row = AssetType(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        key=body.key.strip(),
        name=body.name.strip(),
        category=body.category,
        icon_name=body.icon_name.strip() if body.icon_name is not None else None,
        description_md=body.description_md,
        default_lifespan_years=body.default_lifespan_years,
        default_actions_json=validate_default_actions(body.default_actions),
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise AssetTypeKeyConflict(
            f"asset type key {body.key!r} already exists in this workspace"
        ) from exc

    write_audit(
        session,
        ctx,
        entity_kind="asset_type",
        entity_id=row.id,
        action="asset_type.create",
        diff={"key": row.key, "name": row.name, "category": row.category},
        clock=resolved_clock,
    )
    return _row_to_view(row)


def update_type(
    session: Session,
    ctx: WorkspaceContext,
    *,
    type_id: str,
    body: AssetTypeUpdate | None = None,
    key: str | None = None,
    name: str | None = None,
    category: str | None = None,
    icon_name: str | None = None,
    icon: str | None = None,
    description_md: str | None = None,
    default_lifespan_years: int | None = None,
    default_actions: Sequence[DefaultActionInput] | None = None,
    clock: Clock | None = None,
) -> AssetTypeView:
    """Patch a workspace-custom asset type and audit material changes."""
    if body is None:
        payload: dict[str, object] = {}
        for field_name, value in (
            ("key", key),
            ("name", name),
            ("category", category),
            ("icon_name", icon_name if icon_name is not None else icon),
            ("description_md", description_md),
            ("default_lifespan_years", default_lifespan_years),
        ):
            if value is not None:
                payload[field_name] = value
        if default_actions is not None:
            payload["default_actions"] = list(default_actions)
        body = AssetTypeUpdate.model_validate(payload)

    row = _load_row(session, ctx, type_id=type_id, include_archived=False)
    _assert_workspace_custom(row)

    if not body.model_fields_set:
        return _row_to_view(row)

    before: dict[str, object | None] = {}
    after: dict[str, object | None] = {}

    if "key" in body.model_fields_set and body.key is not None:
        new_key = body.key.strip()
        if new_key != row.key:
            _raise_on_key_conflict(session, ctx, key=new_key, exclude_id=row.id)
            before["key"] = row.key
            after["key"] = new_key
            row.key = new_key

    for field_name in ("name", "category", "icon_name", "description_md"):
        if field_name not in body.model_fields_set:
            continue
        new_value = getattr(body, field_name)
        if isinstance(new_value, str):
            new_value = new_value.strip()
        if new_value != getattr(row, field_name):
            before[field_name] = getattr(row, field_name)
            after[field_name] = new_value
            setattr(row, field_name, new_value)

    if (
        "default_lifespan_years" in body.model_fields_set
        and body.default_lifespan_years != row.default_lifespan_years
    ):
        before["default_lifespan_years"] = row.default_lifespan_years
        after["default_lifespan_years"] = body.default_lifespan_years
        row.default_lifespan_years = body.default_lifespan_years

    if "default_actions" in body.model_fields_set:
        new_actions = validate_default_actions(body.default_actions)
        if new_actions != row.default_actions_json:
            before["default_actions_json"] = list(row.default_actions_json)
            after["default_actions_json"] = new_actions
            row.default_actions_json = new_actions

    if not after:
        return _row_to_view(row)

    resolved_clock = clock if clock is not None else SystemClock()
    row.updated_at = resolved_clock.now()
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if "key" in after:
            raise AssetTypeKeyConflict(
                f"asset type key {after['key']!r} already exists in this workspace"
            ) from exc
        raise

    write_audit(
        session,
        ctx,
        entity_kind="asset_type",
        entity_id=row.id,
        action="asset_type.update",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )
    return _row_to_view(row)


def delete_type(
    session: Session,
    ctx: WorkspaceContext,
    *,
    type_id: str,
    clock: Clock | None = None,
) -> AssetTypeView | None:
    """Delete an unused type, or archive it when assets reference it."""
    row = _load_row(session, ctx, type_id=type_id, include_archived=True)
    _assert_workspace_custom(row)
    if row.deleted_at is not None:
        return None

    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    if _asset_reference_count(session, ctx, type_id=type_id) > 0:
        row.deleted_at = now
        row.updated_at = now
        session.flush()
        write_audit(
            session,
            ctx,
            entity_kind="asset_type",
            entity_id=row.id,
            action="asset_type.delete",
            diff={"archived": True, "key": row.key},
            clock=resolved_clock,
        )
        return _row_to_view(row)

    row_id = row.id
    row_key = row.key
    # justification: asset_type may include system rows; the predicate
    # pins this hard-delete to the caller's workspace-custom row.
    with tenant_agnostic():
        session.execute(
            delete(AssetType).where(
                AssetType.id == row_id,
                AssetType.workspace_id == ctx.workspace_id,
            )
        )
        session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="asset_type",
        entity_id=row_id,
        action="asset_type.delete",
        diff={"archived": False, "key": row_key},
        clock=resolved_clock,
    )
    return None


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    type_id: str,
    include_archived: bool,
) -> AssetType:
    stmt = select(AssetType).where(
        AssetType.id == type_id,
        or_(
            AssetType.workspace_id == ctx.workspace_id,
            AssetType.workspace_id.is_(None),
        ),
    )
    if not include_archived:
        stmt = stmt.where(AssetType.deleted_at.is_(None))
    # justification: asset_type intentionally mixes workspace rows with
    # system rows (`workspace_id IS NULL`); every query reasserts scope.
    with tenant_agnostic():
        row = session.scalars(stmt).one_or_none()
    if row is None:
        raise AssetTypeNotFound(type_id)
    return row


def _assert_workspace_custom(row: AssetType) -> None:
    if row.workspace_id is None:
        raise AssetTypeReadOnly("system asset types are read-only")


def _raise_on_key_conflict(
    session: Session,
    ctx: WorkspaceContext,
    *,
    key: str,
    exclude_id: str | None = None,
) -> None:
    stmt = select(AssetType.id).where(
        AssetType.workspace_id == ctx.workspace_id,
        AssetType.key == key,
    )
    if exclude_id is not None:
        stmt = stmt.where(AssetType.id != exclude_id)
    # justification: asset_type has nullable workspace_id for system rows;
    # this conflict probe explicitly pins the workspace partition.
    with tenant_agnostic():
        existing = session.scalars(stmt).first()
    if existing is not None:
        raise AssetTypeKeyConflict(
            f"asset type key {key!r} already exists in this workspace"
        )


def _asset_reference_count(
    session: Session,
    ctx: WorkspaceContext,
    *,
    type_id: str,
) -> int:
    stmt = (
        select(func.count())
        .select_from(Asset)
        .where(
            Asset.workspace_id == ctx.workspace_id,
            Asset.asset_type_id == type_id,
        )
    )
    # justification: archive decisions must count only the caller's
    # workspace assets while bypassing any absent ambient context in tests.
    with tenant_agnostic():
        return int(session.scalar(stmt) or 0)


def _row_to_view(row: AssetType) -> AssetTypeView:
    return AssetTypeView(
        id=row.id,
        workspace_id=row.workspace_id,
        key=row.key,
        name=row.name,
        category=row.category,
        icon_name=row.icon_name,
        description_md=row.description_md,
        default_lifespan_years=row.default_lifespan_years,
        default_actions=list(row.default_actions_json),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        deleted_at=_as_utc(row.deleted_at) if row.deleted_at is not None else None,
        is_system=row.workspace_id is None,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
