"""Places context router + properties roster surface (cd-75wp, cd-lzh1, cd-yjw5).

Owns properties, units, areas, and closures (spec §01 "Context map",
§04 "Properties / areas / stays"). Two router factories live in this
module:

* :data:`router` — the empty places-context scaffold mounted by the
  app factory at ``/w/<slug>/api/v1/places``. Sub-routes (units,
  closures, area CRUD) land here as cd-75wp and friends fill in.

* :func:`build_properties_router` — the workspace properties roster
  endpoint (cd-lzh1) mounted **outside** the ``/places`` URL segment
  at ``/w/<slug>/api/v1/properties``. The SPA's manager pages
  (``SchedulesPage``, ``PropertiesPage``, ``PropertyDetailPage``,
  ``EmployeesPage``) and worker pages (``HistoryPage``,
  ``NewTaskModal``, ``SubmitExpenseForm``) call
  ``fetchJson<Property[]>('/api/v1/properties')`` verbatim — a flat
  array, no pagination envelope — so the roster sits at the top of
  the workspace tree (matches the cd-g6nf precedent for
  ``/employees``). The router still tags its operations ``places`` so
  the OpenAPI document clusters it under the places context alongside
  the eventual property CRUD routes.

**Why a bare array, not the ``{data, next_cursor, has_more}``
envelope?** Same reason as ``/employees`` (cd-g6nf, cd-jtgo): the
SPA's ``fetchJson<Property[]>`` calls expect a flat list. Switching
to a cursor envelope here without migrating every SPA call site would
break the SPA pages on first load. The roster is bounded by the
workspace's property count (≈ tens to low hundreds in a typical
deployment), well within budget for an unbounded fetch. A separate
follow-up will pair the envelope shape with an SPA call-site migration.

**Per-role projection (cd-yjw5).** The endpoint carries no
action-key gate beyond :func:`current_workspace_context` (the
production middleware only mints a context for users with a live
``UserWorkspace`` row). An ``action_key`` gate on
``scope.view@workspace`` would be too narrow: a property-pinned
worker (no workspace-wide grant) is intentionally NOT in
``all_workers@workspace`` per
:func:`app.authz.membership.is_member_of`, so the gate would 403
the very actor the narrowing branch was written for. The body is
split by role inside the handler:

* **Owners + managers** (``properties.read`` resolves allow): full
  projection — every field on :class:`PropertyResponse`, including
  governance-adjacent ``client_org_id`` / ``owner_user_id`` (§22)
  and workspace-level ``settings_override``.
* **Workers** (``properties.read`` resolves deny): narrowed to the
  properties the actor holds a ``role_grant`` on (workspace-wide
  grant fans out across every live property; property-pinned
  grants restrict to the named property only) AND the three
  governance-adjacent fields are masked to safe nulls / empty
  mapping. The wire shape stays the SPA's single :class:`Property`
  type — masking, not a separate response type, keeps the SPA call
  sites unchanged. The worker pages that motivated cd-yjw5
  (``HistoryPage``, ``NewTaskModal``, ``SubmitExpenseForm``) need
  the name + city + timezone for property-pinned data they already
  see elsewhere; the governance fields they didn't have before
  stay hidden.

A worker with zero matching grants legitimately gets an empty
array — silently. The privacy contract is honoured by the
narrowing (you cannot see what you have no grant for) rather than
by a hard 403; surfacing "this user has no properties" as a deny
would leak whether *any* property exists.

**Field defaults.** The current v1 ORM does not yet carry every field
the SPA's :class:`Property` shape declares — ``city`` (we project from
``address_json.city``), ``color`` (palette pick by id hash), ``areas``
(from the :class:`Area` join), ``evidence_policy`` (default
``"inherit"``), ``settings_override`` (default ``{}``). Each default
is documented inline against the column it will eventually resolve
from once the matching ORM widening lands.

See ``docs/specs/12-rest-api.md`` §"Properties / areas / stays",
``docs/specs/05-employees-and-roles.md`` §"Action catalog" /
§"How a rule narrows or widens a default", and
``app/web/src/types/property.ts``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context, db_session
from app.authz import PermissionDenied, require
from app.authz.dep import Permission
from app.domain.places import (
    area_service,
    closure_service,
    membership_service,
    property_service,
    unit_service,
)
from app.events.bus import bus as default_event_bus
from app.events.types import PropertyWorkspaceChanged
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock

__all__ = [
    "PropertyResponse",
    "build_properties_router",
    "router",
]


router = APIRouter(tags=["places"])

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Cursor = Annotated[str | None, Query(max_length=64)]
_Limit = Annotated[int, Query(ge=1, le=100)]


# ---------------------------------------------------------------------------
# Static defaults for fields the v1 ORM does not yet carry. Each constant
# is named after the SPA field it backs so a future migration that lands
# the real column can grep for the constant and remove it in lockstep.
# ---------------------------------------------------------------------------

# §05 "Worker settings" cascade defaults this to ``inherit``; the
# property-level column lands with the §04 evidence-policy widening.
_EVIDENCE_POLICY_DEFAULT: Literal["inherit"] = "inherit"

# Mirrors the workspace default in :class:`app.adapters.db.workspace.models.Workspace`
# (default_locale defaults to ``""``); the SPA's ``Property.locale`` is
# typed as a non-nullable string so a NULL on the row needs a placeholder
# rather than ``null`` on the wire. Empty string preserves "inherit
# workspace default" semantics — the SPA falls back to the workspace
# locale when a property carries no explicit override.
_LOCALE_DEFAULT: str = ""

# The :class:`Property` row's ``country`` column defaults to ``"XX"``
# at the migration layer (a placeholder for legacy rows pre-cd-8u5).
# The SPA's ``Property.country`` is a non-nullable string; the wire
# value flows through unchanged. Documented here so a future migration
# that tightens the column can prune the placeholder reference.
_COUNTRY_FALLBACK: str = "XX"

# Palette of accent colors the SPA's :data:`PropertyColor` declares.
# Order is stable; :func:`_color_for` picks deterministically by
# hashing the property id so two reloads pin the same color.
_COLOR_PALETTE: tuple[Literal["moss", "sky", "rust"], ...] = (
    "moss",
    "sky",
    "rust",
)

# Per-property settings cascade override blob. The SPA's
# ``Property.settings_override`` is typed as ``Record<string, unknown>``;
# the v1 ORM has no settings_override column on :class:`Property` yet
# (§05 "Settings cascade" lands the per-property override with the
# next migration), so projection emits a frozen empty mapping. Future
# migration: replace this constant with a column read in
# :func:`_project_property`. ``mappingproxy`` would be more correct
# but it's not JSON-serialisable by Pydantic v2 — the freshly-built
# ``dict[str, object]`` returned at projection time keeps the wire
# shape JSON-serialisable; the constant is the named seam to grep for.
_SETTINGS_OVERRIDE_DEFAULT: dict[str, object] = {}


# ---------------------------------------------------------------------------
# Wire-facing shape — flat ``Property`` matching app/web/src/types/property.ts.
# ---------------------------------------------------------------------------


class PropertyResponse(BaseModel):
    """Flat ``Property`` projection — see module docstring for the join.

    Mirrors :class:`Property` in ``app/web/src/types/property.ts``
    field-for-field. Fields the v1 ORM does not yet carry are
    documented inline; future migrations replace the static defaults
    with the real column reads in lockstep.

    **Per-role masking (cd-yjw5).** Three governance-adjacent fields
    are masked to safe defaults when the caller is a worker — i.e.
    when ``properties.read`` resolves deny:

    * ``client_org_id`` → ``None`` (the §22 billing org)
    * ``owner_user_id`` → ``None`` (the §22 owner-of-record)
    * ``settings_override`` → ``{}`` (the §05 per-property override
      blob)

    The owner / manager projection emits the real values for all
    three. The decision to mask in-place rather than ship a separate
    ``WorkerPropertyResponse`` model keeps the SPA's single
    :class:`Property` type unchanged across roles — the SPA call
    sites in ``HistoryPage`` / ``NewTaskModal`` / ``SubmitExpenseForm``
    don't need to branch by role to render a name + city + timezone.
    """

    id: str
    name: str
    city: str
    timezone: str
    color: Literal["moss", "sky", "rust"]
    kind: Literal["str", "vacation", "residence", "mixed"]
    areas: list[str]
    evidence_policy: Literal["inherit", "require", "optional", "forbid"]
    country: str
    locale: str
    # ``Record<string, unknown>`` on the SPA side. ``object`` keeps the
    # value space soundly typed without opting out of mypy strict (which
    # ``Any`` would). Today the field is a static ``{}`` placeholder
    # until the per-property settings_override column lands. Masked to
    # ``{}`` for workers regardless of the eventual column value.
    settings_override: dict[str, object]
    # Governance-adjacent (§22). Masked to ``None`` for workers — the
    # cross-roster surface intentionally hides which org bills the
    # property and who the owner-of-record is. The SPA type is already
    # nullable so the masking is a value-only change.
    client_org_id: str | None
    owner_user_id: str | None


class PropertyWriteRequest(BaseModel):
    """HTTP write body for property create/update.

    The domain DTO keeps the legacy rendered ``address`` required.
    The REST surface accepts canonical ``address_json`` as the primary
    address shape and derives a rendered address when callers omit it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    kind: property_service.PropertyKind = "residence"
    address: str | None = Field(default=None, min_length=1, max_length=500)
    address_json: property_service.AddressPayload = Field(
        default_factory=property_service.AddressPayload
    )
    country: str | None = Field(default=None, min_length=2, max_length=2)
    locale: str | None = Field(default=None, max_length=35)
    default_currency: str | None = Field(default=None, max_length=3)
    timezone: str = Field(..., min_length=1, max_length=64)
    lat: float | None = None
    lon: float | None = None
    client_org_id: str | None = Field(default=None, max_length=64)
    owner_user_id: str | None = Field(default=None, max_length=64)
    tags_json: list[str] = Field(default_factory=list, max_length=50)
    welcome_defaults_json: dict[str, Any] = Field(default_factory=dict)
    property_notes_md: str = Field(default="", max_length=20_000)
    label: str | None = Field(default=None, max_length=200)

    def to_create(self) -> property_service.PropertyCreate:
        return property_service.PropertyCreate.model_validate(
            self._domain_payload(include_label=True)
        )

    def to_update(self) -> property_service.PropertyUpdate:
        return property_service.PropertyUpdate.model_validate(
            self._domain_payload(include_label=False)
        )

    def _domain_payload(self, *, include_label: bool) -> dict[str, Any]:
        payload: dict[str, Any] = self.model_dump()
        payload["address"] = self.address or _render_address(self.address_json)
        if not include_label:
            payload.pop("label", None)
        return payload


