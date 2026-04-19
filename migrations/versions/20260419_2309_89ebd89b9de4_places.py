"""places

Revision ID: 89ebd89b9de4
Revises: 819a4ed33ac2
Create Date: 2026-04-19 23:09:14.842905

Creates the five places tables that back the property / unit / area /
sharing / closure surface (see ``docs/specs/02-domain-model.md``
§"property_workspace" and ``docs/specs/04-properties-and-stays.md``
§"Property" / §"Unit" / §"Area"):

* ``property`` — the physical place. **Not** workspace-scoped: the
  same villa can belong to several workspaces through
  ``property_workspace`` below (§02 "Villa belongs to many
  workspaces"). The v1 slice carries the minimum shared by every
  downstream context — ``address`` (single text), IANA
  ``timezone``, optional ``lat`` / ``lon``, a ``tags_json`` payload
  and ``created_at``. Richer §04 columns (structured
  ``address_json``, ``kind``, ``client_org_id``, ``owner_user_id``,
  ``welcome_defaults_json``, ``deleted_at``) land with cd-8u5.

* ``property_workspace`` — the workspace-scoped junction. Composite
  PK on ``(property_id, workspace_id)`` lets one property attach
  to many workspaces. ``membership_role`` (``owner_workspace`` /
  ``managed_workspace`` / ``observer_workspace``) is enforced by a
  CHECK constraint. Registered as workspace-scoped via the
  ``app/adapters/db/places/__init__.py`` import hook — the ORM
  tenant filter auto-injects a ``workspace_id`` predicate on every
  SELECT. The extended §02 surface
  (``share_guest_identity``, ``invite_id``, ``added_via``,
  ``added_by_user_id``) lands with cd-8u5 / cd-79r.

* ``unit`` — bookable subdivision of a property. v1 carries
  ``label`` / ``type`` (physical kind, enum) / ``capacity``. The
  §04 ``default_checkin_time`` / ``welcome_overrides_json`` /
  ``settings_override_json`` / ``ordinal`` columns land with cd-8u5.
  Workspace isolation is enforced by joining through
  ``property_workspace`` — the adapter layer owns that guarantee
  in v1; a later filter-side enhancement will make it automatic.

* ``area`` — subdivision of a property (kitchen, pool, garden…).
  v1 carries ``label`` / ``icon`` (lucide icon slug) / ``ordering``
  (integer walk-order hint). The §04 ``unit_id`` (for unit-scoped
  areas) / ``kind`` enum / ``parent_area`` self-FK land with cd-8u5.

* ``property_closure`` — blackout window on a property. The CHECK
  ``ends_after_starts`` guards against zero-or-negative-length
  windows (a closure that covers no time is a data bug, not a
  legitimate state). ``created_by_user_id`` is nullable and
  ``ON DELETE SET NULL`` so history survives the actor's removal.

All child tables CASCADE on the parent ``property``: hard-deleting
a property sweeps its units, areas, closures, and workspace links
atomically. The ``property_workspace`` row also CASCADE on
``workspace`` so workspace hard-delete sweeps the junction. The
audit pointer on ``property_closure`` uses ``SET NULL`` so history
survives the acting user's removal.

``role_grant.scope_property_id`` remains a **soft reference** in
this migration: promoting it to a real FK on ``property.id`` would
require a batch-alter on SQLite plus a backfill guarantee, which
belongs in a dedicated follow-up (tracked in a Beads task kicked
off by cd-79r).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "89ebd89b9de4"
down_revision: str | Sequence[str] | None = "819a4ed33ac2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "property",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_property")),
    )

    op.create_table(
        "area",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("ordering", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_area_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_area")),
    )
    with op.batch_alter_table("area", schema=None) as batch_op:
        batch_op.create_index("ix_area_property", ["property_id"], unique=False)

    op.create_table(
        "property_closure",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ends_at > starts_at",
            name=op.f("ck_property_closure_ends_after_starts"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_property_closure_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_property_closure_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_property_closure")),
    )
    with op.batch_alter_table("property_closure", schema=None) as batch_op:
        batch_op.create_index(
            "ix_property_closure_property_starts",
            ["property_id", "starts_at"],
            unique=False,
        )

    op.create_table(
        "property_workspace",
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("membership_role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "membership_role IN ('owner_workspace', 'managed_workspace', "
            "'observer_workspace')",
            name=op.f("ck_property_workspace_membership_role"),
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_property_workspace_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_property_workspace_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "property_id", "workspace_id", name=op.f("pk_property_workspace")
        ),
    )
    with op.batch_alter_table("property_workspace", schema=None) as batch_op:
        batch_op.create_index(
            "ix_property_workspace_property", ["property_id"], unique=False
        )
        batch_op.create_index(
            "ix_property_workspace_workspace", ["workspace_id"], unique=False
        )

    op.create_table(
        "unit",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "type IN ('apartment', 'studio', 'room', 'bungalow', 'villa', 'other')",
            name=op.f("ck_unit_type"),
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_unit_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unit")),
    )
    with op.batch_alter_table("unit", schema=None) as batch_op:
        batch_op.create_index("ix_unit_property", ["property_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("unit", schema=None) as batch_op:
        batch_op.drop_index("ix_unit_property")
    op.drop_table("unit")

    with op.batch_alter_table("property_workspace", schema=None) as batch_op:
        batch_op.drop_index("ix_property_workspace_workspace")
        batch_op.drop_index("ix_property_workspace_property")
    op.drop_table("property_workspace")

    with op.batch_alter_table("property_closure", schema=None) as batch_op:
        batch_op.drop_index("ix_property_closure_property_starts")
    op.drop_table("property_closure")

    with op.batch_alter_table("area", schema=None) as batch_op:
        batch_op.drop_index("ix_area_property")
    op.drop_table("area")

    op.drop_table("property")
