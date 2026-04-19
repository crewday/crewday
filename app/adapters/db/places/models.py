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

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["Area", "Property", "PropertyClosure", "PropertyWorkspace", "Unit"]


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


class Property(Base):
    """A physical place the workspace operates in.

    The v1 slice carries only the minimum shared by every downstream
    context (``address`` as a single text field, IANA ``timezone``,
    optional ``lat``/``lon``, a ``tags_json`` payload and
    ``created_at``). The structured ``address_json`` + ``kind`` +
    ``client_org_id`` columns from §04 land with cd-8u5.

    The table is **NOT** workspace-scoped: the same row may link to
    several workspaces through :class:`PropertyWorkspace`. Services
    that need a workspace-filtered property list MUST join through
    the junction; see the package docstring.
    """

    __tablename__ = "property"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # v1 stores the postal address as a single text blob. §04's
    # structured ``address_json`` lands with cd-8u5; until then
    # callers persist a rendered single-line form here.
    address: Mapped[str] = mapped_column(String, nullable=False)
    # IANA timezone (e.g. ``Europe/Paris``). Every timestamp that is
    # "local to this place" — stay check-in/out, task occurrence —
    # resolves through this column.
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Free-form labels a workspace uses to group properties (e.g.
    # ``["riviera", "off-season"]``). The list shape is declared on
    # the mapped annotation so callers writing a typed payload don't
    # need an ``Any`` cast; the DB column is a plain JSON blob.
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "membership_role IN ('" + "', '".join(_MEMBERSHIP_ROLE_VALUES) + "')",
            name="membership_role",
        ),
        # Composite PK already covers "workspaces for this property";
        # these indexes speed the sibling lookup directions.
        Index("ix_property_workspace_workspace", "workspace_id"),
        Index("ix_property_workspace_property", "property_id"),
    )


class Unit(Base):
    """Bookable subdivision of a property.

    v1 slice: ``id`` / ``property_id`` / ``label`` / ``type`` /
    ``capacity`` / ``created_at``. The richer §04 columns
    (``default_checkin_time``, ``welcome_overrides_json``,
    ``settings_override_json``, ``ordinal``) land with cd-8u5.
    Workspace isolation is enforced by joining through
    :class:`PropertyWorkspace` — the package docstring spells out
    why ``unit`` itself stays unregistered.
    """

    __tablename__ = "unit"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    # Physical-kind taxonomy; a tighter spec-matched enum lands with
    # cd-8u5. CHECK enforces the v1 set.
    type: Mapped[str] = mapped_column(String, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('" + "', '".join(_UNIT_TYPE_VALUES) + "')",
            name="type",
        ),
        Index("ix_unit_property", "property_id"),
    )


class Area(Base):
    """Subdivision of a property — kitchen, pool, garden, etc.

    v1 slice: ``id`` / ``property_id`` / ``label`` / ``icon`` /
    ``ordering`` / ``created_at``. The §04 ``unit_id`` (for
    unit-scoped areas), ``kind`` enum and ``parent_area`` self-FK
    land with cd-8u5.
    """

    __tablename__ = "area"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    # ``icon`` is the lucide icon slug the UI renders next to the
    # label (e.g. ``"utensils"``, ``"waves"``). Nullable — areas
    # without a canonical icon just render the label.
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    # ``ordering`` is the integer walk-order hint (§04 "Auto-seeded
    # areas"); lower values render first.
    ordering: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (Index("ix_area_property", "property_id"),)


class PropertyClosure(Base):
    """Blackout window on a property — renovation, owner-stay, etc.

    v1 slice: ``id`` / ``property_id`` / ``starts_at`` / ``ends_at``
    / ``reason`` / ``created_by_user_id`` / ``created_at``. The
    CHECK ``ends_after_starts`` guards against zero-or-negative-
    length windows (a closure that covers no time is a data bug,
    not a legitimate operational state).

    ``created_by_user_id`` is nullable + ``ON DELETE SET NULL`` so
    history survives the actor's deletion; every other FK cascades
    on the parent property's delete.
    """

    __tablename__ = "property_closure"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        Index("ix_property_closure_property_starts", "property_id", "starts_at"),
    )