class PropertyDetailResponse(BaseModel):
    id: str
    name: str
    kind: property_service.PropertyKind
    address: str
    address_json: dict[str, Any]
    country: str
    locale: str | None
    default_currency: str | None
    timezone: str
    lat: float | None
    lon: float | None
    client_org_id: str | None
    owner_user_id: str | None
    tags_json: list[str]
    welcome_defaults_json: dict[str, Any]
    property_notes_md: str
    created_at: datetime
    updated_at: datetime | None
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: property_service.PropertyView) -> PropertyDetailResponse:
        return cls(
            id=view.id,
            name=view.name,
            kind=view.kind,
            address=view.address,
            address_json=dict(view.address_json),
            country=view.country,
            locale=view.locale,
            default_currency=view.default_currency,
            timezone=view.timezone,
            lat=view.lat,
            lon=view.lon,
            client_org_id=view.client_org_id,
            owner_user_id=view.owner_user_id,
            tags_json=list(view.tags_json),
            welcome_defaults_json=dict(view.welcome_defaults_json),
            property_notes_md=view.property_notes_md,
            created_at=view.created_at,
            updated_at=view.updated_at,
            deleted_at=view.deleted_at,
        )


class UnitResponse(BaseModel):
    id: str
    property_id: str
    name: str
    ordinal: int
    default_checkin_time: str | None
    default_checkout_time: str | None
    max_guests: int | None
    welcome_overrides_json: dict[str, Any]
    settings_override_json: dict[str, Any]
    notes_md: str
    created_at: datetime
    updated_at: datetime | None
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: unit_service.UnitView) -> UnitResponse:
        return cls(
            id=view.id,
            property_id=view.property_id,
            name=view.name,
            ordinal=view.ordinal,
            default_checkin_time=view.default_checkin_time,
            default_checkout_time=view.default_checkout_time,
            max_guests=view.max_guests,
            welcome_overrides_json=dict(view.welcome_overrides_json),
            settings_override_json=dict(view.settings_override_json),
            notes_md=view.notes_md,
            created_at=view.created_at,
            updated_at=view.updated_at,
            deleted_at=view.deleted_at,
        )


