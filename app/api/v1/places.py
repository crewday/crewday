"""Places context router + properties roster surface (cd-75wp, cd-lzh1).

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
  ``EmployeesPage``) call ``fetchJson<Property[]>('/api/v1/properties')``
  verbatim — a flat array, no pagination envelope — so the roster sits
  at the top of the workspace tree (matches the cd-g6nf precedent for
  ``/employees``). The router still tags its operations ``places`` so
  the OpenAPI document clusters it under the places context alongside
  the eventual property CRUD routes.

**Why a bare array, not the ``{data, next_cursor, has_more}``
envelope?** Same reason as ``/employees`` (cd-g6nf, cd-jtgo): the
SPA's ``fetchJson<Property[]>`` calls expect a flat list. Switching
to a cursor envelope here without migrating every SPA call site would
break the manager pages on first load. The action-catalog gate keeps
the page bounded enough that an unbounded fetch is a manager's choice;
unbounded enumeration of property rows (≈ tens to low hundreds in a
typical deployment) is well within budget. A separate follow-up will
pair the envelope shape with an SPA call-site migration.

**Why manager-only (``properties.read``)?** Same posture as
``employees.read``: the workspace-wide property roster is a manager
view that surfaces governance-adjacent fields (``client_org_id`` /
``owner_user_id`` per §22) and workspace-level
``settings_override`` blobs that a worker should not enumerate.
Property-pinned worker access to a specific property's data already
lives in the property-scoped surfaces (``/tasks``, ``/stays``, …)
gated on their own property-scope rules; this read gates only the
cross-roster listing.

**Field defaults.** The current v1 ORM does not yet carry every field
the SPA's :class:`Property` shape declares — ``city`` (we project from
``address_json.city``), ``color`` (palette pick by id hash), ``areas``
(from the :class:`Area` join), ``evidence_policy`` (default
``"inherit"``), ``settings_override`` (default ``{}``). Each default
is documented inline against the column it will eventually resolve
from once the matching ORM widening lands.

See ``docs/specs/12-rest-api.md`` §"Properties / areas / stays",
``docs/specs/05-employees-and-roles.md`` §"Action catalog", and
``app/web/src/types/property.ts``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.api.deps import current_workspace_context, db_session
from app.authz import Permission
from app.tenancy import WorkspaceContext

__all__ = [
    "PropertyResponse",
    "build_properties_router",
    "router",
]


router = APIRouter(tags=["places"])

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


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
    # until the per-property settings_override column lands.
    settings_override: dict[str, object]
    client_org_id: str | None
    owner_user_id: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
) -> PropertyResponse:
    """Build one :class:`PropertyResponse` from the joined rows."""
    # ``name`` is nullable at the DB layer for the cd-8u5 cheap
    # backfill; the service always writes a non-blank value on insert.
    # A NULL row is a pre-migration artefact — fall back to ``address``
    # so the SPA still has something to render.
    name = row.name if row.name is not None else row.address
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
        # Fresh dict per row — never share the module-level constant
        # by reference, in case Pydantic mutates the value during
        # validation (it doesn't today; the copy is cheap insurance).
        settings_override=dict(_SETTINGS_OVERRIDE_DEFAULT),
        client_org_id=row.client_org_id,
        owner_user_id=row.owner_user_id,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_properties_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the properties roster.

    Mounted by the v1 app factory at
    ``/w/<slug>/api/v1/properties``. Tests instantiate it directly via
    :func:`tests.unit.api.v1.identity.conftest.build_client` to keep
    the dependency-override cache per-case.
    """
    api = APIRouter(prefix="/properties", tags=["places", "properties"])

    read_gate = Depends(Permission("properties.read", scope_kind="workspace"))

    @api.get(
        "",
        response_model=list[PropertyResponse],
        operation_id="properties.list",
        summary="List properties in the caller's workspace (manager roster)",
        dependencies=[read_gate],
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
        """Return every property in the workspace as a flat array.

        Joins :class:`PropertyWorkspace` (workspace scoping),
        :class:`Property` (the row), and :class:`Area` (the labels for
        the SPA's ``areas`` field).

        Bare-array response — see module docstring for the rationale.
        """
        rows = _list_workspace_properties(session, ctx)
        if not rows:
            return []
        areas_by_property = _load_areas_by_property(
            session, property_ids=[r.id for r in rows]
        )
        return [
            _project_property(row, areas=areas_by_property.get(row.id, []))
            for row in rows
        ]

    return api
