"""``unit`` CRUD service — bookable subdivisions of a property.

A :class:`Unit` row is a bookable subdivision of a parent
:class:`Property` — "Room 1", "Apt 3B", "Main house". §04 "Unit"
calls out the spec invariant the service enforces:

* **Every property has at least one unit.** :func:`create_unit` is
  symmetric to :func:`property_service.create_property`'s bootstrap:
  the property service auto-creates a default unit at property
  bootstrap (``name = property.name``, ``ordinal = 0``) so there is
  always at least one. :func:`soft_delete_unit` refuses to retire
  the last live unit, surfacing :class:`LastUnitProtected`.
* **Unit names are unique within a property.** §04 lists
  ``UNIQUE(property_id, name) WHERE deleted_at IS NULL``. The
  uniqueness window is intentionally per-property, not
  per-(workspace, property): with multi-belonging
  (§04 "Multi-belonging") a property can be linked to several
  workspaces and every linked workspace sees the same unit list.
  The partial UNIQUE ``uq_unit_property_name_active`` matches the
  spec; the service runs a pre-flight SELECT to raise
  :class:`UnitNameTaken` with a clear message before the DB
  partial UNIQUE fires.

Public surface:

* **DTOs** — :class:`UnitCreate`, :class:`UnitUpdate`,
  :class:`UnitView`. Update is treated as a full replacement of the
  mutable body, matching :mod:`property_service`.
* **Service functions** — :func:`create_unit`, :func:`update_unit`,
  :func:`soft_delete_unit`, :func:`list_units`, :func:`get_unit`.
  Every function takes a :class:`~app.tenancy.WorkspaceContext` as
  its first argument; workspace scoping flows through the
  :class:`~app.adapters.db.places.models.PropertyWorkspace` junction
  joined on the parent property's id.
* **Errors** — :class:`UnitNotFound`, :class:`UnitNameTaken`,
  :class:`LastUnitProtected`.

**Workspace scoping.** Reads + writes filter the parent property
through ``property_workspace`` joined on ``workspace_id =
ctx.workspace_id`` — a unit whose property is not linked to the
caller's workspace is invisible. ``get`` / ``update`` /
``soft_delete`` raise :class:`UnitNotFound` (404) rather than 403,
matching the §01 "tenant surface is not enumerable" stance.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction.

**Auto-creation hook.** :func:`create_default_unit_for_property`
is called by :mod:`property_service` inside the property bootstrap
flush. It bypasses the ``UnitNameTaken`` pre-flight (the property
just landed; there cannot be a sibling) and writes the audit row
itself. External callers should always go through :func:`create_unit`.

See ``docs/specs/04-properties-and-stays.md`` §"Unit" /
§"Welcome overrides merge", ``docs/specs/02-domain-model.md``
§"unit".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property, PropertyWorkspace, Unit
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "LastUnitProtected",
    "UnitCreate",
    "UnitNameTaken",
    "UnitNotFound",
    "UnitUpdate",
    "UnitView",
    "create_default_unit_for_property",
    "create_unit",
    "get_unit",
    "list_units",
    "soft_delete_unit",
    "update_unit",
]


# Caps mirror :mod:`property_service` so the audit + DB payload stays
# bounded without being restrictive in practice.
_MAX_NAME_LEN = 200
_MAX_NOTES_LEN = 20_000
_MAX_ID_LEN = 64
_MAX_TIME_LEN = 5  # ``HH:MM``
_AREA_SEED_PROPERTY_KINDS = frozenset({"vacation", "str", "mixed"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnitNotFound(LookupError):
    """The requested unit does not exist in the caller's workspace.

    404-equivalent. Raised by :func:`get_unit`, :func:`update_unit`,
    and :func:`soft_delete_unit` when the id is unknown, soft-
    deleted (unless ``include_deleted`` is set), or when the parent
    property is not linked to the caller's workspace via
    ``property_workspace``. Cross-workspace and not-found collapse
    to one surface per §01 "tenant surface is not enumerable".
    """


class UnitNameTaken(ValueError):
    """A live unit in the same property already carries this name.

    422-equivalent (collapsed to 409 by the HTTP layer once the API
    surface lands). Surfaced by :func:`create_unit` and
    :func:`update_unit` before the partial UNIQUE
    ``uq_unit_property_name_active`` fires, so callers get a clean
    domain error rather than an opaque ``IntegrityError``.
    """


class LastUnitProtected(ValueError):
    """Refusal to soft-delete the last remaining live unit.

    422-equivalent. §04 "Unit" guarantees every property has at
    least one unit; tombstoning the only one would violate that
    invariant. The caller must add a sibling first (or
    soft-delete the property itself) before retiring the last unit.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