class UnitListResponse(BaseModel):
    data: list[UnitResponse]
    next_cursor: str | None
    has_more: bool


class AreaResponse(BaseModel):
    id: str
    property_id: str
    unit_id: str | None
    name: str
    kind: area_service.AreaKind
    order_hint: int
    parent_area_id: str | None
    notes_md: str
    created_at: datetime
    updated_at: datetime | None
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: area_service.AreaView) -> AreaResponse:
        return cls(
            id=view.id,
            property_id=view.property_id,
            unit_id=view.unit_id,
            name=view.name,
            kind=view.kind,
            order_hint=view.order_hint,
            parent_area_id=view.parent_area_id,
            notes_md=view.notes_md,
            created_at=view.created_at,
            updated_at=view.updated_at,
            deleted_at=view.deleted_at,
        )


class AreaListResponse(BaseModel):
    data: list[AreaResponse]
    next_cursor: str | None
    has_more: bool


class ClosureCreateRequest(closure_service.PropertyClosureCreate):
    property_id: str = Field(..., min_length=1, max_length=64)
    unit_id: str | None = Field(default=None, max_length=64)


class ClosureUpdateRequest(closure_service.PropertyClosureUpdate):
    unit_id: str | None = Field(default=None, max_length=64)


class ClosureResponse(BaseModel):
    id: str
    property_id: str
    starts_at: datetime
    ends_at: datetime
    reason: closure_service.ClosureReason
    source_ical_feed_id: str | None
    created_by_user_id: str | None
    created_at: datetime
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: closure_service.PropertyClosureView) -> ClosureResponse:
        return cls(
            id=view.id,
            property_id=view.property_id,
            starts_at=view.starts_at,
            ends_at=view.ends_at,
            reason=view.reason,
            source_ical_feed_id=view.source_ical_feed_id,
            created_by_user_id=view.created_by_user_id,
            created_at=view.created_at,
            deleted_at=view.deleted_at,
        )


class ClosureListResponse(BaseModel):
    data: list[ClosureResponse]
    next_cursor: str | None
    has_more: bool


class MembershipResponse(BaseModel):
    property_id: str
    workspace_id: str
    label: str
    membership_role: str
    status: str
    share_guest_identity: bool
    created_at: datetime

    @classmethod
    def from_view(cls, view: membership_service.MembershipRead) -> MembershipResponse:
        return cls(
            property_id=view.property_id,
            workspace_id=view.workspace_id,
            label=view.label,
            membership_role=view.membership_role,
            status=view.status,
            share_guest_identity=view.share_guest_identity,
            created_at=view.created_at,
        )


class MembershipListResponse(BaseModel):
    data: list[MembershipResponse]
    next_cursor: str | None
    has_more: bool


class ShareCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str | None = Field(default=None, max_length=64)
    workspace_slug: str | None = Field(default=None, max_length=80)
    membership_role: membership_service.MembershipRole = "managed_workspace"
    share_guest_identity: bool = False


class ShareUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    membership_role: membership_service.MembershipRole | None = None
    share_guest_identity: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_address(address: property_service.AddressPayload) -> str:
    parts = [
        address.line1,
        address.line2,
        address.city,
        address.state_province,
        address.postal_code,
        address.country,
    ]
    rendered = ", ".join(part.strip() for part in parts if part and part.strip())
    if rendered:
        return rendered
    raise ValueError(
        "address or address_json with at least one address field is required"
    )


def _paginate[T](
    rows: Sequence[T],
    *,
    cursor: str | None,
    limit: int,
    id_of: Callable[[T], str],
) -> tuple[list[T], str | None, bool]:
    start = 0
    if cursor is not None:
        for index, row in enumerate(rows):
            if id_of(row) == cursor:
                start = index + 1
                break
        else:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_cursor"},
            )
    window = list(rows[start : start + limit + 1])
    has_more = len(window) > limit
    data = window[:limit]
    next_cursor = id_of(data[-1]) if has_more and data else None
    return data, next_cursor, has_more


def _http(status_code: int, error: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": error})


def _property_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "property_not_found")


def _unit_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "unit_not_found")


def _area_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "area_not_found")


def _closure_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "property_closure_not_found")


def _membership_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "property_workspace_not_found")


def _validation_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "validation_error", "message": str(exc)},
    )


def _conflict(error: str, exc: Exception | None = None) -> HTTPException:
    detail: dict[str, object] = {"error": error}
    if exc is not None:
        detail["message"] = str(exc)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _resolve_workspace_id(
    session: Session,
    *,
    workspace_id: str | None,
    workspace_slug: str | None,
) -> str:
    if workspace_id is not None and workspace_slug is not None:
        raise _validation_error(
            ValueError("set either workspace_id or workspace_slug, not both")
        )
    if workspace_id is not None:
        with tenant_agnostic():
            resolved = session.scalars(
                select(Workspace.id).where(Workspace.id == workspace_id)
            ).one_or_none()
        if resolved is None:
            raise _membership_not_found()
        return resolved
    if workspace_slug is None:
        raise _validation_error(
            ValueError("workspace_id or workspace_slug is required")
        )
    with tenant_agnostic():
        resolved = session.scalars(
            select(Workspace.id).where(Workspace.slug == workspace_slug)
        ).one_or_none()
    if resolved is None:
        raise _membership_not_found()
    return resolved


