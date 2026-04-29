"""Property / Unit / Area / PropertyWorkspace / PropertyClosure models.

v1 slice per cd-i6u. The richer §02 / §04 surface (structured
``address_json``, ``kind`` / ``client_org_id`` / ``owner_user_id``
on ``property``; ``unit.default_checkin_time`` /
``welcome_overrides_json`` / ``settings_override_json``;
``area.kind`` / ``unit_id`` / ``parent_area``; extended
``property_workspace.share_guest_identity`` / ``invite_id`` /
``added_via`` / ``added_by_user_id``; etc.) is deferred to cd-8u5
(property domain service) and follow-up migrations without
breaking this migration's public write contract.

The `property` row itself is **NOT** workspace-scoped — the same
villa can belong to several workspaces through the
``property_workspace`` junction (§02 "Villa belongs to many
workspaces"). Adapters that need a workspace-filtered property list
MUST join through ``property_workspace``; see the package
docstring for the tenancy contract on ``unit`` / ``area`` /
``property_closure``.

See ``docs/specs/02-domain-model.md`` §"property_workspace",
``docs/specs/04-properties-and-stays.md`` §"Property" / §"Unit" /
§"Area".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``user.id`` /
# ``user_work_role.id`` / ``workspace.id`` / ``pay_rule.id`` FKs
# below resolve against ``Base.metadata`` only if the target
# packages have been imported, so we register them here as a side
# effect.
from app.adapters.db.identity import models as _identity_models  # noqa: F401
from app.adapters.db.payroll import models as _payroll_models  # noqa: F401
from app.adapters.db.stays import models as _stays_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = [
    "Area",
    "Property",
    "PropertyClosure",
    "PropertyWorkRoleAssignment",
    "PropertyWorkspace",
    "Unit",
]


# Allowed ``property_workspace.membership_role`` values, enforced by a
# CHECK constraint. Matches §02 "property_workspace" —
# ``owner_workspace`` is the governance anchor (at most one per
# property), ``managed_workspace`` is operational access granted by
# the owner, ``observer_workspace`` is read-only.
_MEMBERSHIP_ROLE_VALUES: tuple[str, ...] = (
    "owner_workspace",
    "managed_workspace",
    "observer_workspace",
)

# Allowed ``property_workspace.status`` values, enforced by a CHECK
# constraint. ``invited`` covers a non-owner row the owner has minted
# but the recipient workspace has not yet accepted; ``active`` covers
# the live, in-force row. Owner-workspace bootstrap rows always carry
# ``active`` (the seeding workspace consents implicitly by creating
# the property). Lands with cd-hsk.
_PROPERTY_WORKSPACE_STATUS_VALUES: tuple[str, ...] = (
    "invited",
    "active",
)

# Allowed ``property.kind`` values — drives default lifecycle rule +
# area seeding behaviour (§04 "`kind` semantics"). The CHECK on the
# column enforces the enum; the domain layer narrows the loaded
# string to a :class:`Literal` on read.
_PROPERTY_KIND_VALUES: tuple[str, ...] = (
    "residence",
    "vacation",
    "str",
    "mixed",
)

# Allowed ``unit.type`` values for the v1 slice. §04 speaks of a
# free-form unit kind ("Room 1", "Apt 3B"); the column here carries
# the physical-kind taxonomy (apartment / studio / room / bungalow /
# villa / other). A tighter spec-matched enum lands with cd-8u5.
_UNIT_TYPE_VALUES: tuple[str, ...] = (
    "apartment",
    "studio",
    "room",
    "bungalow",
    "villa",
    "other",
)

# Allowed ``area.kind`` values. §04 leaves room for future expansion
# ("..."), but the current domain service only needs the concrete set
# used by the seeded STR defaults and manager CRUD.
_AREA_KIND_VALUES: tuple[str, ...] = (
    "indoor_room",
    "outdoor",
    "service",
)


class Property(Base):
    """A physical place the workspace operates in.

    The v1 slice (cd-i6u) landed ``id`` / ``address`` / ``timezone``
    / ``lat`` / ``lon`` / ``tags_json`` / ``created_at``. cd-8u5
    added the richer §02 / §04 surface the manager UI and the
    property domain service need:

    * ``name`` — human-visible display name.
    * ``kind`` — lifecycle-seeding enum (``residence | vacation |
      str | mixed``).
    * ``address_json`` — canonical structured address; ``country``
      inside it is back-filled on write (§04 "`address_json`
      canonical shape").
    * ``country`` — ISO-3166-1 alpha-2 country code.
    * ``locale`` / ``default_currency`` — optional per-property
      overrides; inherit workspace defaults when ``NULL``.
    * ``client_org_id`` / ``owner_user_id`` — soft references to
      ``organization`` (cd-t8m) and ``users``.
    * ``welcome_defaults_json`` / ``property_notes_md`` — JSON blob
      + staff-visible notes.
    * ``updated_at`` / ``deleted_at`` — mutation + soft-delete
      timestamps.

    The table is **NOT** workspace-scoped: the same row may link to
    several workspaces through :class:`PropertyWorkspace`. Services
    that need a workspace-filtered property list MUST join through
    the junction; see the package docstring.
    """

    __tablename__ = "property"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Display name ("Villa Sud", "Apt 3B"). Nullable at the DB layer
    # so the cd-8u5 migration can backfill from ``address`` without
    # a two-step tighten; the domain service always writes a non-
    # blank value on insert.
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Lifecycle-seeding enum. CHECK-enforced via ``ck_property_kind``.
    # Server default ``residence`` (most conservative seed) so legacy
    # rows keep working; the service narrows to a :class:`Literal`
    # on read.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="residence")
    # v1 stores the postal address as a single text blob. cd-8u5
    # keeps ``address`` as the rendered single-line form for legacy
    # adapters and adds ``address_json`` for the canonical shape.
    address: Mapped[str] = mapped_column(String, nullable=False)
    # Canonical structured address — ``line1`` / ``line2`` / ``city``
    # / ``state_province`` / ``postal_code`` / ``country``. Empty
    # object for legacy rows; the service back-fills ``country`` in
    # both directions on write.
    address_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # ISO-3166-1 alpha-2 country code. Authoritative source is
    # ``address_json.country`` when present; the back-fill keeps both
    # columns in sync on every write.
    country: Mapped[str] = mapped_column(String, nullable=False, default="XX")
    # BCP-47 locale tag; nullable = inherit workspace language +
    # property country at render time (§04 "Property" — locale field).
    locale: Mapped[str | None] = mapped_column(String, nullable=True)
    # ISO-4217 currency override; nullable = inherit workspace
    # ``default_currency``.
    default_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    # IANA timezone (e.g. ``Europe/Paris``). Every timestamp that is
    # "local to this place" — stay check-in/out, task occurrence —
    # resolves through this column.
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Soft reference to ``organization.id`` (cd-t8m). NULL = the
    # workspace is its own employer (§04 "Billing client").
    client_org_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft reference to ``users.id`` — display-only "owner of record"
    # pointer. Authorisation is governed by ``property_workspace`` +
    # the workspace's ``owners`` group, never by this column.
    owner_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Free-form labels a workspace uses to group properties (e.g.
    # ``["riviera", "off-season"]``). The list shape is declared on
    # the mapped annotation so callers writing a typed payload don't
    # need an ``Any`` cast; the DB column is a plain JSON blob.
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Welcome-page payload (§04 "Welcome defaults"). Empty object
    # when unset; the guest welcome page merges unit overrides over
    # this blob.
    welcome_defaults_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # Internal staff-visible notes (§04 "Property" — property_notes_md).
    property_notes_md: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Mutation timestamp — bumped on every domain-service update.
    # Nullable for the cd-8u5 migration's cheap backfill path; the
    # service always writes it on insert + update.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft-delete marker; live rows carry ``NULL``. The service's
    # default list excludes non-null rows.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('" + "', '".join(_PROPERTY_KIND_VALUES) + "')",
            name="kind",
        ),
        Index("ix_property_deleted", "deleted_at"),
    )


class PropertyWorkspace(Base):
    """Junction row binding a property to a workspace.

    The composite PK ``(property_id, workspace_id)`` lets the same
    physical property belong to several workspaces at once.
    ``workspace_id`` is what the ORM tenant filter
    (:mod:`app.tenancy.orm_filter`) pins to the active
    :class:`~app.tenancy.WorkspaceContext`, so reads of this junction
    are naturally scoped to the caller's workspace.

    ``membership_role`` expresses how the workspace relates to the
    property — owner / managed / observer (§02 "Villa belongs to
    many workspaces"). The v1 slice defaults new rows to
    ``owner_workspace``; the CHECK constraint enforces the enum.
    """

    __tablename__ = "property_workspace"

    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    membership_role: Mapped[str] = mapped_column(
        String, nullable=False, default="owner_workspace"
    )
    # PII boundary widening flag (§15 "Cross-workspace visibility").
    # ``False`` by default — non-owner workspaces see only the
    # operational minimum unless the owner explicitly widens the share.
    # The owner workspace's own row always behaves as if ``True`` at
    # read time (it owns the data); the column is defaulted false there
    # too so the CHECK on toggling stays simple. Lands with cd-hsk.
    share_guest_identity: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Invite-acceptance lifecycle. ``invited`` covers a non-owner row
    # minted by the owner that the recipient workspace has not yet
    # accepted; ``active`` covers the live, in-force row. Owner rows
    # are always seeded ``active``. CHECK-enforced via ``ck_property_workspace_status``.
    # Lands with cd-hsk.
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "membership_role IN ('" + "', '".join(_MEMBERSHIP_ROLE_VALUES) + "')",
            name="membership_role",
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(_PROPERTY_WORKSPACE_STATUS_VALUES) + "')",
            name="status",
        ),
        # Composite PK already covers "workspaces for this property";
        # these indexes speed the sibling lookup directions.
        Index("ix_property_workspace_workspace", "workspace_id"),
        Index("ix_property_workspace_property", "property_id"),
    )


class Unit(Base):
    """Bookable subdivision of a property.

    The v1 slice (cd-i6u) landed ``id`` / ``property_id`` /
    ``label`` / ``type`` / ``capacity`` / ``created_at`` — the
    minimum a downstream context might want to know about a sub-
    property bookable. cd-y62 added the richer §02 / §04 surface
    the manager UI and the unit domain service need:

    * ``name`` — human-visible name ("Room 1", "Apt 3B"). The new
      domain service writes a non-blank value on every insert.
    * ``ordinal`` — display order among siblings.
    * ``default_checkin_time`` / ``default_checkout_time`` — per-
      unit override of the property's check-in/out window. Stored
      as ``HH:MM`` text; nullable = inherit.
    * ``max_guests`` — bookable cap surfaced on the guest welcome
      page. Distinct from the v1 ``capacity`` column (physical-
      kind cap).
    * ``welcome_overrides_json`` — per-unit overrides that merge
      over the property's ``welcome_defaults_json`` at render time
      (§04 "Welcome overrides merge").
    * ``settings_override_json`` — per-unit cascade layer between
      property and work_engagement (§02 "Settings cascade").
    * ``notes_md`` — internal staff-visible notes.
    * ``updated_at`` / ``deleted_at`` — mutation + soft-delete
      timestamps.

    The legacy ``label`` / ``type`` / ``capacity`` columns survive
    for back-compat (``label`` was relaxed to nullable). Workspace
    isolation is enforced by joining through
    :class:`PropertyWorkspace` — the package docstring spells out
    why ``unit`` itself stays unregistered. The partial UNIQUE
    ``uq_unit_property_name_active`` enforces "one live name per
    property" while letting a soft-deleted row coexist with a re-
    created sibling.
    """

    __tablename__ = "unit"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Human-visible name. Nullable at the DB layer so the cd-y62
    # migration could backfill from ``label``; the domain service
    # always writes a non-blank value on insert + update.
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Display order among siblings (lower = earlier). Server default
    # ``0`` keeps legacy rows readable.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-unit check-in/out overrides as ``HH:MM`` text. Nullable =
    # inherit from property. Text storage keeps SQLite + Postgres in
    # sync (SQLite's TIME affinity is a thin layer over TEXT).
    default_checkin_time: Mapped[str | None] = mapped_column(String, nullable=True)
    default_checkout_time: Mapped[str | None] = mapped_column(String, nullable=True)
    # Bookable cap surfaced on the guest welcome page.
    max_guests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Welcome overrides — merged over property ``welcome_defaults_json``
    # at render time (§04 "Welcome overrides merge").
    welcome_overrides_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # Per-unit settings cascade layer (§02 "Settings cascade").
    settings_override_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # Internal staff-visible notes (markdown).
    notes_md: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Legacy v1 columns — relaxed to nullable in cd-y62 so the new
    # domain service can write rows without populating them. They
    # survive so existing adapters keep reading; a future cleanup
    # may drop them.
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    # Physical-kind taxonomy. CHECK still enforces the v1 set when
    # the column carries a value; nullable allows the new service to
    # skip it on insert.
    type: Mapped[str | None] = mapped_column(String, nullable=True)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Mutation timestamp — bumped on every domain-service update.
    # Nullable for the cd-y62 migration's cheap backfill path; the
    # service always writes it on insert + update.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft-delete marker; live rows carry ``NULL``. The service's
    # default list excludes non-null rows; the partial UNIQUE on
    # ``(property_id, name)`` excludes tombstoned rows so a re-
    # create after a soft-delete works.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "type IS NULL OR type IN ('" + "', '".join(_UNIT_TYPE_VALUES) + "')",
            name="type",
        ),
        Index("ix_unit_property", "property_id"),
        Index("ix_unit_deleted", "deleted_at"),
        # Partial UNIQUE on ``(property_id, name)`` excluding tomb-
        # stoned rows — enforces "one live name per property" while
        # letting a re-create after soft-delete mint a fresh row
        # without colliding with the historical one.
        Index(
            "uq_unit_property_name_active",
            "property_id",
            "name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class Area(Base):
    """Subdivision of a property — kitchen, pool, garden, etc.

    The v1 slice landed ``id`` / ``property_id`` / ``label`` /
    ``icon`` / ``ordering`` / ``created_at``. cd-a2k adds the §04
    service surface while preserving those legacy columns:

    * ``name`` — human-visible display name. Backfilled from
      ``label``; the domain service writes both so older adapters
      keep reading ``label``.
    * ``unit_id`` — nullable FK for unit-specific areas. ``NULL`` is
      a shared/property-level area.
    * ``kind`` — ``indoor_room | outdoor | service``.
    * ``ordering`` — existing integer walk-order column, surfaced by
      the domain service as ``order_hint``.
    * ``parent_area_id`` — optional self-FK. The service enforces the
      one-level nesting invariant.
    * ``notes_md`` / ``updated_at`` / ``deleted_at`` — internal notes
      and mutation/soft-delete timestamps.
    """

    __tablename__ = "area"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    unit_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("unit.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="indoor_room")
    # ``icon`` is the lucide icon slug the UI renders next to the
    # label (e.g. ``"utensils"``, ``"waves"``). Nullable — areas
    # without a canonical icon just render the label.
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    # ``ordering`` is the integer walk-order hint (§04 "Auto-seeded
    # areas"); lower values render first.
    ordering: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_area_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("area.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes_md: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('" + "', '".join(_AREA_KIND_VALUES) + "')",
            name="kind",
        ),
        Index("ix_area_property", "property_id"),
        Index("ix_area_unit", "unit_id"),
        Index("ix_area_parent", "parent_area_id"),
        Index("ix_area_deleted", "deleted_at"),
    )


class PropertyClosure(Base):
    """Blackout window on a property — renovation, owner-stay, etc.

    v1 slice: ``id`` / ``property_id`` / ``unit_id`` / ``starts_at`` / ``ends_at``
    / ``reason`` / ``source_ical_feed_id`` / ``source_external_uid`` /
    ``source_last_seen_at`` / ``created_by_user_id`` / ``created_at`` /
    ``deleted_at``. The CHECK ``ends_after_starts`` guards against
    zero-or-negative-length windows (a closure that covers no time
    is a data bug, not a legitimate operational state).

    ``created_by_user_id`` is nullable + ``ON DELETE SET NULL`` so
    history survives the actor's deletion. ``source_ical_feed_id``
    is nullable + ``ON DELETE SET NULL`` so iCal-sourced closures
    survive a feed delete; manual closures (owner-stay, renovation)
    leave the column ``NULL``. Every remaining FK cascades on the
    parent property's delete. ``unit_id`` is nullable + ``ON DELETE
    SET NULL`` so unit churn does not erase closure history; NULL
    means the closure applies to the whole property.
    """

    __tablename__ = "property_closure"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    unit_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("unit.id", ondelete="SET NULL"),
        nullable=True,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    # Attribution back to the upstream ``ical_feed`` row when the
    # closure was minted from a Blocked-pattern VEVENT (§04 "iCal
    # feed" §"Polling behavior"). NULL for manual closures.
    # ``SET NULL`` on feed delete so closure history survives a
    # feed swap / disable / delete.
    source_ical_feed_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("ical_feed.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_external_uid: Mapped[str | None] = mapped_column(String, nullable=True)
    source_last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        Index("ix_property_closure_property_starts", "property_id", "starts_at"),
        Index("ix_property_closure_unit", "unit_id"),
        Index("ix_property_closure_source_ical_feed", "source_ical_feed_id"),
        Index(
            "ix_property_closure_source_uid",
            "source_ical_feed_id",
            "source_external_uid",
        ),
        Index("ix_property_closure_deleted", "deleted_at"),
    )


class PropertyWorkRoleAssignment(Base):
    """Per-property pinning of a :class:`UserWorkRole` (cd-e4m3).

    Pins a :class:`~app.adapters.db.workspace.models.UserWorkRole` to
    a specific :class:`Property`. The absence of any assignment row
    leaves the user's role **workspace-wide** (a "generalist" —
    eligible for every property in the workspace, per §05 "Property
    work role assignment"). One or more rows narrow eligibility to
    those properties only.

    A single (user_work_role, property) pair is **uniquely
    identified** by an active row here — variation in *when* the
    user works that property (e.g. Mon mornings vs. Mon afternoons)
    is expressed by the multi-slot :ref:`schedule_ruleset`
    referenced via :attr:`schedule_ruleset_id`, not by stacking
    multiple ``property_work_role_assignment`` rows. That makes the
    partial ``UNIQUE (user_work_role_id, property_id) WHERE
    deleted_at IS NULL`` the natural identity key.

    **Tenancy.** The table carries a denormalised ``workspace_id``
    column even though the parent ``user_work_role`` already encodes
    the workspace. This matches the
    :class:`~app.adapters.db.workspace.models.WorkEngagement` /
    :class:`~app.adapters.db.workspace.models.UserWorkRole`
    pattern — the ORM tenant filter rides a local column rather
    than threading a join through the parent on every read. The
    package's ``__init__`` registers the table so a bare SELECT
    without a :class:`~app.tenancy.WorkspaceContext` raises
    :class:`~app.tenancy.orm_filter.TenantFilterMissing`.

    **Domain-enforced invariants** (write-path; not expressed in
    DDL):

    1. ``workspace_id`` must equal the parent ``user_work_role``'s
       ``workspace_id``. Cross-workspace borrowing is already
       blocked by §05 "User work role"; the redundancy is explicit
       here so a future bulk-loader can't slip a row through.
    2. ``property_id`` must point at a property that is linked to
       ``workspace_id`` through a live ``property_workspace`` row —
       a workspace cannot pin a role to a property it doesn't
       operate. Validated at write time by the future API service
       (cd-za6n).

    **Soft references** (no FK declared):

    * ``schedule_ruleset_id`` — the ``schedule_ruleset`` table
      does not yet exist (§06 "Schedule ruleset (per-property
      rota)"; landing in a sibling task). Plain :class:`str` until
      the table lands; a follow-up migration may promote it into a
      real FK without disturbing domain callers (same pattern as
      :attr:`~app.adapters.db.workspace.models.UserWorkRole.pay_rule_id`).

    **Real foreign keys** (already in the schema):

    * ``user_work_role_id`` → ``user_work_role.id`` ``ON DELETE
      CASCADE`` — hard-deleting a user_work_role sweeps every
      assignment row.
    * ``property_id`` → ``property.id`` ``ON DELETE CASCADE`` —
      hard-deleting the property sweeps the row (matching the
      sibling :class:`Unit` / :class:`Area` / :class:`PropertyClosure`
      cascade).
    * ``workspace_id`` → ``workspace.id`` ``ON DELETE CASCADE``.
    * ``property_pay_rule_id`` → ``pay_rule.id`` ``ON DELETE SET
      NULL`` — losing the pay rule drops the override but keeps the
      assignment alive (the engagement-level rule re-applies).

    See ``docs/specs/05-employees-and-roles.md`` §"Property work
    role assignment", ``docs/specs/02-domain-model.md`` §"People,
    work roles, engagements", and
    ``docs/specs/06-tasks-and-scheduling.md`` §"Schedule ruleset
    (per-property rota)".
    """

    __tablename__ = "property_work_role_assignment"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Denormalised tenancy column — the ORM tenant filter rides this
    # local column rather than threading a join through
    # ``user_work_role`` on every read. Always equal to the parent
    # ``user_work_role.workspace_id`` (write-path invariant; see the
    # class docstring).
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_work_role_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user_work_role.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Soft reference to the future ``schedule_ruleset`` table (§06).
    # NULL = no rota declared — eligibility falls back to
    # ``user_weekly_availability`` alone (§05 "Property work role
    # assignment").
    schedule_ruleset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Per-property rate override. NULL = inherit the engagement-level
    # rule. ``ON DELETE SET NULL`` so losing the pay rule doesn't
    # nuke the assignment — the engagement rule re-applies.
    property_pay_rule_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("pay_rule.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Soft-delete tombstone; live rows carry NULL. The partial UNIQUE
    # below excludes tombstoned rows so a re-pin after an archive
    # mints a fresh row without colliding with the historical one.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Identity key — one live row per (user_work_role, property).
        # Variation in *when* the user works the property is
        # expressed by ``schedule_ruleset_slot`` rows under the
        # referenced ruleset, not by stacking multiple assignments.
        # Tombstoned rows are excluded so an archive + re-pin works.
        Index(
            "uq_property_work_role_assignment_role_property_active",
            "user_work_role_id",
            "property_id",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # "List live assignments for this workspace" hot path —
        # leading ``workspace_id`` carries the tenant filter;
        # trailing ``deleted_at`` lets the planner skip tombstones.
        Index(
            "ix_property_work_role_assignment_workspace_deleted",
            "workspace_id",
            "deleted_at",
        ),
        # "Every assignment of this user_work_role" — the employees
        # surface walks this index to display per-user property
        # narrowings.
        Index(
            "ix_property_work_role_assignment_workspace_user_work_role",
            "workspace_id",
            "user_work_role_id",
        ),
        # "Every assignment at this property" — the property's
        # workforce panel walks this index to list the workers
        # operating the place.
        Index(
            "ix_property_work_role_assignment_workspace_property",
            "workspace_id",
            "property_id",
        ),
    )