def _validate_hhmm(value: str, field: str) -> str:
    """Validate + normalise ``HH:MM`` text.

    The DB column carries free-form text; the DTO narrows to the
    canonical ``HH:MM`` shape (00..23 : 00..59) so downstream
    callers never have to re-parse. Returns the value unchanged on
    success; raises :class:`ValueError` on a bad shape so pydantic
    surfaces a 422.
    """
    if len(value) != _MAX_TIME_LEN or value[2] != ":":
        raise ValueError(f"{field} must be HH:MM (24-hour); got {value!r}")
    hh_text, mm_text = value[:2], value[3:]
    if not (hh_text.isdigit() and mm_text.isdigit()):
        raise ValueError(f"{field} must be HH:MM with digit-only fields; got {value!r}")
    hh, mm = int(hh_text), int(mm_text)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(
            f"{field} components out of range (00..23 : 00..59); got {value!r}"
        )
    return value


class _UnitBody(BaseModel):
    """Shared mutable body of the create + update DTOs.

    Held as a private base so the ``model_validator`` runs on both.
    Pydantic v2's ``model_validator`` decorates the parent class
    once and every subclass inherits the rule.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    ordinal: int = Field(default=0, ge=0)
    default_checkin_time: str | None = Field(
        default=None, min_length=_MAX_TIME_LEN, max_length=_MAX_TIME_LEN
    )
    default_checkout_time: str | None = Field(
        default=None, min_length=_MAX_TIME_LEN, max_length=_MAX_TIME_LEN
    )
    max_guests: int | None = Field(default=None, ge=1)
    welcome_overrides_json: dict[str, Any] = Field(default_factory=dict)
    settings_override_json: dict[str, Any] = Field(default_factory=dict)
    notes_md: str = Field(default="", max_length=_MAX_NOTES_LEN)

    @model_validator(mode="after")
    def _normalise(self) -> _UnitBody:
        """Trim + validate ``name`` and the time strings."""
        if not self.name.strip():
            raise ValueError("name must be a non-blank string")
        if self.default_checkin_time is not None:
            _validate_hhmm(self.default_checkin_time, "default_checkin_time")
        if self.default_checkout_time is not None:
            _validate_hhmm(self.default_checkout_time, "default_checkout_time")
        return self


class UnitCreate(_UnitBody):
    """Request body for :func:`create_unit`.

    The ``property_id`` is supplied as a function argument rather
    than carried in the body so the router can route on
    ``/properties/{property_id}/units`` cleanly without a
    redundant body field. The service still validates that the
    parent property is reachable from the caller's workspace.
    """


class UnitUpdate(_UnitBody):
    """Request body for :func:`update_unit`.

    v1 treats update as a full replacement of the mutable body —
    the spec does not (yet) call for per-field PATCH. Callers send
    the full desired state; the service diffs against the current
    row, writes through, and records the before/after diff in the
    audit log.
    """


@dataclass(frozen=True, slots=True)
class UnitView:
    """Immutable read projection of a ``unit`` row.

    Returned by every service read + write. A frozen / slotted
    dataclass (not a Pydantic model) because reads carry audit-
    sensitive fields (``deleted_at``, ``created_at``) that are
    managed by the service, not the caller's payload.
    """

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


# ---------------------------------------------------------------------------
# Row ↔ view projection
# ---------------------------------------------------------------------------


def _row_to_view(row: Unit) -> UnitView:
    """Project a loaded :class:`Unit` row into a read view.

    The ``name`` column is nullable at the DB layer for the cd-y62
    migration's cheap backfill path; the service always writes a
    non-blank value on insert. A ``NULL`` here is a pre-migration
    row — fall back to the legacy ``label`` so the read surface
    never returns an empty string.
    """
    name = row.name if row.name is not None else (row.label or "")
    return UnitView(
        id=row.id,
        property_id=row.property_id,
        name=name,
        ordinal=row.ordinal,
        default_checkin_time=row.default_checkin_time,
        default_checkout_time=row.default_checkout_time,
        max_guests=row.max_guests,
        welcome_overrides_json=dict(row.welcome_overrides_json or {}),
        settings_override_json=dict(row.settings_override_json or {}),
        notes_md=row.notes_md or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _view_to_diff_dict(view: UnitView) -> dict[str, Any]:
    """Flatten a :class:`UnitView` into a JSON-safe audit payload."""
    return {
        "id": view.id,
        "property_id": view.property_id,
        "name": view.name,
        "ordinal": view.ordinal,
        "default_checkin_time": view.default_checkin_time,
        "default_checkout_time": view.default_checkout_time,
        "max_guests": view.max_guests,
        "welcome_overrides_json": dict(view.welcome_overrides_json),
        "settings_override_json": dict(view.settings_override_json),
        "notes_md": view.notes_md,
        "created_at": view.created_at.isoformat(),
        "updated_at": (
            view.updated_at.isoformat() if view.updated_at is not None else None
        ),
        "deleted_at": (
            view.deleted_at.isoformat() if view.deleted_at is not None else None
        ),
    }


# ---------------------------------------------------------------------------
# Workspace-scoped loaders
# ---------------------------------------------------------------------------


def _assert_property_in_workspace(
    session: Session, ctx: WorkspaceContext, *, property_id: str
) -> None:
    """Raise :class:`UnitNotFound` unless ``property_id`` is reachable.

    The unit table does not carry a ``workspace_id`` column —
    workspace scoping flows through the parent property's
    ``property_workspace`` junction. Mirrors the
    :func:`property_service._load_row` pattern.

    A property whose junction row does not include the caller's
    workspace is invisible: every unit-level operation against it
    collapses to :class:`UnitNotFound` (404) so the surface does
    not leak the existence of a row in another workspace.
    """
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
        raise UnitNotFound(property_id)


def _property_kind_in_workspace(
    session: Session, ctx: WorkspaceContext, *, property_id: str
) -> str:
    """Return ``property.kind`` when the property is visible to ``ctx``."""
    stmt = (
        select(Property.kind)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            Property.id == property_id,
            Property.deleted_at.is_(None),
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    kind = session.scalars(stmt).one_or_none()
    if kind is None:
        raise UnitNotFound(property_id)
    return kind


def _load_unit_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    unit_id: str,
    include_deleted: bool = False,
) -> Unit:
    """Load ``unit_id`` scoped to the caller's workspace.

    The row is reached via :class:`PropertyWorkspace` joined on the
    parent property — a unit whose property is not linked to the
    caller's workspace is invisible. Soft-deleted rows are excluded
    unless ``include_deleted`` is set.
    """
    stmt = (
        select(Unit)
        .join(Property, Property.id == Unit.property_id)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            Unit.id == unit_id,
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    if not include_deleted:
        stmt = stmt.where(Unit.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise UnitNotFound(unit_id)
    return row


def _name_taken(
    session: Session,
    *,
    property_id: str,
    name: str,
    exclude_unit_id: str | None = None,
) -> bool:
    """Return ``True`` if a live sibling unit already carries ``name``.

    Case-sensitive: spec §04 does not call out case-insensitive
    uniqueness, and "Room 1" / "ROOM 1" are legitimate distinct
    names a manager may want to register on a multi-language
    property. Soft-deleted rows are excluded (the partial UNIQUE
    is partial on ``deleted_at IS NULL``).
    """
    stmt = select(Unit.id).where(
        Unit.property_id == property_id,
        Unit.name == name,
        Unit.deleted_at.is_(None),
    )
    if exclude_unit_id is not None:
        stmt = stmt.where(Unit.id != exclude_unit_id)
    return session.scalars(stmt).first() is not None


def _live_unit_count(session: Session, *, property_id: str) -> int:
    """Return the number of live (non-tombstoned) units on a property."""
    stmt = select(func.count(Unit.id)).where(
        Unit.property_id == property_id,
        Unit.deleted_at.is_(None),
    )
    result = session.scalars(stmt).one()
    return int(result)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_unit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    unit_id: str,
    include_deleted: bool = False,
) -> UnitView:
    """Return the unit identified by ``unit_id``.

    Raises :class:`UnitNotFound` if the id is unknown, soft-deleted
    (unless ``include_deleted`` is set), or the parent property is
    not linked to the caller's workspace.
    """
    row = _load_unit_row(session, ctx, unit_id=unit_id, include_deleted=include_deleted)
    return _row_to_view(row)


def list_units(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    deleted: bool = False,
) -> Sequence[UnitView]:
    """Return every unit of ``property_id`` linked to the caller's workspace.

    Ordered by ``ordinal`` ascending with ``id`` as a stable
    tiebreaker. Filter semantics:

    * ``deleted`` — ``False`` (the default) returns only live rows;
      ``True`` returns only soft-deleted rows. No "both" mode —
      mixing active + retired rows in one list screen is an
      anti-pattern (mirrors :func:`list_properties`).

    Raises :class:`UnitNotFound` when the parent property is not
    linked to the caller's workspace; the property's existence is
    a precondition for any unit-level enumeration.
    """
    _assert_property_in_workspace(session, ctx, property_id=property_id)
    stmt = select(Unit).where(Unit.property_id == property_id)
    if deleted:
        stmt = stmt.where(Unit.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Unit.deleted_at.is_(None))
    stmt = stmt.order_by(Unit.ordinal.asc(), Unit.id.asc())
    rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_unit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    body: UnitCreate,
    clock: Clock | None = None,
) -> UnitView:
    """Insert a fresh ``unit`` under ``property_id``.

    Workspace-scoped: a property not linked to ``ctx.workspace_id``
    raises :class:`UnitNotFound`. A duplicate name in the same
    property raises :class:`UnitNameTaken` before the partial
    UNIQUE fires.

    Records one ``unit.create`` audit row; the ``after`` diff
    carries the resolved view. Returns the full :class:`UnitView`
    so the router can echo it back without a second SELECT.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    property_kind = _property_kind_in_workspace(
        session, ctx, property_id=property_id
    )
    if _name_taken(session, property_id=property_id, name=body.name):
        raise UnitNameTaken(
            f"a unit named {body.name!r} already exists on property {property_id!r}"
        )

    view = _insert_unit_row(
        session,
        property_id=property_id,
        body=body,
        now=now,
    )
    write_audit(
        session,
        ctx,
        entity_kind="unit",
        entity_id=view.id,
        action="create",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    if property_kind in _AREA_SEED_PROPERTY_KINDS:
        from app.domain.places.area_service import seed_default_areas_for_unit

        seed_default_areas_for_unit(
            session,
            ctx,
            property_id=property_id,
            unit_id=view.id,
            now=now,
            clock=resolved_clock,
        )
    return view


def create_default_unit_for_property(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    name: str,
    property_kind: str = "residence",
    now: datetime,
    clock: Clock,
) -> UnitView:
    """Bootstrap the §04 "every property has >= 1 unit" invariant.

    Called by :mod:`property_service.create_property` inside the
    same flush as the property + ``property_workspace`` rows. The
    unit is named after the property (``name = property.name``) and
    carries ``ordinal = 0``. Bypasses the :class:`UnitNameTaken`
    pre-flight (the property just landed; there cannot be a
    sibling) but still writes the audit row so the bootstrap is
    fully traceable. External callers should always use
    :func:`create_unit`.

    The caller passes the resolved ``now`` so the property + unit
    + audit rows share an identical ``created_at`` — a single
    point in time for the bootstrap.
    """
    body = UnitCreate.model_validate({"name": name, "ordinal": 0})
    view = _insert_unit_row(
        session,
        property_id=property_id,
        body=body,
        now=now,
    )
    write_audit(
        session,
        ctx,
        entity_kind="unit",
        entity_id=view.id,
        action="create",
        diff={"after": _view_to_diff_dict(view)},
        clock=clock,
    )
    if property_kind in _AREA_SEED_PROPERTY_KINDS:
        from app.domain.places.area_service import seed_default_areas_for_unit

        seed_default_areas_for_unit(
            session,
            ctx,
            property_id=property_id,
            unit_id=view.id,
            now=now,
            clock=clock,
        )
    return view


def _insert_unit_row(
    session: Session,
    *,
    property_id: str,
    body: UnitCreate,
    now: datetime,
) -> UnitView:
    """Build + flush a fresh :class:`Unit` row.

    Shared between :func:`create_unit` and
    :func:`create_default_unit_for_property` so the row construction
    stays in lockstep with future field additions. The audit write
    is the caller's responsibility — the two entry points format
    the diff differently in case the bootstrap surface ever needs
    a distinct action label.
    """
    row = Unit(
        id=new_ulid(),
        property_id=property_id,
        name=body.name,
        ordinal=body.ordinal,
        default_checkin_time=body.default_checkin_time,
        default_checkout_time=body.default_checkout_time,
        max_guests=body.max_guests,
        welcome_overrides_json=dict(body.welcome_overrides_json),
        settings_override_json=dict(body.settings_override_json),
        notes_md=body.notes_md,
        # Legacy v1 columns: keep ``label`` mirroring ``name`` so
        # adapters that still read ``label`` see a sensible value.
        # ``type`` stays NULL on writes from the new service; the
        # CHECK now allows NULL.
        label=body.name,
        type=None,
        capacity=1,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return _row_to_view(row)


def update_unit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    unit_id: str,
    body: UnitUpdate,
    clock: Clock | None = None,
) -> UnitView:
    """Replace the mutable body of ``unit_id``.

    Workspace-scoped: a unit whose parent property is not linked to
    ``ctx.workspace_id`` raises :class:`UnitNotFound` (404). A name
    collision with a live sibling raises :class:`UnitNameTaken`
    before the partial UNIQUE fires.

    Records one ``unit.update`` audit row with the full before/after
    diff so operators can reconstruct the change.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_unit_row(session, ctx, unit_id=unit_id)
    before = _row_to_view(row)

    if body.name != before.name and _name_taken(
        session,
        property_id=row.property_id,
        name=body.name,
        exclude_unit_id=row.id,
    ):
        raise UnitNameTaken(
            f"a unit named {body.name!r} already exists on property {row.property_id!r}"
        )

    row.name = body.name
    row.ordinal = body.ordinal
    row.default_checkin_time = body.default_checkin_time
    row.default_checkout_time = body.default_checkout_time
    row.max_guests = body.max_guests
    row.welcome_overrides_json = dict(body.welcome_overrides_json)
    row.settings_override_json = dict(body.settings_override_json)
    row.notes_md = body.notes_md
    # Keep ``label`` in sync so legacy adapters keep reading the
    # current name.
    row.label = body.name
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="unit",
        entity_id=row.id,
        action="update",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def soft_delete_unit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    unit_id: str,
    clock: Clock | None = None,
) -> UnitView:
    """Soft-delete ``unit_id`` by stamping ``deleted_at``.

    Workspace-scoped — a unit whose parent property is not linked to
    ``ctx.workspace_id`` raises :class:`UnitNotFound`.

    Refuses to retire the last live unit on a property: §04 "Unit"
    guarantees every property has at least one unit, so the call
    raises :class:`LastUnitProtected` instead. The caller must add
    a sibling first (or soft-delete the property itself) before
    retiring the last unit.

    Records one ``unit.delete`` audit row with the before / after
    diff. Returns the post-delete view so the router can echo the
    ``deleted_at`` timestamp back to the client.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_unit_row(session, ctx, unit_id=unit_id)

    # Concurrency note: the "is last unit" check + the tombstone
    # write are two statements; a Postgres deployment running two
    # concurrent ``soft_delete_unit`` calls against the last two
    # remaining live units could race past the gate and end with
    # zero live units. We accept the race on SQLite (single-writer)
    # and rely on the "every property has >= 1 unit" invariant
    # being soft (no DB-level CHECK enforces it; §04 "Invariants"
    # says "enforced by application"). When the production target
    # is firmly Postgres (cd-75wp REST API + production deploy) we
    # tighten this to ``SELECT ... FOR UPDATE`` on the parent
    # property row before the count, which serialises the gate
    # under read-committed. Tracked in cd-zfcj.
    if _live_unit_count(session, property_id=row.property_id) <= 1:
        raise LastUnitProtected(
            f"cannot soft-delete the last remaining unit on property "
            f"{row.property_id!r}"
        )

    before = _row_to_view(row)
    row.deleted_at = now
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="unit",
        entity_id=row.id,
        action="delete",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after