def _resolve_workspace_path_ref(session: Session, workspace_ref: str) -> str:
    with tenant_agnostic():
        resolved = session.scalars(
            select(Workspace.id).where(Workspace.slug == workspace_ref)
        ).one_or_none()
        if resolved is not None:
            return resolved
        resolved = session.scalars(
            select(Workspace.id).where(Workspace.id == workspace_ref)
        ).one_or_none()
    if resolved is None:
        raise _membership_not_found()
    return resolved


def _publish_membership_changed(
    ctx: WorkspaceContext,
    *,
    property_id: str,
    target_workspace_id: str,
    change_kind: Literal["invited", "revoked", "updated"],
) -> None:
    now = SystemClock().now()
    for workspace_id in dict.fromkeys((ctx.workspace_id, target_workspace_id)):
        default_event_bus.publish(
            PropertyWorkspaceChanged(
                workspace_id=workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=now,
                property_id=property_id,
                target_workspace_id=target_workspace_id,
                change_kind=change_kind,
            )
        )


def _refuse_stay_clashes(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    starts_at: datetime,
    ends_at: datetime,
    force: bool,
) -> None:
    if force:
        return
    clashes = closure_service.detect_clashes(
        session,
        ctx,
        property_id=property_id,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    if clashes.stays:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "closure_stay_conflict",
                "stay_ids": [stay.id for stay in clashes.stays],
            },
        )


def _reject_unit_scoped_closure(unit_id: str | None) -> None:
    if unit_id is not None:
        raise _validation_error(
            ValueError("unit-scoped property closures are not supported yet")
        )


def _color_for(property_id: str) -> Literal["moss", "sky", "rust"]:
    """Pick a stable :data:`PropertyColor` from ``property_id``.

    The mock layer assigns colors by hand at seed time; the real ORM
    has no ``color`` column. A deterministic hash over the id keeps
    the palette stable across reloads (so a manager doesn't see the
    same property in three different colors as the page refreshes)
    without storing the value. SHA-256 (not built-in :func:`hash`)
    because the latter is salted per-process and would shuffle the
    palette across restarts.
    """
    digest = hashlib.sha256(property_id.encode("utf-8")).digest()
    return _COLOR_PALETTE[digest[0] % len(_COLOR_PALETTE)]


def _city_for(address_json: dict[str, Any] | None) -> str:
    """Pluck the SPA's ``city`` field out of the canonical address blob.

    §04 "`address_json` canonical shape" stores the structured address
    under ``address_json``; the SPA's ``Property.city`` is the rendered
    city name. A row that pre-dates the cd-8u5 widening carries an
    empty blob — fall back to the empty string so the SPA renders
    ``"—"`` (its non-empty placeholder) instead of crashing on
    ``undefined``.
    """
    if not address_json:
        return ""
    raw = address_json.get("city")
    if isinstance(raw, str):
        return raw
    return ""


def _narrow_kind(value: str) -> Literal["str", "vacation", "residence", "mixed"]:
    """Narrow a loaded DB string to the SPA's :data:`PropertyKind`.

    The DB CHECK gate already rejects anything else; this helper
    exists purely to satisfy mypy's strict-Literal reading without
    a ``cast``. An unexpected value is loud rather than silent —
    schema drift is worth a stack trace, not a default.
    """
    if value == "residence":
        return "residence"
    if value == "vacation":
        return "vacation"
    if value == "str":
        return "str"
    if value == "mixed":
        return "mixed"
    raise ValueError(f"unknown property.kind {value!r} on loaded row")


def _list_workspace_properties(
    session: Session,
    ctx: WorkspaceContext,
) -> list[Property]:
    """Return every live property linked to ``ctx.workspace_id``.

    Joins :class:`PropertyWorkspace` to scope the result to the active
    workspace and filters ``Property.deleted_at IS NULL`` so retired
    rows never surface to the SPA. Ordered by ``Property.created_at``
    ascending with ``id`` as a stable tiebreaker — the SPA renders
    the list in oldest-first order across reloads.

    The explicit ``PropertyWorkspace.workspace_id == ctx.workspace_id``
    is defence-in-depth alongside the ORM tenant filter — same shape
    as :func:`app.domain.places.property_service._load_row`.
    """
    stmt = (
        select(Property)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            PropertyWorkspace.workspace_id == ctx.workspace_id,
            Property.deleted_at.is_(None),
        )
        .order_by(Property.created_at.asc(), Property.id.asc())
    )
    return list(session.scalars(stmt).all())


def _load_areas_by_property(
    session: Session,
    *,
    property_ids: list[str],
) -> dict[str, list[str]]:
    """Return ``{property_id: [area.label, ...]}`` ordered by ``Area.ordering``.

    The mock layer carries areas as a flat list of labels on the
    :class:`Property` row; the v1 ORM normalises them into the
    :class:`Area` table. Project the labels back into a list per
    property, sorted by ``Area.ordering`` (the §04 walk-order hint)
    with ``label`` as a stable tiebreaker so two areas with equal
    ordering render in alphabetical order. :class:`Area` is reached
    via a single ``IN`` query so the route stays one round-trip
    regardless of property count.
    """
    if not property_ids:
        return {}
    stmt = (
        select(Area.property_id, Area.label, Area.ordering)
        .where(Area.property_id.in_(property_ids))
        .order_by(Area.property_id.asc(), Area.ordering.asc(), Area.label.asc())
    )
    out: dict[str, list[str]] = defaultdict(list)
    for property_id, label, _ordering in session.execute(stmt).all():
        out[property_id].append(label)
    return dict(out)


def _project_property(
    row: Property,
    *,
    areas: list[str],
    mask_governance: bool,
) -> PropertyResponse:
    """Build one :class:`PropertyResponse` from the joined rows.

    ``mask_governance`` is the cd-yjw5 per-role projection switch.
    When ``True`` the three governance-adjacent fields
    (``client_org_id``, ``owner_user_id``, ``settings_override``)
    are emitted as their safe defaults regardless of the row value —
    workers must never enumerate the §22 billing-org / owner-of-record
    coupling or the per-property settings cascade. Manager / owner
    callers pass ``False`` and see the real values.
    """
    # ``name`` is nullable at the DB layer for the cd-8u5 cheap
    # backfill; the service always writes a non-blank value on insert.
    # A NULL row is a pre-migration artefact — fall back to ``address``
    # so the SPA still has something to render.
    name = row.name if row.name is not None else row.address
    if mask_governance:
        client_org_id: str | None = None
        owner_user_id: str | None = None
        # Fresh empty dict — see the manager-branch comment below for
        # the rationale on never sharing a module-level mapping.
        settings_override: dict[str, object] = {}
    else:
        client_org_id = row.client_org_id
        owner_user_id = row.owner_user_id
        # Fresh dict per row — never share the module-level constant
        # by reference, in case Pydantic mutates the value during
        # validation (it doesn't today; the copy is cheap insurance).
        settings_override = dict(_SETTINGS_OVERRIDE_DEFAULT)
    return PropertyResponse(
        id=row.id,
        name=name,
        city=_city_for(row.address_json),
        timezone=row.timezone,
        color=_color_for(row.id),
        kind=_narrow_kind(row.kind),
        areas=areas,
        evidence_policy=_EVIDENCE_POLICY_DEFAULT,
        country=row.country if row.country else _COUNTRY_FALLBACK,
        locale=row.locale if row.locale is not None else _LOCALE_DEFAULT,
        settings_override=settings_override,
        client_org_id=client_org_id,
        owner_user_id=owner_user_id,
    )


def _visible_property_ids_for_worker(
    session: Session,
    ctx: WorkspaceContext,
    *,
    workspace_property_ids: list[str],
) -> set[str]:
    """Return the set of property ids the current worker may scope.view.

    Mirrors the :class:`RoleGrant`-driven fan-out used by
    ``app/api/v1/employees.py::_load_property_ids_by_user``: the
    actor's grants on this workspace are walked once; a workspace-wide
    grant (``scope_property_id IS NULL``) widens to every live
    property, and each property-pinned grant narrows to its single
    target. Properties the actor does not appear on collapse out of
    the result silently.

    ``workspace_property_ids`` is the list of live property ids in the
    workspace (passed in by the caller so the heavy
    :class:`PropertyWorkspace` x :class:`Property` join only runs
    once per request). Property-pinned grants are gated through this
    list so a grant pointing at a retired or sibling-workspace
    property never leaks into the worker's view.

    The query is a single ``SELECT scope_property_id FROM role_grant
    WHERE workspace_id = ? AND user_id = ?`` — bounded by the user's
    grant fan-out (typically one to a handful of rows) so an in-memory
    walk is cheap. The same logic lives in ``employees.py``; cd-yjw5
    leaves the duplication intentional and files the DRY follow-up
    as cd-atvn (hoist into ``app/authz/places_visibility.py`` once
    both call sites can move in lockstep).
    """
    live = set(workspace_property_ids)
    if not live:
        return set()

    grants_stmt = select(RoleGrant.scope_property_id).where(
        RoleGrant.workspace_id == ctx.workspace_id,
        RoleGrant.user_id == ctx.actor_id,
    )
    visible: set[str] = set()
    for (scope_property_id,) in session.execute(grants_stmt).all():
        if scope_property_id is None:
            # Workspace-wide grant fans out across every live property.
            return set(live)
        if scope_property_id in live:
            visible.add(scope_property_id)
    return visible


def _can_read_full_roster(
    session: Session,
    ctx: WorkspaceContext,
) -> bool:
    """Return ``True`` iff the caller passes ``properties.read``.

    Probes the canonical permission resolver via :func:`require` and
    swallows :class:`PermissionDenied` — the same resolver runs at the
    gate so the answer here matches the answer the gate would give
    for a manager. Routing the question through :func:`require`
    (rather than reimplementing "is owner OR manager") keeps the
    decision in one place: a future ``permission_rule`` row that
    grants or denies ``properties.read`` for a non-default subject is
    automatically honoured.

    Other authz exceptions (``UnknownActionKey`` / ``InvalidScope``)
    propagate — they signal a caller bug that ``properties.read``'s
    catalog entry has drifted, not a permission decision.
    """
    try:
        require(
            session,
            ctx,
            action_key="properties.read",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except PermissionDenied:
        return False
    return True


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_properties_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the properties roster.

    Mounted by the v1 app factory at
    ``/w/<slug>/api/v1``. Tests instantiate it directly via
    :func:`tests.unit.api.v1.identity.conftest.build_client` to keep
    the dependency-override cache per-case.
    """
    api = APIRouter(tags=["places", "properties"])

    read_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    create_gate = Depends(Permission("properties.create", scope_kind="workspace"))
    edit_gate = Depends(Permission("properties.edit", scope_kind="workspace"))
    archive_gate = Depends(Permission("properties.archive", scope_kind="workspace"))
    share_gate = Depends(
        Permission(
            "places.share",
            scope_kind="property",
            scope_id_from_path="property_id",
        )
    )

    @api.post(
        "/properties",
        status_code=status.HTTP_201_CREATED,
        response_model=PropertyDetailResponse,
        operation_id="properties.create",
        summary="Create a property in the caller's workspace",
        dependencies=[create_gate],
    )
    def create_property(
        body: PropertyWriteRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PropertyDetailResponse:
        try:
            view = property_service.create_property(
                session,
                ctx,
                body=body.to_create(),
            )
        except property_service.AddressCountryMismatch as exc:
            raise _validation_error(exc) from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return PropertyDetailResponse.from_view(view)

    # cd-yjw5 — no action-key gate. The endpoint accepts every
    # authenticated workspace member (the :func:`current_workspace_context`
    # dep already enforces authentication; the production middleware
    # only mints a context for users with a live ``UserWorkspace``
    # row). Per-role narrowing + masking happens inside the handler:
    # managers / owners get the full roster, workers get a filtered +
    # governance-masked projection. Gating on ``scope.view@workspace``
    # would be too narrow — a property-pinned worker (no workspace-
    # wide grant) is intentionally NOT in ``all_workers@workspace``
    # per :func:`app.authz.membership.is_member_of`, so they would
    # 403 at the gate and never reach the property-narrowing branch
    # that exists precisely for them.

    @api.get(
        "/properties",
        response_model=list[PropertyResponse],
        operation_id="properties.list",
        summary="List properties visible to the caller",
        openapi_extra={
            "x-cli": {
                "group": "properties",
                "verb": "list",
                "summary": "List properties in a workspace",
                "mutates": False,
            },
        },
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
    ) -> list[PropertyResponse]:
        """Return the properties visible to the caller as a flat array.

        Joins :class:`PropertyWorkspace` (workspace scoping),
        :class:`Property` (the row), and :class:`Area` (the labels for
        the SPA's ``areas`` field).

        Per-role projection (cd-yjw5):

        * Owners + managers (``properties.read`` resolves allow): the
          full workspace roster, every field on
          :class:`PropertyResponse`.
        * Workers (``properties.read`` resolves deny): only the
          properties they hold a ``role_grant`` on (workspace-wide
          grant fans out across every live property; property-pinned
          grants stay narrow), with ``client_org_id``,
          ``owner_user_id``, and ``settings_override`` masked to safe
          defaults — see :class:`PropertyResponse` for the full list.

        Bare-array response — see module docstring for the rationale.
        """
        rows = _list_workspace_properties(session, ctx)
        if not rows:
            return []

        # Per-role split. ``properties.read`` answers "may this caller
        # see governance-adjacent fields and the cross-roster listing?".
        # Owners + managers pass; workers fall through to the narrowed
        # branch where the roster is filtered by their grant fan-out
        # and the §22 fields are masked.
        full_access = _can_read_full_roster(session, ctx)

        if full_access:
            visible_rows = rows
        else:
            visible_ids = _visible_property_ids_for_worker(
                session,
                ctx,
                workspace_property_ids=[r.id for r in rows],
            )
            if not visible_ids:
                return []
            visible_rows = [r for r in rows if r.id in visible_ids]

        areas_by_property = _load_areas_by_property(
            session, property_ids=[r.id for r in visible_rows]
        )
        mask = not full_access
        return [
            _project_property(
                row,
                areas=areas_by_property.get(row.id, []),
                mask_governance=mask,
            )
            for row in visible_rows
        ]

    @api.get(
        "/properties/{property_id}",
        response_model=PropertyDetailResponse,
        operation_id="properties.get",
        summary="Read one property",
        dependencies=[read_gate],
    )
    def get_property(
        property_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> PropertyDetailResponse:
        try:
            return PropertyDetailResponse.from_view(
                property_service.get_property(session, ctx, property_id=property_id)
            )
        except property_service.PropertyNotFound as exc:
            raise _property_not_found() from exc

    @api.patch(
        "/properties/{property_id}",
        response_model=PropertyDetailResponse,
        operation_id="properties.update",
        summary="Update one property",
        dependencies=[edit_gate],
    )
    def update_property(
        property_id: str,
        body: PropertyWriteRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PropertyDetailResponse:
        try:
            view = property_service.update_property(
                session,
                ctx,
                property_id=property_id,
                body=body.to_update(),
            )
        except property_service.PropertyNotFound as exc:
            raise _property_not_found() from exc
        except property_service.AddressCountryMismatch as exc:
            raise _validation_error(exc) from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return PropertyDetailResponse.from_view(view)

    @api.delete(
        "/properties/{property_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="properties.delete",
        summary="Soft-delete one property",
        dependencies=[archive_gate],
    )
    def delete_property(
        property_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            property_service.soft_delete_property(
                session,
                ctx,
                property_id=property_id,
            )
        except property_service.PropertyNotFound as exc:
            raise _property_not_found() from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/properties/{property_id}/units",
        response_model=UnitListResponse,
        operation_id="units.list",
        summary="List units for a property",
        dependencies=[read_gate],
    )
    def list_units(
        property_id: str,
        ctx: _Ctx,
        session: _Db,
        cursor: _Cursor = None,
        limit: _Limit = 50,
        deleted: bool = False,
    ) -> UnitListResponse:
        try:
            views = unit_service.list_units(
                session, ctx, property_id=property_id, deleted=deleted
            )
        except unit_service.UnitNotFound as exc:
            raise _unit_not_found() from exc
        page, next_cursor, has_more = _paginate(
            views, cursor=cursor, limit=limit, id_of=lambda view: view.id
        )
        return UnitListResponse(
            data=[UnitResponse.from_view(view) for view in page],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @api.post(
        "/properties/{property_id}/units",
        status_code=status.HTTP_201_CREATED,
        response_model=UnitResponse,
        operation_id="units.create",
        summary="Create a unit for a property",
        dependencies=[edit_gate],
    )
    def create_unit(
        property_id: str,
        body: unit_service.UnitCreate,
        ctx: _Ctx,
        session: _Db,
    ) -> UnitResponse:
        try:
            view = unit_service.create_unit(
                session, ctx, property_id=property_id, body=body
            )
        except unit_service.UnitNotFound as exc:
            raise _unit_not_found() from exc
        except unit_service.UnitNameTaken as exc:
            raise _conflict("unit_name_taken", exc) from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return UnitResponse.from_view(view)

    @api.get(
        "/units/{unit_id}",
        response_model=UnitResponse,
        operation_id="units.get",
        summary="Read one unit",
        dependencies=[read_gate],
    )
    def get_unit(
        unit_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> UnitResponse:
        try:
            return UnitResponse.from_view(
                unit_service.get_unit(session, ctx, unit_id=unit_id)
            )
        except unit_service.UnitNotFound as exc:
            raise _unit_not_found() from exc

    @api.patch(
        "/units/{unit_id}",
        response_model=UnitResponse,
        operation_id="units.update",
        summary="Update one unit",
        dependencies=[edit_gate],
    )
    def update_unit(
        unit_id: str,
        body: unit_service.UnitUpdate,
        ctx: _Ctx,
        session: _Db,
    ) -> UnitResponse:
        try:
            view = unit_service.update_unit(session, ctx, unit_id=unit_id, body=body)
        except unit_service.UnitNotFound as exc:
            raise _unit_not_found() from exc
        except unit_service.UnitNameTaken as exc:
            raise _conflict("unit_name_taken", exc) from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return UnitResponse.from_view(view)

    @api.delete(
        "/units/{unit_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="units.delete",
        summary="Soft-delete one unit",
        dependencies=[edit_gate],
    )
    def delete_unit(
        unit_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            unit_service.soft_delete_unit(session, ctx, unit_id=unit_id)
        except unit_service.UnitNotFound as exc:
            raise _unit_not_found() from exc
        except unit_service.LastUnitProtected as exc:
            raise _conflict("last_unit_protected", exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/properties/{property_id}/areas",
        response_model=AreaListResponse,
        operation_id="areas.list",
        summary="List areas for a property",
        dependencies=[read_gate],
    )
    def list_areas(
        property_id: str,
        ctx: _Ctx,
        session: _Db,
        cursor: _Cursor = None,
        limit: _Limit = 50,
        deleted: bool = False,
    ) -> AreaListResponse:
        try:
            views = area_service.list_areas(
                session, ctx, property_id=property_id, deleted=deleted
            )
        except area_service.AreaNotFound as exc:
            raise _area_not_found() from exc
        page, next_cursor, has_more = _paginate(
            views, cursor=cursor, limit=limit, id_of=lambda view: view.id
        )
        return AreaListResponse(
            data=[AreaResponse.from_view(view) for view in page],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @api.post(
        "/properties/{property_id}/areas",
        status_code=status.HTTP_201_CREATED,
        response_model=AreaResponse,
        operation_id="areas.create",
        summary="Create an area for a property",
        dependencies=[edit_gate],
    )
    def create_area(
        property_id: str,
        body: area_service.AreaCreate,
        ctx: _Ctx,
        session: _Db,
    ) -> AreaResponse:
        try:
            view = area_service.create_area(
                session, ctx, property_id=property_id, body=body
            )
        except area_service.AreaNotFound as exc:
            raise _area_not_found() from exc
        except area_service.AreaNestingTooDeep as exc:
            raise _validation_error(exc) from exc
        return AreaResponse.from_view(view)

    @api.get(
        "/areas/{area_id}",
        response_model=AreaResponse,
        operation_id="areas.get",
        summary="Read one area",
        dependencies=[read_gate],
    )
    def get_area(
        area_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> AreaResponse:
        try:
            return AreaResponse.from_view(
                area_service.get_area(session, ctx, area_id=area_id)
            )
        except area_service.AreaNotFound as exc:
            raise _area_not_found() from exc

    @api.patch(
        "/areas/{area_id}",
        response_model=AreaResponse,
        operation_id="areas.update",
        summary="Update one area",
        dependencies=[edit_gate],
    )
    def update_area(
        area_id: str,
        body: area_service.AreaUpdate,
        ctx: _Ctx,
        session: _Db,
    ) -> AreaResponse:
        try:
            view = area_service.update_area(session, ctx, area_id=area_id, body=body)
        except area_service.AreaNotFound as exc:
            raise _area_not_found() from exc
        except area_service.AreaNestingTooDeep as exc:
            raise _validation_error(exc) from exc
        return AreaResponse.from_view(view)

    @api.delete(
        "/areas/{area_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="areas.delete",
        summary="Soft-delete one area",
        dependencies=[edit_gate],
    )
    def delete_area(
        area_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            area_service.delete_area(session, ctx, area_id=area_id)
        except area_service.AreaNotFound as exc:
            raise _area_not_found() from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/property_closures",
        response_model=ClosureListResponse,
        operation_id="property_closures.list",
        summary="List property closures",
        dependencies=[read_gate],
    )
    def list_closures(
        property_id: str,
        ctx: _Ctx,
        session: _Db,
        cursor: _Cursor = None,
        limit: _Limit = 50,
        unit_id: str | None = None,
        from_: Annotated[datetime | None, Query(alias="from")] = None,
        to: datetime | None = None,
    ) -> ClosureListResponse:
        try:
            _reject_unit_scoped_closure(unit_id)
            views = closure_service.list_closures(
                session, ctx, property_id=property_id
            )
        except closure_service.ClosureNotFound as exc:
            raise _closure_not_found() from exc
        if from_ is not None:
            views = [view for view in views if view.ends_at > from_]
        if to is not None:
            views = [view for view in views if view.starts_at < to]
        page, next_cursor, has_more = _paginate(
            views, cursor=cursor, limit=limit, id_of=lambda view: view.id
        )
        return ClosureListResponse(
            data=[ClosureResponse.from_view(view) for view in page],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @api.post(
        "/property_closures",
        status_code=status.HTTP_201_CREATED,
        response_model=ClosureResponse,
        operation_id="property_closures.create",
        summary="Create a property closure",
        dependencies=[edit_gate],
    )
    def create_closure(
        body: ClosureCreateRequest,
        ctx: _Ctx,
        session: _Db,
        force: bool = False,
    ) -> ClosureResponse:
        try:
            _reject_unit_scoped_closure(body.unit_id)
            _refuse_stay_clashes(
                session,
                ctx,
                property_id=body.property_id,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
                force=force,
            )
            view = closure_service.create_closure(
                session, ctx, property_id=body.property_id, body=body
            )
        except closure_service.ClosureNotFound as exc:
            raise _closure_not_found() from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return ClosureResponse.from_view(view)

    @api.patch(
        "/property_closures/{closure_id}",
        response_model=ClosureResponse,
        operation_id="property_closures.update",
        summary="Update a property closure",
        dependencies=[edit_gate],
    )
    def update_closure(
        closure_id: str,
        body: ClosureUpdateRequest,
        ctx: _Ctx,
        session: _Db,
        force: bool = False,
    ) -> ClosureResponse:
        try:
            _reject_unit_scoped_closure(body.unit_id)
            current = closure_service.get_closure(
                session, ctx, closure_id=closure_id
            )
            _refuse_stay_clashes(
                session,
                ctx,
                property_id=current.property_id,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
                force=force,
            )
            view = closure_service.update_closure(
                session, ctx, closure_id=closure_id, body=body
            )
        except closure_service.ClosureNotFound as exc:
            raise _closure_not_found() from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return ClosureResponse.from_view(view)

    @api.delete(
        "/property_closures/{closure_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="property_closures.delete",
        summary="Delete a property closure",
        dependencies=[edit_gate],
    )
    def delete_closure(
        closure_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            closure_service.delete_closure(session, ctx, closure_id=closure_id)
        except closure_service.ClosureNotFound as exc:
            raise _closure_not_found() from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/properties/{property_id}/share",
        response_model=MembershipListResponse,
        operation_id="property_workspace.list",
        summary="List property workspace memberships",
        dependencies=[read_gate],
    )
    def list_share(
        property_id: str,
        ctx: _Ctx,
        session: _Db,
        cursor: _Cursor = None,
        limit: _Limit = 50,
    ) -> MembershipListResponse:
        try:
            views = membership_service.list_memberships(
                session, ctx, property_id=property_id
            )
        except (membership_service.MembershipNotFound, LookupError) as exc:
            raise _membership_not_found() from exc
        except PermissionError as exc:
            raise _http(status.HTTP_403_FORBIDDEN, "permission_denied") from exc
        page, next_cursor, has_more = _paginate(
            views, cursor=cursor, limit=limit, id_of=lambda view: view.workspace_id
        )
        return MembershipListResponse(
            data=[MembershipResponse.from_view(view) for view in page],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @api.post(
        "/properties/{property_id}/share",
        status_code=status.HTTP_201_CREATED,
        response_model=MembershipResponse,
        operation_id="property_workspace.share",
        summary="Share a property with another workspace",
        dependencies=[share_gate],
    )
    def create_share(
        property_id: str,
        body: ShareCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> MembershipResponse:
        try:
            target_workspace_id = _resolve_workspace_id(
                session,
                workspace_id=body.workspace_id,
                workspace_slug=body.workspace_slug,
            )
            view = membership_service.invite_workspace(
                session,
                ctx,
                property_id=property_id,
                target_workspace_id=target_workspace_id,
                role=body.membership_role,
                share_guest_identity=body.share_guest_identity,
            )
            _publish_membership_changed(
                ctx,
                property_id=property_id,
                target_workspace_id=target_workspace_id,
                change_kind="invited",
            )
        except membership_service.MembershipAlreadyExists as exc:
            raise _conflict("property_workspace_exists", exc) from exc
        except PermissionError as exc:
            raise _http(status.HTTP_403_FORBIDDEN, "permission_denied") from exc
        except LookupError as exc:
            raise _membership_not_found() from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return MembershipResponse.from_view(view)

    @api.patch(
        "/properties/{property_id}/share/{workspace_slug}",
        response_model=MembershipResponse,
        operation_id="property_workspace.update",
        summary="Update a property share",
        dependencies=[share_gate],
    )
    def update_share(
        property_id: str,
        workspace_slug: str,
        body: ShareUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> MembershipResponse:
        try:
            target_workspace_id = _resolve_workspace_path_ref(session, workspace_slug)
            view: membership_service.MembershipRead | None = None
            if body.membership_role is not None:
                view = membership_service.update_membership_role(
                    session,
                    ctx,
                    property_id=property_id,
                    target_workspace_id=target_workspace_id,
                    role=body.membership_role,
                )
            if body.share_guest_identity is not None:
                view = membership_service.update_share_guest_identity(
                    session,
                    ctx,
                    property_id=property_id,
                    target_workspace_id=target_workspace_id,
                    share_guest_identity=body.share_guest_identity,
                )
            if view is None:
                raise _validation_error(ValueError("no share fields supplied"))
            _publish_membership_changed(
                ctx,
                property_id=property_id,
                target_workspace_id=target_workspace_id,
                change_kind="updated",
            )
        except PermissionError as exc:
            raise _http(status.HTTP_403_FORBIDDEN, "permission_denied") from exc
        except LookupError as exc:
            raise _membership_not_found() from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return MembershipResponse.from_view(view)

    @api.delete(
        "/properties/{property_id}/share/{workspace_slug}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="property_workspace.revoke",
        summary="Revoke a property share",
        dependencies=[share_gate],
    )
    def delete_share(
        property_id: str,
        workspace_slug: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            target_workspace_id = _resolve_workspace_path_ref(session, workspace_slug)
            membership_service.revoke_workspace(
                session,
                ctx,
                property_id=property_id,
                target_workspace_id=target_workspace_id,
            )
            _publish_membership_changed(
                ctx,
                property_id=property_id,
                target_workspace_id=target_workspace_id,
                change_kind="revoked",
            )
        except PermissionError as exc:
            raise _http(status.HTTP_403_FORBIDDEN, "permission_denied") from exc
        except LookupError as exc:
            raise _membership_not_found() from exc
        except ValueError as exc:
            raise _validation_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api
